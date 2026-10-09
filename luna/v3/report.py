"""Research V3 report (spec §4, §9, E2): the V2 report machinery with criterion 8 replaced by the
family null baseline compared on daily Sharpe, the V3 caveats first, and a ``v3`` block in
summary.json (families, null baseline, cumulative trial counts). The untouched final generalization
set is never read."""

from __future__ import annotations

import json
import math
from pathlib import Path

from luna.config import Settings
from luna.report.gonogo import MIN_TRADES, GoNoGo
from luna.report.walkforward import Inputs
from luna.v2.report import NO_CANDIDATE, build_v2_report, write_v2_report
from luna.v2.runner import ResearchResults
from luna.v2.walkforward import count_trials
from luna.v3.runner import NULL_SEEDS

# earlier experiments on the same development set (docs/results/walkforward.md, research-v2.md)
PRIOR_TRIALS = {"v1": 208, "v2": {"perp": 184, "spot": 88}}
CRITERION_8 = "daily Sharpe (edge) > mean null daily Sharpe"
V3_CAVEATS = [
    "Research V3 runs on a reused development set (OOS 2024-09 -> 2026-07) that V1 and V2 already "
    "examined many times: every V3 figure is exploratory and not generalization evidence (spec E2).",
    "Criterion 8 compares the candidate's daily Sharpe (edge variant) with its family null baseline: "
    "shuffled scores on the same eligible assets (seeds 1-5, mean over seeds that traded) for premium / "
    "low-vol windows, the same book without vol targeting for vol-managed windows (spec §4). Sharpe is "
    "scale-free; R would penalise vol targeting by construction. V1 naive momentum is information only.",
    "Perp-spot premium = perp close / spot close - 1 at the daily decision bar; NaN when either bar is "
    "missing or forward-filled. Perp funding history starts 2025-10-06 (unknown funding is never made up).",
]


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def null_check(cand_sharpe, null: dict, cand_r, n_seeds: int) -> tuple[bool, str]:
    ns, ne = null["null_sharpe_daily"], null["null_expectancy_r"]
    if _fin(ns):
        ok = _fin(cand_sharpe) and cand_sharpe > ns
        bar = f"null {ns:.3f} (mean of {null['null_seeds_traded']}/{n_seeds} seeds that traded)"
    else:
        ok = _fin(cand_sharpe) and cand_sharpe > 0
        bar = "null made no trades: compared with cash (0)"
    cs = f"{cand_sharpe:.3f}" if _fin(cand_sharpe) else "n/a"
    r = f"{cand_r:.3f}" if _fin(cand_r) else "n/a"
    nr = f"{ne:.3f}" if _fin(ne) else "n/a"
    return bool(ok), (
        f"[V3 null baseline] daily Sharpe {cs} vs {bar}; need strictly greater "
        f"(info: expectancy {r} R vs null {nr} R)"
    )


def write_v3_report(res: ResearchResults, null: dict, inp: Inputs, settings: Settings, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    rep = build_v2_report(res, inp, settings, baseline_expectancy=null["null_expectancy_r"])
    e = rep.metrics["edge"]
    checks = dict(rep.gonogo.checks)
    checks["complexity_adds_value"] = null_check(
        e.get("sharpe_daily"), null, e.get("expectancy_r"), len(NULL_SEEDS)
    )
    if int(e.get("n_trades") or 0) < MIN_TRADES:
        verdict = "INSUFFICIENT DATA"
    elif all(ok for ok, _ in checks.values()):
        verdict = "GO"
    else:
        verdict = "NO-GO"
    rep.gonogo = GoNoGo(verdict, checks)
    rep.candidate = (
        NO_CANDIDATE
        if verdict != "GO"
        else "GO: the nested train-only selection (candidate per window below) passes every V3 check"
    )
    rep.caveats = V3_CAVEATS + list(rep.caveats)
    path = write_v2_report(rep, out_dir)
    path.write_text(
        path.read_text(encoding="utf-8").replace("Luna Research V2", "Luna Research V3"), encoding="utf-8"
    )

    sp = out_dir / "summary.json"
    s = json.loads(sp.read_text(encoding="utf-8"))
    s["v3"] = {
        "dataset_note": V3_CAVEATS[0],
        "families": sorted({getattr(c, "signal", c.family) for c in res.configs}),
        "criterion_8": CRITERION_8,
        "null_sharpe_daily": null["null_sharpe_daily"] if _fin(null["null_sharpe_daily"]) else None,
        "null_expectancy_r": null["null_expectancy_r"] if _fin(null["null_expectancy_r"]) else None,
        "null_seed_sharpe_daily": null.get("null_seed_sharpe_daily"),
        "null_seed_expectancy_r": null.get("null_seed_expectancy_r"),
        "null_seeds": list(NULL_SEEDS),
        "null_seeds_traded": null.get("null_seeds_traded"),
        "naive_momentum_expectancy_r": (s.get("naive_momentum") or {}).get("expectancy_r"),
        "cumulative_trials": {**PRIOR_TRIALS, "v3": count_trials(res.configs)},
    }
    sp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    return path
