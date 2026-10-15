"""Deterministic risk layer (spec S-7.3, S-0.6, S-2.3 U6/U8, S-4.2, S-5.4).

Identical for every arm; ML has no access. Enforced by type: no ``RiskLayer`` method takes a score, rank or
model input, and the constructor applies ``RiskActions.forced`` before any score is read.

* **Leg stop:** adverse move >= 15 % from the average entry price (long: price <= 0.85 avg; short: price >=
  1.15 avg) -> forced taker exit; a 24 h cooldown starts at the exit FILL (``start_cooldown``, called by the
  engine). The trigger records a pending cooldown; ``pre_decision`` raises if that leg has left the book
  without ``start_cooldown`` (a forgotten fill hook is detectable). Evaluated on the mark at every 15-minute
  tick (``on_tick``) and on the latest mark at a decision (``pre_decision``) (AMB-17 (a)).
* **Daily loss:** at a 15-minute mark, equity <= 98 % of the 00:00 UTC equity -> no entries until the next
  00:00 UTC (sticky; a recovery the same day does not lift it). Before the run's first 00:00 mark the
  day-start equity is the run's starting equity (AMB-17 (b)).
* **Drawdown kill:** at a 15-minute mark, equity <= 85 % of the running peak of the 15-minute marked equity ->
  exit everything (legs and hedge), no re-entry for the rest of the run.
* **Ineligibility exit:** a held leg failing U4, U5 at the 20 bps exit threshold (missing h-hat counts as
  failing) or U7 on two consecutive decisions -> forced taker exit (AMB-17 (c)).
* **U6:** a held leg whose gate row has U6 failing (a matched notice with ``known_at <= t - 1 min``, i.e. the
  first decision >= known_at + 1 min) -> forced taker exit (``universe.exit_triggered``).
* **U8 false:** blocks entries / increases of that symbol; never an exit. **Staleness** (decision-level flag
  from the engine): entries skipped, exits still run.
* **Precedence** (the reason recorded when several apply): kill > leg stop > U6 > ineligibility for exits;
  kill > daily-loss pause > staleness for the decision-wide entry block; per symbol cooldown, then U8.
* Gross <= 1.0 E, per leg <= 0.5 E / K and the liquidity cap bind the targets and the entries, applied by
  ``constructor.targets``; the marked book drifts within the S-7.1 25 % band, and the gross <= 1.0 E limit
  also binds the marked book at every decision (proportional reduce-only ``risk_gross``; AMB-16 (e), user
  decision 2026-10-15, implementation reading R35).

A halted instrument gets no order of any kind: forced exits are emitted as intents and the constructor flags
them ``halted`` so the engine defers them (S-5.4); legs with a ``pending_exit`` get no new exit.
State is immutable: every method returns a new ``RiskState``. Fail-closed sequencing: ``on_tick`` must be
called at every 15-minute mark with no gap (ts == last tick + 15 min); ``pre_decision`` must be called
exactly once per decision (it advances the two-consecutive-decision counters) and only after the mark at or
before t was processed (0 <= t - last tick < 15 min). Every forced exit carries its trigger time (S-4.2 is
the engine's).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from luna.v7.book import EPS, Leg
from luna.v7.constants import FROZEN
from luna.v7.timeutil import MIN, D, H
from luna.v7.universe import GateRow, exit_triggered

RK = FROZEN.risk
TICK = 15 * MIN

ExitReason = Literal["kill", "leg_stop", "u6", "ineligible"]
EXIT_PRECEDENCE: tuple[ExitReason, ...] = ("kill", "leg_stop", "u6", "ineligible")


def _frozen_map(d: Mapping) -> Mapping:
    return MappingProxyType(dict(sorted(d.items())))


@dataclass(frozen=True)
class RiskState:
    peak: float  # running peak of the 15-minute marked equity
    day: int  # UTC day index of ``day_start_equity``
    day_start_equity: float
    paused_until: int | None  # daily-loss pause end (next 00:00 UTC), or None
    killed: bool
    last_tick: int | None
    last_decision: int | None
    cooldown_until: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    inelig_streak: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    pending_cooldown: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))  # stop trigger


@dataclass(frozen=True)
class ForcedExit:
    symbol: str
    side: int
    reason: ExitReason
    position_id: str
    cooldown: bool  # True for a leg stop: the engine calls ``start_cooldown`` at the exit fill
    trigger_ts: int  # the tick / decision time at which the risk rule fired (S-4.2 fill timing)


@dataclass(frozen=True)
class RiskActions:
    """What the risk layer imposes at one tick / decision. ``forced`` is sorted by symbol."""

    t: int
    forced: tuple[ForcedExit, ...]
    killed: bool
    paused: bool
    stale: bool
    blocked: Mapping[str, str]  # symbol -> per-symbol entry block reason ("cooldown" | "u8")
    notes: tuple[str, ...] = ()

    @property
    def entries_open(self) -> bool:
        return not (self.killed or self.paused or self.stale)

    @property
    def entry_block(self) -> str | None:
        """Decision-wide entry block reason by precedence: kill > daily_loss > stale."""
        if self.killed:
            return "kill"
        if self.paused:
            return "daily_loss"
        if self.stale:
            return "stale"
        return None

    def allows_entry(self, symbol: str) -> bool:
        return self.entries_open and symbol not in self.blocked


def _finite(x: float, name: str) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {x!r}")
    return v


def leg_stop_hit(leg: Leg, price: float | None) -> bool | None:
    """Adverse move >= 15 % from the average entry price; None if the price is missing / not > 0."""
    if price is None or not (math.isfinite(float(price)) and float(price) > 0):
        return None
    move = leg.side * (float(price) / leg.avg_entry - 1.0)
    return move <= -RK.leg_stop + EPS


def _inelig_fail(g: GateRow, h_hat: float | None) -> bool:
    """U4, U5 at the 20 bps exit threshold, or U7 fails (missing h-hat fails, fail closed)."""
    u5_exit = h_hat is not None and math.isfinite(float(h_hat)) and float(h_hat) <= RK.u5_exit
    return not (g.u4 and u5_exit and g.u7)


def _active(legs: Iterable[Leg]) -> list[Leg]:
    legs = sorted(legs, key=lambda x: x.symbol)
    syms = [x.symbol for x in legs]
    if len(set(syms)) != len(syms):
        raise ValueError("a symbol is held more than once")
    return [x for x in legs if x.pending_exit is None]


class RiskLayer:
    """Stateless rules over an explicit ``RiskState`` (S-7.3)."""

    def initial_state(self, equity: float, ts: int) -> RiskState:
        e = _finite(equity, "equity")
        return RiskState(
            peak=e,
            day=int(ts) // D,
            day_start_equity=e,
            paused_until=None,
            killed=False,
            last_tick=None,
            last_decision=None,
        )

    def start_cooldown(self, state: RiskState, symbol: str, exit_fill_ts: int) -> RiskState:
        """24 h cooldown from a leg-stop exit fill; never shortens a running cooldown."""
        until = int(exit_fill_ts) + RK.cooldown_h * H
        cur = dict(state.cooldown_until)
        cur[symbol] = max(until, cur.get(symbol, until))
        pend = {k: v for k, v in state.pending_cooldown.items() if k != symbol}
        return replace(state, cooldown_until=_frozen_map(cur), pending_cooldown=_frozen_map(pend))

    def _paused(self, state: RiskState, t: int) -> bool:
        return state.paused_until is not None and t < state.paused_until

    def on_tick(
        self, state: RiskState, ts: int, equity: float, legs: Iterable[Leg], marks: Mapping[str, float]
    ) -> tuple[RiskState, RiskActions]:
        """A 15-minute mark: day roll at 00:00, peak, drawdown kill, daily-loss pause, leg stops on marks."""
        ts = int(ts)
        e = _finite(equity, "equity")
        if ts % TICK:
            raise ValueError("ticks are on the 15-minute grid")
        if state.last_tick is not None and ts != state.last_tick + TICK:
            raise ValueError("ticks must be consecutive 15-minute marks (no skipped tick)")
        day = ts // D
        s = state
        if day != s.day:
            if ts % D:
                raise ValueError("the 00:00 UTC mark of a new day is missing")
            paused_until = s.paused_until if s.paused_until is not None and ts < s.paused_until else None
            s = replace(s, day=day, day_start_equity=e, paused_until=paused_until)
        s = replace(s, peak=max(s.peak, e), last_tick=ts)
        if not s.killed and e <= RK.dd_kill * s.peak * (1 + EPS):
            s = replace(s, killed=True)
        if not s.killed and not self._paused(s, ts) and e <= RK.daily_loss * s.day_start_equity * (1 + EPS):
            s = replace(s, paused_until=(day + 1) * D)
        forced, notes = self._exits(s, ts, legs, marks, None, None)
        s = self._record_pending(s, forced)
        return s, RiskActions(ts, forced, s.killed, self._paused(s, ts), False, _frozen_map({}), notes)

    def pre_decision(
        self,
        state: RiskState,
        t: int,
        legs: Iterable[Leg],
        gates: Mapping[str, GateRow],
        half_spread: Mapping[str, float | None],
        marks: Mapping[str, float],
        stale: bool,
    ) -> tuple[RiskState, RiskActions]:
        """Forced exits and entry blocks at decision t. ``gates``: a GateRow for every held leg (required) and
        every candidate; ``half_spread``: h-hat(t) per held leg (None = missing); ``marks``: latest mark per
        held leg; ``stale``: a required decision input is past its freshness rule."""
        t = int(t)
        if state.last_decision is not None and t <= state.last_decision:
            raise ValueError("decisions must be strictly increasing")
        if state.last_tick is None or not 0 <= t - state.last_tick < TICK:
            raise ValueError("the 15-minute mark at or before t was not processed (call on_tick first)")
        legs = list(legs)
        held_syms = {x.symbol for x in legs}
        lost = sorted(k for k in state.pending_cooldown if k not in held_syms)
        if lost:
            raise ValueError(f"leg-stop exit of {lost} left the book without start_cooldown (fail closed)")
        active = _active(legs)
        streak = {}
        for lg in active:
            g = gates.get(lg.symbol)
            if g is None:
                raise ValueError(f"no gate row for held leg {lg.symbol} (fail closed)")
            fail = _inelig_fail(g, half_spread.get(lg.symbol))
            streak[lg.symbol] = state.inelig_streak.get(lg.symbol, 0) + 1 if fail else 0
        s = replace(state, inelig_streak=_frozen_map(streak), last_decision=t)
        forced, notes = self._exits(s, t, legs, marks, gates, streak)
        s = self._record_pending(s, forced)
        blocked: dict[str, str] = {}
        for sym, until in s.cooldown_until.items():
            if t < until:
                blocked[sym] = "cooldown"
        for sym, g in gates.items():
            if sym not in blocked and g is not None and not g.u8:
                blocked[sym] = "u8"
        acts = RiskActions(t, forced, s.killed, self._paused(s, t), bool(stale), _frozen_map(blocked), notes)
        return s, acts

    def _record_pending(self, s: RiskState, forced: tuple[ForcedExit, ...]) -> RiskState:
        pend = dict(s.pending_cooldown)
        for f in forced:
            if f.cooldown:
                pend.setdefault(f.symbol, f.trigger_ts)
        return replace(s, pending_cooldown=_frozen_map(pend))

    def _exits(
        self,
        s: RiskState,
        ts: int,
        legs: Iterable[Leg],
        marks: Mapping[str, float],
        gates: Mapping[str, GateRow] | None,
        streak: Mapping[str, int] | None,
    ) -> tuple[tuple[ForcedExit, ...], tuple[str, ...]]:
        out: list[ForcedExit] = []
        notes: list[str] = []
        for lg in _active(legs):
            reasons: list[ExitReason] = []
            if s.killed:
                reasons.append("kill")
            hit = leg_stop_hit(lg, marks.get(lg.symbol))
            if hit is None:
                notes.append(f"unpriced:{lg.symbol}")
            elif hit:
                reasons.append("leg_stop")
            if gates is not None and streak is not None:
                if exit_triggered(gates[lg.symbol]):
                    reasons.append("u6")
                if streak[lg.symbol] >= RK.inelig_consecutive:
                    reasons.append("ineligible")
            if reasons:
                why = min(reasons, key=EXIT_PRECEDENCE.index)
                out.append(ForcedExit(lg.symbol, lg.side, why, lg.position_id, why == "leg_stop", ts))
        return tuple(out), tuple(notes)
