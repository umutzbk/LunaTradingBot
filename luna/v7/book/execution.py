"""Book execution: taker, forced, maker-first, halted-exit and terminal-settlement fills (spec S-4.2, S-4.3,
S-4.4, S-5.3 price rules, S-5.4, S-6.1, S-6.2, S-8.1, E10, DEC-40, DEC-43; AMB-19).

Every function is pure over an ``ExecView`` (one instrument restricted to ONE instance and to timestamps
before the window end, ``refs.restrict``; E10, S-1.5) and returns one of
* ``Fill`` -- booked at its own timestamp (S-8.1); costs are split into fee, spread and slippage, all >= 0,
  charged against ``ref_mid`` (the reference mid of S-8.3: P(tau), the halted bar's close, the official
  settlement price, or the maker limit price);
* ``Pending`` -- an exit due at ``due`` while the instrument is HALTED (S-5.4): it waits for the first
  executable bar of the SAME instance (``halted_exit``), never filled retroactively or synthetically; with
  ``terminal`` set, the instance ceases inside the 60-minute data-gap window before any bar: the position
  reaches its cessation and the terminal rule (DEC-43) settles it;
* ``NoTrade`` -- an entry / increase that cannot trade (halted, price or fill-time proxy missing; S-4.4,
  S-5.4: no order while halted);
* ``RunInvalid`` -- the exact INVALID RUN string (S-5.4, S-13.4; ``INVALID_BTC``: AMB-21 (b), raised by the
  engine; ``INVALID_EXIT_GAP``: a required non-halted exit without any 1-minute bar within the 60-minute
  data-gap window, round-2 override 2 / DK-69).

Data-gap fills (S-5.4, DK-10 approved 2026-10-15): when P(tau) is missing and the price is the open of the
first 1-minute bar within 60 min after tau, the fill is booked at THAT bar's open time (S-8.1 "every fill is
booked at its own timestamp"), with h_fill at that time; under E_M the limit rests from that time.

Prices (S-5.3): a trade in direction d (+1 buy, -1 sell) at reference p with half-spread h and slippage s
fills at ``p * (1 + d (h + s))``; fee = rate x qty x fill price; spread cost = qty p h; slippage = qty p s.
Fill-time half-spread ``h_fill(tau)`` is the S-4.5 proxy at the fill time (S-4.4); a missing proxy blocks an
entry / increase and gives an exit 25 bps + 3 x slip (flag ``proxy_fallback``). Halted exits: the first
volume > 0 1-minute bar of the instance opening >= due, filled at its close at its close time, half-spread
max(h_fill, 25 bps), 3 x slip (flag ``halted_exit``). Terminal A: official time and price, taker fee, no
spread / slippage (flag ``terminal_A``); B / C: ``RunInvalid(INVALID_TERMINAL)``.

Lazy 1-minute access (red-team r2 MJ1, AMB-38 (h)): the view's 1-minute input may be a ``lazy1m.Lazy1m``;
``ExecView.b1(lo, hi)`` then reads the rows of [lo, hi) on demand (the same rows, sorted, as the slice of the
resident sorted frame) and the halted-exit search runs forward window by window
(``ExecView.first_executable``; the first executable bar of disjoint ordered windows is the one of their
union).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Literal

import numpy as np
import pandas as pd

from luna.v7 import lazy1m
from luna.v7.book import EPS
from luna.v7.constants import FROZEN
from luna.v7.data.terminal import TerminalEvent
from luna.v7.lazy1m import Lazy1m
from luna.v7.market import P, first_bar_within, first_executable_after, halted
from luna.v7.refs import InstView, positive_close, ref_price, trim, usable_open
from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, H, ns_array

TAKER, MAKER = FROZEN.fees.taker, FROZEN.fees.maker
SLIP = FROZEN.costs.slip
FB_H = FROZEN.costs.fallback_half_spread
FB_SLIP = FROZEN.costs.fallback_slip_mult * FROZEN.costs.slip
MAKER_WINDOW = FROZEN.execution.maker_window_min * MIN  # S-6.2 W
FORCED_DELAY = FROZEN.execution.forced_delay_min * MIN  # S-4.2: first 1-minute bar after trigger + 1 min
ADVERSE_MIN = FROZEN.execution.adverse_min  # S-6.2 reported adverse mid move horizons
BAR_15M = 15 * MIN

INVALID_TERMINAL = "INVALID RUN — terminal settlement not reconstructible"  # S-5.4 / S-13.4 exact text
INVALID_NO_EXIT = "INVALID RUN — no executable exit before the window's terminal unwind"  # S-5.4 (AMB-19)
# AMB-21 (b), user decision 2026-10-15: a BTC price required at that timestamp (hedge construction, hedge
# marking, canonical section 8 accounting B(e)) is missing; never a stale / interpolated BTC price
INVALID_BTC = "INVALID RUN — required BTC reference price missing"
# round-2 override 2 (DK-69, user decision 2026-10-15): a required exit of a NON-HALTED instrument with no
# executable 1-minute bar within the frozen 60-minute data-gap window (S-5.4); never carried to a later bar
INVALID_EXIT_GAP = "INVALID RUN — required exit without a reference bar within the 60-minute data-gap window"
# round-2 override 3 (DK-70, user decision 2026-10-15): a held leg with neither the mark-price nor the
# last-price 15-minute close at a tick (S-8.1, S-4.6 chain); no previous-event stale carry
INVALID_MARK = "INVALID RUN — required leg mark missing"

Liquidity = Literal["taker", "maker", "settlement"]


@dataclass(frozen=True)
class Trade:
    """An executable trade on one instrument instance. ``direction``: +1 buy, -1 sell. ``qty``: base quantity
    (book units, > 0), or None for an entry sized at the fill as ``notional / ref price`` (S-5.3 q = 1 /
    P(t_e) per unit). ``opening``: increases exposure (entry, hedge increase): needs a price and the
    fill-time proxy, never a fallback (S-4.4)."""

    symbol: str
    direction: int
    qty: float | None
    notional: float | None
    opening: bool
    reason: str
    position_id: str

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty / notional")
        v = self.qty if self.qty is not None else self.notional
        if not (v is not None and math.isfinite(v) and v > 0):
            raise ValueError(f"trade size must be finite and > 0, got {v!r}")


@dataclass(frozen=True)
class Fill:
    ts: int
    symbol: str
    instance: int
    direction: int  # +1 buy, -1 sell
    qty: float  # > 0
    ref_mid: float  # S-8.3 p_j(f)
    price: float
    fee: float
    spread_cost: float
    slip_cost: float
    liquidity: Liquidity
    reason: str
    position_id: str
    flags: tuple[str, ...] = ()
    adverse: tuple[float | None, ...] | None = None  # maker fills: adverse mid move at 5 / 30 / 60 min
    beta: float | None = None  # entries: beta_hat at the opening decision (S-8.3 beta_episode)
    btc_ref: float | None = None  # B(ts): BTC reference mid at the fill (S-8.3 mkt_c)

    @property
    def dq(self) -> float:
        return self.direction * self.qty


@dataclass(frozen=True)
class Pending:
    trade: Trade
    due: int  # S-5.4: the wait starts at the time the exit became due
    terminal: bool = False  # no bar before the instance's cessation inside the gap window: terminal rule


@dataclass(frozen=True)
class NoTrade:
    trade: Trade
    ts: int
    reason: str  # halted | price_missing | proxy_missing


@dataclass(frozen=True)
class RunInvalid:
    reason: str  # INVALID_TERMINAL | INVALID_NO_EXIT | INVALID_BTC | INVALID_EXIT_GAP | INVALID_MARK
    symbol: str
    instance: int
    ts: int
    detail: str = ""


Outcome = Fill | Pending | NoTrade | RunInvalid  # RunInvalid: a required exit only (DK-69)


@dataclass(frozen=True)
class ExecView:
    """``InstView`` (one instance, before the window end) with sorted frames and fast time slicing; every
    rule still runs through ``market`` / ``spread`` / ``refs`` on the sliced frames."""

    symbol: str
    x: InstView
    trades: pd.DataFrame | None = None  # aggTrades (ts, price, qty), E_M only

    @staticmethod
    def build(symbol: str, x: InstView, aggtrades: pd.DataFrame | None = None) -> ExecView:
        s = {k: getattr(x, k).sort_values("open_ts", kind="mergesort").reset_index(drop=True)
             for k in ("b1", "b15", "m15") if not isinstance(getattr(x, k), Lazy1m)}  # lazy: sorted reads
        tr = None
        if aggtrades is not None:
            hi = x.end if x.inst.last_trade_ts is None else min(x.end, x.inst.last_trade_ts)
            tr = trim(aggtrades, "ts", x.inst.first_bar_ts, hi)
            tr = tr.sort_values("ts", kind="mergesort").reset_index(drop=True)
        return ExecView(symbol, replace(x, **s), tr)

    @property
    def inst(self):
        return self.x.inst

    @cached_property
    def _t1(self) -> np.ndarray:
        return ns_array(self.x.b1["open_ts"], "open_ts") if len(self.x.b1) else np.empty(0, np.int64)

    @cached_property
    def _t15(self) -> np.ndarray:
        return ns_array(self.x.b15["open_ts"], "open_ts") if len(self.x.b15) else np.empty(0, np.int64)

    @cached_property
    def _tm(self) -> np.ndarray:
        return ns_array(self.x.m15["open_ts"], "open_ts") if len(self.x.m15) else np.empty(0, np.int64)

    @staticmethod
    def _cut(df: pd.DataFrame, t: np.ndarray, lo: int, hi: int) -> pd.DataFrame:
        a, b = int(np.searchsorted(t, lo, "left")), int(np.searchsorted(t, hi, "left"))
        return df.iloc[a:b]

    def b1(self, lo: int, hi: int) -> pd.DataFrame:
        if isinstance(self.x.b1, Lazy1m):
            return self.x.b1.frame(lo, hi)
        return self._cut(self.x.b1, self._t1, lo, hi)

    def first_executable(self, due: int, hi: int) -> tuple[int, float] | None:
        """``market.first_executable_after(self.b1(due, hi), due, inst)``; lazy: window by window
        (``lazy1m.SCAN_STEP_NS``), stopping at the first window with an executable bar."""
        if not isinstance(self.x.b1, Lazy1m):
            return first_executable_after(self.b1(due, hi), due, self.inst)
        s = int(due)
        while s < hi:
            e = min(int(hi), s + lazy1m.SCAN_STEP_NS)
            fx = first_executable_after(self.b1(s, e), due, self.inst)
            if fx is not None:
                return fx
            s = e
        return None

    def b15(self, lo: int, hi: int) -> pd.DataFrame:
        return self._cut(self.x.b15, self._t15, lo, hi)

    def m15(self, lo: int, hi: int) -> pd.DataFrame:
        return self._cut(self.x.m15, self._tm, lo, hi)

    @cached_property
    def _tt(self) -> np.ndarray:
        t = self.trades
        return ns_array(t["ts"], "ts") if t is not None and len(t) else np.empty(0, np.int64)

    def trades_in(self, lo: int, hi: int) -> pd.DataFrame:
        """aggTrades with lo <= ts < hi (sorted, searchsorted slice)."""
        if self.trades is None:
            raise ValueError(f"no aggTrades for {self.symbol}")
        return self._cut(self.trades, self._tt, lo, hi)

    def halted(self, tau: int) -> bool:
        if isinstance(self.x.b1, Lazy1m):  # the same rows' rule on arrays (``refs.InstIndex.halted``)
            return self.x.b1.halted(tau)
        return halted(self.b1(tau - H, tau), tau)

    def ref(self, tau: int) -> tuple[float, bool] | None:
        """P(tau) with the 60-min data-gap rule (S-5.2, S-5.4; refs.ref_price)."""
        v = replace(self.x, b1=self.b1(tau, tau + H + MIN), b15=self.b15(tau, tau + 1))
        return ref_price(v, tau)

    def ref_at(self, tau: int) -> tuple[int, float, bool] | None:
        """(booking time, price, gap_used) of a fill intended at tau (DK-10): P(tau) booked at tau, else the
        open of the first usable 1-minute bar within 60 min after tau (S-5.4 data-gap rule, the same bar as
        ``ref``) booked at that bar's open time; None if neither exists."""
        r = self.ref(tau)
        if r is None:
            return None
        if not r[1]:
            return int(tau), r[0], False
        g = first_bar_within(usable_open(self.b1(tau, tau + H + MIN)), tau, H)
        if g is None or float(g[1]) != r[0]:
            raise RuntimeError("internal: the data-gap bar differs from the reference price")
        return int(g[0]), r[0], True

    def p_exact(self, tau: int) -> float | None:
        """P(tau) without the gap rule (adverse-move report)."""
        return P(self.b1(tau, tau + 1), self.b15(tau, tau + 1), tau)

    def h_fill(self, tau: int) -> float | None:
        """S-4.4 h_fill(tau): the S-4.5 proxy at tau on this instance's bars."""
        return half_spread(self.b15(tau - FROZEN.spread.window_ns - BAR_15M, tau), tau)

    def mark(self, ts: int) -> tuple[float | None, str]:
        """Mark at a 15-minute tick: the mark-price 15m bar ending at ts (close), else the last-price 15m bar
        ending at ts (close) (the S-4.6 chain without a record; AMB-19 (e)); (None, "missing") otherwise."""
        for bars, src in ((self.m15, "mark"), (self.b15, "last")):
            b = positive_close(bars(ts - BAR_15M, ts - BAR_15M + 1))
            if len(b):
                return float(b["c"].iloc[0]), src
        return None, "missing"


