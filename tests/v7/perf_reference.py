"""Slow reference implementations for the Task 26b equivalence tests (SC-01 part 2): VERBATIM copies of the
functions Task 26b changed, as they were at commit b0fbd3a (extracted with ``ast`` from ``git show``, not
retyped). Only three mechanical edits: ``universe.assert_pit`` is called ``uni_assert_pit`` here (the name
clashes with ``features.assert_pit``), ``pit_funding`` is the direct ``_pit_funding_reference`` path (Task 19b
proved the incremental memo identical to it) and ``features.xs_input`` / ``features.compute_rows`` inside the
copied ``DecisionProvider`` resolve to the copies below (``features`` is a namespace of this module).

Helpers Task 26b did not change are imported from the production modules. Never used by production code.
"""

# ruff: noqa
from __future__ import annotations

import math
import types
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v7 import guard, k9
from luna.v7.arms import cost_side
from luna.v7.beta import beta_hat, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.book.engine import DecisionView
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance, start_known_at, started_instance
from luna.v7.data.intervals import annotate, u3_gap
from luna.v7.data.notices import CLEAR_AFTER_NS, HALT_GRID_NS, NoticeVersion, _Event, confident_match, known_at, \
    scheduled_ts
from luna.v7.features import (
    CARRY_DIRECTION, F, F6_SAMPLES, F6_STEP, FEATURES, F14_COLS, NAN, XS_KEYS, FeatureRow, R4Bars, XSInput,
    _capped_ratio, _f, _funding_feats, _hourly, _mean, _ret, _rvol, assert_pit,
)
from luna.v7.market import _num
from luna.v7.labels import _F, _FB_H, _FB_SLIP, _SLIP, LabelInputs, LabelResult, LegData, _Invalid
from luna.v7.refs import FundingProblem, pos
from luna.v7.runner import CAND_COLS, QV24_NS, UNIVERSE, DecisionRecord, _max_ka as _run_max_ka, _mx as _run_mx
from luna.v7.timeutil import MIN, D, H, ns_array
from luna.v7.universe import (
    _PF_COLS, U, GateRow, LeakageError, SymbolData, _max_ka, _mx, _u2, _u4, _u8, daily_sums, eligible,
    instrument_exclusion, median_daily_quote_volume,
)
from luna.v7.universe import assert_pit as uni_assert_pit

BAR_15M = 15 * MIN

# ---- verbatim from luna/v7/universe.py @ b0fbd3a

def pit_rows(df: pd.DataFrame, t_ns: int, name: str) -> pd.DataFrame:
    """Rows with known_at <= t - 1 min (required column, validated)."""
    if "known_at" not in df.columns:
        raise ValueError(f"{name}: no known_at column: point-in-time filtering is impossible")
    if len(df) == 0:
        return df
    return df[ns_array(df["known_at"], f"{name}.known_at") <= t_ns - MIN]


def _pit_funding_reference(f: pd.DataFrame, cur: Instance, t_ns: int) -> pd.DataFrame:
    """The direct computation (the definition the incremental path must reproduce)."""
    cols = _PF_COLS
    f = pit_rows(f, t_ns, "funding_annot")
    if len(f):
        f = f[ns_array(f["funding_ts"], "funding_ts") >= cur.first_bar_ts].sort_values("funding_ts")
    if len(f) == 0:
        return annotate(pd.DataFrame({c: pd.Series(dtype="int64") for c in cols}), [])
    raw = f[cols].reset_index(drop=True)
    return annotate(raw, [Instance(cur.idx, cur.first_bar_ts, None, None)])


