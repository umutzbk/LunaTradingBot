"""Research V2 run orchestration (plan Task 10): fits, configuration walk-forwards, asset
leave-one-out re-runs, the account candidate's variants and the naive momentum bar, as resumable
units.

Each unit's result is pickled to ``<out_dir>/units/<unit>.pkl`` as soon as it finishes; ``resume``
reloads finished units instead of recomputing them. ``jobs > 1`` runs independent units in worker
processes, each holding one copy of the inputs (set once per worker by the pool initializer).

Nothing here reads data at or after ``V2_DATA_END`` (the caller passes inputs from
``luna.v2.data.load_v2_inputs``) or touches the final generalization set.
"""

from __future__ import annotations

import json
import logging
import pickle
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from luna.config import Settings
from luna.engine.backtest import BacktestResult
from luna.engine.walkforward import WalkForwardResult, Window, _git_dirty, _git_sha
from luna.report.walkforward import Inputs, run_naive
from luna.types import AccountKind
from luna.v2.configs import V2Config, primary_configs, sensitivity_configs
from luna.v2.data import V2_DATA_END, v2_settings, v2_windows
from luna.v2.walkforward import (
    ConfigWindowModel,
    build_v2_tradelists,
    count_trials,
    first_active,
    fit_config,
    refit_without,
    run_config_walkforward,
    run_selected_walkforward,
    select_candidate,
    v2_variants,
)

log = logging.getLogger("luna.v2.runner")

_CTX: dict[str, Any] = {}


@dataclass
class ResearchResults:
    account: AccountKind
    windows: list[Window]
    start_k: int
    span: tuple[pd.Timestamp, pd.Timestamp]
    configs: list[V2Config]
    fitted: dict[str, list[ConfigWindowModel]]
    config_runs: dict[str, dict[str, WalkForwardResult]]
    selected: list[ConfigWindowModel | None]
    candidate_runs: dict[str, WalkForwardResult]
    naive: dict[str, BacktestResult]
    naive_skipped: dict[str, str]
    trials: dict
    config_loo: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)  # closed trades only
    candidate_loo: dict[str, pd.DataFrame] = field(default_factory=dict)  # closed trades only
    loo_assets: dict[str, list[str]] = field(default_factory=dict)
    runtime: dict[str, float] = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)  # code, account, assets, data end, last bar read


# --- units (run in the parent or in workers; inputs come from _CTX) -------------------------------


def _init(inp: Inputs, tl: pd.DataFrame, settings: Settings, windows: list[Window]) -> None:
    _CTX.update(inp=inp, tl=tl, settings=settings, windows=windows)


def _u_fit(cfg: V2Config, exclude: tuple[str, ...]) -> list[ConfigWindowModel]:
    return fit_config(cfg, _CTX["windows"], _CTX["tl"], _CTX["inp"], _CTX["settings"], exclude)


def _u_run(fitted: list[ConfigWindowModel], start_k: int, variant: dict) -> WalkForwardResult:
    return run_config_walkforward(fitted, start_k, _CTX["inp"], _CTX["settings"], **variant)


def _u_selected(
    selected: list[ConfigWindowModel | None], start_k: int, kind: AccountKind, variant: dict
) -> WalkForwardResult:
    return run_selected_walkforward(
        selected, _CTX["windows"], start_k, _CTX["inp"], _CTX["settings"], kind, **variant
    )


def _u_refit(cfg: V2Config, original: list[ConfigWindowModel], asset: str) -> list[ConfigWindowModel]:
    return refit_without(cfg, original, asset, _CTX["tl"], _CTX["inp"], _CTX["settings"])


def _u_run_trades(fitted: list[ConfigWindowModel], start_k: int, variant: dict) -> pd.DataFrame:
    """Leave-one-out runs: robustness reads only the closed trades (keeps memory small)."""
    return _u_run(fitted, start_k, variant).oos.trades


def _u_selected_trades(
    selected: list[ConfigWindowModel | None], start_k: int, kind: AccountKind, variant: dict
) -> pd.DataFrame:
    return _u_selected(selected, start_k, kind, variant).oos.trades


