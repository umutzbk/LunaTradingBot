"""CarryCore: the deterministic layer between the strategy and an executor.

One ``run_cycle(now, snapshot, funding, executor, allow_rebalance)`` is the whole evaluation; the
paper engine (``engine.py``) and the replay/backtest adapter (``replay.py``) both call it, so the
decision path is identical and only the executor differs.

Per cycle:

1. mark the book at the snapshot's mark prices, settle every ended funding period (pro rata, Kraken
   sign: positive funding -> longs pay shorts), roll the risk day, apply the portfolio-level risk
   actions (R3 day loss -> no entries today, R4 drawdown -> kill switch, R18 -> pause);
2. compute the signals; forced exits (kill switch, leg stop, asset ineligible for longer than its
   grace, asset left the universe); a stale snapshot sends nothing; a global data outage (>= half
   of the universe without a fresh quote or fresh funding) freezes the book (no entries, no
   ineligibility exits);
3. ``strategy.decide`` -> actions; each action becomes an order group sized here
   (``leg_frac x equity`` per new leg, instrument qty step) and is pre-checked as a unit by
   ``luna.risk.engine.check`` on the account state after its own closes: a group with any veto is
   rejected whole (a pair never opens one leg), its open assets get a short veto cooldown and the
   decision is re-run (at most ``_MAX_PASSES``);
4. phase 1 sends every close / reduce, books the fills; phase 2 re-checks the opens against the
   new account state (risk engine again) and sends the approved ones. A switch or neutrality
   restore whose close did not fill completely sends no open.

Nothing here can bypass ``check``: every order goes through it right before the executor.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import pandas as pd

from luna.carry.config import CarryConfig, CarryEvaluation
from luna.carry.funding import FundingLedger, FundingStore, Settlement
from luna.carry.market import MarketSnapshot
from luna.carry.signals import AssetSignal, compute_signals
from luna.carry.strategy import Action, Decision, DecisionInput, HeldLeg, LegOrder, Record, decide
from luna.planner import round_qty
from luna.portfolio.portfolio import Portfolio, Position
from luna.risk.engine import OrderCheck, RiskContext, apply_actions, check, portfolio_actions
from luna.risk.state import RiskState, roll_day, state_from_dict, state_to_dict
from luna.store import models as m
from luna.types import ExitSpec, Fill, Instrument, Leg, OrderRequest, StopSpec

BAR = pd.Timedelta("15min")
_MAX_PASSES = 3
VETO_COOLDOWN = pd.Timedelta(minutes=10)
RECENT_CLOSED = pd.Timedelta(hours=8)  # closed legs kept for late funding attribution
DATA_REASONS = frozenset({"no_quote", "stale_quote", "funding_stale"})
_DEPENDENT = ("neutrality_", "net_exposure_trim")  # forced changes that follow from other exits
_STATE_RULES = ("R1_", "R2_", "R3_", "R4_", "R18_", "R19_")
_QTY_TOL = 1e-9

Submission = tuple[OrderRequest, float]  # order, reference mid (decision price)


class Executor(Protocol):
    def execute(self, batch: list[Submission], snap: MarketSnapshot) -> list[tuple[Fill, str]]:
        """Send the orders; return their fills as (fill, position id)."""


class SimExecutor:
    """Backtest/replay fills: an IOC buy fills at ask x (1 + slippage), a sell at bid x (1 -
    slippage), full size, taker fee on the notional."""

    def __init__(self, fee_rate: float = 0.0005, slippage_bps: float = 0.0) -> None:
        self.fee_rate, self.slip = fee_rate, slippage_bps * 1e-4

    def execute(self, batch: list[Submission], snap: MarketSnapshot) -> list[tuple[Fill, str]]:
        out = []
        for o, _ in batch:
            q = snap.quotes.get(o.asset)
            if q is None or not q.valid:
                continue
            px = q.ask * (1 + self.slip) if o.side == "buy" else q.bid * (1 - self.slip)
            if (o.side == "buy" and px > o.limit_price) or (o.side == "sell" and px < o.limit_price):
                continue
            out.append(
                (
                    Fill(o.client_id, o.ts, o.asset, o.side, o.qty, px, self.fee_rate * o.qty * px, "taker"),
                    o.position_id,
                )
            )
        return out


class Trigger:
    """When economic rebalancing is allowed. ``event``: every evaluation; ``interval``: every
    ``interval_hours`` since the last rebalance; ``daily`` / ``weekly``: once after each scheduled
    UTC time. The first evaluation (``last is None``) is always allowed."""

    def __init__(self, ev: CarryEvaluation) -> None:
        self.ev = ev

    def scheduled(self, now: pd.Timestamp) -> pd.Timestamp:
        t = now.floor("D") + pd.Timedelta(hours=self.ev.at_hour)
        if t > now:
            t -= pd.Timedelta(days=1)
        if self.ev.trigger == "weekly":
            t -= pd.Timedelta(days=(t.weekday() - self.ev.weekday) % 7)
        return t

    def due(self, now: pd.Timestamp, last: pd.Timestamp | None) -> bool:
        if self.ev.trigger == "event" or last is None:
            return True
        if self.ev.trigger == "interval":
            return now - last >= pd.Timedelta(hours=self.ev.interval_hours)
        return last < self.scheduled(now)


@dataclass
class Group:
    action: Action
    closes: list[Submission]
    opens: list[Submission]
    status: str = "planned"  # planned | rejected | sent | partial
    reason: str = ""


@dataclass
class CycleResult:
    now: pd.Timestamp
    snapshot_ts: pd.Timestamp | None
    equity: float
    signals: dict[str, AssetSignal]
    decision: Decision | None
    records: list[Record]
    groups: list[Group]
    orders: list[Submission]
    fills: list[tuple[Fill, str]]
    settlements: list[Settlement]
    allow_rebalance: bool
    opens_allowed: bool
    forced: dict[str, str]
    closed: list[tuple[Position, str]] = field(default_factory=list)
    touched_closed: list[Position] = field(default_factory=list)  # closed positions with new funding
    forced_blocked: bool = False  # a forced exit could not be ordered: everything else held back
    equity_start: float = math.nan  # after mark + settlement, before this cycle's orders


def _reject(a: Action, reason: str) -> Record:
    return Record(
        "REJECT",
        reason,
        a.side,
        a.asset_out,
        a.asset_in,
        a.expected_carry,
        a.current_carry,
        a.incremental_carry,
        a.switch_cost,
        a.safety_buffer,
        a.value,
    )


def _plain(reason: str) -> Record:
    nan = math.nan
    return Record("REJECT", reason, None, None, None, nan, nan, nan, nan, nan, nan)


class CarryCore:
    def __init__(
        self,
        cfg: CarryConfig,
        instruments: dict[str, Instrument],
        fee_rate: float,
        mode: Literal["backtest", "paper"] = "paper",
    ) -> None:
        self.cfg = cfg
        self.instruments = instruments
        self.fee_rate = fee_rate
        self.mode = mode
        self.portfolio = Portfolio("perp", cfg.capital_usd)
        self.risk_state = RiskState()
        self.ledger = FundingLedger()
        self.cooldown: dict[str, pd.Timestamp] = {}
        self.ineligible_since: dict[str, pd.Timestamp] = {}
        self.last_rebalance: pd.Timestamp | None = None
        self.seq = 0
        self.exit_reason: dict[str, str] = {}  # position id -> reason of the close in flight
        self.recent_closed: dict[
            str, tuple[Position, pd.Timestamp, str]
        ] = {}  # pid -> (leg, closed at, reason)
        self.trigger = Trigger(cfg.evaluation)
        self.reconciliation_ok = True
        self.on_submit: Callable[[list[Submission]], None] | None = None  # paper: persist first
        self.on_booked: Callable[[], None] | None = None  # paper: fills of the last send are booked
        self.dropped: list[Position] = []  # legs registered for an open that never filled
        self.pending_settlements: list[Settlement] = []  # settled, not yet written to the Db

    # --- helpers -----------------------------------------------------------------------------------

    def legs(self, signals: dict[str, AssetSignal]) -> tuple[HeldLeg, ...]:
        out = []
        for p in self.portfolio.positions.values():
            q = p.qty.get(p.key, 0.0)
            if q == 0.0:
                continue
            s = signals.get(p.key)
            px = (
                s.mid
                if s is not None and math.isfinite(s.mid)
                else self.portfolio.marks.get(p.key, p.entry_ref)
            )
            out.append(HeldLeg(p.key, p.direction, abs(q) * px, p.entry_ts, p.id))
        return tuple(sorted(out, key=lambda g: g.asset))

    def _next_id(self) -> int:
        self.seq += 1
        return self.seq

    # --- one evaluation ------------------------------------------------------------------------------

    def run_cycle(
        self,
        now: pd.Timestamp,
        snap: MarketSnapshot | None,
        store: FundingStore,
        executor: Executor,
        allow_rebalance: bool,
    ) -> CycleResult:
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware (UTC)")
        cfg = self.cfg
        self._mark(snap)
        settlements, touched = self._settle(now, store)
        self._expire(now)
        equity = self.portfolio.equity()
        self.risk_state = roll_day(self.risk_state, now, equity)
        ctx0 = self._ctx(now, snap, equity, self.portfolio.holdings)
        self.risk_state = apply_actions(self.risk_state, portfolio_actions(ctx0), now)

        leg_notional = cfg.leg_frac * equity
        signals = compute_signals(snap, store, now, cfg, leg_notional, self.fee_rate)
        records: list[Record] = []
        res = CycleResult(
            now,
            snap.ts if snap is not None else None,
            equity,
            signals,
            None,
            records,
            [],
            [],
            [],
            settlements,
            allow_rebalance,
            False,
            {},
            touched_closed=touched,
            equity_start=equity,
        )

        fresh = snap is not None and snap.age_s(now) <= cfg.gates.max_quote_age_s
        if not fresh:
            records.append(_plain("stale_snapshot"))
            return res
        n_data = sum(1 for s in signals.values() if DATA_REASONS & set(s.reasons))
        outage = n_data * 2 >= len(cfg.universe)
        if outage:
            records.append(_plain("global_data_outage"))
            self.ineligible_since.clear()
        forced = self._forced(now, snap, signals, outage)
        st = self.risk_state
        opens_allowed = (
            not outage
            and not st.kill_switch
            and not st.paused
            and self.reconciliation_ok
            and st.entries_blocked_day != now.date()
        )
        blocked = {a for a, until in self.cooldown.items() if until > now}
        res.forced, res.opens_allowed = forced, opens_allowed

        groups: list[Group] = []
        decision: Decision | None = None
        for pass_no in range(_MAX_PASSES):
            inp = DecisionInput(
                now,
                equity,
                signals,
                self.legs(signals),
                dict(forced),
                frozenset(blocked),
                allow_rebalance,
                opens_allowed,
            )
            decision = decide(inp, cfg)
            groups, vetoed = self._plan_groups(now, snap, decision, equity)
            for g in vetoed:  # (their legs are already unregistered by _plan_groups)
                for o, _ in g.opens:
                    blocked.add(o.asset)
                    self.cooldown[o.asset] = max(self.cooldown.get(o.asset, now), now + VETO_COOLDOWN)
                if any(g.reason.startswith(r) for r in _STATE_RULES):
                    opens_allowed = False
                records.append(_reject(g.action, g.reason))
            if not vetoed or pass_no == _MAX_PASSES - 1:
                break  # the last pass keeps its approved groups (and their registered legs)
            for g in groups:  # re-plan from scratch: unregister this pass's other legs
                for o, _ in g.opens:
                    self._drop_empty(o.position_id, report=False)
        assert decision is not None
        res.decision = decision
        records.extend(r for r in decision.records)
        for g in groups:
            if g.status == "rejected" and g not in vetoed and g.reason:
                records.append(_reject(g.action, g.reason))  # no quote / no instrument / below min
        # a forced exit that could not even be ordered (no quote, suspended, no instrument, below
        # the minimum) counts as unfilled: what depends on it is held back below
        forced_failed = any(
            g.status != "planned" and g.action.forced and not g.action.reason.startswith(_DEPENDENT)
            for g in groups
        )
        res.forced_blocked = forced_failed
        groups = [g for g in groups if g.status == "planned"]
        if allow_rebalance and opens_allowed:  # a blocked scheduled rebalance is not used up
            self.last_rebalance = now

        # phase 1a: forced exits (stop, kill, ineligible, ...); phase 1b: every other close/reduce
        # (neutrality unwinds, trims, switches, edge exits) only if all forced exits filled, since
        # those decisions assumed the forced legs are gone
        primary = [g for g in groups if g.action.forced and not g.action.reason.startswith(_DEPENDENT)]
        batch1 = [s for g in primary for s in g.closes]
        fills1 = self._send(batch1, snap, executor, now)
        filled_close = self._fully_filled(batch1, fills1)
        if all(filled_close.values()) and not forced_failed:
            rest = [s for g in groups if g not in primary for s in g.closes]
            fills1b = self._send(rest, snap, executor, now)
            filled_close |= self._fully_filled(rest, fills1b)
            batch1, fills1 = batch1 + rest, fills1 + fills1b
        else:
            for g in groups:
                if g not in primary and g.closes:
                    g.status, g.reason = "partial", "forced_exit_not_filled"
                    records.append(_reject(g.action, "forced_exit_not_filled"))
        # phase 2: opens of groups whose closes all filled, re-checked on the new account state
        equity = self.portfolio.equity()
        # any close that did not fill completely (spread beyond the band, no book): no opens this
        # cycle at all, so a failed exit can never leave the book with an extra leg
        all_closed = all(filled_close.values()) and not forced_failed
        ready = [g for g in groups if g.opens and all_closed and g.status == "planned"]
        for g in groups:
            if g.opens and g not in ready and g.status == "planned":
                g.status, g.reason = "partial", "close_not_filled"
                records.append(_reject(g.action, "close_not_filled"))
        batch2: list[Submission] = []
        ctx = self._ctx(now, snap, equity, self.portfolio.holdings)
        for g in ready:
            checks = [self._order_check(o, ref, equity) for o, ref in g.opens]
            r = check(checks, ctx)
            if r.vetoed:
                g.status, g.reason = "rejected", r.vetoed[0][1]
                records.append(_reject(g.action, g.reason))
                continue
            batch2 += g.opens
            ctx = self._ctx_after(ctx, g.opens)
        fills2 = self._send(batch2, snap, executor, now)
        for o, _ in batch2:  # an IOC open without any fill: retry the asset only after a pause
            p = self.portfolio.positions.get(o.position_id)
            if p is not None and p.qty.get(p.key, 0.0) == 0.0:
                self.cooldown[o.asset] = max(self.cooldown.get(o.asset, now), now + VETO_COOLDOWN)
        for g in groups:  # every leg registered for an open that did not fill
            for o, _ in g.opens:
                self._drop_empty(o.position_id)
        self.exit_reason.clear()  # closes that did not fill completely are re-decided next cycle
        res.groups = groups
        res.orders = batch1 + batch2
        res.fills = fills1 + fills2
        self._mark(snap)  # new legs at the mark, not at their fill price
        res.equity = self.portfolio.equity()
        res.closed = self._collect_closed(now)
        return res

    # --- forced exits ------------------------------------------------------------------------------

    def _forced(
        self, now: pd.Timestamp, snap: MarketSnapshot, signals: dict[str, AssetSignal], outage: bool
    ) -> dict[str, str]:
        cfg, g = self.cfg, self.cfg.gates
        out: dict[str, str] = {}
        held = {p.key: p for p in self.portfolio.positions.values() if p.qty.get(p.key, 0.0) != 0.0}
        for a, p in held.items():
            if self.risk_state.kill_switch:
                out[a] = "kill_switch"
                continue
            if a not in cfg.universe:
                out[a] = "not_in_universe"
                continue
            q = snap.quotes.get(a)
            mark = q.mark if q is not None and math.isfinite(q.mark) and q.mark > 0 else math.nan
            entry = p.entry_price.get(a, p.entry_ref)
            if math.isfinite(mark) and entry > 0 and -p.direction * (mark / entry - 1) >= cfg.leg_stop_frac:
                out[a] = "stop"
                continue
            if outage:
                continue
            s = signals.get(a)
            hard = self._exit_reasons(s, q)
            if not hard:
                self.ineligible_since.pop(a, None)
                continue
            since = self.ineligible_since.setdefault(a, now)
            if (now - since).total_seconds() >= g.ineligible_exit_grace_s:
                out[a] = "ineligible:" + ",".join(hard)
        for a in list(self.ineligible_since):
            if a not in held:
                del self.ineligible_since[a]
        return out

    def _exit_reasons(self, s: AssetSignal | None, q: Any) -> list[str]:
        """Reasons that make a HELD leg exit after the grace: entry-only gates (spread above the
        entry limit but below the exit limit, a thin top of book) do not count."""
        if s is None:
            return ["no_signal"]
        out = [r for r in s.reasons if r not in ("wide_spread", "thin_book")]
        if "wide_spread" in s.reasons and not s.spread_full <= self.cfg.gates.exit_spread_bps * 1e-4:
            out.append("wide_spread")
        return out

    # --- planning ------------------------------------------------------------------------------------

    def _plan_groups(
        self, now: pd.Timestamp, snap: MarketSnapshot, decision: Decision, equity: float
    ) -> tuple[list[Group], list[Group]]:
        groups: list[Group] = []
        vetoed: list[Group] = []
        holdings = dict(self.portfolio.holdings)
        ctx = self._ctx(now, snap, equity, holdings)
        for a in decision.actions:
            g = self._group(now, snap, a, equity)
            if g.status != "planned":
                groups.append(g)
                continue
            # the group's opens are checked on the state after the group's own (and earlier) closes
            after = self._ctx_after(ctx, g.closes)
            checks = [self._order_check(o, ref, equity) for o, ref in g.opens]
            r = check(checks, after) if checks else None
            if r is not None and r.vetoed:
                g.status, g.reason = "rejected", r.vetoed[0][1]
                vetoed.append(g)
                for o, _ in g.opens:
                    self._drop_empty(o.position_id, report=False)  # never persisted nor ordered
            else:
                ctx = self._ctx_after(after, g.opens)
            groups.append(g)
        return groups, vetoed

    def _group(self, now: pd.Timestamp, snap: MarketSnapshot, a: Action, equity: float) -> Group:
        g = Group(a, [], [])
        for lo in a.legs:
            q = snap.quotes.get(lo.asset)
            if q is None or not q.valid:
                g.status, g.reason = "rejected", f"no_quote:{lo.asset}"
                return g
            if lo.intent == "open":
                sub = self._open(now, lo, q.mid)
            else:
                sub = self._close(now, lo, q.mid, a.reason)
            if sub is None:
                why = "no_instrument" if lo.asset not in self.instruments else "below_min_qty"
                g.status, g.reason = "rejected", f"{why}:{lo.asset}"
                return g
            (g.opens if lo.intent == "open" else g.closes).append(sub)
        return g

    def _limit(self, side: str, mid: float) -> float:
        b = self.cfg.order_band
        return mid * (1 + b) if side == "buy" else mid * (1 - b)

    def _open(self, now: pd.Timestamp, lo: LegOrder, mid: float) -> Submission | None:
        inst = self.instruments.get(lo.asset)
        if inst is None:
            return None
        qty = round_qty(lo.notional / mid, inst.qty_step)
        if not qty >= inst.min_qty or qty <= 0:
            return None
        n = self._next_id()
        tag = "L" if lo.side == 1 else "S"
        pid = f"carry:{lo.asset}:{tag}:{n:06d}"
        side = "buy" if lo.side == 1 else "sell"
        stop = self.cfg.leg_stop_frac
        pos = Position(
            id=pid,
            strategy="carry",
            key=lo.asset,
            legs=(Leg(lo.asset, 1.0),),
            direction=lo.side,
            qty={lo.asset: 0.0},
            entry_price={},
            entry_ts=now,
            entry_ref=mid,
            stop=StopSpec("price", mid * (1 - lo.side * stop), stop),
            exit=ExitSpec(max_hold_bars=0),
            risk_amount=stop * qty * mid,
            regime="carry",
        )
        self.portfolio.open_position(pos)
        order = OrderRequest(
            f"{pid}:open",
            now,
            lo.asset,
            side,
            qty,
            self._limit(side, mid),
            "IOC",
            "open",
            pid,
            f"carry:{n:06d}",
        )
        return order, mid

    def _close(self, now: pd.Timestamp, lo: LegOrder, mid: float, reason: str) -> Submission | None:
        p = self.portfolio.positions.get(lo.position_id or "")
        if p is None:
            return None
        held = p.qty.get(lo.asset, 0.0)
        inst = self.instruments.get(lo.asset)
        if inst is None:
            return None
        if lo.intent == "close":
            qty = abs(held)
        else:
            qty = min(abs(held), round_qty(lo.notional / mid, inst.qty_step))
        if qty <= 0:
            return None
        n = self._next_id()
        side = "sell" if held > 0 else "buy"
        self.exit_reason[p.id] = reason
        order = OrderRequest(
            f"{p.id}:{lo.intent}:{n:06d}",
            now,
            lo.asset,
            side,
            qty,
            self._limit(side, mid),
            "IOC",
            lo.intent,
            p.id,
            f"carry:{n:06d}",
        )
        return order, mid

    # --- risk context ------------------------------------------------------------------------------

    def _ctx(
        self, now: pd.Timestamp, snap: MarketSnapshot | None, equity: float, holdings: dict[str, float]
    ) -> RiskContext:
        pf = self.portfolio
        last_bar = (snap.ts - BAR) if snap is not None else pd.NaT
        liq = {}
        for a, inst in self.instruments.items():
            liq[a] = self._liq_distance(a, inst, equity)
        return RiskContext(
            limits=self.cfg.risk,
            kind="perp",
            mode=self.mode,
            state=self.risk_state,
            equity=equity,
            open_positions=sum(1 for p in pf.positions.values() if p.qty.get(p.key, 0.0) != 0.0),
            open_risk=sum(p.risk_amount for p in pf.positions.values() if p.qty.get(p.key, 0.0) != 0.0),
            gross_notional=pf.gross_notional(),
            holdings=dict(holdings),
            last_bar_ts=last_bar,
            now=now,
            liq_distance=liq,
            min_qty={a: i.min_qty for a, i in self.instruments.items()},
            min_notional={a: i.min_notional for a, i in self.instruments.items()},
            available_cash=equity,
            reconciliation_ok=self.reconciliation_ok,
            open_position_ids=frozenset(p.id for p in pf.positions.values() if p.qty.get(p.key, 0.0) != 0.0),
        )

    def _liq_distance(self, asset: str, inst: Instrument, equity: float) -> float:
        """Fractional adverse move to liquidation of a new leg of ``leg_frac x equity`` on top of
        the book (cross margin): (equity - (gross + leg) x mm) / leg."""
        leg = self.cfg.leg_frac * equity
        if not leg > 0:
            return 0.0
        mm = inst.maintenance_margin
        return max(0.0, (equity - (self.portfolio.gross_notional() + leg) * mm) / leg)

    def _ctx_after(self, ctx: RiskContext, subs: list[Submission]) -> RiskContext:
        """The context after these orders (closes free their slot, risk and notional; opens take
        them). Used only to pre-check a group's opens after its own closes."""
        holdings = dict(ctx.holdings)
        gross, positions, risk = ctx.gross_notional, ctx.open_positions, ctx.open_risk
        ids = set(ctx.open_position_ids)
        orders_today = ctx.state.orders_today
        for o, ref in subs:
            signed = o.qty if o.side == "buy" else -o.qty
            holdings[o.asset] = holdings.get(o.asset, 0.0) + signed
            orders_today += 1
            if o.intent == "open":
                gross += o.qty * ref
                positions += 1
                risk += self.cfg.leg_stop_frac * o.qty * ref
                ids.add(o.position_id)
                continue
            gross -= o.qty * ref
            p = self.portfolio.positions.get(o.position_id)
            held = abs(p.qty.get(p.key, 0.0)) if p is not None else 0.0
            if p is not None and held > 0:
                risk -= p.risk_amount * min(1.0, o.qty / held)
            if o.intent == "close":
                positions -= 1
                ids.discard(o.position_id)
        state = replace(ctx.state, orders_today=orders_today)
        return replace(
            ctx,
            holdings=holdings,
            gross_notional=max(0.0, gross),
            open_positions=positions,
            open_risk=max(0.0, risk),
            open_position_ids=frozenset(ids),
            state=state,
        )

    def _order_check(self, o: OrderRequest, mid: float, equity: float) -> OrderCheck:
        notional = o.qty * mid
        is_open = o.intent == "open"
        risk = self.cfg.leg_stop_frac * notional if is_open else 0.0
        held = self.portfolio.holdings.get(o.asset, 0.0)
        grows = is_open and held != 0.0 and (held > 0) == (o.side == "buy")
        return OrderCheck(
            order=o,
            notional=notional,
            new_position_risk=risk,
            new_position_risk_frac=risk / equity if equity > 0 else math.inf,
            stop_distance_frac=self.cfg.leg_stop_frac if is_open else None,
            has_stop=is_open,
            increases_abs_position=grows,
            mid=mid,
        )

    # --- execution / booking -----------------------------------------------------------------------

    def _send(
        self, batch: list[Submission], snap: MarketSnapshot, executor: Executor, now: pd.Timestamp
    ) -> list[tuple[Fill, str]]:
        if not batch:
            return []
        equity = self.portfolio.equity()
        ctx = self._ctx(now, snap, equity, self.portfolio.holdings)
        r = check([self._order_check(o, ref, equity) for o, ref in batch], ctx)  # the last word
        ok = {o.client_id for o in r.approved}
        send = [(o, ref) for o, ref in batch if o.client_id in ok]
        for o, _ in r.vetoed:
            if o.intent != "open":
                self.exit_reason.pop(o.position_id, None)
        if not send:
            return []
        if self.on_submit is not None:
            self.on_submit(send)
        self.risk_state = replace(self.risk_state, orders_today=self.risk_state.orders_today + len(send))
        fills = executor.execute(send, snap)
        refs = {o.client_id: ref for o, ref in send}
        for f, pid in fills:
            self.book_fill(f, pid, refs.get(f.client_id, f.price))
        if self.on_booked is not None:
            self.on_booked()
        return fills

    @staticmethod
    def _fully_filled(batch: list[Submission], fills: list[tuple[Fill, str]]) -> dict[str, bool]:
        got: dict[str, float] = {}
        for f, _ in fills:
            got[f.client_id] = got.get(f.client_id, 0.0) + f.qty
        return {o.client_id: got.get(o.client_id, 0.0) >= o.qty * (1 - 1e-9) for o, _ in batch}

    def book_fill(self, fill: Fill, position_id: str, ref_price: float) -> None:
        """Book one fill (also used to replay fills recorded before a restart)."""
        pf = self.portfolio
        if position_id not in pf.positions:
            raise ValueError(f"fill {fill.client_id} for unknown position {position_id}")
        pf.apply_fill(fill, position_id, ref_price)
        self.ledger.on_fill(fill.asset, fill.ts, pf.holdings.get(fill.asset, 0.0), position_id)
        p = pf.positions[position_id]
        if p.qty.get(p.key, 0.0) == 0.0:
            reason = self.exit_reason.pop(position_id, "closed")
            closed = pf.close_position(position_id, fill.ts, reason)
            self.recent_closed[position_id] = (closed, fill.ts, reason)
            if reason == "stop":
                self.cooldown[p.key] = fill.ts + pd.Timedelta(hours=self.cfg.stop_cooldown_hours)
        else:
            q = abs(p.qty[p.key])
            p.risk_amount = self.cfg.leg_stop_frac * q * p.entry_price.get(p.key, p.entry_ref)

    def _drop_empty(self, position_id: str, report: bool = True) -> None:
        """Remove a pre-registered leg that never filled (``report``: it may have been persisted as
        open before its order was sent, so the Db must close it)."""
        p = self.portfolio.positions.get(position_id)
        if p is not None and p.qty.get(p.key, 0.0) == 0.0 and not p.pnl["fees"]:
            del self.portfolio.positions[position_id]
            if report:
                self.dropped.append(p)

    def _collect_closed(self, now: pd.Timestamp) -> list[tuple[Position, str]]:
        out = [(p, r) for p, t, r in self.portfolio.closed]
        self.portfolio.closed.clear()
        return out

    # --- marks, funding, expiry ----------------------------------------------------------------------

    def _mark(self, snap: MarketSnapshot | None) -> None:
        if snap is None:
            return
        marks = {}
        for a in self.portfolio.holdings:
            q = snap.quotes.get(a)
            if q is not None and math.isfinite(q.mark) and q.mark > 0:
                marks[a] = q.mark
        if marks:
            self.portfolio.mark(marks)

    def _settle(self, now: pd.Timestamp, store: FundingStore) -> tuple[list[Settlement], list[Position]]:
        grace = pd.Timedelta(hours=self.cfg.funding_unknown_grace_hours)
        out = self.ledger.settle(now, store, grace)
        self.pending_settlements += out
        touched: dict[str, Position] = {}
        pf = self.portfolio
        for s in out:
            amount = 0.0
            for pid, amt in s.amounts.items():
                pos = pf.positions.get(pid)
                if pos is None and pid in self.recent_closed:
                    pos = self.recent_closed[pid][0]
                    touched[pid] = pos
                if pos is not None:
                    pos.pnl["funding"] += amt
                    if not s.known:
                        pos.pnl["funding_unknown_bars"] += 1
                amount += amt
            pf.cash += amount
            pf.funding_log.append((s.period_start, s.asset, s.funding_rel, amount))
        return out, list(touched.values())

    def _expire(self, now: pd.Timestamp) -> None:
        for a in [a for a, t in self.cooldown.items() if t <= now]:
            del self.cooldown[a]
        for pid in [p for p, (_, t, _) in self.recent_closed.items() if now - t > RECENT_CLOSED]:
            del self.recent_closed[pid]
        self.portfolio.funding_log = self.portfolio.funding_log[-500:]

    # --- persistence -------------------------------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        pf = self.portfolio
        pos = [m.position_to_row("x", p, "open")["data"] for p in pf.positions.values()]
        closed = [
            [m.position_to_row("x", p, "closed")["data"], m.ts_to_str(t), r]
            for p, t, r in self.recent_closed.values()
        ]
        return {
            "cash": pf.cash,
            "holdings": dict(pf.holdings),
            "avg_entry": dict(pf.avg_entry),
            "marks": dict(pf.marks),
            "positions": pos,
            "recent_closed": closed,
            "risk": {
                k: v for k, v in state_to_dict(self.risk_state).items() if k not in ("kill_switch", "paused")
            },
            "flags": {"kill_switch": self.risk_state.kill_switch, "paused": self.risk_state.paused},
            "ledger": self.ledger.to_dict(),
            "cooldown": {a: m.ts_to_str(t) for a, t in self.cooldown.items()},
            "ineligible_since": {a: m.ts_to_str(t) for a, t in self.ineligible_since.items()},
            "last_rebalance": m.ts_to_str(self.last_rebalance) if self.last_rebalance is not None else None,
            "seq": self.seq,
            "exit_reason": dict(self.exit_reason),
        }

    @classmethod
    def from_state(
        cls,
        d: dict[str, Any],
        cfg: CarryConfig,
        instruments: dict[str, Instrument],
        fee_rate: float,
        mode: Literal["backtest", "paper"] = "paper",
    ) -> CarryCore:
        core = cls(cfg, instruments, fee_rate, mode)
        pf = core.portfolio
        pf.cash = float(d["cash"])
        pf.holdings = {a: float(v) for a, v in d["holdings"].items()}
        pf.avg_entry = {a: float(v) for a, v in d["avg_entry"].items()}
        pf.marks = {a: float(v) for a, v in d["marks"].items()}
        for data in d.get("positions", []):
            p = m.position_from_row({"data": data})
            pf.positions[p.id] = p
        for data, t, r in d.get("recent_closed", []):
            p = m.position_from_row({"data": data})
            core.recent_closed[p.id] = (p, m.ts_from_str(t), r)
        rs = dict(d.get("risk", {}))
        rs.update(d.get("flags", {}))
        core.risk_state = state_from_dict(rs)
        core.ledger = FundingLedger.from_dict(d.get("ledger", {}))
        core.cooldown = {a: m.ts_from_str(t) for a, t in d.get("cooldown", {}).items()}
        core.ineligible_since = {a: m.ts_from_str(t) for a, t in d.get("ineligible_since", {}).items()}
        lr = d.get("last_rebalance")
        core.last_rebalance = m.ts_from_str(lr) if lr else None
        core.seq = int(d.get("seq", 0))
        core.exit_reason = dict(d.get("exit_reason", {}))
        return core
