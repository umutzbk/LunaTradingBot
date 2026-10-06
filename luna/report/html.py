"""Minimal self-contained HTML report: summary table, equity curves, P&L breakdown."""

from __future__ import annotations

import html
import math
from pathlib import Path

import plotly.graph_objects as go

from luna.engine.backtest import BacktestResult
from luna.report.metrics import summary
from luna.research.stats import block_bootstrap_ci

PRIMARY = [
    ("n_trades", "n_trades", "{:.0f}"),
    ("expectancy_r", "expectancy_r (net)", "{:.3f}"),
    ("ci", "expectancy_r CI (day-block bootstrap)", None),
    ("profit_factor", "profit_factor", "{:.2f}"),
    ("max_dd", "max_dd", "{:.2%}"),
    ("worst_day", "worst_day", "{:.2%}"),
    ("cvar_5_daily", "cvar_5_daily", "{:.2%}"),
    ("turnover", "turnover (x equity / day)", "{:.2f}"),
    ("exposure", "exposure", "{:.1%}"),
]
PNL = ["pnl_gross", "fees", "spread_slip", "funding", "pnl_net"]
SECONDARY = [("sharpe_daily", "sharpe_daily", "{:.2f}"), ("sortino_daily", "sortino_daily", "{:.2f}")]


def expectancy_ci(res: BacktestResult, n: int = 2000, ci: float = 0.90) -> tuple[float, float]:
    """(lo, hi) day-block bootstrap CI of the mean R multiple, blocks = UTC day of the trade exit."""
    tr = res.trades
    if tr.empty:
        return (math.nan, math.nan)
    days = tr["exit_ts"].dt.floor("D").astype("int64").to_numpy()
    _, lo, hi = block_bootstrap_ci(tr["r_multiple"].to_numpy(dtype=float), days, n=n, ci=ci)
    return (lo, hi)


def metrics_with_ci(res: BacktestResult) -> dict[str, float]:
    lo, hi = expectancy_ci(res)
    return {**summary(res), "ci_lo": lo, "ci_hi": hi}


def _fmt(v: float | None, f: str) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "inf" if isinstance(v, float) and v == math.inf else "n/a"
    return f.format(v)


def _table(rows: list[tuple[str, list[str]]], names: list[str], header: str) -> str:
    head = "".join(f"<th>{html.escape(n)}</th>" for n in names)
    body = "".join(
        f"<tr><td>{html.escape(label)}</td>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
        for label, cells in rows
    )
    return f"<h3>{html.escape(header)}</h3><table><tr><th></th>{head}</tr>{body}</table>"


def _summary_html(m: dict[str, dict[str, float]]) -> str:
    names = list(m)
    prim = []
    for key, label, f in PRIMARY:
        if key == "ci":
            cells = [f"[{_fmt(m[n]['ci_lo'], '{:.3f}')}, {_fmt(m[n]['ci_hi'], '{:.3f}')}]" for n in names]
        else:
            cells = [_fmt(m[n][key], f) for n in names]
        prim.append((label, cells))
    pnl = [(k, [_fmt(m[n][k], "{:.2f}") for n in names]) for k in PNL]
    sec = [(label, [_fmt(m[n][k], f) for n in names]) for k, label, f in SECONDARY]
    return (
        _table(prim, names, "Primary metrics")
        + _table(pnl, names, "P&L breakdown (USD)")
        + _table(sec, names, "Secondary - informational (daily returns)")
    )


def render_report(
    results: dict[str, BacktestResult],
    out_dir: Path,
    title: str,
    extra_sections: list[tuple[str, str]] | None = None,
) -> Path:
    """Write `report.html` into out_dir and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    m = {name: metrics_with_ci(res) for name, res in results.items()}

    eq = go.Figure()
    for name, res in results.items():
        eq.add_trace(go.Scatter(x=res.equity.index, y=res.equity.to_numpy(), mode="lines", name=name))
    eq.update_layout(title="Equity", xaxis_title="time (UTC)", yaxis_title="equity (USD)")

    bar = go.Figure()
    for k in PNL:
        bar.add_trace(go.Bar(name=k, x=list(m), y=[m[n][k] for n in m]))
    bar.update_layout(title="P&L breakdown (USD)", barmode="group")

    sections = [
        ("Summary", _summary_html(m)),
        ("Equity curves", eq.to_html(full_html=False, include_plotlyjs="cdn")),
        ("P&L breakdown", bar.to_html(full_html=False, include_plotlyjs=False)),
        *(extra_sections or []),
    ]
    body = "".join(f"<section><h2>{html.escape(h)}</h2>{c}</section>" for h, c in sections)
    doc = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        "<style>body{font-family:sans-serif;margin:24px}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:4px 10px;text-align:right}"
        "td:first-child,th:first-child{text-align:left}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    path = out_dir / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path