# --- fills ----------------------------------------------------------------------------------------------


def _book(v: ExecView, tr: Trade, ts: int, ref: float, h: float, s: float, fee_rate: float,
          liq: Liquidity, flags: tuple[str, ...], qty: float | None = None) -> Fill:
    q = qty if qty is not None else (tr.qty if tr.qty is not None else tr.notional / ref)  # type: ignore[operator]
    price = ref * (1 + tr.direction * (h + s))
    return Fill(int(ts), tr.symbol, v.inst.idx, tr.direction, float(q), float(ref), float(price),
                fee_rate * q * price, q * ref * h, q * ref * s, liq, tr.reason, tr.position_id, flags)


def _no_reference(v: ExecView, tr: Trade, tau: int) -> NoTrade | Pending | RunInvalid:
    """No 1-minute bar within the 60-minute data-gap window after tau, the instrument NOT halted at tau.

    Entry / increase: no trade (S-4.4, S-5.4). A required exit: INVALID RUN (round-2 override 2, DK-69; the
    S-5.4 stressed wait is for HALTED instruments only, an exit is never carried to an arbitrarily later bar),
    except when the instance ceases inside that window before the window end: the position then reaches its
    cessation and the terminal rule (S-5.4 / DEC-43, unchanged) settles it (``Pending(terminal=True)``)."""
    if tr.opening:
        return NoTrade(tr, tau, "price_missing")
    last = v.inst.last_trade_ts
    if last is not None and last <= tau + H and last < v.x.end:
        return Pending(tr, tau, terminal=True)
    return RunInvalid(INVALID_EXIT_GAP, v.symbol, v.inst.idx, int(tau),
                      f"{tr.reason} exit due at {int(tau)}: no 1-minute bar within 60 min "
                      "(not halted; DK-69)")


