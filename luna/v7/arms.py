"""Arms and scores (spec S-7.2, S-0.6, S-9 FL30, DEC-8, DEC-20).

A0 rule score ``s_rule = c * h* - 1.5 * 2 * cost - 5 bps`` with ``c`` the frozen V5 ``carry_per_hour`` on
FL30 (FL30 is a mean r8, i.e. per 8 h: ``c = carry_per_hour(side, FL30 / 8)``, AMB-13), ``h*`` in hours,
``cost`` = taker fee + h-hat(t) + slip per side. Hurdle ``s_rule > 0``. A1 = A0's entries minus those with
calibrated P-hat < 0.50, ranked in A0 order. A2 / A3 rank by raw ``yhat(h*)`` with hurdle ``yhat_cal > 0``.
ML only orders / removes (S-0.6): the scorers take already-gated candidates and refuse anything else.
Reference arms (A0-V5, TS EWMA / AR(1)) are reference only (no test).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.carry.config import CarryConfig, load_carry_config
from luna.carry.funding import carry_per_hour
from luna.carry.signals import AssetSignal
from luna.carry.strategy import Decision, DecisionInput, HeldLeg, decide
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance
from luna.v7.timeutil import D, H, ns_array
from luna.v7.universe import pit_funding

R8_H = 8.0  # hours per r8 unit
EWMA_HALF_LIFE_H = 72
AR1_MIN_OBS = 10

_FAMILY = {"A2": ("en", "logit_en"), "A3": ("lgb_reg", "lgb_clf")}  # ML* -> (regressor, classifier)

# Frozen V5 strategy knobs read by ``decide`` (spec S-7.2 A0-V5 row), pinned by sha256.
V5_FROZEN_KNOBS = {
    "k": 2,
    "gross": 1.0,
    "decision_mode": "cost_aware",
    "switch_horizon_hours": 168.0,
    "min_carry_improvement_bps": 5.0,
    "switch_cost_multiplier": 1.5,
    "safety_buffer_bps": 5.0,
    "min_hold_hours": 24.0,
    "hold_rank_buffer": 0,
    "max_actions_per_cycle": 4,
    "max_net_frac": 0.10,
}
V5_FROZEN_KNOBS_SHA256 = "4a83eaeb4b3e47c4cb2950ea628eec43bb230e824ff83ba3fbd1143961bccd00"


def knobs_sha256(knobs: dict) -> str:
    return hashlib.sha256(json.dumps(knobs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finite(x: float, name: str) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {x!r}")
    return v


def a0_score(fl30_r8: float, side: int, h_star: float, cost_side: float) -> float:
    """``c * h* - mult * sides * cost - buffer`` (S-7.2). NaN inputs give NaN (never a pass)."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side}")
    h = _finite(h_star, "h_star")
    if h <= 0:
        raise ValueError("h_star must be > 0")
    if not (math.isfinite(fl30_r8) and math.isfinite(cost_side)):
        return math.nan
    c = carry_per_hour(side, fl30_r8 / R8_H)
    a = FROZEN.a0
    return c * h - a.mult * a.sides * cost_side - a.buffer


def a0_passes(score: float) -> bool:
    return bool(math.isfinite(score) and score > 0)


def cost_side(half_spread_hat: float) -> float:
    """V5 per-side cost = taker fee + h-hat(t) + slip (S-7.2)."""
    return FROZEN.fees.taker + _finite(half_spread_hat, "half_spread_hat") + FROZEN.costs.slip


def a1_keep(p_cal: float) -> bool:
    """A1 veto: drop if calibrated P-hat < 0.50 (DEC-8). Missing / NaN fails closed (dropped)."""
    return bool(math.isfinite(p_cal) and p_cal >= FROZEN.a1_veto)


def ml_score(yhat: float, yhat_cal: float) -> tuple[float, bool]:
    """(rank_key, passes): rank by raw yhat, hurdle yhat_cal > 0 (S-7.2). With b = 0 the calibrated value is
    the constant ``a`` (entries iff a > 0) and the ranking still uses yhat."""
    ok = math.isfinite(yhat) and math.isfinite(yhat_cal) and yhat_cal > 0
    return (float(yhat) if math.isfinite(yhat) else -math.inf, bool(ok))