def _funding(f: pd.DataFrame, cur: Instance, t_ns: int) -> tuple[bool, bool, int | None]:
    """(U3, U7, max known_at) on ``pit_funding``."""
    a = pit_funding(f, cur, t_ns)
    if len(a) == 0:
        return False, False, None
    ts = ns_array(a["funding_ts"], "funding_ts")
    ka = ns_array(a["known_at"], "known_at")
    nom = a["nominal_h"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
    ok = ~a["invalid"].to_numpy(bool)  # invalid records are missing (S-2.6), for U3 and U7 alike
    tv, nv = ts[ok], nom[ok]
    if len(tv) == 0:
        return False, False, int(ka.max())
    # U7: the last valid settled record; an undetermined nominal fails closed
    u7 = bool(not np.isnan(nv[-1]) and t_ns - tv[-1] <= nv[-1] * H + U.u7_slack_min * MIN)
    u3 = bool(tv[0] <= t_ns - U.u3_days * D)
    if u3:
        lo = max(int(np.searchsorted(tv, t_ns - U.u3_gap_lookback_days * D, "left")), 1)
        for i in range(lo, len(tv)):
            if u3_gap(nv[i - 1], nv[i], int(tv[i] - tv[i - 1])):
                u3 = False
                break
    return u3, u7, int(ka.max())


def gates(sd: SymbolData, t_ns: int) -> GateRow:
    """Evaluate U1-U8 for one symbol at decision time t (S-2.3)."""
    t_ns = int(t_ns)
    why = instrument_exclusion(sd.symbol, sd.identity)
    if why is not None:
        return GateRow(*(False,) * 8, (f"excluded:{why}",), None)
    cur = current_instance(sd.insts, t_ns)
    if cur is None:
        return GateRow(*(False,) * 8, ("no_current_instance",), None)
    b15 = pit_rows(sd.bars15, t_ns, "bars15")
    if len(b15):
        b15 = b15[ns_array(b15["open_ts"], "open_ts") >= cur.first_bar_ts]
    u1 = cur.first_bar_ts <= t_ns - U.u1_days * D
    u2, k2 = _u2(b15, t_ns) if len(b15) else (False, None)
    u4, k4 = _u4(b15, t_ns) if len(b15) else (False, None)
    u8, k8 = _u8(b15, t_ns) if len(b15) else (False, None)
    h = half_spread(b15, t_ns) if len(b15) else None
    u5 = h is not None and h <= U.u5_max_half_spread
    k5 = None
    if len(b15):
        k5 = _max_ka(b15, ns_array(b15["open_ts"], "open_ts") >= t_ns - FROZEN.spread.window_ns)
    u3, u7, kf = _funding(sd.funding_annot, cur, t_ns)
    b1 = sd.bars1m
    b1 = pit_rows(b1, t_ns, "bars1m")
    kn = [n for n in sd.notices if (k := known_at(n)) is not None and k <= t_ns - MIN]
    u6_active = U6State(kn, sd.symbol, sd.insts).active(cur, t_ns, lambda tau: halted(b1, tau))
    k6 = _mx(*(known_at(n) for n in kn))
    flags = (u1, u2, u3, u4, u5, not u6_active, u7, u8)
    reasons = tuple(f"U{i + 1}" for i, ok in enumerate(flags) if not ok)
    row = GateRow(*flags, reasons, _mx(k2, k4, k5, k8, kf, k6))
    uni_assert_pit(row, t_ns)
    return row


# ---- verbatim from luna/v7/data/notices.py @ b0fbd3a

class U6State:
    """U6 for one symbol, built from its notice versions and instances (S-2.5)."""

    def __init__(self, versions: list[NoticeVersion], symbol: str, insts: list[Instance]) -> None:
        self.symbol = symbol
        self._events: dict[int, list[_Event]] = {}
        self._clear_cache: dict[tuple, bool] = {}
        for v in versions:
            ka = known_at(v)
            if ka is None or not confident_match(v, symbol):
                continue
            # the instance live at the version's known_at (attribution; used only once that instance is
            # current, DK-108: a version published in its first 16 min is not dropped)
            inst = started_instance(insts, ka)
            if inst is None:
                continue
            sched = scheduled_ts(v, symbol) if v.kind == "delisting" else None
            ev = _Event(ka, (ka, v.release_ts, v.notice_id, v.version), v.kind, sched)
            self._events.setdefault(inst.idx, []).append(ev)
        for evs in self._events.values():
            evs.sort(key=lambda e: e.order)

    def _no_halt(self, sched: int, halted_fn: Callable[[int], bool]) -> bool:
        """True when the instrument is not HALTED at any time in [sched, sched + 72 h] (cached)."""
        key = (sched, halted_fn)
        if key not in self._clear_cache:
            end = sched + CLEAR_AFTER_NS
            grid = [*range(sched, end, HALT_GRID_NS), end]
            self._clear_cache[key] = not any(halted_fn(x) for x in grid)
        return self._clear_cache[key]

    def _cleared(self, sched: int | None, at: int, halted_fn: Callable[[int], bool]) -> bool:
        return sched is not None and sched + CLEAR_AFTER_NS <= at and self._no_halt(sched, halted_fn)

    def active(self, instance: Instance, t_ns: int, halted_fn: Callable[[int], bool]) -> bool:
        if t_ns - MIN < start_known_at(instance) or (  # not yet current (DK-108)
            instance.end_known_at is not None and t_ns >= instance.end_known_at
        ):
            return False  # U6 ends with its instance
        inforce: list[int | None] = []  # sched of each in-force version
        for e in self._events.get(instance.idx, []):
            if e.ka > t_ns:
                break
            inforce = [x for x in inforce if not self._cleared(x, e.ka, halted_fn)]
            if e.kind != "delisting":
                inforce = []  # a matched postponement / cancellation clears every earlier in-force version
                continue
            if self._cleared(e.sched, e.ka, halted_fn):
                continue  # the 72 h test already passed at known_at: never activates
            inforce.append(e.sched)
        return any(not self._cleared(x, t_ns, halted_fn) for x in inforce)


# ---- verbatim from luna/v7/market.py @ b0fbd3a

def halted(bars1m: pd.DataFrame, tau_ns: int) -> bool:
    """S-5.4: no volume > 0 1-minute bar opening in the half-open window [tau - 60 min, tau)."""
    t = ns_array(bars1m["open_ts"], "open_ts")
    v = bars1m["vol_base"].to_numpy(np.float64)
    return not bool(((t >= tau_ns - H) & (t < tau_ns) & (v > 0)).any())


def _open_at(bars: pd.DataFrame, tau_ns: int) -> float | None:
    hit = bars.loc[bars["open_ts"] == tau_ns, "o"]
    if len(hit) == 0 or not np.isfinite(float(hit.iloc[0])):
        return None
    return float(hit.iloc[0])


def P(bars1m: pd.DataFrame, bars15: pd.DataFrame, tau: int) -> float | None:  # noqa: N802
    """S-5.2: open of the 1m bar starting at tau; if that bar is absent or its open is unusable (NaN /
    non-positive), the open of the 15m bar starting at tau; else None."""
    v = _open_at(bars1m, tau)
    if v is None or v <= 0:
        v = _open_at(bars15, tau)  # 1m bar absent or its open unusable (NaN / non-positive)
    return v if v is not None and v > 0 else None


def first_bar_within(bars1m: pd.DataFrame, tau: int, window_ns: int = H) -> tuple[int, float] | None:
    """Data-gap rule (S-5.4): first 1m bar opening in [tau, tau + window]; (open_ts, open) or None."""
    sel = bars1m[(bars1m["open_ts"] >= tau) & (bars1m["open_ts"] <= tau + window_ns)]
    if len(sel) == 0:
        return None
    row = sel.loc[sel["open_ts"].idxmin()]
    return int(row["open_ts"]), float(row["o"])


def first_executable_after(
    bars1m: pd.DataFrame, tau: int, instance: Instance
) -> tuple[int, float] | None:
    """S-5.4 exit from a halted instrument: first volume > 0 1m bar of ``instance`` opening >= tau.

    Returns (fill_ts = bar close time, price = bar close). Bars outside the instance (E10) are ignored: the
    bar must open at/after ``first_bar_ts`` and close by ``last_trade_ts``. The instance end used is the
    reconstructed (retrospective) one; this is admissible because the fill is booked only once the bar has
    closed. Bars whose close is not finite and positive are not executable (skipped; no price is invented).
    No usable bar -> None (the caller applies the terminal / invalid rules).
    """
    t = ns_array(bars1m["open_ts"], "open_ts")
    ok = (t >= tau) & (bars1m["vol_base"].to_numpy(np.float64) > 0) & (t >= instance.first_bar_ts)
    if instance.last_trade_ts is not None:
        ok &= t + MIN <= instance.last_trade_ts
    c = bars1m["c"].to_numpy(np.float64)
    ok &= np.isfinite(c) & (c > 0)  # a bar without a usable close is not executable (no price invented)
    if not ok.any():
        return None
    row = bars1m[ok].sort_values("open_ts").iloc[0]
    return int(row["open_ts"]) + MIN, float(row["c"])


def mark_at_settle(
    funding_row: Any, mark15: pd.DataFrame, last15: pd.DataFrame, settle_ts: int
) -> float | None:
    """S-4.6 order: record mark -> mark-price 15m bar ending at settlement (close) -> last-price close."""
    m = _num(funding_row.get("mark_at_settle")) if funding_row is not None else None
    if m is not None:
        return m
    for bars in (mark15, last15):
        hit = bars.loc[bars["open_ts"] == settle_ts - BAR_15M, "c"]
        if len(hit):
            c = _num(hit.iloc[0])
            if c is not None:
                return c
    return None


# ---- verbatim from luna/v7/refs.py @ b0fbd3a

def trim(df: pd.DataFrame, col: str, lo: int | None, hi: int) -> pd.DataFrame:
    """Rows with lo <= df[col] < hi (lo None: no lower bound)."""
    if len(df) == 0:
        return df
    t = ns_array(df[col], col)
    m = t < hi
    if lo is not None:
        m &= t >= lo
    return df[m]


@dataclass(frozen=True)
class InstView:
    """An instrument restricted to one instance and to timestamps before the window end."""

    inst: Instance
    b1: pd.DataFrame
    b15: pd.DataFrame
    m15: pd.DataFrame
    funding: pd.DataFrame
    end: int  # window end


def restrict(bars1m: pd.DataFrame, bars15: pd.DataFrame, mark15: pd.DataFrame, funding: pd.DataFrame,
             cur: Instance, w_end: int) -> InstView:
    hi = w_end if cur.last_trade_ts is None else min(w_end, cur.last_trade_ts)
    return InstView(
        cur,
        trim(bars1m, "open_ts", cur.first_bar_ts, hi),
        trim(bars15, "open_ts", cur.first_bar_ts, hi),
        trim(mark15, "open_ts", cur.first_bar_ts, hi),
        trim(funding, "funding_ts", None, w_end),  # pit_funding applies the instance start
        w_end,
    )


def usable_open(b1: pd.DataFrame) -> pd.DataFrame:
    o = b1["o"].to_numpy(np.float64)
    return b1[np.isfinite(o) & (o > 0)]


def ref_price(x: InstView, tau: int, before_gap=None) -> tuple[float, bool] | None:
    """Reference price at tau (S-5.2) with the S-5.4 data-gap rule; (price, gap_used) or None.
    ``before_gap`` (the guard for the extended span) runs before the gap search reads past tau + 1 min."""
    p = P(x.b1, x.b15, tau)
    if p is not None:
        return p, False
    if before_gap is not None:
        before_gap()
    g = first_bar_within(usable_open(x.b1), tau, H)
    if g is None:
        return None
    return float(g[1]), True


def positive_close(bars: pd.DataFrame) -> pd.DataFrame:
    """Bars whose close is finite and > 0 (a mark fallback never uses an unusable price)."""
    if len(bars) == 0:
        return pd.DataFrame({"open_ts": pd.Series(dtype="int64"), "c": pd.Series(dtype="float64")})
    c = bars["c"].to_numpy(np.float64)
    return bars[np.isfinite(c) & (c > 0)]


@dataclass(frozen=True)
class FundingSchedule:
    """One instance's funding records known by ``known_by`` (``pit_funding``), sorted by ``funding_ts``."""

    ts: np.ndarray
    rate: np.ndarray
    nom: np.ndarray
    invalid: np.ndarray
    raw_marks: dict[int, object]
    m15: pd.DataFrame
    l15: pd.DataFrame

    def mark(self, i: int) -> float | None:
        """S-4.6 mark at settlement ``ts[i]``: record -> mark 15m close -> last-price 15m close."""
        k = int(self.ts[i])
        return pos(mark_at_settle({"mark_at_settle": pos(self.raw_marks.get(k))}, self.m15, self.l15, k))

    def usable(self, i: int) -> bool:
        """A valid record (S-2.6) with a finite rate."""
        return bool(not self.invalid[i] and np.isfinite(self.rate[i]))

    def check(self, lo: int, hi: int) -> str | None:
        """The first problem of the settlements in (lo, hi], or None (S-5.5, DEC-29, S-2.6, S-4.6).

        Missing settlement (gap rule incl. head = last record <= lo and tail up to hi) -> funding_missing;
        an invalid record or a non-finite rate -> funding_invalid; no usable mark -> mark_missing."""
        ts, nom = self.ts, self.nom
        if len(ts) == 0:
            return "funding_missing"
        p = int(np.searchsorted(ts, lo, "right")) - 1
        q = int(np.searchsorted(ts, hi, "right"))
        if p < 0:
            return "funding_missing"  # no head record: a missing settlement cannot be excluded
        for i in range(p + 1, q):
            if u3_gap(nom[i - 1], nom[i], int(ts[i] - ts[i - 1])):
                return "funding_missing"
        if u3_gap(nom[q - 1], nom[q - 1], int(hi - ts[q - 1])):
            return "funding_missing"
        if self.invalid[p + 1:q].any() or not np.isfinite(self.rate[p + 1:q]).all():
            return "funding_invalid"
        for i in range(p + 1, q):
            if self.mark(i) is None:
                return "mark_missing"
        return None

    def window_sum(self, lo: int, hi: int) -> float:
        """Sum of r_k * M(tau_k) over settlements in (lo, hi]; raises ``FundingProblem``."""
        why = self.check(lo, hi)
        if why is not None:
            raise FundingProblem(why)
        p = int(np.searchsorted(self.ts, lo, "right"))
        q = int(np.searchsorted(self.ts, hi, "right"))
        return float(sum(float(self.rate[i]) * self.mark(i) for i in range(p, q)))  # type: ignore[operator]


def funding_schedule(x: InstView, known_by: int) -> FundingSchedule:
    """The instance's funding records with ``known_at`` <= ``known_by`` (instance start applied)."""
    f = x.funding
    fts = ns_array(f["funding_ts"], "funding_ts") if len(f) else np.empty(0, np.int64)
    if np.unique(fts).size != fts.size:
        raise ValueError("funding: duplicate funding_ts")
    a = pit_funding(f, x.inst, known_by + MIN)
    marks: dict[int, object] = {}
    if "mark_at_settle" in f.columns:
        marks = dict(zip(fts.tolist(), f["mark_at_settle"].tolist(), strict=True))
    if len(a) == 0:
        e = np.empty(0)
        return FundingSchedule(np.empty(0, np.int64), e, e, np.empty(0, bool), marks,
                               positive_close(x.m15), positive_close(x.b15))
    return FundingSchedule(
        ns_array(a["funding_ts"], "funding_ts"),
        a["rate"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
        a["nominal_h"].astype("Float64").to_numpy(dtype=float, na_value=np.nan),
        a["invalid"].to_numpy(bool),
        marks,
        positive_close(x.m15),
        positive_close(x.b15),
    )


# ---- verbatim from luna/v7/features.py @ b0fbd3a

def _instance_rows(df: pd.DataFrame, cur: Instance, t_ns: int, name: str) -> pd.DataFrame:
    """PIT rows (``known_at <= t - 1 min``) of the current instance (``open_ts >= first_bar_ts``)."""
    df = pit_rows(df, t_ns, name)
    if len(df):
        df = df[ns_array(df["open_ts"], f"{name}.open_ts") >= cur.first_bar_ts]
    return df.sort_values("open_ts")


def _premium(r4: R4Bars, cur: Instance, t_ns: int) -> dict[str, float]:
    """F5 per bar: premium-index close; where it is missing, (mark - index) / index of the same bar."""
    p = _instance_rows(r4.premium, cur, t_ns, "premium")
    m = _instance_rows(r4.mark, cur, t_ns, "mark")
    i = _instance_rows(r4.index, cur, t_ns, "index")
    ps = pd.Series(p["c"].to_numpy(np.float64), index=ns_array(p["open_ts"], "premium.open_ts"))
    ps = ps[np.isfinite(ps.to_numpy())]
    ms = pd.Series(m["c"].to_numpy(np.float64), index=ns_array(m["open_ts"], "mark.open_ts"))
    ix = pd.Series(i["c"].to_numpy(np.float64), index=ns_array(i["open_ts"], "index.open_ts"))
    joined = pd.concat([ms.rename("m"), ix.rename("i")], axis=1, join="inner")
    ok = np.isfinite(joined["m"]) & np.isfinite(joined["i"]) & (joined["i"] > 0)
    fb = ((joined["m"] - joined["i"]) / joined["i"])[ok]
    s = pd.concat([ps, fb[~fb.index.isin(ps.index)]]).sort_index()
    close = s.index.to_numpy(np.int64) + BAR_15M
    v = s.to_numpy(np.float64)
    w8, w24 = close > t_ns - 8 * H, close > t_ns - 24 * H
    return dict(
        premium_mean_8h=_mean(v[w8]),
        premium_mean_24h=_mean(v[w24]),
        premium_last=float(v[w24][-1]) if w24.any() else NAN,
    )


def _symbol(sd: SymbolData, r4: R4Bars, t_ns: int, xs_only: bool) -> tuple[dict[str, float], dict]:
    """Raw (unsigned) symbol features and the PIT frames used. No current instance -> {} (all missing)."""
    used: dict[str, pd.DataFrame] = {}
    cur = current_instance(sd.insts, t_ns)
    if cur is None:
        for name, df in (("bars15", sd.bars15), ("premium", r4.premium), ("mark", r4.mark),
                         ("index", r4.index), ("funding", sd.funding_annot)):
            pit_rows(df, t_ns, f"{sd.symbol}.{name}")  # inputs are still validated (known_at required)
        return {}, used
    fund = pit_funding(sd.funding_annot, cur, t_ns)
    used["funding"] = fund
    out = _funding_feats(fund, t_ns)
    out.update(_premium(r4, cur, t_ns))
    used.update({k: _instance_rows(getattr(r4, k), cur, t_ns, k) for k in ("premium", "mark", "index")})
    b15 = _instance_rows(sd.bars15, cur, t_ns, f"{sd.symbol}.bars15")
    used["bars15"] = b15
    if xs_only:
        return out, used
    h0 = half_spread(b15, t_ns) if len(b15) else None
    recent = b15[ns_array(b15["open_ts"], "open_ts") >= t_ns - 8 * D] if len(b15) else b15
    hs = [half_spread(recent, t_ns - k * F6_STEP) if len(recent) else None for k in range(F6_SAMPLES)]
    hs = [x for x in hs if x is not None]
    out["f6_half_spread"] = _f(h0)
    out["f6_half_spread_median_7d"] = float(np.median(hs)) if hs else NAN
    med, _ = median_daily_quote_volume(b15, t_ns, 30) if len(b15) else (0.0, None)
    out["f8_log_median_quote_vol_30d"] = math.log(med) if med > 0 else NAN
    hc = _hourly(b15)
    d_t = t_ns // D
    if len(b15):
        q, _ = daily_sums(b15, t_ns, 7, "vol_quote")
        am = []
        for j, d in enumerate(range(d_t - 7, d_t)):
            a, b = hc.get((d + 1) * D), hc.get(d * D)
            if a is not None and b is not None and q[j] > 0:
                am.append(abs(a / b - 1.0) / q[j])
        out["f8_amihud_7d"] = _mean(np.array(am))
        if "n_trades" in b15.columns:
            n, _ = daily_sums(b15, t_ns, 7, "n_trades")
            out["f8_log_trades_per_day_7d"] = math.log(n.sum() / 7) if n.sum() > 0 else NAN
    out["f9_rvol_24h"] = _rvol(hc, t_ns, D)
    out["f9_rvol_7d"] = _rvol(hc, t_ns, 7 * D)
    for name, w in (("f10_ret_24h", D), ("f10_ret_7d", 7 * D), ("f10_ret_30d", 30 * D)):
        out[name] = _ret(hc, t_ns, w)
    out["_h0"] = _f(h0)
    return out, used


def xs_input(sd: SymbolData, r4: R4Bars, t_ns: int) -> XSInput:
    """Gates and raw cross-sectional values (FL1, FL7, FL30, F5) of one symbol at t, for F11 / F12."""
    t_ns = int(t_ns)
    g = gates(sd, t_ns)
    vals, used = _symbol(sd, r4, t_ns, xs_only=True)
    ka = assert_pit(used, t_ns)
    ka = max((x for x in (ka, g.max_known_at) if x is not None), default=None)
    return XSInput(sd.symbol, t_ns, g, {k: vals.get(k, NAN) for k in XS_KEYS}, ka)


def _cross_section(rows: Sequence[XSInput], t_ns: int, symbol: str, own: dict[str, float]) -> dict:
    seen: set[str] = set()
    for r in rows:
        if r.t_ns != t_ns:
            raise ValueError(f"universe row {r.symbol} built at {r.t_ns}, not at t = {t_ns}")
        if r.symbol in seen:
            raise ValueError(f"duplicate universe row {r.symbol}")
        seen.add(r.symbol)
        if r.max_known_at is not None and r.max_known_at > t_ns - MIN:
            raise LeakageError(f"universe row {r.symbol}: max_known_at > t - 1 min")
    el = {r.symbol: r.values for r in rows if eligible(r.gate)}
    fl1 = np.array([v["fl1"] for v in el.values()], np.float64)
    fl1 = fl1[np.isfinite(fl1)]
    out = dict(
        f11_xs_median_fl1=float(np.median(fl1)) if len(fl1) else NAN,
        f11_xs_share_fl1_pos=float(np.mean(fl1 > 0)) if len(fl1) else NAN,
    )
    for k in XS_KEYS:
        x = own.get(k, NAN)
        vals = {s: v[k] for s, v in el.items()}
        vals[symbol] = x  # the candidate's own value (identical when it is eligible)
        v = np.array([y for y in vals.values() if math.isfinite(y)], np.float64)
        if not math.isfinite(x):
            out[f"f12_rank_{k}"] = NAN
            continue
        less, eq = float(np.sum(v < x)), float(np.sum(v == x))
        out[f"f12_rank_{k}"] = (less + (eq + 1.0) / 2.0) / len(v)  # average-rank percentile
    return out


def compute_rows(
    sd: SymbolData, btc: SymbolData, universe_rows: Sequence[XSInput], t_ns: int, sides: Sequence[int], *,
    r4: R4Bars
) -> dict[int, FeatureRow]:
    """``compute_row`` for several sides of one symbol at t: the side-independent values (symbol, BTC, cross
    section) are computed once and side-adjusted per side (Task 19b speed-up; identical values)."""
    sides = tuple(sides)
    for side in sides:
        if side not in (1, -1):
            raise ValueError(f"side must be +1 or -1, got {side!r}")
    t_ns = int(t_ns)
    raw, used = _symbol(sd, r4, t_ns, xs_only=False)
    v: dict[str, float] = {}
    for k in ("fl1", "fl3", "fl7", "fl30"):
        v[f"f1_{k}"] = raw.get(k, NAN)
    for k in ("premium_mean_8h", "premium_mean_24h", "premium_last"):
        v[f"f5_{k}"] = raw.get(k, NAN)
    for c in FEATURES:
        if c in raw:
            v[c] = raw[c]
    # BTC (hedge instrument): its own current instance, PIT
    bcur = current_instance(btc.insts, t_ns)
    bb = _instance_rows(btc.bars15, bcur, t_ns, "btc.bars15") if bcur is not None else btc.bars15.iloc[0:0]
    if bcur is None:
        pit_rows(btc.bars15, t_ns, "btc.bars15")
    used["btc_bars15"] = bb
    bhc = _hourly(bb)
    v["f11_btc_ret_30d"] = _ret(bhc, t_ns, 30 * D)
    v["f11_btc_rvol_7d"] = _rvol(bhc, t_ns, 7 * D)
    resid = NAN
    ab = used.get("bars15")  # the asset's current-instance PIT bars (absent without a current instance)
    if ab is not None and len(ab) and len(bb):
        beta = beta_hat(_hourly(ab), bhc, t_ns, hourly_known_at_from_15m(ab), hourly_known_at_from_15m(bb))
        ra, rb = raw.get("f10_ret_7d", NAN), _ret(bhc, t_ns, 7 * D)
        if beta is not None:
            resid = ra - beta * rb
    v["f10_resid_ret_7d"] = _f(resid)
    v.update(_cross_section(universe_rows, t_ns, sd.symbol, raw))
    v["f13_rvol_over_carry"] = _capped_ratio(
        raw.get("f9_rvol_7d", NAN), abs(raw.get("fl7", NAN)) * F.f13_settlements_per_year
    )
    v["f13_r8_std_over_fl30"] = _capped_ratio(raw.get("r8_std_30d", NAN), abs(raw.get("fl30", NAN)))
    c = FROZEN.costs
    cost = 2.0 * FROZEN.fees.taker + 2.0 * raw.get("_h0", NAN) + 2.0 * c.slip  # S-4.3 taker, S-4.4/4.5
    v["f14_cost_bps"] = cost * 1e4
    for h, col in zip(FROZEN.horizons_h, F14_COLS, strict=True):
        v[col] = abs(raw.get("fl7", NAN)) * 3.0 * h / 24.0 / cost  # same units (fractions): unitless
    ka = assert_pit(used, t_ns)
    xs_ka = [r.max_known_at for r in universe_rows if r.max_known_at is not None]
    ka = max([x for x in (ka, *xs_ka) if x is not None], default=None)
    rows: dict[int, FeatureRow] = {}
    for side in sides:
        vs = {**v, "side": float(side)}
        out = {k: (-side * vs.get(k, NAN) if k in CARRY_DIRECTION else vs.get(k, NAN)) for k in FEATURES}
        rows[side] = FeatureRow({k: _f(x) for k, x in out.items()}, ka)
    return rows


# ---- verbatim from luna/v7/labels.py @ b0fbd3a

def _restrict(d: LegData, cur: Instance, w_end: int) -> InstView:
    return restrict(d.bars1m, d.bars15, d.mark15, d.funding, cur, w_end)


def _beta(leg: InstView, btc: InstView, t: int) -> float | None:
    a, b = leg.b15, btc.b15
    return beta_hat(hourly_close_from_15m(a), hourly_close_from_15m(b), t,
                    hourly_known_at_from_15m(a), hourly_known_at_from_15m(b))


def _funding_sum(x: InstView, lo: int, hi: int, known_by: int) -> float:
    """Sum of r_k * M(tau_k) over settlements tau_k in (lo, hi] of the instance (S-4.6, S-5.3, S-5.5); the
    missing / invalid / mark rules are ``refs.FundingSchedule.check``. Records count only when ``known_at``
    <= ``known_by``."""
    try:
        return funding_schedule(x, known_by).window_sum(lo, hi)
    except FundingProblem as e:
        raise _Invalid(e.reason) from None


def leg_label(inputs: LabelInputs, t: int, side: int, h: int) -> LabelResult | None:
    """S-5.3 / S-5.4 leg label for decision t, side s in {+1, -1}, horizon h (hours); see module docstring."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side!r}")
    if h not in FROZEN.horizons_h:
        raise ValueError(f"h must be one of {FROZEN.horizons_h}, got {h!r}")
    w_lo, w_end = int(inputs.window[0]), int(inputs.window[1])
    t = int(t)
    if not (w_lo <= t < w_end):
        raise ValueError("decision t outside its window")
    t_e = t + FROZEN.decision.fill_delay_min * MIN
    t_x = t_e + h * H

    def check(hi: int) -> None:
        guard.check_outcome_window(t, min(hi, w_end), auth=inputs.auth, now_ns=inputs.now_ns)

    if t_x >= w_end:  # S-1.5 / S-5.4: no label; the guard still sees the call (no data is read)
        check(w_end)
        return None
    check(t_x + MIN)
    base = dict(t=t, symbol=inputs.symbol, side=side, h=h)
    try:
        return _compute(inputs, t, side, h, t_e, t_x, w_end, check, base)
    except _Invalid as e:
        tx = t_x if e.t_x_eff is None else e.t_x_eff
        return LabelResult(**base, components={}, net=math.nan, y_reg_bps=math.nan, y_pos=None,
                           t_x_eff=tx, label_known_at=tx + MIN, invalid_reason=e.reason)


def _compute(inputs: LabelInputs, t: int, s: int, h: int, t_e: int, t_x: int, w_end: int, check,
             base: dict) -> LabelResult:
    flags: list[str] = []
    cur = current_instance(inputs.leg.insts, t)
    cur_b = current_instance(inputs.btc.insts, t)
    if cur is None or cur_b is None:
        raise _Invalid("no_instance")
    leg = _restrict(inputs.leg, cur, w_end)
    btc = _restrict(inputs.btc, cur_b, w_end)

    # --- entry (S-5.4 entry while halted, data gap; S-4.4 entry proxy) -----------------------------------
    if halted(leg.b1, t_e):
        raise _Invalid("entry_halted")
    if halted(btc.b1, t_e):
        raise _Invalid("btc_entry_halted")
    pe = ref_price(leg, t_e)
    be = ref_price(btc, t_e)
    if pe is None or be is None:
        raise _Invalid("entry_price_missing")
    P_e, B_e = pe[0], be[0]
    if pe[1]:
        flags.append("entry_gap_price")
    if be[1]:
        flags.append("btc_entry_gap_price")
    ha_e = half_spread(leg.b15, t_e)
    hb_e = half_spread(btc.b15, t_e)
    if ha_e is None or hb_e is None:
        raise _Invalid("entry_proxy_missing")
    beta = _beta(leg, btc, t)
    if beta is None:
        raise _Invalid("beta_missing")

    # --- exit (S-5.4) ------------------------------------------------------------------------------------
    ceased = cur.last_trade_ts is not None and cur.last_trade_ts <= t_x
    terminal = False
    if not ceased and halted(leg.b1, t_x):
        check(w_end)  # the wait may read up to the window end
        fx = first_executable_after(trim(leg.b1, "open_ts", None, w_end - MIN), t_x, cur)  # fill < end
        if fx is not None:
            t_xe, P_x = fx
            hf = half_spread(leg.b15, t_xe)
            hx, sx = max(hf if hf is not None else 0.0, _FB_H), _FB_SLIP
            flags.append("halted_exit")
            if hf is None:
                flags.append("exit_proxy_fallback")
        elif cur.last_trade_ts is not None and cur.last_trade_ts < w_end:
            terminal = True  # cessation reached first while waiting
        else:
            raise _Invalid("no_executable_exit")
    elif ceased:
        terminal = True
    else:
        t_xe = t_x
        px = ref_price(leg, t_x, lambda: check(t_x + H + MIN))
        if px is None:
            raise _Invalid("exit_price_missing")
        if px[1]:
            flags.append("exit_gap_price")
        P_x = px[0]
        hf = half_spread(leg.b15, t_x)
        if hf is None:
            hx, sx = _FB_H, _FB_SLIP
            flags.append("exit_proxy_fallback")
        else:
            hx, sx = hf, _SLIP
    if terminal:
        ev = inputs.leg.terminal
        if ev is None or ev.instance.idx != cur.idx or ev.instance.first_bar_ts != cur.first_bar_ts:
            raise ValueError("the label reaches its instance's cessation: the classified TerminalEvent of "
                             "that instance is required")
        if ev.klass != "A":
            raise _Invalid("terminal_not_reconstructible")
        sp = pos(ev.settle_price)
        if ev.settle_ts is None or sp is None:  # an A event without exact time / price: fail closed
            raise _Invalid("terminal_not_reconstructible")
        st = int(ev.settle_ts)
        if st <= t_e:
            raise _Invalid("terminal_at_or_before_entry", st)
        if st >= w_end:
            raise _Invalid("no_executable_exit")
        check(st + MIN)
        t_xe, P_x, hx, sx = st, sp, 0.0, 0.0
        flags.append("terminal_A")

    # --- pro-forma BTC hedge exit at the same time --------------------------------------------------------
    if halted(btc.b1, t_xe):
        raise _Invalid("btc_exit_halted", t_xe)
    bx = ref_price(btc, t_xe, lambda: check(t_xe + H + MIN))
    if bx is None:
        raise _Invalid("exit_price_missing", t_xe)
    if bx[1]:
        flags.append("btc_exit_gap_price")
    B_x = bx[0]
    hbf = half_spread(btc.b15, t_xe)
    if hbf is None:
        hbx, sbx = _FB_H, _FB_SLIP
        flags.append("btc_exit_proxy_fallback")
    else:
        hbx, sbx = hbf, _SLIP

    # --- funding (S-4.6, S-5.5) ---------------------------------------------------------------------------
    known_by = t_xe + MIN  # S-5.6 label_known_at
    try:
        f_leg = _funding_sum(leg, t_e, t_xe, known_by)
        f_btc = _funding_sum(btc, t_e, t_xe, known_by)
    except _Invalid as e:
        raise _Invalid(e.reason, t_xe) from None

    # --- S-5.3 components ---------------------------------------------------------------------------------
    q = 1.0 / P_e
    qb = beta / B_e
    E = P_e * (1 + s * (ha_e + _SLIP))
    X = P_x * (1 - s * (hx + sx))
    E_B = B_e * (1 - s * (hb_e + _SLIP))
    X_B = B_x * (1 + s * (hbx + sbx))
    comp = {
        "funding_leg": -s * q * f_leg,
        "price_resid": s * ((P_x / P_e - 1) - beta * (B_x / B_e - 1)),
        "fee_in": _F * q * E,
        "fee_out": _F * q * X,
        "ss_in": q * P_e * (ha_e + _SLIP),
        "ss_out": q * P_x * (hx + sx),
        "hedge_cost": _F * qb * E_B + _F * qb * X_B + qb * B_e * (hb_e + _SLIP) + qb * B_x * (hbx + sbx),
        "hedge_funding": s * qb * f_btc,
    }
    net = (comp["funding_leg"] + comp["price_resid"] - comp["fee_in"] - comp["fee_out"] - comp["ss_in"]
           - comp["ss_out"] - comp["hedge_cost"] + comp["hedge_funding"])
    return LabelResult(**base, components=comp, net=net, y_reg_bps=net * 1e4, y_pos=int(net > 0),
                       t_x_eff=t_xe, label_known_at=t_xe + MIN, invalid_reason=None, beta=beta,
                       flags=tuple(flags))


# ---- verbatim from luna/v7/runner.py @ b0fbd3a

def _instance_bars_reference(sd: SymbolData, cur: Instance, t: int) -> pd.DataFrame:
    """The boolean definition of the PIT current-instance bars (``universe.pit_rows`` + instance start)."""
    b = pit_rows(sd.bars15, int(t), f"{sd.symbol}.bars15")
    return b[ns_array(b["open_ts"], "open_ts") >= cur.first_bar_ts]


class DecisionProvider:
    """Point-in-time ``DecisionView`` per decision t (see module docstring). ``with_features`` adds the §9
    features to the candidate rows (needed by the ML arms and the labels; A0 needs only its own inputs).

    Memory: the per-(symbol, t) bar slices are kept for the CURRENT t only (a new t evicts them) and are
    positional slices (no copy of the history) when the bars are sorted by ``open_ts`` with non-decreasing
    ``known_at`` (else the boolean reference path). Views and records are an LRU of at most
    ``max_cached_decisions`` decisions (None = unbounded); an evicted decision is recomputed on demand
    (deterministic), so the bound trades memory for recomputation (the S-10.6 driver replays the same
    decisions for each of its books).

    Guard (SC-03): ``auth`` is the hardened ``HoldoutAuthorization`` (None before the opening); a K9 log must
    be configured. The universe's data span is computed and checked at construction; that span (the inputs
    are not copied: callers must not mutate them afterwards) and the decision time t are checked again on
    every call and every public per-t method (``instance_bars``, ``h_hat``, ``beta``, ``qv24``,
    ``stale_reasons``, ``xs``), cached or not, so a W_HOLD t / W_HOLD input is refused before the opening and
    M6 until R_V6 (K9-logged), and an authorization invalidated later (verdict) stops serving."""

    def __init__(self, u: ResearchUniverse, with_features: bool = True,
                 max_cached_decisions: int | None = 2048, *,
                 auth: guard.HoldoutAuthorization | None = None) -> None:
        if max_cached_decisions is not None and int(max_cached_decisions) < 1:
            raise ValueError("max_cached_decisions must be >= 1 or None")
        k9.require()
        self._span = u.check_data(auth)
        self.auth = auth
        self.u = u
        self.with_features = bool(with_features)
        self.max_cached = None if max_cached_decisions is None else int(max_cached_decisions)
        self._views: OrderedDict[int, DecisionView] = OrderedDict()
        self._records: OrderedDict[int, DecisionRecord] = OrderedDict()
        self._bars: dict[str, pd.DataFrame | None] = {}
        self._bars_t: int | None = None
        self._pos: dict[str, tuple[bool, np.ndarray, np.ndarray]] = {}

    def _positions(self, sym: str) -> tuple[bool, np.ndarray, np.ndarray]:
        """(sliceable, open_ts, known_at) of the symbol's 15m bars, validated once."""
        if sym not in self._pos:
            b = self.u.sd[sym].bars15
            ot = ns_array(b["open_ts"], f"{sym}.bars15.open_ts") if len(b) else np.empty(0, np.int64)
            ka = ns_array(b["known_at"], f"{sym}.bars15.known_at") if len(b) else np.empty(0, np.int64)
            ok = bool(len(b) == 0 or ((np.diff(ot) >= 0).all() and (np.diff(ka) >= 0).all()))
            self._pos[sym] = (ok, ot, ka)
        return self._pos[sym]

    # per (symbol, t) inputs
    def _instance_bars(self, sym: str, t: int) -> pd.DataFrame | None:
        """PIT 15m bars (known_at <= t - 1 min) of the current instance at t; None without one."""
        t = int(t)
        if self._bars_t != t:
            self._bars, self._bars_t = {}, t
        if sym not in self._bars:
            self._bars[sym] = self._slice(sym, t)
        return self._bars[sym]

    def _slice(self, sym: str, t: int) -> pd.DataFrame | None:
        sd = self.u.sd[sym]
        cur = current_instance(sd.insts, t)
        if cur is None:
            return None
        ok, ot, ka = self._positions(sym)
        if ok:
            lo = int(np.searchsorted(ot, cur.first_bar_ts, "left"))
            hi = int(np.searchsorted(ka, t - MIN, "right"))
            return sd.bars15.iloc[lo:max(lo, hi)]
        return _instance_bars_reference(sd, cur, t)

    def _h_hat(self, sym: str, t: int) -> float | None:
        """S-4.4 decision-time h-hat(t): the S-4.5 proxy at t on the current instance (None = missing)."""
        b = self._instance_bars(sym, t)
        return None if b is None or len(b) == 0 else half_spread(b, int(t))

    def _beta(self, sym: str, t: int) -> float | None:
        """S-5.2 beta-hat(t) against the hedge instrument (current instances, hourly known_at series)."""
        a, b = self._instance_bars(sym, t), self._instance_bars(self.u.hedge, t)
        if a is None or b is None or len(a) == 0 or len(b) == 0:
            return None
        return beta_hat(hourly_close_from_15m(a), hourly_close_from_15m(b), int(t),
                        hourly_known_at_from_15m(a), hourly_known_at_from_15m(b))

    def _qv24(self, sym: str, t: int) -> tuple[float | None, int | None]:
        """S-4.4 trailing 24 h quote volume known at t - 1 min (AMB-25 (a)): the sum of ``vol_quote`` over the
        current instance's 15m bars whose close lies in (t - 24 h - 1 min, t - 1 min]; a non-finite value
        counts 0 (as ``universe.daily_sums``, AMB-07); no such bar -> None (missing). Returns (value, max
        known_at of the bars used)."""
        b = self._instance_bars(sym, t)
        if b is None or len(b) == 0:
            return None, None
        close = ns_array(b["open_ts"], "open_ts") + BAR_15M
        hi = int(t) - MIN
        m = (close > hi - QV24_NS) & (close <= hi)
        if not m.any():
            return None, None
        q = pd.to_numeric(b["vol_quote"], errors="coerce").to_numpy(np.float64)[m]
        return float(np.where(np.isfinite(q), q, 0.0).sum()), _run_max_ka(b[m])

    def _stale_reasons(self, t: int) -> tuple[str, ...]:
        """S-7.3 staleness (AMB-25 (b)): the decision-wide inputs every entry needs are those of the hedge
        instrument (S-7.4: beta_book is hedged at every decision). Stale when the hedge has no current
        instance, its last 15m bar (closing at the last 15-minute boundary <= t - 1 min) is missing /
        unpriced, or its h-hat(t) is missing (S-4.5: a missing proxy is a staleness failure at decisions).
        Per-symbol freshness (U4, U5, U7, U8) is already a gate of that symbol."""
        t = int(t)
        hb = self._instance_bars(self.u.hedge, t)
        if hb is None:
            return ("hedge_no_instance",)
        out: list[str] = []
        last_close = (t - MIN) // BAR_15M * BAR_15M
        ot = ns_array(hb["open_ts"], "open_ts") if len(hb) else np.empty(0, np.int64)
        c = pd.to_numeric(hb["c"], errors="coerce").to_numpy(np.float64) if len(hb) else np.empty(0)
        if not ((ot == last_close - BAR_15M) & np.isfinite(c) & (c > 0)).any():
            out.append("hedge_last_bar_missing")
        if self._h_hat(self.u.hedge, t) is None:
            out.append("hedge_h_hat_missing")
        return tuple(out)

    def _xs(self, t: int) -> list[XSInput]:
        u = self.u
        return [features.xs_input(u.sd[s], u.r4(s), int(t)) for s in u.candidates]

    def _cands(self, t: int, xs: list[XSInput],
               hh: Mapping[str, float | None]) -> tuple[pd.DataFrame, int | None]:
        u = self.u
        rows: list[dict] = []
        ka: int | None = None
        for x in xs:
            if not eligible(x.gate):
                continue
            fr = None
            if self.with_features:
                fr = features.compute_rows(u.sd[x.symbol], u.sd[u.hedge], xs, t, (1, -1), r4=u.r4(x.symbol))
            for side in (1, -1):
                h = hh[x.symbol]  # eligible => U5 => h-hat present; cost_side refuses None (fail closed)
                r: dict = dict(symbol=x.symbol, side=side, gate_ok=True, fl30_r8=x.values["fl30"],
                               cost_side=cost_side(h))  # type: ignore[arg-type]
                mk = x.max_known_at
                if fr is not None:
                    r.update(fr[side].values)
                    r["side"] = side
                    mk = _run_mx(mk, fr[side].max_known_at)
                r["max_known_at"] = mk
                ka = _run_mx(ka, mk)
                rows.append(r)
        return (pd.DataFrame(rows) if rows else pd.DataFrame(columns=CAND_COLS)), ka

    def _guard(self, t: int) -> int:
        """The decision time t and the universe span through ``guard.check_data_span`` (every call)."""
        t = int(t)
        guard.check_data_span(t, t + 1, auth=self.auth, what="decision t")
        if self._span is not None:
            guard.check_data_span(*self._span, auth=self.auth, what=UNIVERSE)
        return t

    # public per-t inputs: guarded, then the private implementation
    def instance_bars(self, sym: str, t: int) -> pd.DataFrame | None:
        return self._instance_bars(sym, self._guard(t))

    def h_hat(self, sym: str, t: int) -> float | None:
        return self._h_hat(sym, self._guard(t))

    def beta(self, sym: str, t: int) -> float | None:
        return self._beta(sym, self._guard(t))

    def qv24(self, sym: str, t: int) -> tuple[float | None, int | None]:
        return self._qv24(sym, self._guard(t))

    def stale_reasons(self, t: int) -> tuple[str, ...]:
        return self._stale_reasons(self._guard(t))

    def xs(self, t: int) -> list[XSInput]:
        return self._xs(self._guard(t))

    def __call__(self, t: int) -> DecisionView:
        t = self._guard(t)
        if t in self._views:
            self._views.move_to_end(t)
            self._records.move_to_end(t)
        else:
            u = self.u
            xs = self._xs(t)
            syms = u.candidates
            hh = {s: self._h_hat(s, t) for s in syms}
            betas = {s: self._beta(s, t) for s in syms}
            qv = {s: self._qv24(s, t) for s in syms}
            stale = self._stale_reasons(t)
            gates = {x.symbol: x.gate for x in xs}
            cands, ka = self._cands(t, xs, hh)
            bars_ka = [_run_max_ka(b) for s in (*syms, u.hedge) if (b := self._instance_bars(s, t)) is not None]
            mk = _run_mx(ka, *(x.max_known_at for x in xs), *(v[1] for v in qv.values()), *bars_ka)
            if mk is not None and mk > t - MIN:
                raise LeakageError(f"decision {t}: an input known at {mk} > t - 1 min")
            vols = {s: v[0] for s, v in qv.items()}
            self._views[t] = DecisionView(t, gates, hh, betas, vols, cands, bool(stale))
            el = tuple(x.symbol for x in xs if eligible(x.gate))
            self._records[t] = DecisionRecord(t, gates, hh, betas, vols, bool(stale), stale, el, len(cands),
                                              mk)
            while self.max_cached is not None and len(self._views) > self.max_cached:
                self._views.popitem(last=False)
                self._records.popitem(last=False)
        return self._views[t]

    def record(self, t: int) -> DecisionRecord:
        self(int(t))
        return self._records[int(t)]


# ---- verbatim from luna/v7/spread.py @ b0fbd3a

def _pairs(bars15: pd.DataFrame, span: tuple[int, int] | None = None
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(open_ts of bar k, pi_k, known_at) arrays over consecutive present bars with valid prices (numpy core).

    ``open_ts`` / ``known_at`` are validated on the whole frame (fail closed: a missing column, a non-finite
    or out-of-range value raises ValueError even on a bar with an invalid price). ``span`` = [lo, hi): only
    bars opening in it enter the products (``half_spread``: every pair of its window has bar k opening >= lo
    and bar k + 1 opening < tau, so trimming first leaves the window's pair set unchanged; performance)."""
    if "known_at" not in bars15.columns:
        raise ValueError("bars15 has no known_at column: point-in-time filtering is impossible")
    t = ns_array(bars15["open_ts"], "open_ts")
    ka = ns_array(bars15["known_at"], "known_at")  # ValueError on missing / non-finite / out-of-range values
    h = bars15["h"].to_numpy(np.float64)
    lo = bars15["l"].to_numpy(np.float64)
    c = bars15["c"].to_numpy(np.float64)
    if span is not None:
        m = (t >= span[0]) & (t < span[1])
        t, ka, h, lo, c = t[m], ka[m], h[m], lo[m], c[m]
    order = np.argsort(t, kind="stable")
    h, lo, c = h[order], lo[order], c[order]
    ok = np.isfinite(h) & np.isfinite(lo) & np.isfinite(c) & (h > 0) & (lo > 0) & (c > 0)
    t, ka, h, lo, c = t[order][ok], ka[order][ok], h[ok], lo[ok], c[ok]
    lc = np.log(c)
    eta = (np.log(h) + np.log(lo)) / 2.0
    consec = (t[1:] - t[:-1]) == BAR_15M
    pi = 4.0 * (lc[:-1] - eta[:-1]) * (lc[:-1] - eta[1:])
    known = np.maximum(ka[1:], t[1:] + BAR_15M)
    return t[:-1][consec], pi[consec], known[consec]


def half_spread(bars15_instance: pd.DataFrame, tau_ns: int) -> float | None:
    """S-4.5 proxy value h(tau) (decision-time or fill-time); None if the pair set is empty."""
    lo = tau_ns - FROZEN.spread.window_ns
    t, pi, known = _pairs(bars15_instance, (lo, tau_ns))
    sel = pi[(t >= lo) & (known <= tau_ns - MIN)]
    if len(sel) == 0:
        return None
    s = float(np.sqrt(max(float(np.mean(sel)), 0.0)))
    return max(FROZEN.spread.lam * s / 2.0, FROZEN.spread.floor)


# ---- verbatim from luna/v7/beta.py @ b0fbd3a

def _pit(close: pd.Series, known_at: pd.Series, t_ns: int) -> pd.Series:
    """Keep prices with known_at <= t - 1 min; a price without a known_at entry is dropped.

    ``known_at`` values are validated with ``timeutil.ns_array`` (ValueError on non-finite / out-of-range).
    """
    if not isinstance(known_at, pd.Series):
        raise TypeError("known_at must be a pandas Series (point-in-time filter is mandatory)")
    known_at = pd.Series(ns_array(known_at, "known_at"), index=known_at.index)
    ka = known_at.reindex(close.index)
    return close[(ka <= t_ns - MIN).to_numpy()]


def _rets(close_1h: pd.Series, hours: np.ndarray) -> np.ndarray:
    cur = close_1h.reindex(hours).to_numpy(np.float64)
    prev = close_1h.reindex(hours - H).to_numpy(np.float64)
    with np.errstate(all="ignore"):
        r = np.log(cur / prev)
    r[~np.isfinite(r)] = np.nan
    return r


def beta_hat(
    asset_close_1h: pd.Series,
    btc_close_1h: pd.Series,
    t_ns: int,
    asset_known_at: pd.Series,
    btc_known_at: pd.Series,
) -> float | None:
    cfg = FROZEN.beta
    asset_close_1h = _pit(asset_close_1h, asset_known_at, t_ns)
    btc_close_1h = _pit(btc_close_1h, btc_known_at, t_ns)
    last = ((t_ns - MIN) // H) * H
    hours = np.arange(last - cfg.days * D + H, last + H, H, dtype=np.int64)
    ra = _rets(asset_close_1h, hours)
    rb = _rets(btc_close_1h, hours)
    ok = np.isfinite(ra) & np.isfinite(rb)
    if int(ok.sum()) < cfg.min_obs:
        return None
    x, y = rb[ok], ra[ok]
    var = float(np.var(x))
    if var <= 0.0:
        return None
    ols = float(np.mean((x - x.mean()) * (y - y.mean())) / var)
    lo, hi = cfg.clip
    return float(min(max(cfg.w * ols + (1.0 - cfg.w) * cfg.prior, lo), hi))


pit_funding = _pit_funding_reference
features = types.SimpleNamespace(xs_input=xs_input, compute_rows=compute_rows)
