"""H3 deterministic book (spec S-H3-2 ... S-H3-12). No optimiser, no free penalty: the target book
is a closed-form function of the frozen constants, the H2a forecasts and the risk model.

Notional ``n`` is signed USD (long > 0, short < 0); ``E`` is equity. Long and short sides are
lists of asset names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6.riskmodel import Cov
from luna.v6.spec import constants

_C = constants()["h3"]
K_MIN = _C["min_k_prime"]
BUFFER = _C["rank_buffer"]
HORIZON = _C["horizon_hours"]
DOLLAR_CAP = _C["dollar_cap_frac_of_G"]
RATIO_MAX = _C["standalone_side_ratio_max"]
SIGMA_STAR = _C["sigma_star_annual"]
GROSS_CAP = _C["gross_cap"]
BETA_LIMIT = _C["net_beta_limit_frac_equity"]
BAND = _C["size_band"]
MIN_LEG = _C["min_leg_usd"]
TOL = _C["phi_dollar_check_tolerance"]
DOLLAR_LO, DOLLAR_HI = (1 - DOLLAR_CAP) / (1 + DOLLAR_CAP), (1 + DOLLAR_CAP) / (1 - DOLLAR_CAP)  # [2/3, 1.5]


@dataclass(frozen=True)
class Inst:
    mid: float
    step: float
    min_qty: float

    @property
    def qty_step(self) -> float:
        return max(self.step, self.min_qty)

    @property
    def min_notional(self) -> float:
        return max(self.min_qty * self.mid, MIN_LEG)

    def round_qty(self, notional: float) -> float:
        """Quantity for a signed notional, rounded toward zero to the quantity step."""
        q = notional / self.mid
        n = math.floor(abs(q) / self.qty_step + 1e-9) * self.qty_step
        return math.copysign(n, q) if n else 0.0


# --- selection (S-H3-2 ... S-H3-5) -----------------------------------------------------------------


@dataclass
class Selection:
    kp: int
    r: int
    short: list = field(default_factory=list)
    long: list = field(default_factory=list)
    exits: list = field(default_factory=list)  # (asset, reason)
    infeasible: bool = False
    median: float = math.nan


def select(s: pd.Series, held: dict, cost: pd.Series, k: int, blocked: set | frozenset = frozenset()) -> Selection:
    """``s``: H2a(168) forecasts of the ELIGIBLE assets; ``held``: asset -> side (+1 long / -1 short);
    ``cost``: one-way cost (fee + half spread + slip) known at t; ``blocked``: no new entry (cooldown,
    R3). Held assets that are not eligible are exited (they are not ranked)."""
    s = s.dropna()
    order = sorted(s.index, key=lambda a: (-s[a], a))
    ne = len(order)
    kp = min(k, ne // 2)
    sel = Selection(kp, max(0, min(BUFFER, ne // 2 - kp)))
    if kp < K_MIN:
        sel.infeasible = True
        sel.exits = [(a, "infeasible") for a in held]
        return sel
    m = float(np.median(s.to_numpy()))
    sel.median = m
    hold_short, hold_long = set(order[: kp + sel.r]), set(order[-(kp + sel.r):])

    def carry(a: str, side: int) -> float:
        return -side * (float(s[a]) - m)

    kept = {1: [], -1: []}
    for a, side in sorted(held.items()):
        if a not in s.index:
            sel.exits.append((a, "ineligible"))
            continue
        zone = hold_short if side == -1 else hold_long
        if a not in zone:
            sel.exits.append((a, "rank"))
        elif carry(a, side) * HORIZON < -float(cost[a]):
            sel.exits.append((a, "carry"))
        else:
            kept[side].append(a)
    zones = {-1: order[:kp], 1: list(reversed(order[-kp:]))}  # best candidate first (D-B2)
    for side in (-1, 1):
        free = kp - len(kept[side])
        new = []
        for a in zones[side]:
            if free <= 0:
                break
            if a in held or a in blocked:
                continue
            if carry(a, side) * HORIZON > 2 * float(cost[a]):
                new.append(a)
                free -= 1
        (sel.short if side == -1 else sel.long).extend(kept[side] + new)
    return sel


# --- construction (S-H3-7) -------------------------------------------------------------------------


@dataclass
class BuildResult:
    status: str  # "OK" | "INFEASIBLE"
    n: dict = field(default_factory=dict)
    qty: dict = field(default_factory=dict)
    reason: str = ""
    diag: dict = field(default_factory=dict)


def _sub(cov: Cov, names: list) -> np.ndarray:
    return cov.Sigma.loc[names, names].to_numpy()


def side_risk_ratio(n: dict, L: list, S: list, cov: Cov) -> float:
    """Standalone long-side risk / standalone short-side risk of the signed notionals ``n``."""
    l_ = [a for a in L if n.get(a)]
    s_ = [a for a in S if n.get(a)]
    nl = np.array([n[a] for a in l_])
    ns = np.array([abs(n[a]) for a in s_])
    rl = math.sqrt(nl @ _sub(cov, l_) @ nl) if len(l_) else 0.0
    rs = math.sqrt(ns @ _sub(cov, s_) @ ns) if len(s_) else 0.0
    return rl / rs if rs > 0 else math.inf


def checks(n: dict, L: list, S: list, cov: Cov, E: float, step_notional: float) -> dict:
    """The three post-rounding checks (S-H3-7; S-H3-9 and S-H3-10 use the same)."""
    names = [a for a in n if n[a]]
    beta_net = sum(n[a] * float(cov.beta[a]) for a in names)
    dl = sum(n[a] for a in L if n.get(a))
    ds = sum(abs(n[a]) for a in S if n.get(a))
    q = side_risk_ratio(n, L, S, cov)
    out = {
        "beta_net": beta_net / E if E else math.nan,
        "beta_ok": abs(beta_net) <= BETA_LIMIT * E,
        "dollar_ok": abs(dl - ds) <= DOLLAR_CAP * (dl + ds) + step_notional,
        "q": q,
        "q_ok": (1 / RATIO_MAX) <= q <= RATIO_MAX,
    }
    out["ok"] = out["beta_ok"] and out["dollar_ok"] and out["q_ok"]
    return out


def _infeasible(reason: str, diag: dict) -> BuildResult:
    return BuildResult("INFEASIBLE", {}, {}, reason, diag)


def build(L: list, S: list, cov: Cov, E: float, insts: dict[str, Inst]) -> BuildResult:
    L, S = list(L), list(S)
    diag: dict = {"dropped": []}
    for _ in range(len(L) + len(S) + 1):
        if len(L) < K_MIN or len(S) < K_MIN:
            return _infeasible("fewer than 3 legs on a side", diag)
        sig = cov.sigma
        u = {}
        for side in (L, S):
            inv = {a: 1.0 / float(sig[a]) for a in side}
            z = sum(inv.values())
            u.update({a: inv[a] / z for a in side})
        uL = np.array([u[a] for a in L])
        uS = np.array([u[a] for a in S])
        sL = math.sqrt(uL @ _sub(cov, L) @ uL)
        sS = math.sqrt(uS @ _sub(cov, S) @ uS)
        phL, phS = sS / (sL + sS), sL / (sL + sS)
        cap = abs(phL - phS) > DOLLAR_CAP
        if cap:
            hi, lo = 0.5 + DOLLAR_CAP / 2, 0.5 - DOLLAR_CAP / 2
            phL, phS = (hi, lo) if phL > phS else (lo, hi)
        q = (phL * sL) / (phS * sS)
        ratio_bound = False
        if q > RATIO_MAX:
            phL, ratio_bound = phL * RATIO_MAX / q, True
        elif q < 1 / RATIO_MAX:
            phS, ratio_bound = phS * RATIO_MAX * q, True
        diag.update(sigt_L=sL, sigt_S=sS, phi_L=phL, phi_S=phS, cap_bound=cap, q=q, ratio_bound=ratio_bound)
        if abs(phL - phS) > DOLLAR_CAP * (phL + phS) + TOL:
            return _infeasible("dollar cap after the side-ratio rule (basket vol ratio beyond 2.25)", diag)
        names = L + S
        w0 = np.array([phL * u[a] for a in L] + [-phS * u[a] for a in S])
        bv = math.sqrt(w0 @ _sub(cov, names) @ w0)
        G = min(GROSS_CAP / (phL + phS), SIGMA_STAR / bv) if bv > 0 else GROSS_CAP / (phL + phS)
        w = G * w0
        beta = np.array([float(cov.beta[a]) for a in names])
        bn = float(w @ beta)
        diag.update(G=G, beta_pre=bn, beta_scaled=abs(bn) > BETA_LIMIT)
        if abs(bn) > BETA_LIMIT:
            w = w * BETA_LIMIT / abs(bn)
        qty = {a: insts[a].round_qty(wi * E) for a, wi in zip(names, w)}
        n = {a: qty[a] * insts[a].mid for a in names}
        small = [a for a in names if abs(n[a]) < insts[a].min_notional]
        if not small:
            step_n = max(insts[a].qty_step * insts[a].mid for a in names)
            c = checks(n, L, S, cov, E, step_n)
            diag.update(post=c, RC=risk_contributions(n, L, S, cov))
            if not c["ok"]:
                return _infeasible("post-rounding checks", diag)
            return BuildResult("OK", n, qty, "", diag)
        drop = min(small, key=lambda a: (abs(n[a]), a))
        diag["dropped"].append(drop)
        (L if drop in L else S).remove(drop)
    return _infeasible("drop loop exhausted", diag)


def risk_contributions(n: dict, L: list, S: list, cov: Cov) -> dict:
    """Ex-ante Euler risk contributions of the long and short sides (S-H3-8; report only)."""
    names = [a for a in L + S if n.get(a)]
    if not names:
        return {"RC_L": math.nan, "RC_S": math.nan}
    w = np.array([n[a] for a in names])
    Sg = _sub(cov, names)
    tot = float(w @ Sg @ w)
    contrib = w * (Sg @ w) / tot if tot > 0 else np.full(len(w), np.nan)
    rc = dict(zip(names, contrib))
    return {"RC_L": float(sum(rc[a] for a in names if a in L)), "RC_S": float(sum(rc[a] for a in names if a in S))}


# --- resize band (S-H3-9) --------------------------------------------------------------------------


@dataclass
class BandResult:
    status: str
    n: dict
    repaired: bool = False


def apply_band(target: dict, held: dict, L: list, S: list, cov: Cov, E: float, step_notional: float) -> BandResult:
    out = {a: 0.0 for a in held if a not in target}  # target 0 -> exit
    for a, nt in target.items():
        nh = held.get(a, 0.0)
        out[a] = nh if nh and abs(nt - nh) <= BAND * abs(nt) else nt
    live = {a: v for a, v in out.items() if v}
    if checks(live, L, S, cov, E, step_notional)["ok"]:
        return BandResult("OK", out)
    full = dict(target)
    if checks(full, L, S, cov, E, step_notional)["ok"]:
        return BandResult("OK", {**{a: 0.0 for a in held if a not in target}, **full}, repaired=True)
    return BandResult("INFEASIBLE", {}, repaired=True)


# --- intraday repair after an intraday exit (S-H3-10) ----------------------------------------------


@dataclass
class RepairResult:
    status: str  # "OK" | "EXIT_ALL"
    n: dict


def _round(n: dict, insts: dict[str, Inst]) -> dict:
    return {a: insts[a].round_qty(v) * insts[a].mid for a, v in n.items()}


def _drop_small(n: dict, insts: dict[str, Inst]) -> dict:
    return {a: v for a, v in n.items() if abs(v) >= insts[a].min_notional}


def intraday_repair(n: dict, cov: Cov, E: float, insts: dict[str, Inst]) -> RepairResult:
    n = {a: v for a, v in n.items() if v}
    for _ in range(len(n) + 1):
        L = [a for a in n if n[a] > 0]
        S = [a for a in n if n[a] < 0]
        if len(L) < K_MIN or len(S) < K_MIN:
            return RepairResult("EXIT_ALL", {})
        # (1) beta
        bn = sum(v * float(cov.beta[a]) for a, v in n.items())
        if abs(bn) > BETA_LIMIT * E:
            n = {a: v * BETA_LIMIT * E / abs(bn) for a, v in n.items()}
        # (2) net dollar or q
        c = checks(n, L, S, cov, E, 0.0)
        if not (c["dollar_ok"] and c["q_ok"]):
            dl = sum(n[a] for a in L)
            ds = sum(-n[a] for a in S)
            rl = math.sqrt(np.array([n[a] for a in L]) @ _sub(cov, L) @ np.array([n[a] for a in L]))
            rs = math.sqrt(np.array([-n[a] for a in S]) @ _sub(cov, S) @ np.array([-n[a] for a in S]))
            lo = max(DOLLAR_LO * ds / dl, (1 / RATIO_MAX) * rs / rl)
            hi = min(DOLLAR_HI * ds / dl, RATIO_MAX * rs / rl)
            if lo > hi:
                return RepairResult("EXIT_ALL", {})
            a_ = min(max(1.0, lo), hi)  # the ratio closest to 1 inside [lo, hi]
            if a_ < 1:
                n = {a: (v * a_ if v > 0 else v) for a, v in n.items()}
            elif a_ > 1:
                n = {a: (v / a_ if v < 0 else v) for a, v in n.items()}
        # (3) beta again
        bn = sum(v * float(cov.beta[a]) for a, v in n.items())
        if abs(bn) > BETA_LIMIT * E:
            n = {a: v * BETA_LIMIT * E / abs(bn) for a, v in n.items()}
        # (4) rounding and sub-minimum exits; repeat until stable
        rn = _drop_small(_round(n, insts), insts)
        L = [a for a in rn if rn[a] > 0]
        S = [a for a in rn if rn[a] < 0]
        if len(L) < K_MIN or len(S) < K_MIN:
            return RepairResult("EXIT_ALL", {})
        if len(rn) == len(n) and checks(rn, L, S, cov, E, 0.0)["ok"]:
            return RepairResult("OK", rn)
        n = rn
    return RepairResult("EXIT_ALL", {})