def _sorted(df: pd.DataFrame, key: np.ndarray, passes: np.ndarray) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "symbol": df["symbol"].to_numpy(),
            "side": df["side"].to_numpy(),
            "rank_key": key,
            "passes_hurdle": passes,
        }
    )
    out["_k"] = np.where(np.isfinite(out["rank_key"]), out["rank_key"], -np.inf)
    out = out.sort_values(["_k", "symbol", "side"], ascending=[False, True, True], kind="mergesort")
    return out.drop(columns="_k").reset_index(drop=True)


def arm_scores(arm: str, cands: pd.DataFrame, h_star: float, ml_star: str | None = None) -> pd.DataFrame:
    """(rank_key, passes_hurdle) per candidate side, deterministic, ties by (symbol, side), best first.

    ``cands`` holds ALREADY-GATED candidates only (column ``gate_ok`` must be all True: ML cannot add
    gate-failing candidates, S-0.6) with columns symbol, side, fl30_r8, cost_side and, for ML arms, the
    ``FoldModel.predict`` columns of the family (A1: ``p_<clf>_cal``; A2/A3: ``yhat_<fam>``,
    ``yhat_cal_<fam>``). ``ml_star`` in {"A2","A3"} picks the classifier family for A1.
    """
    if arm not in ("A0", "A1", "A2", "A3"):
        raise ValueError(f"unknown arm {arm!r}")
    for col in ("symbol", "side", "gate_ok"):
        if col not in cands:
            raise ValueError(f"cands lacks column {col!r}")
    if not cands["gate_ok"].astype(bool).all():
        raise ValueError("arm_scores takes already-gated candidates only (S-0.6)")
    if cands.duplicated(["symbol", "side"]).any():
        raise ValueError("duplicate (symbol, side) candidates")
    if arm in ("A2", "A3"):
        reg = _FAMILY[arm][0]
        yh = cands[f"yhat_{reg}"].to_numpy(np.float64)
        yc = cands[f"yhat_cal_{reg}"].to_numpy(np.float64)
        pairs = [ml_score(a, b) for a, b in zip(yh, yc, strict=True)]
        return _sorted(
            cands, np.array([p[0] for p in pairs], np.float64), np.array([p[1] for p in pairs], bool)
        )
    s = np.array(
        [
            a0_score(f, int(sd), h_star, c)
            for f, sd, c in zip(cands["fl30_r8"], cands["side"], cands["cost_side"], strict=True)
        ],
        np.float64,
    )
    ok = np.array([a0_passes(v) for v in s], bool)
    if arm == "A1":
        if ml_star not in _FAMILY:
            raise ValueError("A1 needs ml_star in {'A2','A3'} (classifier family of ML*)")
        p = cands[f"p_{_FAMILY[ml_star][1]}_cal"].to_numpy(np.float64)
        ok = ok & np.array([a1_keep(v) for v in p], bool)
    return _sorted(cands, s, ok)


# --- TS references (DEC-20) -------------------------------------------------------------------------


