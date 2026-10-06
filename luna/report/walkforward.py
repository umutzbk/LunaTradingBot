"""Walk-forward / holdout evaluation runs feeding the full report and the GO/NO-GO (spec §8).

Every run simulates two main variants on the same fitted models (``luna.report.gonogo``):
"realistic" (config as-is) and "edge" (drawdown kill switch off, ``max_drawdown_override=1.0``).
Around them:

- sensitivity (never the headline): 2x costs (realistic and edge); perp funding for the hours
  without a known rate at 0x / estimated / 2x estimated (edge);
- ablations (walk-forward only, edge): regime gates off (``no_regime``, trade lists built with
  ``use_regime=False``), random ranker instead of the expectancy ranker (``random_ranker``);
- naive baselines (spec §8.1): each naive strategy alone with ``AcceptAllRanker`` over exactly the
  OOS span [first test_start, last test_end), universe selected at the span start with the same rule
  as the walk-forward (edge variant; spot skips pairs).

The holdout (``holdout_report``) is never run by the walk-forward; it uses ``run_holdout``,
which refuses a second run per account.
"""

from __future__ import annotations

import json
import logging
import math
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from luna.config import Settings
from luna.data.store import BarStore, fill_missing
from luna.engine.backtest import BacktestResult, run_backtest
from luna.engine.walkforward import (
    WalkForwardResult,
    _bounds,
    _variant,
    build_walkforward_tradelists,
    make_windows,
    run_holdout,
    run_walkforward,
    universe_at,
)
from luna.ranking.ranker import AcceptAllRanker
from luna.report.gonogo import CHECKS, GoNoGo, evaluate, funding_known_mask
from luna.report.html import render_evaluation
from luna.report.metrics import metrics_with_ci
from luna.strategy.naive import NaiveMeanReversion, NaiveMomentum, NaivePairs
from luna.types import AccountKind, Instrument

log = logging.getLogger("luna.report.walkforward")
BAR = pd.Timedelta("15min")
VENUE = {"perp": "kraken_futures", "spot": "kraken_spot"}
NAIVE_PAIR = "ETH/BTC"
MAIN = ("realistic", "edge")
ABLATIONS = ("no_regime", "random_ranker")
SENSITIVITY = ("cost_2x", "cost_2x_edge", "funding_zero", "funding_estimated", "funding_estimated_x2")
EDGE = 1.0  # max_drawdown_override of the "edge" variant
WINDOW_COLUMNS = ["test_start", "test_end", "status", "universe", "pairs_selected", "accepted_cells",
                  "n_trades", "expectancy_r", "net_pnl"]
WARMUP = pd.Timedelta(days=90)  # indicator / regime warm-up (regime quantile lookback 8640 bars)


def walkforward_variants(kind: AccountKind) -> dict[str, dict]:
    """Variant name -> run_walkforward keyword arguments."""
    edge = {"max_drawdown_override": EDGE}
    v: dict[str, dict] = {
        "realistic": {},
        "edge": dict(edge),
        "cost_2x": {"cost_mult": 2.0},
        "cost_2x_edge": {"cost_mult": 2.0, **edge},
        "no_regime": {"ablation": "no_regime", **edge},
        "random_ranker": {"ablation": "random_ranker", **edge},
    }
    if kind == "perp":
        for mode in ("zero", "estimated", "estimated_x2"):
            v[f"funding_{mode}"] = {"funding_mode": mode, **edge}
    return v


@dataclass
class Inputs:
    panel: dict[str, pd.DataFrame]
    funding: dict[str, pd.DataFrame]
    universe_panel: dict[str, pd.DataFrame] | None
    instruments: dict[str, Instrument]
    dropped: list[str]  # assets without instrument metadata


@dataclass
class EvalReport:
    account: AccountKind
    mode: Literal["walkforward", "holdout"]
    variants: dict[str, WalkForwardResult]
    naive: dict[str, BacktestResult]
    naive_skipped: dict[str, str]
    gonogo: GoNoGo
    funding_known_from: dict | None  # None: spot (no funding)
    span: tuple[pd.Timestamp, pd.Timestamp]
    benchmarks: dict[str, float]  # name -> total return over the span
    metrics: dict[str, dict]  # variant -> metrics_with_ci(oos), computed once
    naive_metrics: dict[str, dict]  # naive run -> metrics_with_ci, computed once
    setup: dict[str, str] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)