def taker_at(v: ExecView, tr: Trade, tau: int) -> Outcome:
    """S-6.1 taker fill intended at tau (S-5.3 price rules, S-4.4 h_fill, S-5.4 halted / data gap). A data-gap
    price is booked at its bar's open time, with h_fill there (DK-10)."""
    if v.halted(tau):
        return NoTrade(tr, tau, "halted") if tr.opening else Pending(tr, tau)
    r = v.ref_at(tau)
    if r is None:
        return _no_reference(v, tr, tau)
    ts, ref, gap = r
    flags = ("gap_price",) if gap else ()
    h = v.h_fill(ts)
    if h is None:
        if tr.opening:
            return NoTrade(tr, tau, "proxy_missing")
        return _book(v, tr, ts, ref, FB_H, FB_SLIP, TAKER, "taker", flags + ("proxy_fallback",))
    return _book(v, tr, ts, ref, h, SLIP, TAKER, "taker", flags)


def forced_at(v: ExecView, tr: Trade, trigger_ts: int) -> Outcome:
    """S-4.2 forced risk exit: the first 1-minute bar opening >= trigger + 1 min (within the 60-min gap rule),
    taker at its open at its open time; halted at trigger + 1 min -> ``Pending`` (S-5.4); no bar within the
    60 min (not halted) -> INVALID RUN (DK-69) or, if the instance ceases first, the terminal rule."""
    if tr.opening:
        raise ValueError("a forced exit never opens exposure")
    tau = int(trigger_ts) + FORCED_DELAY
    if v.halted(tau):
        return Pending(tr, tau)
    g = first_bar_within(usable_open(v.b1(tau, tau + H + 1)), tau, H)
    if g is None:
        return _no_reference(v, tr, tau)
    ts, o = g
    h = v.h_fill(ts)
    if h is None:
        return _book(v, tr, ts, o, FB_H, FB_SLIP, TAKER, "taker", ("proxy_fallback",))
    return _book(v, tr, ts, o, h, SLIP, TAKER, "taker", ())


