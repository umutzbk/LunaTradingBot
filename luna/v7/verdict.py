"""The `W_HOLD` verdict: P1–P11, S-13.4 precedence, S-13.3 secondary family, K table (spec S-13.1–S-13.4, §12,
§16, EC-11; AMB-21, AMB-22).

Pure: every input is already computed (executed ``BookRun``s, IC / calibration arrays, the ``W_DEV`` facts,
leakage / integrity findings). No I/O, no data access, no training.

* **Required arms on `W_HOLD` (S-13.4, K11, EC-11):** ML* and A0, under ``E_T`` and ``E_M`` (both execution
  models of a required arm, AMB-22 (g)). Any of them ``invalid`` → ``INVALID RUN`` with the run's S-5.4 text
  (B / C: ``INVALID RUN — terminal settlement not reconstructible``). The forward relaxation of EC-11 (A0
  alone becomes unavailable) never applies here. A required ledger failing ``accounting.k10_ok`` (K10, incl.
  the summation-by-parts ``q_breaks``) or ``accounting.daily_ok`` (S-8.1 daily identity) is an integrity
  failure → ``INVALID RUN``; so is any caller-supplied integrity / guard / K8 finding.
* **P1–P11** exactly as S-13.2 tables them (helpers below; P4 / P5 / P8 conventions in AMB-22 (h)–(k)).
  P6 / P10 use ``accounting.mean_daily_net`` (AMB-21 (d)); P1 / P9 use the S-13.1 daily returns.
* **S-13.4 classes**, most severe first: INVALID RUN > NO-GO: leakage > NO-GO: no viable edge (S-10.7 kill, or
  P1 fails under E_T and under E_M) > NO-GO: maker-dependent (P1 fails under E_T, holds under E_M) >
  INCONCLUSIVE (P4) > NO-GO (P2, P3, P5–P11) > V7 HOLDOUT ADVANCE (all of P1–P11).
* **S-13.3 secondary family of exactly 6** (CBB diff test, Holm across the 6): reported only, never changes
  the class. A secondary pair with an invalid / non-accountable run is "unavailable" (voids only its own
  comparison) and stays in the Holm family as a non-rejection (AMB-22 (c)). A1 counts as better than A0 only
  if its Holm-adjusted p < 0.05 and P8-classifier (ECE ≤ 0.05, 10 equal-frequency bins, date-equal weights)
  holds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7 import stats
from luna.v7.accounting import (
    Ledger,
    account,
    daily_ok,
    k10_ok,
    mean_daily_net,
    p11_ok,
    per_asset_net,
    period_net,
    stressed,
)
from luna.v7.book.engine import BookRun
from luna.v7.constants import FROZEN
from luna.v7.cv import _date_weights, date_ics
from luna.v7.timeutil import D

ACC = FROZEN.accept

INVALID = "INVALID RUN"
LEAKAGE = "NO-GO: leakage"
NO_EDGE = "NO-GO: no viable edge"
MAKER = "NO-GO: maker-dependent"
INCONCLUSIVE = "INCONCLUSIVE"
NOGO = "NO-GO"
ADVANCE = "V7 HOLDOUT ADVANCE"
CLASSES = (INVALID, LEAKAGE, NO_EDGE, MAKER, INCONCLUSIVE, NOGO, ADVANCE)  # most severe first
P_IDS = tuple(f"P{i}" for i in range(1, 12))
INTEGRITY_K10 = "INVALID RUN — K10 / accounting integrity failure"

SECONDARY = (  # S-13.3, exactly this family of 6; each pair is (first, second), x = r_first − r_second
    "A1 vs A0",
    "non-selected ML family vs ML*",
    "ML* at non-selected horizon 1 vs A0",
    "ML* at non-selected horizon 2 vs A0",
    "ML* E_M vs E_T",
    "ML* Binance vs Kraken",
)

K_TABLE: dict[str, tuple[str, str]] = {  # §16 (trigger, consequence)
    "K1": ("Any L1–L14 or known-at failure (any model)",
           "stop; fix; restart W_DEV selection from scratch; the holdout stays sealed"),
    "K2": ("Stage 0: funding history, the point-in-time universe, or >= 95 % bar coverage not "
           "reconstructible; or STAGE-0 FAIL / insufficient historical breadth (S-10.2)",
           "stop; explicit new-spec decision by the user (no silent K / D0 change)"),
    "K3": ("S-10.7 development kill", "NO-GO: no viable edge; the holdout stays sealed"),
    "K4": ("LightGBM not chosen under S-10.6", "the linear model is used (not a kill)"),
    "K5": ("Holdout verdict not ADVANCE", "NO-GO; no re-tuning, no re-run, family closed for this spec"),
    "K6": ("P1 holds only under E_M", "NO-GO: maker-dependent"),
    "K7": ("One feature carries > 50 % of total LightGBM gain, or > 50 % of the absolute standardized EN "
           "weight", "mandatory leakage audit before the holdout; if unexplained -> K1"),
    "K8": ("During the holdout run: invalid labels or missing funding > 0.5 %, or a Stage 0-type integrity "
           "failure", "INVALID RUN"),
    "K9": ("Any attempt to read or derive outcomes inside M6 before R_V6, to read W_HOLD before the opening, "
           "or to reach an endpoint the S-1.7 allowlist refuses", "guard refusal; logged; incident report"),
    "K10": ("|Σ_c ρ_c + MKT − PR| > 1e-9 × Σ|fill notional|, or PR != the daily-table price column",
            "INVALID RUN"),
    "K11": ("An actually-held position in a required run reaches a B / C terminal event (S-5.4). Required "
            "runs: W_DEV, the six candidates and A0 at 24 / 72 / 168 h; W_HOLD, ML* and A0; forward, ML* "
            "only (EC-11)",
            "W_DEV: stop before selection, INVALID RUN — historical terminal execution not reconstructible, "
            "holdout sealed (S-10.6); W_HOLD / forward: INVALID RUN. Forward A0 alone: A0 unavailable from "
            "that point, the ML* − A0 report incomplete, no INVALID RUN (§15)"),
}


# --- precedence ---------------------------------------------------------------------------------------------
def applicable(*, invalid: bool, leakage: bool, dev_kill: bool, criteria: Mapping[str, bool],
               p1_em: bool | None) -> list[str]:
    """Every S-13.4 class that applies (most severe first). ``criteria`` holds P1..P11 (P1 under E_T)."""
    out: list[str] = []
    if invalid:
        out.append(INVALID)
    if leakage:
        out.append(LEAKAGE)
    if not invalid:
        if set(criteria) != set(P_IDS):
            raise ValueError("criteria must hold exactly P1..P11")
        p1 = bool(criteria["P1"])
        if dev_kill or (not p1 and not p1_em):
            out.append(NO_EDGE)
        if not p1 and p1_em:
            out.append(MAKER)
        if not criteria["P4"]:
            out.append(INCONCLUSIVE)
        if not all(criteria[p] for p in P_IDS if p not in ("P1", "P4")):
            out.append(NOGO)
        if all(criteria[p] for p in P_IDS):
            out.append(ADVANCE)
    elif dev_kill:
        out.append(NO_EDGE)
    return out


def classify(**kw) -> str:
    """The most severe applicable S-13.4 class."""
    app = applicable(**kw)
    if not app:
        raise RuntimeError("internal: no class applies")
    return min(app, key=CLASSES.index)


# --- P helpers -------------------------------------------------------------------------------------------
def _col(df: pd.DataFrame, c: str, what: str) -> np.ndarray:
    if df is None or c not in df.columns:
        raise ValueError(f"{what}: column {c!r} missing")
    a = df[c].to_numpy(dtype=np.float64)
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{what}.{c}: non-finite values")
    return a


def active_days(marks: pd.DataFrame) -> int:
    """P4: UTC days with gross (leg) exposure ≥ 10 % of the marked equity at ≥ 48 of the 96 quarter-hour
    ticks (AMB-22 (h))."""
    if marks is None or len(marks) == 0:
        return 0
    ts = np.asarray(marks["ts"].to_numpy(), dtype=np.int64)
    g, e = _col(marks, "gross_legs", "marks"), _col(marks, "equity", "marks")
    on = g >= ACC.p4["gross_frac"] * e
    days = ts // D
    counts = pd.Series(on).groupby(days).sum()
    return int((counts >= ACC.p4["quarters"]).sum())


def closed_legs(led: Ledger) -> int:
    """P4: closed leg positions (position ids of closed episodes; AMB-22 (h))."""
    return sum(len(e.position_ids) for e in led.episodes if e.closed)


def p4(run: BookRun, led: Ledger) -> tuple[bool, dict]:
    d = dict(active_days=active_days(run.marks), closed_legs=closed_legs(led), clusters=led.n_clusters)
    ok = (d["active_days"] >= ACC.p4["active_days"] and d["closed_legs"] >= ACC.p4["closed_legs"]
          and d["clusters"] >= ACC.p4["clusters"])
    return bool(ok), d


def max_drawdown(run: BookRun, led: Ledger) -> float:
    """P3: maximum drawdown of the 15-minute marked equity (plus the final, post-unwind equity)."""
    e = np.concatenate([_col(run.marks, "equity", "marks") if run.marks is not None and len(run.marks)
                        else np.zeros(0), stats.ledger_equity(led)[-1:]])
    if np.any(e <= 0):
        return 1.0
    peak = np.maximum.accumulate(e)
    return float(np.max(1.0 - e / peak))


def drawdown_killed(run: BookRun) -> bool:
    """A drawdown kill happened: a decision saw ``killed`` or any fill has reason ``kill``."""
    dec = run.decisions
    k = bool(dec is not None and "killed" in dec.columns and dec["killed"].any())
    if run.fills is not None and len(run.fills) and "reason" in run.fills.columns:
        k |= bool((run.fills["reason"] == "kill").any())
    return k


def p5(led: Ledger, quarter_edges: Sequence[int]) -> tuple[bool, dict]:
    """P5: net > 0 in ≥ 2 of the 4 quarters; no quarter > 60 % of NET; top-1 N_a ≤ 0.30 NET, top-3 ≤ 0.60 NET
    (N_a per symbol, ``accounting.per_asset_net``; top-k = the k largest N_a; AMB-22 (i))."""
    e = [int(x) for x in quarter_edges]
    if len(e) != 5:
        raise ValueError("P5 needs the 5 edges of the 4 holdout quarters")
    st = led.daily["start_ts"].to_numpy()
    if st[0] < e[0] or st[-1] >= e[-1]:
        raise ValueError("quarter edges do not cover the window")
    q = period_net(led, e)
    net = led.NET
    na = sorted(per_asset_net(led).values(), reverse=True)
    top1, top3 = (na[0] if na else 0.0), math.fsum(na[:3])
    P = ACC.p5
    ok = (sum(x > 0 for x in q) >= P["min_pos_q"] and all(x <= P["max_q_share"] * net for x in q)
          and top1 <= P["top1"] * net and top3 <= P["top3"] * net)
    return bool(ok), dict(quarters=q, NET=net, top1=top1, top3=top3)


def p8_slope(yhat_cal, y, dates) -> float:
    """P8-primary: OLS slope of realized y_reg on ŷ_cal, every decision date weighted equally (NaN if ŷ_cal is
    constant)."""
    x, t = np.asarray(yhat_cal, np.float64), np.asarray(y, np.float64)
    if not (len(x) == len(t) == len(dates)) or len(x) == 0 or not (np.isfinite(x).all()
                                                                    and np.isfinite(t).all()):
        raise ValueError("P8: lengths differ, empty or non-finite input")
    w = _date_weights(dates)
    mx, my = float(w @ x), float(w @ t)
    vx = float(w @ (x - mx) ** 2)
    return float(w @ ((x - mx) * (t - my))) / vx if vx > 1e-300 else float("nan")


def p8_ok(slope: float) -> bool:
    lo, hi = ACC.p8
    return bool(math.isfinite(slope) and lo <= slope <= hi)


def ece(p, y_pos, dates, bins: int = ACC.ece_bins) -> float:
    """P8-classifier: Σ_bins |Σ_i w_i (y_i − p_i)| over ``bins`` equal-frequency bins of P̂ (rows sorted by P̂,
    stable; ``numpy.array_split``), date-equal weights summing to 1 (AMB-22 (j))."""
    pa, ya = np.asarray(p, np.float64), np.asarray(y_pos, np.float64)
    if not (len(pa) == len(ya) == len(dates)) or len(pa) < bins:
        raise ValueError("ECE: lengths differ or fewer rows than bins")
    if not (np.isfinite(pa).all() and np.isin(ya, (0.0, 1.0)).all()):
        raise ValueError("ECE: non-finite P-hat or non-binary y_pos")
    w = _date_weights(dates)
    order = np.argsort(pa, kind="stable")
    return float(sum(abs(float(w[b] @ (ya[b] - pa[b]))) for b in np.array_split(order, bins)))


def p7(pred, y, dates, B: int, seed: int) -> tuple[bool, dict]:
    """P7: date-level IC mean > 0 and its S-13.1 LB (ℓ ∈ {14, 28}) > 0 (IC series in date order)."""
    s = date_ics(pred, y, dates).sort_index()
    if len(s) < 2:
        return False, dict(ic_mean=float("nan"), ic_lb=float("nan"), n_dates=len(s))
    m = float(s.mean())
    lb = stats.lower_bound(s.to_numpy(), stats.BLOCKS, B, seed)
    return bool(m > 0 and lb > 0), dict(ic_mean=m, ic_lb=lb, n_dates=len(s))


# --- verdict ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class HoldoutInputs:
    ml_T: BookRun
    a0_T: BookRun
    ml_M: BookRun
    a0_M: BookRun
    quarter_edges: Sequence[int]
    ic: tuple  # (pred, y_reg, dates) of the ML* regression model on W_HOLD (P7)
    calib: tuple  # (ŷ_cal, y_reg(h*), dates) over every eligible W_HOLD candidate (P8-primary)
    dev_mean_daily_net: float  # P10: ML* out-of-fold mean daily net on W_DEV
    dev_kill: Sequence[str] = ()
    leakage: Sequence[str] = ()
    integrity: Sequence[str] = ()  # guard / K8 / Stage 0-type findings (any -> INVALID RUN)
    secondary: Mapping[str, tuple[BookRun | Ledger, BookRun | Ledger] | None] | None = None
    a1_calib: tuple | None = None  # (P̂_cal, y_pos, dates) of the A1 classifier on W_HOLD (P8-classifier)
    B: int = stats.B_DEFAULT
    seed: int = stats.SEED


@dataclass(frozen=True)
class Verdict:
    cls: str
    reasons: tuple[str, ...]
    applicable: tuple[str, ...] = ()
    criteria: dict[str, bool] = field(default_factory=dict)
    p1_em: bool | None = None
    details: dict = field(default_factory=dict)
    secondary: dict = field(default_factory=dict)
    a1_better: bool | None = None


def _check_roles(x: HoldoutInputs) -> None:
    if x.ml_T.arm not in ("A2", "A3") or x.ml_M.arm != x.ml_T.arm:
        raise ValueError("ML* must be the A2 / A3 run under both E_T and E_M")
    if x.a0_T.arm != "A0" or x.a0_M.arm != "A0":
        raise ValueError("the baseline runs must be A0")
    if len({r.h_star for r in (x.ml_T, x.a0_T, x.ml_M, x.a0_M)}) != 1:
        raise ValueError("ML* and A0 runs must share h*")
    exe = (x.ml_T.execution, x.a0_T.execution, x.ml_M.execution, x.a0_M.execution)
    if exe != ("E_T", "E_T", "E_M", "E_M"):
        raise ValueError("execution models must be E_T, E_T, E_M, E_M")
    if not math.isfinite(x.dev_mean_daily_net):
        raise ValueError("dev_mean_daily_net must be finite")


def _usable(obj: BookRun | Ledger) -> Ledger | None:
    if isinstance(obj, BookRun):
        if not obj.valid:
            return None
        obj = account(obj)
    return obj if (k10_ok(obj) and daily_ok(obj)) else None


def secondary_family(pairs: Mapping[str, tuple | None], a1_calib: tuple | None, B: int, seed: int
                     ) -> tuple[dict, bool]:
    """S-13.3: the CBB diff test (max p over ℓ ∈ {14, 28}) of each of the 6 pairs, Holm across the 6; the A1
    claim needs Holm rejection and P8-classifier."""
    if set(pairs) != set(SECONDARY):
        raise ValueError(f"the secondary family must be exactly {SECONDARY}")
    out: dict[str, dict] = {}
    raw: dict[str, float | None] = {}
    for name in SECONDARY:
        pair = pairs[name]
        la = lb = None
        if pair is not None:
            la, lb = _usable(pair[0]), _usable(pair[1])
        if la is None or lb is None:
            out[name] = dict(available=False, p=None, mean_x=None)
            raw[name] = None
            continue
        _, x = stats.paired(la, lb)
        raw[name] = stats.p_max_over_blocks(x, stats.BLOCKS, B, seed)
        out[name] = dict(available=True, p=raw[name], mean_x=float(np.mean(x)))
    adj = stats.holm(raw)
    for name in SECONDARY:
        out[name]["p_holm"] = adj[name]
        out[name]["rejects"] = adj[name] is not None and adj[name] < stats.ALPHA
    e = ece(*a1_calib) if a1_calib is not None else None
    out["P8-classifier"] = dict(ece=e, holds=e is not None and e <= ACC.ece_max)
    return out, bool(out["A1 vs A0"]["rejects"] and out["P8-classifier"]["holds"])


def holdout(x: HoldoutInputs) -> Verdict:
    """The S-13.4 verdict on ``W_HOLD`` (most severe applicable class)."""
    _check_roles(x)
    leak, kill = bool(x.leakage), bool(x.dev_kill)
    reasons = [f"leakage: {s}" for s in x.leakage] + [f"S-10.7: {s}" for s in x.dev_kill]
    req = {"ML* E_T": x.ml_T, "A0 E_T": x.a0_T, "ML* E_M": x.ml_M, "A0 E_M": x.a0_M}
    bad = [f"{k}: {r.invalid}" + (f" ({r.invalid_detail})" if r.invalid_detail else "")
           for k, r in req.items() if not r.valid]
    bad += [f"integrity: {s}" for s in x.integrity]
    leds: dict[str, Ledger] = {}
    if not bad:
        leds = {k: account(r) for k, r in req.items()}
        bad += [f"{k}: {INTEGRITY_K10}" for k, led in leds.items() if not (k10_ok(led) and daily_ok(led))]
    if bad:
        app = applicable(invalid=True, leakage=leak, dev_kill=kill, criteria={}, p1_em=None)
        return Verdict(INVALID, tuple(bad + reasons), tuple(app))

    ml, a0, mlm, a0m = leds["ML* E_T"], leds["A0 E_T"], leds["ML* E_M"], leds["A0 E_M"]
    r, xd = stats.paired(ml, a0)
    t1 = stats.cbb_test(r, xd, stats.BLOCKS, x.B, x.seed)
    rm, xm = stats.paired(mlm, a0m)
    t1m = stats.cbb_test(rm, xm, stats.BLOCKS, x.B, x.seed)
    crit: dict[str, bool] = {}
    det: dict = dict(p1=t1, p1_em=t1m)
    crit["P1"] = t1.holds
    ratio = ml.C_exec / ml.F_net if ml.F_net > 0 else float("inf")
    crit["P2"] = bool(ml.F_net > 0 and ratio <= ACC.p2_ratio)
    mdd, killed = max_drawdown(x.ml_T, ml), drawdown_killed(x.ml_T)
    crit["P3"] = bool(mdd <= ACC.p3_mdd and not killed)
    crit["P4"], det["p4"] = p4(x.ml_T, ml)
    crit["P5"], det["p5"] = p5(ml, x.quarter_edges)
    st = stressed(x.ml_T, **ACC.p6)
    crit["P6"] = bool(mean_daily_net(st) >= 0)
    crit["P7"], det["p7"] = p7(*x.ic, x.B, x.seed)
    slope = p8_slope(*x.calib)
    crit["P8"] = p8_ok(slope)
    sh = stats.sharpe(r)
    crit["P9"] = bool(sh.value >= ACC.p9_sharpe)
    crit["P10"] = bool(x.dev_mean_daily_net > 0)
    crit["P11"] = p11_ok(ml)
    det.update(F_net=ml.F_net, C_exec_over_F_net=ratio, mdd=mdd, killed=killed,
               stressed_mean_daily_net=mean_daily_net(st), p8_slope=slope, sharpe=sh,
               mean_daily_net=mean_daily_net(ml), dev_mean_daily_net=x.dev_mean_daily_net)
    sec, a1 = ({}, None)
    if x.secondary is not None:
        sec, a1 = secondary_family(x.secondary, x.a1_calib, x.B, x.seed)
    app = applicable(invalid=False, leakage=leak, dev_kill=kill, criteria=crit, p1_em=t1m.holds)
    cls = min(app, key=CLASSES.index)
    reasons += [f"{p} fails" for p in P_IDS if not crit[p]]
    return Verdict(cls, tuple(reasons), tuple(app), crit, t1m.holds, det, sec, a1)


__all__ = ["ADVANCE", "CLASSES", "HoldoutInputs", "INCONCLUSIVE", "INVALID", "K_TABLE", "LEAKAGE", "MAKER",
           "NOGO", "NO_EDGE", "P_IDS", "SECONDARY", "Verdict", "active_days", "applicable", "classify",
           "closed_legs", "drawdown_killed", "ece", "holdout", "max_drawdown", "p4", "p5", "p7", "p8_ok",
           "p8_slope", "secondary_family"]