# --- inputs ---------------------------------------------------------------------------------------


def load_inputs(
    settings: Settings,
    store: BarStore,
    account: AccountKind,
    instruments: dict[str, Instrument],
    *,
    data_end: pd.Timestamp | None = None,
    assets: list[str] | None = None,
) -> Inputs:
    """Full-history panel of ``account`` (missing bars filled), perp funding, and the bars the
    universe is selected from (spot: the perp panel, as the baseline). ``data_end`` (exclusive)
    truncates every series; ``assets`` restricts the candidate assets. Bars before
    ``history_start - min_history_days - WARMUP`` are not loaded (spot archives reach back to 2013):
    the universe's history rule at any window (train_end > history_start) and the indicators still
    see everything they can use, so no asset's history is undercounted."""
    cfg = settings.universe
    lo = pd.Timestamp(cfg.history_start, tz="UTC") - pd.Timedelta(days=cfg.min_history_days) - WARMUP
    hi = data_end - BAR if data_end is not None else None
    perp_assets = [a for a in store.assets("kraken_futures") if assets is None or a in assets]
    perp_raw = {a: store.read("kraken_futures", a, lo, hi) for a in perp_assets}
    perp_raw = {a: df for a, df in perp_raw.items() if not df.empty}
    if account == "perp":
        raw, universe_panel = perp_raw, None
    else:
        raw = {a: store.read("kraken_spot", a, lo, hi) for a in store.assets("kraken_spot")
               if assets is None or a in assets}
        raw = {a: df for a, df in raw.items() if not df.empty}
        universe_panel = perp_raw
    dropped = sorted(a for a in raw if a not in instruments)
    if dropped:
        log.warning("no instrument metadata for %s; dropped", dropped)
    panel = {a: fill_missing(df) for a, df in raw.items() if a in instruments}
    if not panel:
        raise ValueError(f"no {account} bars with instrument metadata")
    funding: dict[str, pd.DataFrame] = {}
    if account == "perp":
        for a in panel:
            f = store.read_funding(a)
            funding[a] = f[f.index < data_end] if data_end is not None and len(f) else f
    return Inputs(panel, funding, universe_panel, {a: instruments[a] for a in panel}, dropped)


def build_tradelists(inp: Inputs, settings: Settings, account: AccountKind, *, use_regime: bool,
                     data_end=None, cache_dir: Path | None = None) -> pd.DataFrame:
    return build_walkforward_tradelists(
        inp.panel, inp.funding, settings, account, use_regime=use_regime, data_end=data_end,
        universe_panel=inp.universe_panel,
        cache_dir=cache_dir if cache_dir is not None else Path(settings.var_dir) / "research",
    )


# --- naive baselines and benchmarks ---------------------------------------------------------------


def span_universe(inp: Inputs, settings: Settings, start: pd.Timestamp) -> list[str]:
    """Universe at ``start`` with exactly the walk-forward rule (``universe_at``)."""
    return universe_at(inp.panel, start, settings, inp.universe_panel)


def run_naive(
    inp: Inputs, settings: Settings, kind: AccountKind, start: pd.Timestamp, end: pd.Timestamp
) -> tuple[dict[str, BacktestResult], dict[str, str]]:
    """Each naive strategy alone over [start, end), AcceptAllRanker, edge variant, universe at
    ``start``. Returns (results, skipped name -> reason)."""
    universe = span_universe(inp, settings, start)
    strategies = {"naive_momentum": NaiveMomentum(), "naive_mean_reversion": NaiveMeanReversion()}
    keys = {"naive_momentum": list(universe), "naive_mean_reversion": list(universe)}
    skipped: dict[str, str] = {}
    if kind == "spot":
        skipped["naive_pairs"] = "pairs require shorting (spot account)"
    elif "ETH" in universe and "BTC" in universe:
        strategies["naive_pairs"] = NaivePairs()
        keys["naive_pairs"] = [NAIVE_PAIR]
    else:
        skipped["naive_pairs"] = "ETH/BTC not both in the universe at the span start"
    edge = _variant(settings, kind, EDGE)
    out = {}
    for name, strat in strategies.items():
        if not keys[name]:
            skipped[name] = "empty universe at the span start"
            continue
        out[name] = run_backtest(inp.panel, inp.funding, [strat], {name: keys[name]}, AcceptAllRanker(),
                                 kind, edge, start, end, funding_mode="known_only",
                                 instruments=inp.instruments)
    return out, skipped


