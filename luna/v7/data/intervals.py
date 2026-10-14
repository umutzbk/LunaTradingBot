"""Funding nominal intervals, reconciliation, transitions and r8 (spec S-2.6, DEC-29/40/47, EC-9, U3).

Per record, instance-aware (nothing crosses an instance boundary):

* nominal interval = the explicit field; else the standard interval (1, 2, 4, 8 h) matching the
  spacing to the previous record of the same instance within +-60 s; else the previous nominal
  interval (flagged ``nominal_inherited``).
* reconciled = the spacing equals the previous nominal or the record's own explicit interval within
  +-60 s.
* EC-9: a record with no explicit field whose spacing matches a standard interval but not the
  previous nominal (or the previous nominal is undetermined) is reconciled with
  ``interval_transition = True``. The first record of an instance is reconciled only if it carries
  an explicit interval. Everything else is ``invalid`` (DEC-40).
* An invalid record's r8 is NaN; it never changes its successors' explicit or spacing-inferred
  intervals: its nominal interval is the inherited previous nominal, while its timestamp still
  anchors the successor's spacing.
Records that belong to no instance (dormant gaps) are never used: ``in_instance = False``, no flags,
r8 NaN (AMB-04).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance
from luna.v7.timeutil import H

TOL = FROZEN.funding.tol_ns
STD_H = FROZEN.funding.std_intervals_h


def _std_match(spacing_ns: int) -> float | None:
    best = min(STD_H, key=lambda c: abs(c * H - spacing_ns))
    return float(best) if abs(best * H - spacing_ns) <= TOL else None


def _instances_of(insts: list[Instance], ts: np.ndarray) -> list[int | None]:
    """``instances.instance_of`` for every timestamp (first matching instance in list order), vectorised."""
    out: list[int | None] = [None] * len(ts)
    free = np.ones(len(ts), bool)
    for inst in insts:
        m = free & (ts >= inst.first_bar_ts)
        if inst.last_trade_ts is not None:
            m &= ts < inst.last_trade_ts
        for k in np.flatnonzero(m).tolist():
            out[k] = inst.idx
        free &= ~m
    return out


def _annotate_one(df: pd.DataFrame, insts: list[Instance]) -> pd.DataFrame:
    n = len(df)
    ts_np = df["funding_ts"].to_numpy(np.int64)
    ex_np = df["interval_h_explicit"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
    rate = df["rate"].to_numpy(float)
    inst = _instances_of(insts, ts_np)
    # the per-record recursion runs on Python scalars (same IEEE operations as on numpy scalars, faster)
    ts, ex = ts_np.tolist(), ex_np.tolist()
    nom = [math.nan] * n
    rec = [False] * n
    trans = [False] * n
    inv = [False] * n
    inherited = [False] * n
    for i in range(n):
        if inst[i] is None:
            continue
        if i == 0 or inst[i - 1] != inst[i]:
            nom[i] = ex[i]
            rec[i] = not math.isnan(ex[i])
            inv[i] = not rec[i]
            continue
        sp = int(ts[i] - ts[i - 1])
        prev = nom[i - 1]
        has_prev = not math.isnan(prev)
        match_prev = has_prev and abs(sp - prev * H) <= TOL
        if not math.isnan(ex[i]):
            ok = match_prev or abs(sp - ex[i] * H) <= TOL
            if ok:
                nom[i], rec[i] = ex[i], True
            else:
                nom[i], inv[i], inherited[i] = prev, True, True
            continue
        std = _std_match(sp)
        if std is not None:
            nom[i], rec[i], trans[i] = std, True, not match_prev
        elif match_prev:  # prev nominal is a non-standard explicit value matching the spacing
            nom[i], rec[i] = prev, True
        else:
            nom[i], inv[i], inherited[i] = prev, True, True
    nom = np.array(nom, dtype=float)
    rec, trans, inv, inherited = (np.array(x, dtype=bool) for x in (rec, trans, inv, inherited))
    out = df.copy()
    out["instance"] = pd.Series(inst, index=df.index, dtype=object)
    out["in_instance"] = [x is not None for x in inst]
    out["nominal_h"] = pd.array(nom, dtype="Float64")
    out["nominal_inherited"] = inherited
    out["reconciled"] = rec
    out["interval_transition"] = trans
    out["invalid"] = inv
    ok = rec & ~inv & ~np.isnan(nom)
    r8 = np.full(n, np.nan)
    r8[ok] = rate[ok] * 8.0 / nom[ok]
    out["r8"] = r8
    return out


def annotate(funding: pd.DataFrame, insts: list[Instance] | dict[str, list[Instance]]) -> pd.DataFrame:
    """Add instance, in_instance, nominal_h, nominal_inherited, reconciled, interval_transition, invalid, r8.

    ``funding`` must be sorted by ``funding_ts`` (per symbol). With a ``symbol`` column ``insts`` is a dict
    keyed by symbol; each symbol is annotated separately.
    """
    if "symbol" in funding.columns:
        if not isinstance(insts, dict):
            raise ValueError("a funding frame with a symbol column needs insts as a dict keyed by symbol")
        if not funding.index.is_unique:
            raise ValueError("multi-symbol annotate needs a unique index")
        missing = sorted({str(x) for x in funding["symbol"].unique()} - set(insts))
        if missing:
            raise ValueError(f"no instances given for symbols {missing}")
        parts = [
            _annotate_one(g.sort_values("funding_ts"), insts[str(sym)])
            for sym, g in funding.groupby("symbol", sort=True)
        ]
        return pd.concat(parts).loc[funding.index] if parts else funding.copy()
    if not isinstance(insts, list):
        raise ValueError("a single-symbol funding frame needs insts as a list")
    return _annotate_one(funding, insts)


def u3_gap(prev_nominal_h: float, cur_nominal_h: float, spacing_ns: int) -> bool:
    """U3 adjacent-interval gap: spacing > max(previous, current nominal)*H + 60 s (S-2.3, DEC-29)."""
    known = [float(x) for x in (prev_nominal_h, cur_nominal_h) if not pd.isna(x)]
    if not known:
        return True  # both undetermined: fail closed (AMB-04)
    return spacing_ns > max(known) * H + TOL
