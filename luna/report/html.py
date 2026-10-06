"""Self-contained HTML reports: the minimal baseline report (summary table, equity curves, P&L
breakdown) and the full walk-forward / holdout evaluation report with its GO/NO-GO verdict."""

from __future__ import annotations

import html
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from luna.engine.backtest import BacktestResult
from luna.report.metrics import metrics_with_ci

log = logging.getLogger("luna.report")
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
        ("Equity curves", eq.to_html(full_html=False, include_plotlyjs=True)),
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


# --- full walk-forward / holdout report -----------------------------------------------------------

EVAL_SECTIONS = [
    "Verdict", "Summary", "P&L breakdown", "Funding known vs unknown",
    "Strategy × asset × regime heatmap", "Expectancy table", "Pair selection per window",
    "Ablations", "Baselines", "Sensitivity", "Data caveats",
]
SECONDARY_HEADING = "Secondary — informational, daily returns"
EXTRA_PRIMARY = [
    ("expectancy_bps", "expectancy_bps (net)", "{:.1f}"),
    ("worst_trade_r", "worst_trade_r", "{:.2f}"),
    ("win_rate", "win_rate", "{:.1%}"),
    ("stop_count", "stop_count", "{:.0f}"),
    ("funding_unknown_trades", "trades with unknown funding hours", "{:.0f}"),
    ("avg_leverage", "avg gross leverage", "{:.2f}"),
    ("max_leverage", "max gross leverage", "{:.2f}"),
    ("corr_btc", "corr with BTC (daily)", "{:.2f}"),
]
COMPARE = [  # compact comparison columns: (metric key, label, format)
    ("n_trades", "n_trades", "{:.0f}"),
    ("expectancy_r", "expectancy_r", "{:.3f}"),
    ("ci", "90% CI", None),
    ("profit_factor", "PF", "{:.2f}"),
    ("pnl_net", "net USD", "{:.2f}"),
    ("funding", "funding USD", "{:.2f}"),
    ("max_dd", "max_dd", "{:.2%}"),
]
VARIANT_NOTES = {
    "realistic": "config as-is (drawdown kill switch on)",
    "edge": "kill switch off (max_drawdown_override=1.0)",
    "cost_2x": "realistic, all costs x2",
    "cost_2x_edge": "edge, all costs x2",
    "funding_zero": "edge, unknown funding hours at 0x",
    "funding_estimated": "edge, unknown funding hours at the observed mean rate",
    "funding_estimated_x2": "edge, unknown funding hours at 2x the observed mean rate",
    "no_regime": "edge, regime gates off (trade lists and strategies)",
    "random_ranker": "edge, random ranker instead of the expectancy ranker",
}


def _e(x: object) -> str:
    return html.escape(str(x))


def _day(ts) -> str:
    return "n/a" if ts is None or pd.isna(ts) else f"{pd.Timestamp(ts):%Y-%m-%d}"


