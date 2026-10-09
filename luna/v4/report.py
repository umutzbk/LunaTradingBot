"""Markdown report of a V4 stage run (spec §9). Stage 1 opens with the exploratory caveat."""

from __future__ import annotations

from luna.v4.stats import BLOCKS


def _f(x, fmt="{:.4g}") -> str:
    return "n/a" if x is None else fmt.format(x)


def render_report(s: dict) -> str:
    lines: list[str] = []
    stage = s["stage"]
    lines.append(f"# Luna Research V4 — Stage {stage}" + (f" (look {s['look']})" if s.get("look") else ""))
    lines.append("")
    if stage == 1:
        lines.append(
            f"**{s['label']}** Stage 1 alone can never give GO; GO evidence comes only from the "
            "forward evaluation (Stage 2)."
        )
        lines.append("")
    lines.append(f"**DEPLOYABLE NOW: {s['deployable_now']['deployable_now']}**")
    for r in s["deployable_now"]["reasons"]:
        lines.append(f"- {r}")
    lines.append("")
    lines.append(f"Window: {s['window']} ({s['window_days']} days, DQ-valid {s['dq_valid_days']}).")
    for n in s.get("notes") or []:
        lines.append(f"- {n}")
    if stage == 1:
        lines.append(f"\nStage-1 candidate (information only): {s.get('stage1_candidate') or 'none'}")
    lines.append("\n## RESEARCH EDGE\n")
    lines.append(
        "| config | RESEARCH EDGE | "
        + " | ".join(f"C{i}" for i in range(1, 8))
        + " | "
        + " | ".join(f"M{i}" for i in range(1, 5))
        + " | "
        + " | ".join(f"p b={b}" for b in BLOCKS)
        + " |"
    )
    lines.append("|---" * (2 + 7 + 4 + len(BLOCKS)) + "|")
    for cid, c in s["configs"].items():
        crit = " | ".join("PASS" if c["criteria"][f"C{i}"] else "FAIL" for i in range(1, 8))
        m = " | ".join("PASS" if c["m"].get(f"M{i}") else "FAIL" for i in range(1, 5))
        ps = " | ".join(_f(c["pvalues"].get(f"b{b}")) for b in BLOCKS)
        lines.append(f"| {cid} | {c['research_edge']} | {crit} | {m} | {ps} |")
    lines.append("\n## Sensitivity rows and P&L decomposition\n")
    lines.append(
        "| config | row | mean daily | Sharpe | max DD | spot price | perp price | funding | fees | "
        "spread | liq. penalty | entries |"
    )
    lines.append("|---" * 12 + "|")
    for cid, c in s["configs"].items():
        for name, r in c["rows"].items():
            p = r["pnl"]
            lines.append(
                f"| {cid} | {name} | {_f(r['mean_daily'], '{:.6f}')} | {_f(r['sharpe'], '{:.3f}')} | "
                f"{_f(r['max_drawdown'], '{:.3f}')} | {_f(p['spot_price_pnl'], '{:.2f}')} | "
                f"{_f(p['perp_price_pnl'], '{:.2f}')} | {_f(p['funding_cf'], '{:.2f}')} | "
                f"{_f(p['fees'], '{:.2f}')} | {_f(p['spread_slippage'], '{:.2f}')} | "
                f"{_f(p['liquidation_penalty'], '{:.2f}')} | {r['entries']} |"
            )
    lines.append("\n## Counters (PRIMARY)\n")
    keys = [
        "skipped_actions",
        "min_order_skips",
        "funding_unknown_hours",
        "liquidations",
        "perp_spread_fallbacks",
        "hedge_rebalances",
        "margin_reductions",
        "flat_weeks",
        "outage_days",
        "funding_unknown_flag",
    ]
    lines.append("| config | " + " | ".join(keys) + " |")
    lines.append("|---" * (len(keys) + 1) + "|")
    for cid, c in s["configs"].items():
        prim = c["rows"]["PRIMARY"]
        cnt = {
            **prim["counters"],
            "funding_unknown_flag": "FLAG (>1%)" if prim["funding_unknown_flag"] else "ok",
        }
        lines.append(
            f"| {cid} | "
            + " | ".join(str(v) if isinstance(v := cnt.get(k), str) else _f(v, "{:.4g}") for k in keys)
            + " |"
        )
    lines.append("\n## Nulls (PRIMARY; never GO, never promoted)\n")
    lines.append("| config | null | Sharpe | mean daily | entries |")
    lines.append("|---|---|---|---|---|")
    for cid, ns in s["nulls"].items():
        for nid, r in ns.items():
            lines.append(
                f"| {cid} | {nid} | {_f(r['sharpe'], '{:.3f}')} | {_f(r['mean_daily'], '{:.6f}')} | "
                f"{r['entries']} |"
            )
    lines.append("\n## Diagnostics (not configs; never part of GO / NO-GO)\n")
    d = s["diagnostics"]
    lines.append(f"- D-PERSIST: {d['D-PERSIST']}")
    lines.append(f"- D-H5CELLS: {d['D-H5CELLS']}")
    lines.append(f"- D-DISP: {d['D-DISP']}")
    lines.append(f"- H2 exposure / beta: {d['H2']}")
    lines.append(f"- H5 vs H1-Y10 (information): {d['H5_vs_H1-Y10_sharpe']}")
    if "venue_basis" in d:
        lines.append(f"- Venue basis: {d['venue_basis']}")
    return "\n".join(lines) + "\n"
