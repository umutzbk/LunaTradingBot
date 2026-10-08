"""Research V2 per-account report (spec §3.5, §5, §6, §9, §11; plan Task 11).

The verdict is V1's ``luna.report.gonogo.evaluate`` on the account candidate's walk-forward:
edge / realistic / 2x-cost-edge variants, its windows, and the bar of V1 naive momentum over the
same span and account (spot: long-only, as V1 runs it there). Robustness outputs (§5) and the
"kırılgan" (fragile) tag are diagnostics placed next to the verdict; they never change it.

Every OOS number is the development walk-forward OOS (2024-09 -> 2026-07). The final generalization
set (2026-07 -> 2026-10) is never read, and no metric about it exists here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from luna.config import Settings
from luna.engine.backtest import BacktestResult
from luna.engine.walkforward import WalkForwardResult
from luna.execution.costs import CostModel
from luna.report.gonogo import CHECKS, GoNoGo, evaluate
from luna.report.html import _e, _plain_table
from luna.report.metrics import metrics_with_ci
from luna.report.walkforward import Inputs, _json_safe
from luna.types import default_order_model
from luna.v2.data import V2_DATA_END, v2_settings
from luna.v2.fills import missed_fills
from luna.v2.metrics import R_DEFINITIONS, extended_metrics
from luna.v2.robustness import robustness
from luna.v2.runner import ResearchResults

EVALUATION_SET = "development walk-forward OOS (2024-09 -> 2026-07)"
UNTOUCHED_NOTE = (
    f"Final generalization set 2026-07 -> 2026-10 is untouched: no bar at or after "
    f"{V2_DATA_END:%Y-%m-%d} was read and no metric about it exists in this report "
    "(spec D7, §3.4, §11)."
)
NO_CANDIDATE = "aday yok"
SECTIONS = [
    ("verdict", "Verdict (GO / NO-GO)"),
    ("evaluation-set", "Evaluation set"),
    ("candidate", "Candidate per window (train-only nested selection)"),
    ("variants", "Candidate variants"),
    ("naive", "Naive momentum bar"),
    ("configs", "Configurations"),
    ("extended", "Extended metrics (§6)"),
    ("robustness", "Robustness (§5) - diagnostic, not part of the verdict"),
    ("costs", "Cost attribution"),
    ("trials", "Trials (§3.3)"),
    ("caveats", "Caveats"),
]
_VARIANT_COLS = [
    ("n_trades", "{:.0f}"),
    ("expectancy_r", "{:.3f}"),
    ("ci_lo", "{:.3f}"),
    ("ci_hi", "{:.3f}"),
    ("profit_factor", "{:.2f}"),
    ("max_dd", "{:.2%}"),
    ("worst_day", "{:.2%}"),
    ("cvar_5_daily", "{:.2%}"),
    ("pnl_net", "{:.2f}"),
]


@dataclass
class V2Report:
    account: str
    span: tuple[pd.Timestamp, pd.Timestamp]
    candidate_windows: pd.DataFrame
    variants: dict[str, WalkForwardResult]
    metrics: dict[str, dict]
    configs: dict[str, dict[str, WalkForwardResult]]
    config_metrics: dict[str, dict[str, dict]]
    naive_momentum: BacktestResult | None
    naive_metrics: dict | None
    gonogo: GoNoGo
    candidate: str
    robustness: dict[str, dict]
    extended: dict[str, dict]
    missed_fills: pd.DataFrame
    missed_rebalances: dict
    trials: dict
    caveats: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)  # data end, last bar read, assets, code version


def naive_momentum_expectancy(naive: dict[str, BacktestResult]) -> float:
    """§3.5 item 8 bar: V1 naive momentum only (never the best of all naive baselines)."""
    res = naive.get("naive_momentum")
    if res is None or res.trades.empty:
        return math.nan
    v = float(res.trades["r_multiple"].mean())
    return v if math.isfinite(v) else math.nan


def _candidate_windows(r: ResearchResults) -> pd.DataFrame:
    rows = []
    for w, cw in zip(r.windows[r.start_k :], r.selected[r.start_k :], strict=True):
        rows.append(
            {
                "test_start": w.test_start,
                "test_end": w.test_end,
                "config": cw.config.id if cw else None,
                "params": cw.params_id if cw else None,
                "objective": cw.objective if cw else math.nan,
                "status": "selected" if cw else "flat: no accepted configuration",
            }
        )
    return pd.DataFrame(rows)


def _caveats(r: ResearchResults) -> list[str]:
    out = [
        f"Every OOS figure is the {EVALUATION_SET}. {UNTOUCHED_NOTE}",
        "Survivorship bias: only assets listed today are in the data; each window's universe uses data "
        "before its train end only.",
        "Reported results come from bar-by-bar simulation only; trade lists serve parameter selection.",
        "Open positions at the end of a run are excluded from trade counts and P&L tables.",
        "Cross-sectional orders use the SC-1 limits profile (docs/superpowers/specs/"
        "2026-10-08-research-v2-spec-change-1.md); TSMOM and breakout use V1 R1-R19 unchanged.",
        "TSMOM / breakout have no regime gate (V2 strategies); the regime column is NONE.",
    ]
    if r.account == "spot":
        out.append(
            "Spot: long-only, 1x, no funding, no pairs; bars without trades are forward-filled and "
            "never fill orders."
        )
    else:
        out.append(
            "Perp funding before the first published rate is unknown and never invented in the main "
            "results; the funding variants are sensitivity only."
        )
    return out


def build_v2_report(r: ResearchResults, inp: Inputs, settings: Settings) -> V2Report:
    kind = r.account
    s2 = v2_settings(settings)
    costs = CostModel(s2.costs[kind])
    limits = s2.accounts[kind]
    variants = r.candidate_runs
    metrics = {v: metrics_with_ci(res.oos) for v, res in variants.items()}
    known_from = None if kind == "spot" else dict(variants["edge"].oos.funding_known_from)
    g = evaluate(
        variants["edge"].oos,
        variants["realistic"].oos,
        variants["cost_2x_edge"].oos,
        variants["edge"].per_window,
        naive_momentum_expectancy(r.naive),
        known_from,
        metrics={
            "edge": metrics["edge"],
            "realistic": metrics["realistic"],
            "cost_x2_edge": metrics["cost_2x_edge"],
        },
    )
    candidate = (
        NO_CANDIDATE
        if g.verdict != "GO"
        else ("GO: the nested train-only selection (candidate per window below) passes every §3.5 check")
    )

    rob: dict[str, dict] = {}
    for cid, runs in r.config_runs.items():
        oos = runs["edge"]
        loo = r.config_loo.get(cid)
        rob[cid] = robustness(
            oos.oos.trades,
            oos.per_window,
            run_without=(loo.__getitem__ if loo else None),
            assets=r.loo_assets.get(cid, []),
        )
    rob["candidate"] = robustness(
        variants["edge"].oos.trades,
        variants["edge"].per_window,
        run_without=(r.candidate_loo.__getitem__ if r.candidate_loo else None),
        assets=sorted(r.candidate_loo),
    )

    default = default_order_model(kind)
    alt = variants["order_alt_edge"]
    extended = {
        f"candidate:{v}": extended_metrics(variants[v].oos, inp.panel, costs, kind, limits, default)
        for v in ("realistic", "edge")
    }
    extended["candidate:order_alt_edge"] = extended_metrics(
        alt.oos, inp.panel, costs, kind, limits, "maker" if default == "taker" else "taker"
    )
    for cid, runs in r.config_runs.items():
        extended[cid] = extended_metrics(runs["edge"].oos, inp.panel, costs, kind, limits, default)

    maker_run = alt if default == "taker" else variants["edge"]
    models = [cw.signal for cw in r.selected if cw is not None and cw.signal is not None]
    missed = missed_fills(maker_run.oos, models, inp.panel, inp.funding, s2, kind)
    ev = maker_run.oos.risk_events
    mr = ev[ev["kind"] == "missed_rebalance"] if len(ev) else ev
    missed_reb = {
        "count": int(len(mr)),
        "notional": float(mr["detail"].str.extract(r"notional ([\d.]+)")[0].astype(float).sum())
        if len(mr)
        else 0.0,
    }

    naive = r.naive.get("naive_momentum")
    return V2Report(
        account=kind,
        span=r.span,
        candidate_windows=_candidate_windows(r),
        variants=variants,
        metrics=metrics,
        configs=r.config_runs,
        config_metrics={
            cid: {v: metrics_with_ci(x.oos) for v, x in runs.items()} for cid, runs in r.config_runs.items()
        },
        naive_momentum=naive,
        naive_metrics=metrics_with_ci(naive) if naive is not None else None,
        gonogo=g,
        candidate=candidate,
        robustness=rob,
        extended=extended,
        missed_fills=missed,
        missed_rebalances=missed_reb,
        trials=r.trials,
        caveats=_caveats(r),
        data={k: r.manifest.get(k) for k in ("data_end", "last_bar", "assets", "git_sha", "git_dirty")},
    )


def summary_dict_v2(rep: V2Report) -> dict:
    g = rep.gonogo
    variants = {}
    for name, res in rep.variants.items():
        m = dict(rep.metrics[name])
        net = pd.to_numeric(res.per_window.get("net_pnl", pd.Series(dtype=float)), errors="coerce").dropna()
        m["windows_net_positive"] = f"{int((net > 0).sum())}/{len(net)}"
        m["open_at_end"] = len(res.open_at_end)
        variants[name] = m
    out = {
        "account": rep.account,
        "evaluation_set": EVALUATION_SET,
        "data": rep.data,
        "untouched_set_note": UNTOUCHED_NOTE,
        "verdict": g.verdict,
        "headline": g.headline,
        "candidate": rep.candidate,
        "checks": {
            k: {"passed": ok, "detail": d, "variant": CHECKS[k][0]} for k, (ok, d) in g.checks.items()
        },
        "span": [rep.span[0], rep.span[1]],
        "candidate_windows": rep.candidate_windows.to_dict("records"),
        "variants": variants,
        "naive_momentum": rep.naive_metrics,
        "configs": rep.config_metrics,
        "fragile": {k: v["fragile"] for k, v in rep.robustness.items()},
        "robustness": rep.robustness,
        "extended": rep.extended,
        "missed_fills": {
            "n": int(len(rep.missed_fills)),
            "mean_r_net_taker": float(rep.missed_fills["r_net_taker"].mean())
            if len(rep.missed_fills)
            else math.nan,
            "rows": rep.missed_fills.to_dict("records"),
        },
        "missed_rebalances": rep.missed_rebalances,
        "trials": rep.trials,
        "r_definitions": R_DEFINITIONS,
        "caveats": rep.caveats,
    }
    return _json_safe(out)


# --- HTML -----------------------------------------------------------------------------------------


def _f(v, fmt: str) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    return fmt.format(v) if isinstance(v, int | float) else _e(v)


def _metric_table(rows: dict[str, dict], caption: str) -> str:
    cols = ["run"] + [c for c, _ in _VARIANT_COLS]
    body = [[_e(n)] + [_f(m.get(c), f) for c, f in _VARIANT_COLS] for n, m in rows.items()]
    return _plain_table(cols, body, caption)


def _json_block(obj) -> str:
    return f"<pre>{_e(json.dumps(_json_safe(obj), indent=1))}</pre>"


def _exposure_chart(rep: V2Report) -> str:
    """Exposure over time (§6) of the candidate's edge run: daily gross / net leverage."""
    days = rep.extended.get("candidate:edge", {}).get("exposure_daily", [])
    if not days:
        return ""
    fig = go.Figure()
    x = [d["date"] for d in days]
    for k in ("gross", "net", "max_asset_share"):
        fig.add_trace(go.Scatter(x=x, y=[d[k] for d in days], mode="lines", name=k))
    fig.update_layout(title="Candidate exposure over time (edge)", height=300)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _sections(rep: V2Report) -> dict[str, str]:
    g = rep.gonogo
    checks = [[_e(k), "pass" if ok else "FAIL", _e(CHECKS[k][0]), _e(d)] for k, (ok, d) in g.checks.items()]
    fig = go.Figure()
    for v in ("realistic", "edge"):
        eq = rep.variants[v].oos.equity
        fig.add_trace(go.Scatter(x=eq.index, y=eq.to_numpy(), mode="lines", name=f"candidate {v}"))
    fragile = [
        [
            _e(k),
            "kırılgan" if v["fragile"] else "-",
            _f(v["full"]["mean_r"], "{:.3f}"),
            _f(v["ex_top1"]["mean_r"], "{:.3f}"),
            _f(v["trimmed_mean_r"], "{:.3f}"),
            _f(v["median_r"], "{:.3f}"),
        ]
        for k, v in rep.robustness.items()
    ]
    cfg_rows = {f"{cid} [{v}]": m for cid, runs in rep.config_metrics.items() for v, m in runs.items()}
    costs = {k: v["costs"] | {"cost_to_gross": v["cost_to_gross"]} for k, v in rep.extended.items()}
    cw = rep.candidate_windows
    return {
        "verdict": (
            f"<p class='verdict'><b>{_e(g.verdict)}</b>: {_e(g.headline)}</p>"
            f"<p>Candidate: <b>{_e(rep.candidate)}</b></p>"
            + _plain_table(["check", "result", "variant", "detail"], checks)
        ),
        "evaluation-set": f"<p>{_e(EVALUATION_SET)}: {rep.span[0]:%Y-%m-%d} .. {rep.span[1]:%Y-%m-%d}.</p>"
        f"<p>{_e(UNTOUCHED_NOTE)}</p>"
        f"<p>Data end {_e(rep.data.get('data_end'))}; last bar read {_e(rep.data.get('last_bar'))}; "
        f"code {_e(rep.data.get('git_sha'))}.</p>",
        "candidate": _plain_table(
            list(cw.columns), [[_e(x) for x in row] for row in cw.itertuples(index=False)]
        ),
        "variants": _metric_table(rep.metrics, "development walk-forward OOS")
        + _exposure_chart(rep)
        + fig.to_html(full_html=False, include_plotlyjs=True),
        "naive": _metric_table(
            {"naive_momentum": rep.naive_metrics or {}},
            "V1 naive momentum, same account and span (the §3.5 item 8 bar)",
        ),
        "configs": _metric_table(cfg_rows, "every configuration, train-only fitted"),
        "extended": "<h3>R definitions</h3>" + _json_block(R_DEFINITIONS) + _json_block(rep.extended),
        "robustness": (
            "<p>Diagnostic only (D3): no output here changes the verdict; "
            "'kırılgan' is informational.</p>"
            + _plain_table(["configuration", "tag", "full R", "ex-top1 R", "trimmed R", "median R"], fragile)
            + _json_block(rep.robustness)
        ),
        "costs": _json_block(costs)
        + _json_block({"missed_fills": len(rep.missed_fills), "missed_rebalances": rep.missed_rebalances}),
        "trials": _json_block(rep.trials),
        "caveats": "<ul>" + "".join(f"<li>{_e(c)}</li>" for c in rep.caveats) + "</ul>",
    }


def write_v2_report(rep: V2Report, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    body = _sections(rep)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Luna Research V2 - {_e(rep.account)}</title>"
        "<style>body{font-family:sans-serif;margin:24px}table{border-collapse:collapse;margin:8px 0}"
        "td,th{border:1px solid #ccc;padding:3px 6px;font-size:13px}pre{font-size:11px;max-height:480px;"
        "overflow:auto;background:#f6f6f6}.verdict{font-size:20px}</style></head><body>"
        f"<h1>Luna Research V2 - {_e(rep.account)}</h1>"
        + "".join(f"<section id='{sid}'><h2>{_e(title)}</h2>{body[sid]}</section>" for sid, title in SECTIONS)
        + "</body></html>"
    )
    path = out_dir / "report.html"
    path.write_text(html, encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary_dict_v2(rep), indent=1), encoding="utf-8")
    return path