def _valid_r8(f: pd.DataFrame, cur: Instance, t_ns: int) -> tuple[np.ndarray, np.ndarray]:
    """(funding_ts, r8) of the PIT annotated funding records of the current instance (known_at <= t - 1 min,
    ``universe.pit_funding``) in order, with NaN r8 where the record is invalid (S-2.6)."""
    a = pit_funding(f, cur, int(t_ns))
    ts = ns_array(a["funding_ts"], "funding_ts")
    r8 = a["r8"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
    r8 = np.where(a["invalid"].to_numpy(bool), np.nan, r8)
    return ts, r8


def ts_ewma_r8(f: pd.DataFrame, cur: Instance, t_ns: int) -> float:
    """EWMA of valid r8, half-life 72 h on settlement time, over ``pit_funding(f, cur, t)`` (current instance,
    known_at <= t - 1 min, L1)."""
    ts, x = _valid_r8(f, cur, t_ns)
    m = np.isfinite(x)
    if not m.any():
        return math.nan
    w = 0.5 ** (((int(t_ns) - ts[m]) / H) / EWMA_HALF_LIFE_H)
    return float(w @ x[m] / w.sum())


def ts_ar1_r8(f: pd.DataFrame, cur: Instance, t_ns: int, steps: int) -> float:
    """AR(1) on r8 (OLS r[k+1] = a + phi r[k]) fitted on the valid records of ``pit_funding(f, cur, t)`` with
    funding_ts > t - 30 d, projected ``steps`` = h*/8 settlements ahead; returns the MEAN of the projected
    path (AMB-14). A (r[k], r[k+1]) pair needs both records adjacent in the record sequence and valid (a
    dropped / invalid record breaks the pair). NaN if < 10 pairs or the regressor is constant."""
    if steps < 1:
        raise ValueError("steps must be >= 1")
    ts, x = _valid_r8(f, cur, t_ns)
    keep = ts > int(t_ns) - 30 * D
    ts, x = ts[keep], x[keep]
    if len(x) < 2:
        return math.nan
    ok = np.isfinite(x[:-1]) & np.isfinite(x[1:])
    u, v = x[:-1][ok], x[1:][ok]
    if len(u) < AR1_MIN_OBS:
        return math.nan
    if float(np.ptp(u)) <= 1e-12 * float(np.max(np.abs(u))):  # constant regressor (incl. all-zero): no fit
        return math.nan
    phi = float(np.mean((u - u.mean()) * (v - v.mean())) / np.var(u))
    a = float(v.mean() - phi * u.mean())
    if not np.isfinite(x[-1]):  # the last record is invalid: no state to project from
        return math.nan
    cur_v, path = float(x[-1]), []
    for _ in range(steps):
        cur_v = a + phi * cur_v
        path.append(cur_v)
    return float(np.mean(path))


# --- A0-V5 reference ----------------------------------------------------------------------------------


def frozen_v5_config(universe: list[str]) -> CarryConfig:
    """The V5 ``carry:`` section of config/default.yaml with the V7 universe; its strategy knobs must equal
    ``V5_FROZEN_KNOBS`` and hash to ``V5_FROZEN_KNOBS_SHA256`` (fail closed otherwise, AMB-15)."""
    base = load_carry_config().model_dump()
    knobs = {k: base[k] for k in V5_FROZEN_KNOBS}
    if knobs != V5_FROZEN_KNOBS or knobs_sha256(knobs) != V5_FROZEN_KNOBS_SHA256:
        raise RuntimeError("V5 carry config differs from the frozen A0-V5 knobs")
    base["universe"] = sorted(set(universe))
    return CarryConfig(**base)


@dataclass(frozen=True)
class V5Candidate:
    asset: str
    fl30_r8: float
    cost_side: float
    eligible: bool  # V7 gate result (S-7.1)


def a0_v5_reference(
    now: pd.Timestamp, equity: float, cands: list[V5Candidate], legs: tuple[HeldLeg, ...] = ()
) -> Decision:
    """Reference only: the unchanged V5 ``decide`` with the frozen config on the V7 universe (AMB-15)."""
    nan = math.nan
    sig = {
        c.asset: AssetSignal(
            asset=c.asset,
            eligible=bool(c.eligible),
            reasons=() if c.eligible else ("v7_gate",),
            fl=c.fl30_r8 / R8_H,
            fl_n=0,
            fl_short=nan,
            current_rel=nan,
            pred_rel=nan,
            rank=None,
            pct=None,
            persistence=nan,
            mid=nan,
            mark=nan,
            spread_full=nan,
            half_spread=nan,
            top_book_usd=nan,
            depth_usd=nan,
            volume_usd=nan,
            quote_age_s=nan,
            side_cost=c.cost_side,
        )
        for c in cands
    }
    cfg = frozen_v5_config(list(sig))
    return decide(DecisionInput(now=now, equity=equity, signals=sig, legs=legs), cfg)