def halted_exit(v: ExecView, p: Pending) -> Fill | Literal["terminal"] | None:
    """S-5.4 wait: the first volume > 0 1-minute bar of the SAME instance opening >= due and closing before
    the window end, filled at its close at its close time with stressed costs. None executable: "terminal"
    if the instance ceased before the window end (the terminal rule applies), else None (no exit)."""
    end = v.x.end
    fx = v.first_executable(p.due, end - MIN)  # close <= end - 1 min
    if fx is not None:
        ts, c = fx
        hf = v.h_fill(ts)
        flags = ("halted_exit",) + (("proxy_fallback",) if hf is None else ())
        return _book(v, p.trade, ts, c, max(hf if hf is not None else 0.0, FB_H), FB_SLIP, TAKER, "taker",
                     flags)
    if v.inst.last_trade_ts is not None and v.inst.last_trade_ts < end:
        return "terminal"
    return None


def settle(v: ExecView, tr: Trade, ev: TerminalEvent | None) -> Fill | RunInvalid:
    """Terminal settlement of a position held at its instance's cessation (S-5.4, DEC-43)."""
    inst = v.inst
    if ev is None or ev.instance.idx != inst.idx or ev.instance.first_bar_ts != inst.first_bar_ts:
        raise ValueError(f"{v.symbol}#{inst.idx}: a held position reaches its instance's cessation; the "
                         "classified TerminalEvent of that instance is required")
    if ev.klass != "A" or ev.settle_ts is None or ev.settle_price is None:
        return RunInvalid(INVALID_TERMINAL, v.symbol, inst.idx, ev.cessation_ts, f"class {ev.klass}")
    sp = float(ev.settle_price)
    if not (math.isfinite(sp) and sp > 0):
        return RunInvalid(INVALID_TERMINAL, v.symbol, inst.idx, ev.cessation_ts, "A without a price")
    if int(ev.settle_ts) >= v.x.end:
        return RunInvalid(INVALID_NO_EXIT, v.symbol, inst.idx, int(ev.settle_ts), "settlement at/after end")
    return _book(v, tr, int(ev.settle_ts), sp, 0.0, 0.0, TAKER, "settlement", ("terminal_A",))