def best_naive_expectancy(naive: dict[str, BacktestResult]) -> float:
    vals = [float(r.trades["r_multiple"].mean()) for r in naive.values() if len(r.trades)]
    vals = [v for v in vals if math.isfinite(v)]
    return max(vals) if vals else math.nan


def _close_return(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    c = df.loc[(df.index >= start) & (df.index < end), "close"]
    return float(c.iloc[-1] / c.iloc[0] - 1.0) if len(c) >= 2 else math.nan


def benchmarks(inp: Inputs, settings: Settings, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, float]:
    """Total returns over the span: cash, BTC buy & hold, equal-weight universe buy & hold."""
    out = {"cash": 0.0}
    out["BTC buy & hold"] = _close_return(inp.panel["BTC"], start, end) if "BTC" in inp.panel else math.nan
    rets = [_close_return(inp.panel[a], start, end) for a in span_universe(inp, settings, start)]
    rets = [r for r in rets if math.isfinite(r)]
    out["equal-weight universe buy & hold"] = float(np.mean(rets)) if rets else math.nan
    return out


# --- evaluation -----------------------------------------------------------------------------------


def _pre_holdout_gap(
    inp: Inputs, settings: Settings, data_start, data_end
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[last walk-forward test_end, holdout start): covered by neither the walk-forward OOS nor the
    holdout (only whole test windows are used). Empty when they touch."""
    start, end = _bounds(inp.panel, settings, data_start, data_end)
    windows, (h0, _) = make_windows(start, end, settings.walkforward)
    return (windows[-1].test_end if windows else h0, h0)


def _caveats(kind: AccountKind, inp: Inputs, known_from: dict | None, variants, mode: str,
             gap: tuple[pd.Timestamp, pd.Timestamp]) -> list[str]:
    days = (gap[1] - gap[0]) / pd.Timedelta(days=1)
    out = [
        "Survivorship bias: the universe only contains assets listed today; coins delisted in the past "
        "are absent from the data. Each window's universe is selected with data before its train end.",
        "Regime thresholds are rolling: trailing quantiles (trend 70th, volatility 90th percentile) over "
        "the past 8640 bars (~90 days) per asset, causal at every bar, rather than one constant fixed "
        "on each train window as spec §5.4 describes.",
        f"Pre-holdout gap: [{gap[0]:%Y-%m-%d}, {gap[1]:%Y-%m-%d}) ({days:.0f} days) lies between the last "
        "walk-forward test window and the holdout; it belongs to neither OOS result (only whole test "
        "windows are used).",
        "Open positions at the end of the run are listed separately and are not counted in trade "
        "counts, expectancy or P&L tables (closed trades only).",
        "Reported results come only from the bar-by-bar portfolio simulation; research trade lists "
        "are used for parameter selection and the expectancy table only.",
    ]
    if kind == "perp":
        first = {a: t for a, t in (known_from or {}).items() if a in inp.panel}
        known = [f"{a}: {t:%Y-%m-%d}" for a, t in sorted(first.items()) if not pd.isna(t)]
        none = sorted(a for a, t in first.items() if pd.isna(t))
        unk = int((variants["edge"].oos.trades["funding_unknown_bars"] > 0).sum())
        out.append(
            "Funding gap: perp funding before the first published rate is unknown and never invented in "
            "the main results (main variants: known_only, unknown hours add 0 and are counted). First "
            f"known funding per asset: {', '.join(known) or 'none'}"
            + (f"; never known: {', '.join(none)}" if none else "")
            + f". Edge-variant trades holding unknown-funding hours: {unk}."
        )
    else:
        out.append("Spot: real Kraken spot OHLCVT; bars without trades are forward-filled (filled=True) "
                   "and never fill orders. Spot is long-only, 1x, no funding, no pairs.")
    if inp.dropped:
        out.append(f"Assets dropped for missing instrument metadata: {', '.join(inp.dropped)}.")
    skipped = variants["edge"].per_window
    if mode == "walkforward" and "status" in skipped:
        n_skip = int(skipped["status"].astype(str).str.startswith("skipped").sum())
        if n_skip:
            out.append(f"{n_skip} leading window(s) skipped: empty universe (history rule "
                       "min_history_days).")
    return out


def _setup(kind, inp: Inputs, span, tradelists: pd.DataFrame | None, elapsed: dict[str, float]) -> dict:
    s = {
        "account": kind,
        "oos span": f"{span[0]} .. {span[1]}",
        "panel assets": ", ".join(sorted(inp.panel)),
    }
    if tradelists is not None and len(tradelists):
        combos = tradelists[["strategy", "params_id", "key"]].drop_duplicates()
        s["combinations tried (strategy x params x key)"] = str(len(combos))
    for k, v in elapsed.items():
        s[f"runtime {k}"] = f"{v:.1f}s"
    return s


def _finish(
    kind: AccountKind, mode: Literal["walkforward", "holdout"], variants: dict[str, WalkForwardResult],
    naive: dict[str, BacktestResult], naive_skipped: dict[str, str], inp: Inputs, settings: Settings,
    span: tuple[pd.Timestamp, pd.Timestamp], tradelists: pd.DataFrame, elapsed: dict[str, float],
    gap: tuple[pd.Timestamp, pd.Timestamp],
) -> EvalReport:
    """Metrics (once per result), GO/NO-GO, caveats -> EvalReport."""
    metrics = {n: metrics_with_ci(r.oos) for n, r in variants.items()}
    naive_metrics = {n: metrics_with_ci(r) for n, r in naive.items()}
    known_from = None if kind == "spot" else dict(variants["edge"].oos.funding_known_from)
    g = evaluate(variants["edge"].oos, variants["realistic"].oos, variants["cost_2x_edge"].oos,
                 variants["edge"].per_window, best_naive_expectancy(naive), known_from,
                 metrics={"edge": metrics["edge"], "realistic": metrics["realistic"],
                          "cost_x2_edge": metrics["cost_2x_edge"]})
    caveats = _caveats(kind, inp, known_from, variants, mode, gap)
    if mode == "holdout":
        caveats.append("Holdout: a single window, run exactly once per account (recorded in the holdout "
                       "registry); the windows check reads that one window. Ablations are walk-forward "
                       "only.")
    return EvalReport(
        kind, mode, variants, naive, naive_skipped, g, known_from, span, benchmarks(inp, settings, *span),
        metrics, naive_metrics, _setup(kind, inp, span, tradelists, elapsed), caveats,
    )


def walkforward_report(
    inp: Inputs,
    tradelists: pd.DataFrame,
    tradelists_no_regime: pd.DataFrame,
    settings: Settings,
    kind: AccountKind,
    *,
    data_start=None,
    data_end=None,
    registry: Path | None = None,
) -> EvalReport:
    """Run every walk-forward variant, the naive baselines over the OOS span and the GO/NO-GO."""
    variants: dict[str, WalkForwardResult] = {}
    elapsed: dict[str, float] = {}
    for name, kw in walkforward_variants(kind).items():
        t0 = time.perf_counter()
        tl = tradelists_no_regime if kw.get("ablation") == "no_regime" else tradelists
        variants[name] = run_walkforward(
            inp.panel, inp.funding, tl, settings, kind, data_start=data_start, data_end=data_end,
            instruments=inp.instruments, universe_panel=inp.universe_panel, registry=registry, **kw,
        )
        elapsed[name] = time.perf_counter() - t0
        log.info("walk-forward %s: %d trades (%.1fs)", name, len(variants[name].oos.trades), elapsed[name])
    models = variants["edge"].windows
    span = (models[0].window.test_start, models[-1].window.test_end)
    t0 = time.perf_counter()
    naive, naive_skipped = run_naive(inp, settings, kind, *span)
    elapsed["naive"] = time.perf_counter() - t0
    return _finish(kind, "walkforward", variants, naive, naive_skipped, inp, settings, span, tradelists,
                   elapsed, _pre_holdout_gap(inp, settings, data_start, data_end))


def holdout_report(
    inp: Inputs,
    tradelists: pd.DataFrame,
    settings: Settings,
    kind: AccountKind,
    *,
    registry: Path | None = None,
    data_end=None,
    out_dir: Path | None = None,
) -> EvalReport:
    """The one-shot holdout (``run_holdout``: refuses a second run) + naive baselines over the
    holdout span + GO/NO-GO. The raw variant results are pickled to ``out_dir`` right after the
    holdout is recorded, so a crash while reporting does not lose the only holdout run."""
    t0 = time.perf_counter()
    variants = run_holdout(inp.panel, inp.funding, tradelists, settings, kind, registry=registry,
                           data_end=data_end, instruments=inp.instruments,
                           universe_panel=inp.universe_panel)
    elapsed = {"holdout": time.perf_counter() - t0}
    if out_dir is not None:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(out_dir) / "holdout_results.pkl", "wb") as fh:
            pickle.dump(variants, fh)
    w = variants["edge"].windows[0].window
    span = (w.test_start, w.test_end)
    naive, naive_skipped = run_naive(inp, settings, kind, *span)
    return _finish(kind, "holdout", variants, naive, naive_skipped, inp, settings, span, tradelists,
                   elapsed, _pre_holdout_gap(inp, settings, None, data_end))


# --- outputs --------------------------------------------------------------------------------------


def _json_safe(v):
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_json_safe(x) for x in v]
    if isinstance(v, np.generic):
        v = v.item()
    if v is None or (not isinstance(v, str | int | float | bool) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None if math.isnan(v) else ("inf" if v > 0 else "-inf")
    return v


def _known_split(res: BacktestResult, known_from: dict | None) -> dict:
    tr = res.trades
    mask = funding_known_mask(tr, known_from)
    out = {}
    for name, sel in (("known", mask), ("unknown", ~mask)):
        t = tr[sel]
        out[name] = {"n_trades": len(t),
                     "expectancy_r": float(t["r_multiple"].mean()) if len(t) else math.nan,
                     "pnl_net": float(t["pnl_net"].sum()), "funding": float(t["funding"].sum())}
    return out


def summary_dict(rep: EvalReport) -> dict:
    g = rep.gonogo
    out = {
        "account": rep.account,
        "mode": rep.mode,
        "verdict": g.verdict,
        "headline": g.headline,
        "checks": {k: {"passed": ok, "detail": d, "variant": CHECKS[k][0]}
                   for k, (ok, d) in g.checks.items()},
        "span": [rep.span[0], rep.span[1]],
        "variants": {},
        "naive": rep.naive_metrics,
        "naive_skipped": rep.naive_skipped,
        "best_naive_expectancy_r": best_naive_expectancy(rep.naive),
        "benchmarks": rep.benchmarks,
        "setup": rep.setup,
        "caveats": rep.caveats,
        "funding_known_from": (None if rep.funding_known_from is None else
                               {a: (None if pd.isna(t) else pd.Timestamp(t).isoformat())
                                for a, t in sorted(rep.funding_known_from.items())}),
        "ablations": {
            n: {"d_expectancy_r": rep.metrics[n]["expectancy_r"] - rep.metrics["edge"]["expectancy_r"],
                "d_pnl_net": rep.metrics[n]["pnl_net"] - rep.metrics["edge"]["pnl_net"]}
            for n in ABLATIONS if n in rep.metrics
        },
    }
    for name, res in rep.variants.items():
        m = dict(rep.metrics[name])
        m["open_at_end"] = len(res.open_at_end)
        m["funding_split"] = _known_split(res.oos, rep.funding_known_from)
        pw = res.per_window
        if "net_pnl" in pw:
            net = pd.to_numeric(pw["net_pnl"], errors="coerce").dropna()
            m["windows_net_positive"] = f"{int((net > 0).sum())}/{len(net)}"
        cols = [c for c in WINDOW_COLUMNS if c in pw]
        m["windows"] = [{c: r[c] for c in cols} for r in pw.to_dict("records")]
        out["variants"][name] = m
    return _json_safe(out)


def write_report(rep: EvalReport, out_dir: Path, title: str) -> Path:
    out_dir = Path(out_dir)
    path = render_evaluation(rep, out_dir, title)
    (out_dir / "summary.json").write_text(json.dumps(summary_dict(rep), indent=1), encoding="utf-8")
    return path

