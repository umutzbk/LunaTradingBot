"""Naive baseline runs (spec 8.1): all three naive strategies together plus each alone."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from luna.config import Settings
from luna.data.store import BarStore, fill_missing
from luna.engine.backtest import BacktestResult, run_backtest
from luna.ranking.ranker import AcceptAllRanker
from luna.report.html import render_report
from luna.report.metrics import metrics_with_ci
from luna.strategy.naive import NaiveMeanReversion, NaiveMomentum, NaivePairs
from luna.types import AccountKind, Instrument
from luna.universe import select_universe

log = logging.getLogger("luna.baseline")
BAR = pd.Timedelta("15min")
WARMUP = pd.Timedelta(days=10)  # bars kept before `start` so EMA(480) / EWMA(288) are converged
VENUE = {"perp": "kraken_futures", "spot": "kraken_spot"}
PAIR_KEY = "ETH/BTC"
RUNS = ("naive_momentum", "naive_mean_reversion", "naive_pairs")


def _json_safe(d: dict) -> dict:
    """NaN -> None, +/-inf -> "inf" / "-inf", numpy scalars -> python."""
    out = {}
    for k, v in d.items():
        if isinstance(v, np.generic):
            v = v.item()
        if isinstance(v, float) and not math.isfinite(v):
            v = None if math.isnan(v) else ("inf" if v > 0 else "-inf")
        out[k] = v
    return out


def run_baseline(
    settings: Settings,
    store: BarStore,
    account: AccountKind,
    instruments: dict[str, Instrument],
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    out_dir: Path,
) -> dict:
    """Run, write `report.html` and `summary.json` into out_dir; return the summary dict."""
    cfg = settings.universe
    perp_panel = {a: store.read("kraken_futures", a) for a in store.assets("kraken_futures")}
    if start is None:
        start = pd.Timestamp(cfg.history_start, tz="UTC") + pd.Timedelta(days=cfg.min_history_days)
    universe = select_universe(perp_panel, start, cfg.min_adv_usd, cfg.min_history_days)
    if account == "spot":
        spot_assets = set(store.assets("kraken_spot"))
        universe = [a for a in universe if a in spot_assets]
    dropped = [a for a in universe if a not in instruments]
    if dropped:
        log.warning("no instrument metadata for %s; dropped", dropped)
    universe = [a for a in universe if a in instruments]
    if not universe:
        firsts = [df.index[0] for df in perp_panel.values() if not df.empty]
        hint = ""
        if firsts:
            earliest = min(firsts) + pd.Timedelta(days=cfg.min_history_days)
            hint = f"; earliest valid start is {earliest} (first bar + {cfg.min_history_days}d history)"
        raise ValueError(f"empty {account} universe at {start}{hint}")

    lo = start - WARMUP
    panel = {}
    for a in universe:
        bars = store.read(VENUE[account], a, lo, end - BAR if end is not None else None)
        if not bars.empty:
            panel[a] = fill_missing(bars)
    if end is None:
        end = min(df.index[-1] for df in panel.values()) + BAR
    funding = {a: store.read_funding(a) for a in panel} if account == "perp" else {}
    inst = {a: instruments[a] for a in panel}
    pairs_ok = account == "perp" and "ETH" in panel and "BTC" in panel
    keys = {
        "naive_momentum": sorted(panel),
        "naive_mean_reversion": sorted(panel),
        "naive_pairs": [PAIR_KEY] if pairs_ok else [],
    }
    strategies = {
        "naive_momentum": NaiveMomentum(),
        "naive_mean_reversion": NaiveMeanReversion(),
        "naive_pairs": NaivePairs(),
    }
    if account == "spot" or not pairs_ok:
        strategies.pop("naive_pairs")  # spot: needs shorting; perp: ETH/BTC not both in universe

    def go(names: list[str]) -> BacktestResult:
        log.info("running %s on %d assets, %s .. %s", names, len(panel), start, end)
        return run_backtest(
            panel, funding, [strategies[n] for n in names], {n: keys[n] for n in names},
            AcceptAllRanker(), account, settings, start, end, funding_mode="known_only",
            instruments=inst,
        )

    results = {"combined": go(list(strategies))}
    for n in RUNS:
        if n in strategies:
            results[n] = go([n])

    summ: dict = {k: _json_safe(metrics_with_ci(r)) for k, r in results.items()}
    extra = []
    if account == "spot":
        summ["naive_pairs"] = {"skipped": "pairs require shorting"}
        extra.append(("Skipped", "<p>naive_pairs: pairs require shorting (spot account).</p>"))
    elif not pairs_ok:
        summ["naive_pairs"] = {"skipped": "ETH/BTC not in universe"}
        extra.append(("Skipped", "<p>naive_pairs: ETH/BTC not in universe.</p>"))
    summ["skipped_assets"] = dropped
    extra.append(
        ("Run setup", f"<p>account={account} start={start} end={end} universe={', '.join(sorted(panel))}</p>")
    )
    out_dir = Path(out_dir)
    render_report(results, out_dir, f"Luna naive baseline - {account}", extra)
    (out_dir / "summary.json").write_text(json.dumps(summ, indent=1), encoding="utf-8")
    return summ