def _adverse(v: ExecView, ts: int, price: float, d: int) -> tuple[float | None, ...]:
    """S-6.2 report: adverse mid move -d (P(m + k) / L - 1), m = the first minute >= the fill time."""
    m = -(-int(ts) // MIN) * MIN
    out: list[float | None] = []
    for k in ADVERSE_MIN:
        p = v.p_exact(m + k * MIN)
        out.append(None if p is None else -d * (p / price - 1.0))
    return tuple(out)


def maker_post(v: ExecView, t_e: int, h_hat: float | None) -> int | None:
    """The time an E_M limit order intended at t_e is posted (it then rests for W = 30 min): t_e, or the
    data-gap bar's open time when P(t_e) is missing (DK-10); None if no limit is posted (halted at t_e, no
    reference price within 60 min, or h-hat(t) missing). Shared by ``maker_at`` and the engine (DK-77)."""
    if v.halted(t_e) or h_hat is None or not math.isfinite(h_hat):
        return None
    r = v.ref_at(t_e)
    return None if r is None else r[0]


def maker_at(v: ExecView, tr: Trade, t_e: int, h_hat: float | None, usd_per_unit: float,
             cutoff: int | None = None) -> list[Outcome]:
    """S-6.2 E_M: post at t0 at the touch L = P(t_e) (1 - d h-hat(t)), t0 = t_e, or the open time of the
    data-gap bar that supplies P(t_e) (DK-10, ``maker_post``); fill only from aggTrades strictly through L
    (buy: price < L; sell: price > L) with t0 < ts <= t0 + 30 min, filled qty = min(remaining, cumulative
    trade-through qty) at L with the maker fee; the remainder fills taker at t0 + 30 min.
    Trade quantities are compared at ``usd_per_unit`` USD per book unit (L-norm: E_ref; AMB-19 (g)).
    Halted at t_e: S-5.4 (exit ``Pending``, entry ``NoTrade``); no P(t_e) within 60 min: entry ``NoTrade``,
    exit ``RunInvalid`` (DK-69); h-hat(t) missing: no limit can be posted, the order goes taker at t_e
    (AMB-19 (g)). ``cutoff`` (the terminal unwind): no maker fill at or after it, and a remainder due at or
    after it is ``NoTrade("after_unwind")`` (the unwind closes a held position; an entry stays partial)."""
    if v.trades is None:
        raise ValueError(f"E_M needs aggTrades for {v.symbol}")
    if v.halted(t_e):
        return [NoTrade(tr, t_e, "halted") if tr.opening else Pending(tr, t_e)]
    r = v.ref_at(t_e)
    if r is None:
        return [_no_reference(v, tr, t_e)]
    if h_hat is None or not math.isfinite(h_hat):
        return [taker_at(v, tr, t_e)]
    t0, ref, _ = r
    d = tr.direction
    lim = ref * (1 - d * float(h_hat))
    qty = tr.qty if tr.qty is not None else tr.notional / ref  # type: ignore[operator]
    rem = float(qty) * usd_per_unit
    hi = t0 + MAKER_WINDOW
    if cutoff is not None:
        hi = min(hi, int(cutoff) - 1)
    t = v.trades_in(t0 + 1, hi + 1)  # t0 < ts <= hi
    ts = ns_array(t["ts"], "ts") if len(t) else np.empty(0, np.int64)
    px = t["price"].to_numpy(np.float64) if len(t) else np.empty(0)
    qq = t["qty"].to_numpy(np.float64) if len(t) else np.empty(0)
    through = (px < lim) if d > 0 else (px > lim)
    sel = through & np.isfinite(qq) & (qq > 0)
    out: list[Outcome] = []
    for k in np.unique(ts[sel]):
        if rem <= EPS * qty * usd_per_unit:
            break
        take = min(rem, float(qq[sel & (ts == k)].sum()))
        rem -= take
        f = _book(v, tr, int(k), lim, 0.0, 0.0, MAKER, "maker", ("maker",), qty=take / usd_per_unit)
        out.append(replace(f, adverse=_adverse(v, int(k), lim, d)))
    if rem > EPS * qty * usd_per_unit:
        rest = replace(tr, qty=rem / usd_per_unit, notional=None)
        if cutoff is not None and t0 + MAKER_WINDOW >= cutoff:
            out.append(NoTrade(rest, t0 + MAKER_WINDOW, "after_unwind"))
        else:
            out.append(taker_at(v, rest, t0 + MAKER_WINDOW))
    return out
