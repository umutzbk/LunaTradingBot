"""Research V3 runner (spec §4, §5): the V2 research run (nested train-only selection, variants,
robustness, leave-one-out; resumable checkpoints) on the V3 configurations, then the family null
baselines on the selected plan:

- premium / lowvol windows: the same fitted parameters and universe with a shuffled score restricted
  to the assets the real signal can rank (``shuffle<seed>:<signal>``, seeds 1..5);
- vol-managed windows: the same plan without vol targeting (equal-weight buy and hold);
- flat windows stay flat.

Criterion 8 compares daily Sharpe (scale-free: vol targeting shrinks R by construction); the null
Sharpe / expectancy are means over the seeds that traded (their number is reported). A plan with no
shuffled window is the same for every seed and runs once."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import numpy as np

from luna.config import Settings
from luna.engine.walkforward import Window
from luna.report.metrics import metrics_with_ci
from luna.report.walkforward import Inputs
from luna.types import AccountKind
from luna.v2.data import v2_settings, v2_windows
from luna.v2.runner import ResearchResults, run_research
from luna.v2.walkforward import ConfigWindowModel, run_selected_walkforward, v2_variants
from luna.v3.configs import V3Config, v3_configs

NULL_SEEDS = (1, 2, 3, 4, 5)


def null_model(cw: ConfigWindowModel | None, seed: int) -> ConfigWindowModel | None:
    if cw is None or cw.xsec is None:
        return cw
    params, universe = cw.xsec
    if params.signal == "momentum":  # vol-managed book: same plan, no vol targeting
        null = dataclasses.replace(params, vol_target=None)
    else:
        null = dataclasses.replace(params, signal=f"shuffle{seed}:{params.signal}")
    return dataclasses.replace(cw, params_id=null.params_id, xsec=(null, universe))


def run_v3(
    settings: Settings,
    account: AccountKind,
    inp: Inputs,
    out_dir: Path,
    *,
    windows: list[Window] | None = None,
    configs: list[V3Config] | None = None,
    jobs: int = 1,
    resume: bool = False,
    loo: bool = True,
) -> tuple[ResearchResults, dict]:
    windows = windows if windows is not None else v2_windows(settings)
    configs = configs if configs is not None else v3_configs(account)
    res = run_research(
        settings,
        account,
        inp,
        Path(out_dir),
        windows=windows,
        configs=configs,
        jobs=jobs,
        resume=resume,
        loo=loo,
    )
    s2 = v2_settings(settings)
    edge = v2_variants(account, candidate=False)["edge"]
    shuffled = any(
        cw is not None and cw.xsec is not None and cw.xsec[0].signal != "momentum" for cw in res.selected
    )
    runs = {}
    for seed in NULL_SEEDS:
        if not shuffled and runs:
            runs[seed] = runs[NULL_SEEDS[0]]
            continue
        plan = [null_model(cw, seed) for cw in res.selected]
        runs[seed] = run_selected_walkforward(plan, res.windows, res.start_k, inp, s2, account, **edge)
    mets = [metrics_with_ci(r.oos) for r in runs.values()]
    exps = [_finite(m.get("expectancy_r")) for m in mets]
    sharpes = [_finite(m.get("sharpe_daily")) for m in mets]
    return res, {
        "null_runs": runs,
        "null_expectancy_r": _mean(exps),
        "null_seed_expectancy_r": exps,
        "null_sharpe_daily": _mean(sharpes),
        "null_seed_sharpe_daily": sharpes,
        "null_seeds_traded": sum(1 for m in mets if m.get("n_trades")),
    }


def _finite(x) -> float | None:
    return float(x) if x is not None and math.isfinite(x) else None


def _mean(xs: list[float | None]) -> float:
    vals = [x for x in xs if x is not None]
    return float(np.mean(vals)) if vals else math.nan
