"""Markdown report of a V4 stage run (spec §9). Stage 1 opens with the exploratory caveat."""

from __future__ import annotations

from luna.v4.stats import BLOCKS


def _f(x, fmt="{:.4g}") -> str:
    return "n/a" if x is None else fmt.format(x)


def _count(cid: str, n) -> str:
    """Carry results count position entries; H2 results count order fills (one per leg change)."""
    return f"{n} fills" if cid.startswith("H2") else f"{n} entries"


def _kill_inputs(s: dict) -> list[str]:
    out = [
        "| config | decision | mean daily net | UB95 b=28 | LB95 b=28 | LB95 b=14 | "
        "carry value (H1/H5) | mean carry_net (H2) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cid, c in s["configs"].items():
        d = c.get("stage1") or {}
        why = f" ({', '.join(d['reasons'])})" if d.get("reasons") else ""
        out.append(
            f"| {cid} | {d.get('decision')}{why} | {_f(d.get('mean_daily'), '{:.6f}')} | "
            f"{_f(d.get('ub95_b28'), '{:.6f}')} | {_f(d.get('lb95_b28'), '{:.6f}')} | "
            f"{_f(d.get('lb95_b14'), '{:.6f}')} | {_f(d.get('carry_value'), '{:.2f}')} | "
            f"{_f(d.get('carry_net_mean'), '{:.6f}')} |"
        )
    return out


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
        hyp = ", ".join(
            f"{h}: {'advances' if ok else 'killed'}" for h, ok in (s.get("hypotheses") or {}).items()
        )
        lines.append(f"\nHypotheses (summary only; the forward set is per config): {hyp}")
        lines.append("\n## Stage 1 decision (ADVANCE TO FORWARD / KILL)\n")
        lines.append(
            "KILL reasons: (a) mean < 0 and b=28 one-sided 95% upper bound < 0; (b) carry component does "
            "not cover costs; (c) no position ever open. C1-C7 / M1-M4 are information only.\n"
        )
        lines.extend(_kill_inputs(s))
        lines.append("\n### C1-C7 / M1-M4 (information only)\n")
    else:
        lines.append("\n## RESEARCH EDGE (headline b=28; b=14 mandatory robustness)\n")
        if s.get("killed"):
            lines.append(
                f"KILLED in Stage 1 (p = 1 in the Holm family, not simulated): {', '.join(s['killed'])}\n"
            )
    lines.append(
        "| config | "
        + ("Stage 1 decision" if stage == 1 else "RESEARCH EDGE")
        + " | "
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
        why = (
            f" ({', '.join(c['stage1']['reasons'])})"
            if stage == 1 and c.get("stage1", {}).get("reasons")
            else ""
        )
        lines.append(f"| {cid} | {c['research_edge']}{why} | {crit} | {m} | {ps} |")
    lines.append("\n## Sensitivity rows and P&L decomposition\n")
    lines.append(
        "| config | row | mean daily | Sharpe | max DD | spot price | perp price | funding | fees | "
        "spread | liq. penalty | entries (carry) / fills (H2) |"
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
                f"{_f(p['liquidation_penalty'], '{:.2f}')} | {_count(cid, r['entries'])} |"
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
    lines.append("| config | null | Sharpe | mean daily | entries (carry) / fills (H2) |")
    lines.append("|---|---|---|---|---|")
    for cid, ns in s["nulls"].items():
        for nid, r in ns.items():
            lines.append(
                f"| {cid} | {nid} | {_f(r['sharpe'], '{:.3f}')} | {_f(r['mean_daily'], '{:.6f}')} | "
                f"{_count(cid, r['entries'])} |"
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
    mc = {"valuation_stale": 0, **(s.get("market_counters") or {})}
    lines.append("\n## Market data counters (all runs)\n")
    lines.append(", ".join(f"{k}: {v}" for k, v in sorted(mc.items())))
    return "\n".join(lines) + "\n"
