"""Leg labels: a scoring measurement per 1 USD of entry notional (spec S-1.5, S-4.2-S-4.6, S-5.2-S-5.6, E10,
DEC-27, DEC-35, DEC-40, DEC-43, L3).

``leg_label(inputs, t, side, h)`` returns a ``LabelResult`` (or ``None`` when ``t_x`` = ``t_e`` + h reaches
the window end, S-1.5). Every call first runs ``guard.check_outcome_window`` on the outcome span it reads, and
again before any read that extends past the scheduled exit (gap search, halted wait, terminal settlement).

Timing: ``t_e`` = t + 15 min; ``t_x`` = ``t_e`` + h. ``t_x_eff`` is ``t_x`` unless
* the instrument is HALTED at ``t_x`` (S-5.4): the exit waits for the first volume > 0 1-minute bar of the
  SAME instance (E10) and fills at that bar's close, at its close time, with stressed costs (half-spread
  max(``h_fill``, 25 bps), 3 x slip); the fill must lie strictly before the window end, else the label is
  invalid;
* the label reaches the instance's cessation (DEC-43): class A settles at the R12 time / price with the
  taker fee and no spread or slippage; class B / C is invalid.
The pro-forma BTC hedge (never executed) exits at ``t_x_eff`` as well. Funding of the leg and of BTC counts
over settlements in (``t_e``, ``t_x_eff``] of each one's own current instance, using records with
``known_at`` <= ``label_known_at`` = ``t_x_eff`` + 1 min (S-5.6).

Data gap (not halted, S-5.4): when P(tau) / B(tau) is missing, the reference price is the open of the first
usable 1-minute bar opening in [tau, tau + 60 min]; the fill TIME stays tau (AMB-10).

All frames are trimmed to timestamps strictly before the window end and to the current instance (E10)
before use; no price is interpolated or invented (a price must be finite and > 0).

Real-scale compute (SC-01 part 2, AMB-38): a ``LegData`` built by ``runner.ResearchUniverse`` carries a
``cache``: its restricted ``refs.InstView`` is built once per (instance, window end), and HALTED, the
reference prices, h-hat, the halted-exit search, beta-hat (``beta.beta_hat_sorted`` on the view's hourly
arrays, built once per instance) and the funding schedule read the view's sorted arrays (``refs.InstIndex``).
Identical results (``tests/v7/test_perf_equivalence.py``).

``invalid_reason`` enum (``INVALID_REASONS``):
  no_instance, entry_halted, btc_entry_halted, entry_price_missing, entry_proxy_missing, beta_missing,
  exit_price_missing, btc_exit_halted, no_executable_exit, terminal_not_reconstructible,
  terminal_at_or_before_entry, funding_missing, funding_invalid, mark_missing.
Interpretations the spec does not fix are logged as AMB-10 in docs/results/v7-impl/AMBIGUITIES.md.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from luna.v7 import guard
from luna.v7.beta import beta_hat, beta_hat_sorted, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.data.terminal import TerminalEvent
from luna.v7.lazy1m import Lazy1m, LazyBars
from luna.v7.refs import (
    FundingProblem,
    InstView,
    funding_schedule,
    pos,
    ref_price,
    restrict,
    x_first_executable_after,
    x_half_spread,
    x_halted,
)
from luna.v7.timeutil import MIN, H

COMPONENTS = ("funding_leg", "price_resid", "fee_in", "fee_out", "ss_in", "ss_out", "hedge_cost",
              "hedge_funding")
INVALID_REASONS = frozenset({
    "no_instance", "entry_halted", "btc_entry_halted", "entry_price_missing", "entry_proxy_missing",
    "beta_missing", "exit_price_missing", "btc_exit_halted", "no_executable_exit",
    "terminal_not_reconstructible", "terminal_at_or_before_entry", "funding_missing", "funding_invalid",
    "mark_missing",
})
FLAGS = frozenset({"halted_exit", "terminal_A", "exit_proxy_fallback", "btc_exit_proxy_fallback",
                   "entry_gap_price", "exit_gap_price", "btc_entry_gap_price", "btc_exit_gap_price"})

_F = FROZEN.fees.taker
_SLIP = FROZEN.costs.slip
_FB_H = FROZEN.costs.fallback_half_spread
_FB_SLIP = FROZEN.costs.fallback_slip_mult * FROZEN.costs.slip


@dataclass(frozen=True)
class LegData:
    """One instrument's raw inputs (the candidate leg, or BTC for the pro-forma hedge).

    bars1m: R5 1m last-price bars (``open_ts``, ``o``, ``c``, ``vol_base``), or a ``lazy1m.Lazy1m`` over them
      (red-team r2 MJ1: read per window, identical results).
    bars15: R3 15m last-price bars (``open_ts``, ``o``, ``h``, ``l``, ``c``, ``vol_base``, ``known_at``).
    mark15: R4 15m mark-price bars (``open_ts``, ``c``) for the S-4.6 mark fallback (may be empty).
    funding: raw funding records (``funding_ts``, ``rate``, ``interval_h_explicit``, ``known_at`` and
      optionally ``mark_at_settle``); one row per ``funding_ts``.
    insts: the symbol's instances; terminal: the classified terminal event of the instance a label may
      reach (required when that instance's cessation is reached, else ignored).
    """

    bars1m: pd.DataFrame | Lazy1m
    bars15: pd.DataFrame | LazyBars
    mark15: pd.DataFrame | LazyBars
    funding: pd.DataFrame
    insts: list[Instance]
    terminal: TerminalEvent | None = None
    # restricted views per (instance, window end) (Task 26b); set by runner.ResearchUniverse.leg_data
    cache: OrderedDict | None = field(default=None, compare=False, repr=False)


VIEW_MEMO_ENTRIES = 8  # restricted views kept per LegData (instance x window end; AMB-38)


@dataclass(frozen=True)
class LabelInputs:
    symbol: str
    leg: LegData
    btc: LegData
    window: tuple[int, int]  # the outcome window [lo, end) the label belongs to (W_DEV, W_HOLD, W_FWD7)
    now_ns: int  # passed to the guard (lifts M6 only once R_V6 is known); M6 itself is the guard's
    # canonical span (windows.m6_cached: hash-checked V6 artefacts read once per process, no git per label)
    auth: guard.HoldoutAuthorization | None = None


@dataclass(frozen=True)
class LabelResult:
    t: int
    symbol: str
    side: int
    h: int
    components: dict[str, float]
    net: float
    y_reg_bps: float
    y_pos: int | None
    t_x_eff: int
    label_known_at: int
    invalid_reason: str | None
    beta: float | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)


class _Invalid(Exception):
    def __init__(self, reason: str, t_x_eff: int | None = None) -> None:
        if reason not in INVALID_REASONS:
            raise ValueError(f"unknown invalid_reason {reason!r}")
        super().__init__(reason)
        self.reason = reason
        self.t_x_eff = t_x_eff


# --- helpers (shared with the executed book: luna.v7.refs) ---------------------------------------------

def _restrict(d: LegData, cur: Instance, w_end: int) -> InstView:
    if d.cache is None:
        return restrict(d.bars1m, d.bars15, d.mark15, d.funding, cur, w_end)
    key = (cur.idx, cur.first_bar_ts, cur.last_trade_ts, cur.end_known_at, int(w_end))
    if key in d.cache:
        d.cache.move_to_end(key)
        return d.cache[key]
    x = restrict(d.bars1m, d.bars15, d.mark15, d.funding, cur, w_end)
    d.cache[key] = x
    while len(d.cache) > VIEW_MEMO_ENTRIES:
        d.cache.popitem(last=False)
    return x


def _beta(leg: InstView, btc: InstView, t: int) -> float | None:
    ia, ib = leg.ix(), btc.ix()
    if ia.ok and ib.ok:  # the views' hourly arrays (built once per view; lazy: the window of t)
        return beta_hat_sorted(ia.beta_arrays(t), ib.beta_arrays(t), t)
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


# --- the label ----------------------------------------------------------------------------------------


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
    if x_halted(leg, t_e):
        raise _Invalid("entry_halted")
    if x_halted(btc, t_e):
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
    ha_e = x_half_spread(leg, t_e)
    hb_e = x_half_spread(btc, t_e)
    if ha_e is None or hb_e is None:
        raise _Invalid("entry_proxy_missing")
    beta = _beta(leg, btc, t)
    if beta is None:
        raise _Invalid("beta_missing")

    # --- exit (S-5.4) ------------------------------------------------------------------------------------
    ceased = cur.last_trade_ts is not None and cur.last_trade_ts <= t_x
    terminal = False
    if not ceased and x_halted(leg, t_x):
        check(w_end)  # the wait may read up to the window end
        fx = x_first_executable_after(leg, t_x, w_end - MIN)  # fill < end
        if fx is not None:
            t_xe, P_x = fx
            hf = x_half_spread(leg, t_xe)
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
        hf = x_half_spread(leg, t_x)
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
    if x_halted(btc, t_xe):
        raise _Invalid("btc_exit_halted", t_xe)
    bx = ref_price(btc, t_xe, lambda: check(t_xe + H + MIN))
    if bx is None:
        raise _Invalid("exit_price_missing", t_xe)
    if bx[1]:
        flags.append("btc_exit_gap_price")
    B_x = bx[0]
    hbf = x_half_spread(btc, t_xe)
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


def invalid_rate(results: Iterable[LabelResult | None]) -> float:
    """Share of existing labels (``None`` = no label, S-1.5, is not counted) that are invalid (S-5.5)."""
    rs = [r for r in results if r is not None]
    if not rs:
        return math.nan
    return sum(r.invalid_reason is not None for r in rs) / len(rs)