def _safe(name: str) -> str:
    return name.replace("|", "_").replace(":", "__").replace("/", "-")


def _execute(
    units: list[tuple[str, Callable, tuple]],
    ckpt: Path,
    jobs: int,
    resume: bool,
    pool: ProcessPoolExecutor | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    todo = []
    for name, fn, args in units:
        path = ckpt / f"{_safe(name)}.pkl"
        if resume and path.exists():
            with open(path, "rb") as fh:
                out[name] = pickle.load(fh)
        else:
            todo.append((name, fn, args, path))

    def keep(name: str, path: Path, value: Any) -> None:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(value, fh)
        tmp.replace(path)
        out[name] = value
        log.info("unit done: %s", name)

    if pool is None:
        for name, fn, args, path in todo:
            keep(name, path, fn(*args))
        return out
    futs = {pool.submit(fn, *args): (name, path) for name, fn, args, path in todo}
    try:
        for fut in as_completed(futs):  # checkpoint each unit as soon as it finishes
            name, path = futs[fut]
            keep(name, path, fut.result())
    except BaseException:
        for f in futs:
            f.cancel()  # queued units are dropped; finished ones are already checkpointed
        raise
    return out


# --- manifest -------------------------------------------------------------------------------------


class ManifestMismatch(RuntimeError):
    """A resume would mix checkpoints of a different run (code, account, assets, windows, configs)."""


def run_manifest(
    account: AccountKind, inp: Inputs, windows: list[Window], configs: list[V2Config], loo: bool
) -> dict:
    """What a checkpoint directory belongs to; a resume must match it exactly."""
    last = max(df.index[-1] for df in inp.panel.values() if len(df))
    return {
        "git_sha": _git_sha(),
        "git_dirty": _git_dirty(),
        "account": account,
        "assets": sorted(inp.panel),
        "data_end": V2_DATA_END.isoformat(),
        "last_bar": last.isoformat(),
        "windows": [w.test_start.isoformat() for w in windows],
        "configs": [c.id for c in configs],
        "loo": loo,
    }


def check_manifest(out_dir: Path, manifest: dict, resume: bool) -> None:
    path = out_dir / "manifest.json"
    if resume:
        if not path.exists():
            raise ManifestMismatch(f"{out_dir}: no manifest.json, refusing to resume")
        old = json.loads(path.read_text(encoding="utf-8"))
        diff = sorted(k for k in set(old) | set(manifest) if old.get(k) != manifest.get(k))
        if diff:
            raise ManifestMismatch(f"{out_dir}: resume differs from the original run in {diff}")
        return
    if any((out_dir / "units").glob("*.pkl")):
        for f in (out_dir / "units").glob("*.pkl"):  # a fresh run never reuses old checkpoints
            f.unlink()
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")


# --- the run --------------------------------------------------------------------------------------


def run_research(
    settings: Settings,
    account: AccountKind,
    inp: Inputs,
    out_dir: Path,
    *,
    windows: list[Window] | None = None,
    configs: list[V2Config] | None = None,
    jobs: int = 1,
    resume: bool = False,
    loo: bool = True,
    tl_cache_dir: Path | None = None,
) -> ResearchResults:
    """Every Research V2 simulation of one account (module docstring). ``windows`` / ``configs``
    default to the V2 windows and the full configuration grid (primary + daily sensitivity)."""
    t_start = time.perf_counter()
    s2 = v2_settings(settings)
    windows = windows if windows is not None else v2_windows(settings)
    configs = configs if configs is not None else primary_configs(account) + sensitivity_configs(account)
    primary = [c for c in configs if c.rebalance == "weekly"]
    ckpt = Path(out_dir) / "units"
    ckpt.mkdir(parents=True, exist_ok=True)
    manifest = run_manifest(account, inp, windows, configs, loo)
    check_manifest(Path(out_dir), manifest, resume)
    runtime: dict[str, float] = {}

    t0 = time.perf_counter()
    tl = build_v2_tradelists(inp, s2, account, windows, cache_dir=tl_cache_dir)
    runtime["trade lists"] = time.perf_counter() - t0
    _init(inp, tl, s2, windows)
    pool = ProcessPoolExecutor(jobs, initializer=_init, initargs=(inp, tl, s2, windows)) if jobs > 1 else None
    try:
        t0 = time.perf_counter()
        fits = _execute([(f"fit:{c.id}", _u_fit, (c, ())) for c in configs], ckpt, jobs, resume, pool)
        fitted = {c.id: fits[f"fit:{c.id}"] for c in configs}
        runtime["fits"] = time.perf_counter() - t0
        primary_fitted = {c.id: fitted[c.id] for c in primary}
        start_k = first_active(primary_fitted)
        selected = select_candidate(primary_fitted)
        span = (windows[start_k].test_start, windows[-1].test_end)
        loo_assets = (
            {c.id: sorted({a for cw in fitted[c.id][start_k:] for a in cw.universe}) for c in primary}
            if loo
            else {}
        )

        t0 = time.perf_counter()
        units: list[tuple[str, Callable, tuple]] = []
        for c in configs:
            for vname, kw in v2_variants(account, candidate=False).items():
                units.append((f"run:{c.id}:{vname}", _u_run, (fitted[c.id], start_k, kw)))
        for vname, kw in v2_variants(account, candidate=True).items():
            units.append((f"cand:{vname}", _u_selected, (selected, start_k, account, kw)))
        for cid, assets in loo_assets.items():
            cfg = next(c for c in primary if c.id == cid)
            units += [(f"loofit:{cid}:{a}", _u_refit, (cfg, fitted[cid], a)) for a in assets]
        done = _execute(units, ckpt, jobs, resume, pool)
        runtime["walk-forwards"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        edge = v2_variants(account, candidate=False)["edge"]
        loo_units: list[tuple[str, Callable, tuple]] = []
        every_asset = sorted({a for v in loo_assets.values() for a in v})
        for cid, assets in loo_assets.items():
            loo_units += [
                (f"loo:{cid}:{a}", _u_run_trades, (done[f"loofit:{cid}:{a}"], start_k, edge)) for a in assets
            ]
        for a in every_asset:
            refit = {
                cid: (done[f"loofit:{cid}:{a}"] if a in loo_assets[cid] else primary_fitted[cid])
                for cid in primary_fitted
            }
            loo_units.append(
                (f"candloo:{a}", _u_selected_trades, (select_candidate(refit), start_k, account, edge))
            )
        loo_done = _execute(loo_units, ckpt, jobs, resume, pool)
        runtime["leave-one-out"] = time.perf_counter() - t0
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)

    t0 = time.perf_counter()
    naive, naive_skipped = run_naive(inp, s2, account, *span)
    runtime["naive"] = time.perf_counter() - t0
    runtime["total"] = time.perf_counter() - t_start

    trials = count_trials(configs)
    trials["strategy_params_key_combinations"] = int(
        len(tl[["strategy", "params_id", "key"]].drop_duplicates()) if len(tl) else 0
    )
    res = ResearchResults(
        account=account,
        windows=windows,
        start_k=start_k,
        span=span,
        configs=configs,
        fitted=fitted,
        config_runs={
            c.id: {v: done[f"run:{c.id}:{v}"] for v in v2_variants(account, candidate=False)} for c in configs
        },
        selected=selected,
        candidate_runs={v: done[f"cand:{v}"] for v in v2_variants(account, candidate=True)},
        naive=naive,
        naive_skipped=naive_skipped,
        trials=trials,
        config_loo={
            cid: {a: loo_done[f"loo:{cid}:{a}"] for a in assets} for cid, assets in loo_assets.items()
        },
        candidate_loo={
            a: loo_done[f"candloo:{a}"] for a in sorted({a for v in loo_assets.values() for a in v})
        },
        loo_assets=loo_assets,
        runtime=runtime,
        manifest=manifest,
    )
    with open(Path(out_dir) / "results.pkl", "wb") as fh:
        pickle.dump(res, fh)
    return res