def _plain_table(columns: list[str], rows: list[list[str]], caption: str | None = None) -> str:
    """Cells are inserted as given (callers escape text)."""
    head = "".join(f"<th>{_e(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    cap = f"<h3>{_e(caption)}</h3>" if caption else ""
    return f"{cap}<table><tr>{head}</tr>{body}</table>"


def _cell(m: dict, key: str, f: str | None) -> str:
    if key == "ci":
        return f"[{_fmt(m['ci_lo'], '{:.3f}')}, {_fmt(m['ci_hi'], '{:.3f}')}]"
    v = m.get(key)
    return _fmt(None if v is None or pd.isna(v) else float(v), f)


def _compare_rows(names: list[str], m: dict[str, dict], base: str | None = None) -> str:
    cols = ["variant", "what", *[label for _, label, _ in COMPARE]]
    if base is not None:
        cols += [f"Δ expectancy_r vs {base}", f"Δ net USD vs {base}"]
    rows = []
    for n in names:
        r = [_e(n), _e(VARIANT_NOTES.get(n, ""))] + [_cell(m[n], k, f) for k, _, f in COMPARE]
        if base is not None:
            r += [_fmt(m[n]["expectancy_r"] - m[base]["expectancy_r"], "{:+.3f}"),
                  _fmt(m[n]["pnl_net"] - m[base]["pnl_net"], "{:+.2f}")]
        rows.append(r)
    return _plain_table(cols, rows)


def _group_table(tr: pd.DataFrame, by: list[str], caption: str) -> str:
    if tr.empty:
        return f"<h3>{_e(caption)}</h3><p>no closed trades</p>"
    agg = tr.groupby(by, sort=True).agg(
        n=("pnl_net", "size"), exp_r=("r_multiple", "mean"), gross=("pnl_gross", "sum"),
        fees=("fees", "sum"), spread_slip=("spread_slip", "sum"), funding=("funding", "sum"),
        net=("pnl_net", "sum"),
    )
    rows = []
    for idx, a in agg.iterrows():
        idx = idx if isinstance(idx, tuple) else (idx,)
        money = [_fmt(float(a[c]), "{:.2f}") for c in ("gross", "fees", "spread_slip", "funding", "net")]
        rows.append([_e(i) for i in idx] + [f"{a['n']:.0f}", _fmt(float(a["exp_r"]), "{:.3f}"), *money])
    return _plain_table([*by, "n", "expectancy_r", *PNL], rows, caption)


def _verdict_html(rep) -> str:
    from luna.report.gonogo import CHECKS

    g = rep.gonogo
    color = {"GO": "#1a7f37", "NO-GO": "#b42318", "INSUFFICIENT DATA": "#9a6700"}[g.verdict]
    rows = [[_e(k), _e(CHECKS[k][0]), _e(CHECKS[k][1]), "pass" if ok else "<b>FAIL</b>", _e(d)]
            for k, (ok, d) in g.checks.items()]
    return (
        f"<p class='verdict' style='color:{color}'>{_e(rep.account)} {_e(rep.mode)}: "
        f"<b>{_e(g.verdict)}</b></p><p>{_e(g.headline)}</p>"
        + _plain_table(["check", "variant", "condition (spec §8)", "result", "detail"], rows)
        + "<p>Variants: <b>realistic</b> = the config as-is (drawdown kill switch on); <b>edge</b> = the "
        "same fitted models with the kill switch disabled (max_drawdown_override=1.0). The drawdown and "
        "tail checks (max_drawdown, worst_day, daily_cvar5) read the realistic variant: that is what the "
        "account would have lived through. Every expectancy-based check (min_trades, expectancy_ci_lo, "
        "profit_factor, cost_x2, funding_known_net, windows_positive, complexity_adds_value) reads the "
        "edge variant, so that an early kill switch can neither hide nor fake an edge. "
        "Fewer than 30 trades gives INSUFFICIENT DATA; otherwise every check must pass for GO.</p>"
    )


def _summary_eval(rep, m: dict[str, dict]) -> str:
    names = [n for n in ("realistic", "edge") if n in m]
    prim = [(label, [_cell(m[n], key, f) for n in names]) for key, label, f in PRIMARY + EXTRA_PRIMARY]
    eq = go.Figure()
    for n in names:
        e = rep.variants[n].oos.equity
        eq.add_trace(go.Scatter(x=e.index, y=e.to_numpy(), mode="lines", name=n))
    eq.update_layout(title="OOS equity (stitched test windows)", xaxis_title="time (UTC)",
                     yaxis_title="equity (USD)")
    parts = [_table(prim, names, "Primary metrics"), eq.to_html(full_html=False, include_plotlyjs=True)]
    for n in names:
        rows = []
        for _, r in rep.variants[n].per_window.iterrows():
            rows.append([_e(_day(r["test_start"])), _e(_day(r["test_end"])), _e(r["status"]),
                         _cell(r, "n_trades", "{:.0f}"), _cell(r, "expectancy_r", "{:.3f}"),
                         _cell(r, "net_pnl", "{:.2f}")])
        parts.append(_plain_table(["test start", "test end", "status", "n_trades", "expectancy_r",
                                   "net USD"], rows, f"Per-window results ({n})"))
    for n in names:
        oae = rep.variants[n].open_at_end
        rows = [[_e(r["position_id"]), _e(r["strategy"]), _e(r["key"]), _e(r["direction"]),
                 _e(r["entry_ts"]), _fmt(float(r["risk_amount"]), "{:.2f}"),
                 _fmt(float(r["unrealized_net"]), "{:.2f}")] for _, r in oae.iterrows()]
        parts.append(
            f"<h3>Open positions at the end ({_e(n)})</h3><p>{len(oae)} position(s) still open at the end "
            "of the run: shown here only and not counted in n_trades, expectancy or the P&amp;L tables "
            "(closed trades only); their unrealized net is part of the final equity.</p>"
            + (_plain_table(["position", "strategy", "key", "direction", "entry", "risk USD",
                             "unrealized net USD"], rows) if rows else "")
        )
    sec = [(label, [_cell(m[n], k, f) for n in names]) for k, label, f in SECONDARY]
    parts.append(
        f'<div id="secondary"><h3>{_e(SECONDARY_HEADING)}</h3>'
        + _table(sec, names, "Sharpe / Sortino (annualized, daily UTC returns)")
        + "<p>Sharpe and Sortino are computed from daily returns only, never used in the GO/NO-GO, "
        "and can mislead here (overlapping positions, autocorrelation).</p></div>"
    )
    return "".join(parts)


def _pnl_eval(rep, m: dict[str, dict]) -> str:
    names = [n for n in ("realistic", "edge") if n in m]
    pnl = [(k, [_fmt(m[n][k], "{:.2f}") for n in names]) for k in PNL]
    bar = go.Figure()
    for k in PNL:
        bar.add_trace(go.Bar(name=k, x=names, y=[m[n][k] for n in names]))
    bar.update_layout(title="P&L breakdown (USD)", barmode="group")
    tr = rep.variants["edge"].oos.trades
    return (_table(pnl, names, "Gross / fees / spread+slippage / funding / net (USD)")
            + bar.to_html(full_html=False, include_plotlyjs=False)
            + _group_table(tr, ["strategy"], "By strategy (edge)")
            + _group_table(tr, ["key"], "By asset / pair (edge)"))


def _funding_eval(rep) -> str:
    from luna.report.gonogo import funding_known_mask

    if rep.funding_known_from is None:
        return "<p>Spot account: no funding; every trade belongs to the known period.</p>"
    rows = []
    for n in ("realistic", "edge"):
        tr = rep.variants[n].oos.trades
        mask = funding_known_mask(tr, rep.funding_known_from)
        for label, sel in (("known period", mask), ("unknown period (funding excluded)", ~mask)):
            t = tr[sel]
            rows.append([_e(n), _e(label), str(len(t)),
                         _fmt(float(t["r_multiple"].mean()) if len(t) else None, "{:.3f}"),
                         _fmt(float(t["pnl_gross"].sum()), "{:.2f}"),
                         _fmt(float(t["funding"].sum()), "{:.2f}"),
                         _fmt(float(t["pnl_net"].sum()), "{:.2f}")])
    first = [[_e(a), _e("never" if pd.isna(t) else f"{pd.Timestamp(t):%Y-%m-%d %H:%M}")]
             for a, t in sorted(rep.funding_known_from.items())]
    return (
        "<p>A trade is in the known period if it was entered at or after the first published funding "
        "rate of every asset it holds. Unknown-period results carry no funding P&amp;L (funding "
        "excluded, never invented); see Sensitivity for the 0x / estimated / 2x scenarios.</p>"
        + _plain_table(["variant", "period", "n_trades", "expectancy_r", "gross USD", "funding USD",
                        "net USD"], rows)
        + _plain_table(["asset", "first known funding (UTC)"], first, "Funding known from")
    )


def _heatmap_eval(rep) -> str:
    tr = rep.variants["edge"].oos.trades
    if tr.empty:
        return "<p>No closed trades in the edge variant.</p>"
    t = tr.assign(row=tr["strategy"].astype(str) + " | " + tr["key"].astype(str),
                  regime=tr["regime"].astype(str), r=tr["r_multiple"].astype(float))
    z = t.pivot_table(index="row", columns="regime", values="r", aggfunc="mean")
    cnt = t.pivot_table(index="row", columns="regime", values="r", aggfunc="size").reindex_like(z)
    text = [["" if not np.isfinite(v) else f"n={int(v)}" for v in r] for r in cnt.to_numpy(dtype=float)]
    fig = go.Figure(go.Heatmap(z=z.to_numpy(dtype=float), x=list(z.columns), y=list(z.index), text=text,
                               texttemplate="%{text}", colorscale="RdBu", zmid=0,
                               colorbar={"title": {"text": "expectancy R"}}))
    fig.update_layout(title="Expectancy (R) per strategy | key and regime (edge, closed trades)",
                      height=max(300, 40 * len(z) + 150))
    return (fig.to_html(full_html=False, include_plotlyjs=False)
            + _group_table(tr, ["strategy", "key", "regime"], "Strategy x asset x regime (edge)"))


def _expectancy_eval(rep) -> str:
    models = rep.variants["edge"].windows
    last = models[-1]
    rows = [[_e(s), _e(k), _e(r), str(c.n), _fmt(c.mean_r, "{:.3f}"), _fmt(c.lo_r, "{:.3f}"),
             _fmt(c.hi_r, "{:.3f}"), _fmt(c.shrunk_r, "{:.3f}"), "yes" if c.accepted else "no", _e(c.reason)]
            for (s, k, r), c in sorted(last.table.items())]
    per = []
    for mdl in models:
        acc = sum(1 for (_, k, r), c in mdl.table.items() if k != "*" and r == "*" and c.accepted)
        params = ", ".join(f"{n}={p}" for n, p in mdl.params.items()) or "-"
        per.append([_e(_day(mdl.window.test_start)), _e(_day(mdl.window.train_start)),
                    _e(_day(mdl.window.train_end)), str(acc), _e(params), _e(mdl.funding_rule)])
    w = last.window
    return (
        f"<p>Last window's table, fitted on train trades in [{_day(w.train_start)}, {_day(w.train_end)}) "
        f"and used by the ranker in the test window from {_day(w.test_start)}. CIs (90% day-block "
        "bootstrap) and acceptance live at the (strategy, key) level; regime cells need an accepted "
        "parent and shrunk_r &gt; 0; '*' = all.</p>"
        + (_plain_table(["strategy", "key", "regime", "n", "mean_r", "CI lo", "CI hi", "shrunk_r",
                         "accepted", "reason"], rows) if rows else "<p>empty table (no train trades)</p>")
        + _plain_table(["test start", "train start", "train end", "accepted (strategy, key) cells",
                        "params", "param selection rule"], per, "Accepted cells per window")
    )


def _pairs_eval(rep) -> str:
    if rep.account == "spot":
        note = "<p>Spot never trades pairs (needs shorting): nothing is tested.</p>"
    else:
        note = ("<p>Per train window: correlation prefilter, Engle-Granger tests with Benjamini-Hochberg "
                "FDR control, then the stability test on the FDR passers.</p>")
    rows = []
    for mdl in rep.variants["edge"].windows:
        p = mdl.pairs
        rows.append([_e(_day(mdl.window.test_start)), _e(", ".join(mdl.universe) or "-"), str(p.tested),
                     str(len(p.fdr_pass)), str(len(p.stable)), _e(", ".join(p.selected) or "-")])
    return note + _plain_table(["test start", "universe", "tested", "FDR pass", "stable", "selected"], rows)


def _ablations_eval(rep, m: dict[str, dict]) -> str:
    names = [n for n in ("no_regime", "random_ranker") if n in m]
    if not names:
        return "<p>Ablations are run in the walk-forward only.</p>"
    return ("<p>Each ablation removes one layer and is compared with the main edge variant on "
            "expectancy (R) and net P&amp;L.</p>" + _compare_rows(["edge", *names], m, base="edge"))


def _baselines_eval(rep, m: dict[str, dict], best: float) -> str:
    rows = [[_e("Luna (edge)")] + [_cell(m["edge"], k, f) for k, _, f in COMPARE]]
    for n, x in rep.naive_metrics.items():
        mark = " (best)" if math.isfinite(best) and x["expectancy_r"] == best else ""
        rows.append([_e(n + mark)] + [_cell(x, k, f) for k, _, f in COMPARE])
    for n, why in rep.naive_skipped.items():
        rows.append([_e(n), _e(f"skipped: {why}")] + [""] * (len(COMPARE) - 1))
    bench = [[_e(k), _fmt(v, "{:.2%}")] for k, v in rep.benchmarks.items()]
    s0, s1 = rep.span
    return (
        f"<p>Naive strategies (spec §8.1) each run alone with AcceptAllRanker over exactly the same span "
        f"[{s0:%Y-%m-%d %H:%M}, {s1:%Y-%m-%d %H:%M}), universe selected at the span start, edge variant, "
        "same cost model and risk engine. Check complexity_adds_value compares Luna with the best of "
        "them.</p>"
        + _plain_table(["run", *[label for _, label, _ in COMPARE]], rows)
        + _plain_table(["benchmark", "total return over the span"], bench, "Benchmarks")
    )


def _sensitivity_eval(rep, m: dict[str, dict]) -> str:
    order = ("realistic", "cost_2x", "edge", "cost_2x_edge", "funding_zero", "funding_estimated",
             "funding_estimated_x2")
    return ("<p>Scenarios only, never the headline result. Funding scenarios change only the hours "
            "without a known rate (perp).</p>" + _compare_rows([n for n in order if n in m], m))


def render_evaluation(rep, out_dir: Path, title: str) -> Path:
    """Write the full walk-forward / holdout report (sections ``EVAL_SECTIONS``) to
    ``out_dir/report.html``; ``rep`` is a ``luna.report.walkforward.EvalReport``."""
    from luna.report.walkforward import best_naive_expectancy

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    m = rep.metrics
    setup = _plain_table(["", ""], [[_e(k), _e(v)] for k, v in rep.setup.items()], "Run setup")
    body = {
        "Verdict": _verdict_html(rep),
        "Summary": _summary_eval(rep, m),
        "P&L breakdown": _pnl_eval(rep, m),
        "Funding known vs unknown": _funding_eval(rep),
        "Strategy × asset × regime heatmap": _heatmap_eval(rep),
        "Expectancy table": _expectancy_eval(rep),
        "Pair selection per window": _pairs_eval(rep),
        "Ablations": _ablations_eval(rep, m),
        "Baselines": _baselines_eval(rep, m, best_naive_expectancy(rep.naive)),
        "Sensitivity": _sensitivity_eval(rep, m),
        "Data caveats": "<ul>" + "".join(f"<li>{_e(c)}</li>" for c in rep.caveats) + "</ul>" + setup,
    }
    sections = "".join(f"<section><h2>{_e(h)}</h2>{body[h]}</section>" for h in EVAL_SECTIONS)
    doc = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title>"
        "<style>body{font-family:sans-serif;margin:24px}table{border-collapse:collapse;margin:8px 0}"
        "td,th{border:1px solid #ccc;padding:4px 10px;text-align:right}"
        "td:first-child,th:first-child{text-align:left}.verdict{font-size:28px}</style></head>"
        f"<body><h1>{_e(title)}</h1>{sections}</body></html>"
    )
    path = out_dir / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path
