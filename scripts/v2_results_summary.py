# ruff: noqa: E501  (markdown table rows are kept on one line each)
"""Research V2 final results: per-account markdown from a run's summary.json (read-only).

Usage: uv run python scripts/v2_results_summary.py <summary.json> [<summary.json> ...]

Reads only the fields the closure needs: verdict, checks, variants, naive_momentum,
robustness.candidate, extended["candidate:realistic"], configs, trials, caveats.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def f(x, fmt="{:+.2f}") -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return fmt.format(x) if isinstance(x, (int, float)) else str(x)


def pct(x) -> str:
    return f(None if x is None else 100 * x, "{:.2f}%")


def usd(x) -> str:
    return f(x, "{:+.2f} $")


def ci(m: dict, lo="ci_lo", hi="ci_hi") -> str:
    return f"[{f(m.get(lo))}, {f(m.get(hi))}]"


def section(path: Path) -> str:
    s = json.loads(path.read_text(encoding="utf-8"))
    acct = s["account"]
    v = s["variants"]
    real, edge, c2 = v["realistic"], v["edge"], v.get("cost_2x_edge", {})
    naive = s.get("naive_momentum") or {}
    rob = s.get("robustness", {}).get("candidate", {})
    ext = s.get("extended", {}).get("candidate:realistic", {})
    d = s["data"]
    out = [f"## {acct.capitalize()}: {s['verdict']} ({s['candidate']})", ""]
    out += [
        f"- Run: `{path.parent.as_posix()}` (code `{d['git_sha'][:7]}`, git_dirty={d['git_dirty']}, "
        f"last bar read {d['last_bar']}, data end {d['data_end']})",
        f"- Headline: {s['headline']}",
        "",
        "| Metric | Realistic | Edge |",
        "|---|---|---|",
        f"| Trades | {f(real.get('n_trades'), '{:.0f}')} | {f(edge.get('n_trades'), '{:.0f}')} |",
        f"| Mean R (90% bootstrap CI) | {f(real.get('expectancy_r'))} {ci(real)} | {f(edge.get('expectancy_r'))} {ci(edge)} |",
        f"| Profit factor | {f(real.get('profit_factor'), '{:.2f}')} | {f(edge.get('profit_factor'), '{:.2f}')} |",
        f"| Net P&L | {usd(real.get('pnl_net'))} | {usd(edge.get('pnl_net'))} |",
        f"| Max drawdown | {pct(real.get('max_dd'))} | {pct(edge.get('max_dd'))} |",
        f"| Worst day | {pct(real.get('worst_day'))} | {pct(edge.get('worst_day'))} |",
        f"| Positive windows / total | {real.get('windows_net_positive')} | {edge.get('windows_net_positive')} |",
        f"| 2x costs (edge): mean R, net | {f(c2.get('expectancy_r'))}, {usd(c2.get('pnl_net'))} (n={f(c2.get('n_trades'), '{:.0f}')}) | |",
        "",
        "**GO/NO-GO checks (frozen §3.5):**",
        "",
        "| Check | Variant | Result | Detail |",
        "|---|---|---|---|",
    ]
    for k, c in s["checks"].items():
        out.append(f"| {k} | {c['variant']} | {'pass' if c['passed'] else 'FAIL'} | {c['detail']} |")
    out += [
        "",
        f"**Naive momentum baseline (V1, same account and span):** n={f(naive.get('n_trades'), '{:.0f}')}, "
        f"mean R {f(naive.get('expectancy_r'))} {ci(naive)}, PF {f(naive.get('profit_factor'), '{:.2f}')}, "
        f"net {usd(naive.get('pnl_net'))}, max DD {pct(naive.get('max_dd'))}.",
        "",
    ]
    full = rob.get("full", {})
    if full.get("n"):
        out += [
            "**Robustness (diagnostic only):**",
            "",
            f"- Full: n={full['n']}, mean R {f(full.get('mean_r'))} {ci(full)}; without top 1 trade "
            f"{f(rob['ex_top1'].get('mean_r'))}; without top 3 {f(rob['ex_top3'].get('mean_r'))}; "
            f"trimmed mean {f(rob.get('trimmed_mean_r'))}; median trade {f(rob.get('median_r'))}; "
            f"fragile={rob.get('fragile')}.",
            f"- P&L concentration: top 1% of trades {f(rob['concentration'].get('top1pct'), '{:.2f}')}, "
            f"top 10% {f(rob['concentration'].get('top10pct'), '{:.2f}')} of net; asset HHI "
            f"{f(rob['concentration'].get('asset_hhi'), '{:.2f}')}.",
            "- Asset leave-one-out (nested re-selection): "
            + ", ".join(f"{r['asset']} {f(r['mean_r'])} (n={r['n']})" for r in rob.get("asset_loo", [])),
            "- Period leave-one-out (by test window): "
            + ", ".join(
                f"{str(r['test_start'])[:7]} {f(r['mean_r'])}" for r in rob.get("time_loo", []) if r.get("n")
            ),
            f"- Without best month ({rob.get('ex_best_month', {}).get('removed')}): "
            f"{f(rob.get('ex_best_month', {}).get('mean_r'))}; without best quarter "
            f"({rob.get('ex_best_quarter', {}).get('removed')}): {f(rob.get('ex_best_quarter', {}).get('mean_r'))}.",
            "",
        ]
    else:
        out += [
            "**Robustness / asset LOO / period LOO:** not applicable: the candidate walk-forward made 0 trades.",
            "",
        ]
    costs = ext.get("costs", {})
    if costs:
        out += [
            "**Cost attribution (candidate, realistic):** "
            f"fees {usd(costs.get('fees'))}, spread {usd(costs.get('spread'))}, slippage {usd(costs.get('slippage'))}, "
            f"known funding {usd(costs.get('funding_known'))} ({costs.get('funding_unknown_hours')} unknown funding hours); "
            f"cost/gross {f(ext.get('cost_to_gross'), '{:.2f}')}; gross R {f(ext.get('gross_r'))} vs net R {f(ext.get('net_r'))}.",
            "",
        ]
    windows = [w for w in s.get("candidate_windows", []) if w.get("config")]
    flat = len(s.get("candidate_windows", [])) - len(windows)
    out += [
        f"**Nested selection:** {len(windows)} window(s) traded, {flat} flat (no accepted configuration)."
    ]
    for w in windows:
        out.append(
            f"- {str(w['test_start'])[:7]}: {w['config']} {w.get('params')} (train objective {f(w.get('objective'))})"
        )
    out += [
        "",
        "**Configurations (each fit on train data only; edge variant):**",
        "",
        "| Config | Trades | Mean R | Net |",
        "|---|---|---|---|",
    ]
    for cid, runs in s.get("configs", {}).items():
        m = runs.get("edge", {})
        if m.get("n_trades"):
            cell = cid.replace("|", "\\|")  # a raw pipe would split the markdown table cell
            out.append(
                f"| {cell} | {f(m['n_trades'], '{:.0f}')} | {f(m.get('expectancy_r'))} | {usd(m.get('pnl_net'))} |"
            )
    silent = [cid for cid, runs in s.get("configs", {}).items() if not runs.get("edge", {}).get("n_trades")]
    out += [
        f"| {len(silent)} other configs (no train-accepted window) | 0 | n/a | 0 |",
        "",
        f"**Trials (§3.3):** {s.get('trials')}",
        "",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    print("\n".join(section(Path(p)) for p in sys.argv[1:]))
