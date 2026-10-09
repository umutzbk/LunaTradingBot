"""`luna research-v4` runner (spec R9, §4–§9).

Nothing is computed unless the git tag ``research-v4-spec-frozen`` exists, is an ancestor of HEAD and
the work tree is clean (R9: no return / P&L before the freeze). Stage 2 additionally refuses before
the first look (D0 + 182 days). The guard runs before any data is loaded.

A stage run simulates the 7 configs under every cost row (PRIMARY, S-SPREAD10 [historical only],
S-COST2X, S-MAKER), their nulls (PRIMARY), evaluates M1–M4 / C1–C7 and writes ``manifest.json``,
``summary.json``, ``report.md`` and the daily decomposition CSVs.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from luna.v4 import diagnostics as dg
from luna.v4 import stats
from luna.v4.accounting import LegMeta, SimResult, run_carry, run_h2
from luna.v4.constants import (
    COLLECTOR_START_TS,
    D0,
    DAY,
    HOUR,
    LOOKS,
    STAGE1_END,
    STAGE1_EVAL_START,
    V4_EVIDENCE_START_TS,
    V4_UNIVERSE,
)
from luna.v4.costs import CostRow, cost_rows
from luna.v4.data import EXEC_WINDOW
from luna.v4.signals import venue_basis
from luna.v4.strategies import (
    CONFIGS,
    NULL_SEEDS,
    AlwaysRule,
    CarryRule,
    H2Rule,
    ScheduleRule,
    randentry_schedule,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FREEZE_TAG = "research-v4-spec-frozen"
SPEC_PATH = Path("docs") / "superpowers" / "specs" / "luna-research-v4-design.md"
INSTRUMENTS_SNAPSHOT = Path("config") / "v4_instruments.json"
STAGE1_CAVEAT = "EXPLORATORY — reused development period; not generalization evidence."

# §7.2 proposed DQ thresholds; the freeze commit fixes the exact numbers (recorded in the manifest)
DQ_THRESHOLDS = {
    "skew_valid_share": 0.95,
    "clock_skew_median_ms": 2000.0,
    "ticker_coverage": 0.95,
    "max_gap_s": 900.0,
    "funding_hours": 24,
}
STAGE1_COVERAGE = 0.90


class RefuseRun(RuntimeError):
    """The research run is not allowed (freeze guard or Stage 2 timing)."""


# --- guards ----------------------------------------------------------------------------------------


def _git_rc(repo: Path, *args: str) -> tuple[int | None, str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return out.returncode, out.stdout


def check_freeze(repo: Path | None = None) -> dict[str, str]:
    repo = Path(repo if repo is not None else REPO_ROOT)
    rc, head = _git_rc(repo, "rev-parse", "HEAD") if repo.exists() else (None, "")
    if rc != 0:
        raise RefuseRun(f"git unavailable or {repo} is not a repository: cannot verify {FREEZE_TAG}")
    rc, tag = _git_rc(repo, "rev-parse", "-q", "--verify", f"refs/tags/{FREEZE_TAG}^{{commit}}")
    if rc != 0 or not tag.strip():
        raise RefuseRun(f"git tag {FREEZE_TAG} does not exist: the V4 spec is not frozen (R9)")
    rc, _ = _git_rc(repo, "merge-base", "--is-ancestor", tag.strip(), "HEAD")
    if rc != 0:
        raise RefuseRun(f"{FREEZE_TAG} is not an ancestor of HEAD")
    rc, status = _git_rc(repo, "status", "--porcelain")
    if rc != 0 or status.strip():
        raise RefuseRun("the work tree is dirty (uncommitted changes): commit or stash first")
    spec = SPEC_PATH.as_posix()
    rc_head, _ = _git_rc(repo, "cat-file", "-e", f"HEAD:{spec}")
    rc_tag, _ = _git_rc(repo, "cat-file", "-e", f"{tag.strip()}:{spec}")
    if rc_head != 0 or rc_tag != 0:
        raise RefuseRun(f"the frozen spec {spec} is missing at HEAD or at {FREEZE_TAG}")
    rc, diff = _git_rc(repo, "diff", tag.strip(), "--", spec)
    if rc != 0 or diff.strip():
        raise RefuseRun(f"the spec {spec} differs from its frozen version at {FREEZE_TAG}")
    return {"tag_sha": tag.strip(), "head_sha": head.strip()}


def check_stage2_time(now: pd.Timestamp) -> None:
    if now < LOOKS[0]:
        raise RefuseRun(
            f"Stage 2 refuses before the first look {LOOKS[0]:%Y-%m-%d} (D0 + 182 days); "
            "no evidence simulation runs before a look date (§7.4)"
        )


def current_look(now: pd.Timestamp) -> tuple[int, pd.Timestamp]:
    done = [(i + 1, t) for i, t in enumerate(LOOKS) if t <= now]
    if not done:
        raise RefuseRun("no look date has been reached")
    return done[-1]


# --- inputs ----------------------------------------------------------------------------------------


@dataclass
class StageInputs:
    stage: int
    market: Any
    table: pd.DataFrame
    funding: dict[str, pd.DataFrame]
    days: list[pd.Timestamp]
    d0: pd.Timestamp
    dq_valid: dict[pd.Timestamp, bool]
    meta: dict
    window: dict[str, str]
    basis: dict[str, pd.Series] | None = None
    look: int | None = None
    instruments_sha256: str | None = None
    notes: list[str] = field(default_factory=list)


def stage1_day_validity(hist, days: list[pd.Timestamp]) -> dict[pd.Timestamp, bool]:
    """Stage 1 M2 (§7.3): a day is valid when funding coverage and bar coverage are each >= 90%
    over the assets listed by then (funding: hourly rows; bars: real perp and spot 15m bars)."""
    out = {}
    for d in days:
        f_have = f_need = b_have = b_need = 0
        for a in hist.assets:
            perp, spot = hist.perp.get(a), hist.spot.get(a)
            for df in (perp, spot):
                if df is None or df.empty or df.index.min() > d:
                    continue
                w = df["filled"].iloc[df.index.searchsorted(d) : df.index.searchsorted(d + DAY)]
                b_have += int((~w.astype(bool)).sum())
                b_need += 96
            f = hist.funding.get(a)
            if perp is not None and len(perp) and perp.index.min() <= d:
                f_need += 24
                if f is not None and len(f):
                    f_have += int(f.index.searchsorted(d + DAY) - f.index.searchsorted(d))
        out[d] = bool(
            f_need and b_need and f_have >= STAGE1_COVERAGE * f_need and b_have >= STAGE1_COVERAGE * b_need
        )
    return out


def stage1_inputs(
    hist,
    meta: dict,
    eval_start: pd.Timestamp = STAGE1_EVAL_START,
    end: pd.Timestamp = STAGE1_END,
    instruments_sha256: str | None = None,
) -> StageInputs:
    from luna.v4.market import HistoricalMarket
    from luna.v4.signals import hist_signal_table

    days = list(pd.date_range(eval_start, end - DAY, freq="D"))
    return StageInputs(
        stage=1,
        market=HistoricalMarket(hist),
        table=hist_signal_table(hist, days),
        funding=hist.funding,
        days=days,
        d0=days[0],
        dq_valid=stage1_day_validity(hist, days),
        meta=meta,
        window={"eval_start": eval_start.isoformat(), "end": end.isoformat()},
        basis={a: venue_basis(hist.perp.get(a), hist.spot.get(a)) for a in hist.assets},
        instruments_sha256=instruments_sha256,
        notes=[
            "Instrument metadata (min order, step) is the freeze-time snapshot applied to the past: a "
            "known anachronism (§7.1)."
        ],
    )


def stage2_day_validity(fd, funding: dict[str, pd.DataFrame], days, assets) -> dict[pd.Timestamp, bool]:
    from luna.v4.data import funding_hours_by_day

    hours = funding_hours_by_day(funding)
    q = fd.quality
    out = {}
    for d in days:
        if not len(q) or d not in q.index:
            out[d] = False
            continue
        r = q.loc[d]
        ok = (
            r["pairs"] > 0
            and r["pairs_valid"] / r["pairs"] >= DQ_THRESHOLDS["skew_valid_share"]
            and r["clock_skew_median_ms"] is not None
            and abs(r["clock_skew_median_ms"]) <= DQ_THRESHOLDS["clock_skew_median_ms"]
            and r["fut_ok"] / 1440 >= DQ_THRESHOLDS["ticker_coverage"]
            and r["max_gap_s"] is not None
            and r["max_gap_s"] <= DQ_THRESHOLDS["max_gap_s"]
        )
        for a in assets:
            h = hours["hours"].get((d, a), 0) if len(hours) else 0
            ok = ok and h >= DQ_THRESHOLDS["funding_hours"]
        out[d] = bool(ok)
    return out


def stage2_inputs(
    root: Path, look_ts: pd.Timestamp, look: int, meta: dict, instruments_sha256: str | None = None
) -> StageInputs:
    from functools import partial

    from luna.v4.data import collector_start, load_forward_daily, load_forward_funding
    from luna.v4.market import ForwardMarket, nearest_book
    from luna.v4.signals import forward_signal_table

    start = collector_start(root)
    if start != COLLECTOR_START_TS:
        raise RefuseRun(f"collector_start_ts {start} differs from the frozen {COLLECTOR_START_TS}")
    # nothing after the look is read, except the valuation snapshot of the last evidence day (first
    # 15 min after the look) and the settlement of the last hour's funding (fetched at :05 after it)
    fd = load_forward_daily(root, V4_UNIVERSE, since=start, until=look_ts + EXEC_WINDOW)
    funding = load_forward_funding(root, V4_UNIVERSE, until=look_ts + HOUR, periods_before=look_ts)
    days = list(pd.date_range(D0, look_ts - DAY, freq="D"))
    return StageInputs(
        stage=2,
        market=ForwardMarket(fd, funding, book_loader=partial(nearest_book, root)),
        table=forward_signal_table(fd, funding, days),
        funding=funding,
        days=days,
        d0=D0,
        dq_valid=stage2_day_validity(fd, funding, days, V4_UNIVERSE),
        meta=meta,
        window={
            "d0": D0.isoformat(),
            "look": look_ts.isoformat(),
            "evidence_start": V4_EVIDENCE_START_TS.isoformat(),
        },
        look=look,
        instruments_sha256=instruments_sha256,
    )


def load_instrument_meta(repo: Path) -> tuple[dict, str]:
    path = Path(repo) / INSTRUMENTS_SNAPSHOT
    if not path.exists():
        raise RefuseRun(
            f"instrument metadata snapshot {INSTRUMENTS_SNAPSHOT} is missing (freeze commit, §7.1)"
        )
    raw = path.read_bytes()
    data = json.loads(raw)
    meta = {
        a: {leg: LegMeta(**v) for leg, v in legs.items()} for a, legs in (data.get("assets") or {}).items()
    }
    return meta, hashlib.sha256(raw).hexdigest()


def load_stage_inputs(stage: int, var_dir: Path, now: pd.Timestamp) -> StageInputs:
    """Called only after the freeze guard passed."""
    meta, sha = load_instrument_meta(REPO_ROOT)
    if stage == 1:
        from luna.v4.data import load_historical

        return stage1_inputs(load_historical(var_dir), meta, instruments_sha256=sha)
    look, look_ts = current_look(now)
    return stage2_inputs(Path(var_dir) / "forward", look_ts, look, meta, instruments_sha256=sha)


# --- simulation ------------------------------------------------------------------------------------


def _simulate(cid: str, row: CostRow, inp: StageInputs) -> SimResult:
    cfg = CONFIGS[cid]
    if cfg.account == "carry":
        return run_carry(inp.market, CarryRule(cfg, inp.table), inp.days, row, inp.meta)
    return run_h2(inp.market, H2Rule(cfg, inp.table), inp.days, row, inp.meta)


def _nulls(cid: str, primary: SimResult, row: CostRow, inp: StageInputs) -> tuple[dict[str, SimResult], dict]:
    cfg = CONFIGS[cid]
    out, info = {}, {}
    if cfg.account == "h2":
        for s in NULL_SEEDS:
            out[f"N-SHUFFLE:{s}:{cid}"] = run_h2(
                inp.market, H2Rule(cfg, inp.table, shuffle_seed=s), inp.days, row, inp.meta
            )
        return out, info
    for s in NULL_SEEDS:
        sched, dropped = randentry_schedule(primary.trades, inp.table, inp.days, cid, s)
        nid = f"N-RANDENTRY:{s}:{cid}"
        out[nid] = run_carry(inp.market, ScheduleRule(sched), inp.days, row, inp.meta)
        info[nid] = {"scheduled": len(sched), "dropped": dropped}
    out[f"N-ALWAYS:{cid}"] = run_carry(inp.market, AlwaysRule(inp.table, inp.d0), inp.days, row, inp.meta)
    return out, info


def _sums(daily: pd.DataFrame) -> dict[str, float]:
    cols = [
        "spot_price_pnl",
        "perp_price_pnl",
        "funding_cf",
        "fees",
        "spread_slippage",
        "liquidation_penalty",
    ]
    return {c: float(daily[c].sum()) for c in cols}


UNKNOWN_FUNDING_FLAG = 0.01  # §2: flag when > 1% of held funding hours are unknown


def unknown_funding_flag(counters: dict) -> tuple[float, bool]:
    held = float(counters.get("funding_held_hours", 0.0))
    share = float(counters.get("funding_unknown_hours", 0.0)) / held if held > 0 else 0.0
    return share, share > UNKNOWN_FUNDING_FLAG


def _row_summary(res: SimResult) -> dict:
    r = res.daily["ret"]
    share, flag = unknown_funding_flag(res.counters)
    return {
        "funding_unknown_share": share,
        "funding_unknown_flag": flag,
        "mean_daily": float(r.mean()),
        "sharpe": stats.sharpe(r),
        "max_drawdown": stats.max_drawdown(r),
        "final_equity": float(res.daily["equity"].iloc[-1]),
        "pnl": _sums(res.daily),
        "counters": dict(res.counters),
        "entries": len(res.trades),
    }


def _btc_beta(ret: pd.Series, inp: StageInputs) -> float:
    if "BTC" not in inp.table.index.get_level_values("asset"):
        return math.nan
    px = pd.Series({d: inp.market.value("BTC", "perp", d) for d in inp.days}, dtype="float64")
    prev = px.shift(1)
    btc = px / prev - 1
    j = btc.dropna().index.intersection(ret.index)
    if len(j) < 3 or btc[j].var() == 0:
        return math.nan
    return float(np.cov(ret[j], btc[j])[0, 1] / btc[j].var())


def run_stage(
    inputs: StageInputs,
    out_dir: Path,
    *,
    B: int = stats.B,
    prior_go: tuple[str, ...] = (),  # noqa: N803
    killed: tuple[str, ...] = (),
    rows: dict[str, CostRow] | None = None,
    now: pd.Timestamp | None = None,
    repo: Path | None = None,
    _unsafe_skip_freeze_check_for_tests: bool = False,
) -> dict:
    if _unsafe_skip_freeze_check_for_tests:
        freeze = {"tag_sha": None, "head_sha": _git_rc(REPO_ROOT, "rev-parse", "HEAD")[1].strip() or None}
        freeze_check = "BYPASSED (test only)"
    else:
        freeze = check_freeze(repo if repo is not None else REPO_ROOT)
        if inputs.stage == 2:
            check_stage2_time(now if now is not None else pd.Timestamp.now(tz="UTC"))
        freeze_check = "PASSED"
    inp = inputs
    out_dir = Path(out_dir)
    (out_dir / "daily" / "nulls").mkdir(parents=True, exist_ok=True)
    rows = rows or cost_rows()
    rows = {k: r for k, r in rows.items() if not (inp.stage == 2 and r.historical_only)}

    sims: dict[str, dict[str, SimResult]] = {}
    nulls: dict[str, dict[str, SimResult]] = {}
    null_info: dict[str, dict] = {}
    unknown = [c for c in (*killed, *prior_go) if c not in CONFIGS]
    if unknown:
        raise ValueError(f"unknown config ids {unknown}")
    if inp.stage == 1 and (killed or prior_go):
        raise ValueError("Stage 1 has no killed / prior-GO configs")
    # Stage 2 simulates only configs ADVANCEd by Stage 1 and not yet GO (§7.3, §7.4)
    run_ids = [c for c in CONFIGS if c not in set(killed) and c not in set(prior_go)]
    for cid in run_ids:
        sims[cid] = {name: _simulate(cid, r, inp) for name, r in rows.items()}
        nulls[cid], null_info[cid] = _nulls(cid, sims[cid]["PRIMARY"], rows["PRIMARY"], inp)
        for name, res in sims[cid].items():
            res.daily.to_csv(out_dir / "daily" / f"{cid}__{name}.csv")
        for nid, res in nulls[cid].items():
            res.daily.to_csv(out_dir / "daily" / "nulls" / f"{nid.replace(':', '_')}.csv")

    window_days = len(inp.days)
    dq_days = sum(1 for d in inp.days if inp.dq_valid.get(d))
    evidences = {}
    for cid in run_ids:
        cfg = CONFIGS[cid]
        p = sims[cid]["PRIMARY"]
        d = p.daily
        m = stats.evidence_checks(
            cfg.account,
            window_days,
            dq_days,
            int(d["active"].sum()),
            [t["asset"] for t in p.trades],
            int(p.counters["decisions"]),
        )
        n = nulls[cid]
        if cfg.account == "carry":
            null_sh = [stats.sharpe(n[f"N-RANDENTRY:{s}:{cid}"].daily["ret"]) for s in NULL_SEEDS]
            always = stats.sharpe(n[f"N-ALWAYS:{cid}"].daily["ret"])
            carry_value = float(d["funding_cf"].sum() - d["fees"].sum() - d["spread_slippage"].sum())
            carry_net = None
        else:
            null_sh = [stats.sharpe(n[f"N-SHUFFLE:{s}:{cid}"].daily["ret"]) for s in NULL_SEEDS]
            always, carry_value, carry_net = None, None, d["carry_net"]
        evidences[cid] = stats.ConfigEvidence(
            config_id=cid,
            kind=cfg.account,
            primary=d["ret"],
            cost2x=sims[cid]["S-COST2X"].daily["ret"],
            carry_value=carry_value,
            carry_net=carry_net,
            null_sharpes=null_sh,
            always_sharpe=always,
            had_entries=bool(p.trades) if cfg.account == "carry" else True,
            m=m,
            active_days=int(d["active"].sum()),
        )
    results = stats.evaluate(evidences, inp.stage, B=B, prior_go=prior_go, killed=killed)

    diagnostics = _diagnostics(inp, sims)
    configs_out = {}
    for cid in results:
        res = results[cid]
        configs_out[cid] = {
            **res,
            "research_edge": res["verdict"],
            "rows": {name: _row_summary(r) for name, r in sims[cid].items()},
        }
    summary = {
        "stage": inp.stage,
        "label": STAGE1_CAVEAT if inp.stage == 1 else "FORWARD EVIDENCE",
        "look": inp.look,
        "window": inp.window,
        "window_days": window_days,
        "dq_valid_days": dq_days,
        "configs": configs_out,
        "prior_go": list(prior_go),
        "killed": list(killed),
        "hypotheses": stats.hypothesis_summary(results) if inp.stage == 1 else None,
        "stage1_candidate": stats.stage1_candidate(results) if inp.stage == 1 else None,
        "nulls": {cid: {nid: _row_summary(r) for nid, r in nulls[cid].items()} for cid in run_ids},
        "null_draws": null_info,
        "diagnostics": diagnostics,
        "market_counters": dict(getattr(inp.market, "counters", {}) or {}),
        "deployable_now": stats.deployable_now(),
        "notes": inp.notes,
    }
    manifest = {
        "stage": inp.stage,
        "freeze_check": freeze_check,
        "freeze_tag": FREEZE_TAG,
        "tag_sha": freeze.get("tag_sha"),
        "git_sha": freeze.get("head_sha"),
        "configs": list(CONFIGS),
        "null_seeds": list(NULL_SEEDS),
        "bootstrap": {
            "B": B,
            "seed": stats.SEED,
            "blocks": list(stats.BLOCKS),
            "alpha": stats.ALPHA_STAGE1 if inp.stage == 1 else stats.ALPHA_FORWARD,
        },
        "cost_rows": {k: vars(r) for k, r in rows.items()},
        "universe": list(V4_UNIVERSE),
        "window": inp.window,
        "dq_thresholds": DQ_THRESHOLDS if inp.stage == 2 else {"stage1_coverage": STAGE1_COVERAGE},
        "instruments_sha256": inp.instruments_sha256,
        "prior_go": list(prior_go),
        "killed": list(killed),
        "run_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    write_json(out_dir / "manifest.json", manifest)
    write_json(out_dir / "summary.json", summary)
    from luna.v4.report import render_report

    (out_dir / "report.md").write_text(render_report(_jsonable(summary)), encoding="utf-8")
    return _jsonable(summary)


def _diagnostics(inp: StageInputs, sims: dict[str, dict[str, SimResult]]) -> dict:
    weeks = [d for d in inp.days if d.weekday() == 0 and d + pd.Timedelta(days=7) <= inp.days[-1] + DAY]
    weeks = weeks[::1]
    out = {
        "note": "Diagnostics only: not configs, not trials, never part of GO / NO-GO or config selection.",
        "D-PERSIST": dg.d_persist(inp.funding, weeks),
        "D-H5CELLS": dg.d_h5cells(inp.table, inp.funding),
        "D-DISP": {
            cid: dg.d_disp(sims[cid]["PRIMARY"].daily["ret"], inp.funding)
            for cid, c in CONFIGS.items()
            if cid in sims
            if c.account == "h2"
        },
        "H2": {
            cid: {
                "residual_btc_beta": _btc_beta(sims[cid]["PRIMARY"].daily["ret"], inp),
                "mean_net_exposure": float(sims[cid]["PRIMARY"].daily["net_exposure"].mean()),
                "long_pnl": float(sims[cid]["PRIMARY"].daily["long_pnl"].sum()),
                "short_pnl": float(sims[cid]["PRIMARY"].daily["short_pnl"].sum()),
                "mean_max_share": float(sims[cid]["PRIMARY"].daily["max_share"].mean()),
                "mean_herfindahl": float(sims[cid]["PRIMARY"].daily["herfindahl"].mean()),
                "asset_pnl": sims[cid]["PRIMARY"].asset_pnl,
            }
            for cid, c in CONFIGS.items()
            if cid in sims
            if c.account == "h2"
        },
        "H5_vs_H1-Y10_sharpe": {
            c: stats.sharpe(sims[c]["PRIMARY"].daily["ret"]) for c in ("H5-HFWB", "H1-Y10") if c in sims
        },
    }
    if inp.basis is not None:
        lo = inp.days[0] - pd.Timedelta(days=30)
        out["venue_basis"] = dg.basis_stats({a: b[(b.index >= lo)] for a, b in inp.basis.items()})
    return out


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (pd.Timestamp,)):
        return x.isoformat()
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(float(x)) else float(x)
    return x


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(_jsonable(obj), indent=1), encoding="utf-8")
