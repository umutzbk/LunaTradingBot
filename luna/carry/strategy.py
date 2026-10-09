"""Adaptive carry decision logic (pure; the same function drives paper, replay and backtests).

Book: k long + k short perp legs of ``leg_frac x equity`` each (``leg_frac = gross / 2k``), short the
highest FL30, long the lowest FL30, dollar-neutral. Monitoring is continuous; trading is not: a
change happens only when its expected advantage beats its cost.

Units. ``c(a, side) = carry_per_hour(side, FL30_a)`` is the expected funding income per hour per
USD of notional (shorts earn positive funding). Over the horizon ``H = switch_horizon_hours`` a leg
of notional ``n`` is expected to earn ``n x c x H`` USD. ``cost(a)`` = taker fee + live half spread +
slippage estimate, per side, fraction of notional. Money values below are USD over ``H``.

Order of one decision:

1. forced exits (given by the risk layer: stop, kill switch, ineligible beyond its grace, ...);
2. neutrality by leg count: if #longs != #shorts, either enter the best leg on the light side
   (``N c H - mult 2N cost - buffer``) or exit the worst-carry leg on the heavy side
   (``-n c H - mult n cost``), whichever is worth more (entries only when opens and rebalancing
   are allowed);
3. ``rebalance_to_target`` mode (V4 weekly semantics): at an allowed rebalance, exit every leg not
   in the ideal book and enter the missing ones, ignoring costs. Otherwise (``cost_aware``) up to
   ``max_actions_per_cycle`` greedy steps, best value first, over:
   - ENTER pair (free long and short slot): ``N (c_s + c_l) H - mult N 2(cost_s + cost_l) - 2 buffer``;
   - SWITCH a held leg ``o`` to the best free candidate ``x`` on the same side, only if ``o`` is
     at least ``min_hold_hours`` old, ``o`` ranks outside ``k + hold_rank_buffer`` on its side,
     ``x`` is in the ideal book, the improvement ``(c_x - c_o) H`` is at least
     ``min_carry_improvement_bps``, and
     ``value = N c_x H - n_o c_o H - mult (n_o cost_o + N cost_x) - buffer > 0``
     (the eventual exit of ``x`` replaces the eventual exit of ``o``, so it is not charged);
   - EXIT pair on funding-edge reversal: the worst short + worst long (both past min hold) when
     ``-(n_s c_s + n_l c_l) H - mult (n_s cost_s + n_l cost_l) - 2 buffer > 0``;
4. net-exposure trim: if |sum side x notional| > ``max_net_frac x equity``, reduce the largest
   leg of the heavier side by the excess (reduce-only: the risk engine forbids growing a leg).

An asset is touched at most once per decision (no exit and re-entry in one cycle). Every evaluated
alternative is returned as a record (HOLD / ENTER / EXIT / SWITCH with its economics) for logging.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from luna.carry.config import CarryConfig
from luna.carry.funding import carry_per_hour
from luna.carry.signals import AssetSignal

BPS = 1e-4
DecisionKind = Literal["HOLD", "ENTER", "EXIT", "SWITCH", "REJECT"]


@dataclass(frozen=True)
class HeldLeg:
    asset: str
    side: int  # +1 long, -1 short
    notional: float  # USD at the decision mid
    entry_ts: pd.Timestamp
    position_id: str


@dataclass(frozen=True)
class DecisionInput:
    now: pd.Timestamp
    equity: float
    signals: dict[str, AssetSignal]
    legs: tuple[HeldLeg, ...]
    forced: dict[str, str] = field(default_factory=dict)  # asset -> forced exit reason
    blocked: frozenset[str] = frozenset()  # cooldown: no new leg in these assets
    allow_rebalance: bool = True  # the evaluation trigger allows economic changes now
    opens_allowed: bool = True  # risk state allows new legs


@dataclass(frozen=True)
class LegOrder:
    asset: str
    side: int  # side of the leg (+1 long / -1 short), not the order side
    intent: Literal["open", "close", "reduce"]
    notional: float  # USD: open target, close = the whole leg, reduce = the amount removed
    position_id: str | None = None


@dataclass(frozen=True)
class Action:
    decision: DecisionKind
    legs: tuple[LegOrder, ...]
    reason: str
    side: int | None
    asset_out: str | None
    asset_in: str | None
    expected_carry: float  # USD over the horizon of what the book moves into
    current_carry: float  # USD over the horizon of what it gives up
    incremental_carry: float
    switch_cost: float  # estimated USD cost of the trades (fees + half spreads + slippage)
    safety_buffer: float
    value: float  # incremental - multiplier x cost - buffer (NaN: forced, not value-driven)
    forced: bool = False


@dataclass(frozen=True)
class Record:
    decision: DecisionKind
    reason: str
    side: int | None
    asset_out: str | None
    asset_in: str | None
    expected_carry: float
    current_carry: float
    incremental_carry: float
    switch_cost: float
    safety_buffer: float
    value: float


@dataclass
class Decision:
    actions: list[Action]
    records: list[Record]
    ideal_long: list[str]
    ideal_short: list[str]
    book_carry_h: float  # expected USD per hour of the book before the actions


def _rec(a: Action) -> Record:
    return Record(
        a.decision,
        a.reason,
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


class _Ctx:
    def __init__(self, inp: DecisionInput, cfg: CarryConfig) -> None:
        self.inp, self.cfg = inp, cfg
        self.s = inp.signals
        self.N = cfg.leg_frac * inp.equity
        self.H = cfg.switch_horizon_hours
        self.mult = cfg.switch_cost_multiplier
        self.buf = cfg.safety_buffer_bps * BPS * self.N
        elig = [
            a for a, x in self.s.items() if x.eligible and math.isfinite(x.fl) and math.isfinite(x.side_cost)
        ]
        self.desc = sorted(elig, key=lambda a: (-self.s[a].fl, a))
        self.asc = sorted(elig, key=lambda a: (self.s[a].fl, a))
        self.k = min(cfg.k, len(elig) // 2)
        self.ideal = {-1: self.desc[: self.k], 1: self.asc[: self.k]}

    def c(self, a: str, side: int) -> float:
        x = self.s.get(a)
        return carry_per_hour(side, x.fl) if x is not None and math.isfinite(x.fl) else math.nan

    def c0(self, a: str, side: int) -> float:
        """Carry with unknown treated as 0 (exits must stay possible without a funding signal)."""
        v = self.c(a, side)
        return v if math.isfinite(v) else 0.0

    def cost(self, a: str) -> float:
        x = self.s.get(a)
        return x.side_cost if x is not None else math.nan

    def side_rank(self, a: str, side: int) -> int | None:
        order = self.desc if side == -1 else self.asc
        return order.index(a) + 1 if a in order else None

    def best(self, side: int, exclude: set[str]) -> str | None:
        for a in self.desc if side == -1 else self.asc:
            if a not in exclude:
                return a
        return None


def decide(inp: DecisionInput, cfg: CarryConfig) -> Decision:
    x = _Ctx(inp, cfg)
    book: dict[str, HeldLeg] = {lg.asset: lg for lg in inp.legs}
    actions: list[Action] = []
    records: list[Record] = []
    used: set[str] = set()
    book_carry_h = sum(lg.notional * x.c0(lg.asset, lg.side) for lg in inp.legs)

    def act(a: Action) -> None:
        actions.append(a)
        records.append(_rec(a))
        for o in a.legs:
            used.add(o.asset)
            if o.intent == "close":
                book.pop(o.asset, None)
            elif o.intent == "open":
                book[o.asset] = HeldLeg(o.asset, o.side, o.notional, inp.now, "")
            else:  # reduce
                old = book[o.asset]
                book[o.asset] = HeldLeg(
                    old.asset, old.side, old.notional - o.notional, old.entry_ts, old.position_id
                )

    def exit_action(lg: HeldLeg, reason: str, forced: bool, value: float = math.nan) -> Action:
        carry = lg.notional * x.c0(lg.asset, lg.side) * x.H
        cost = lg.notional * x.cost(lg.asset)
        return Action(
            "EXIT",
            (LegOrder(lg.asset, lg.side, "close", lg.notional, lg.position_id),),
            reason,
            lg.side,
            lg.asset,
            None,
            0.0,
            carry,
            -carry,
            cost,
            0.0,
            value,
            forced,
        )

    def enter_one(a: str, side: int, reason: str, value: float) -> Action:
        carry = x.N * x.c(a, side) * x.H
        cost = 2 * x.N * x.cost(a)
        return Action(
            "ENTER",
            (LegOrder(a, side, "open", x.N),),
            reason,
            side,
            None,
            a,
            carry,
            0.0,
            carry,
            cost,
            x.buf,
            value,
        )

    # 1. forced exits
    for lg in sorted(inp.legs, key=lambda g: g.asset):
        if lg.asset in inp.forced:
            act(exit_action(lg, inp.forced[lg.asset], forced=True))

    opens = inp.opens_allowed and inp.allow_rebalance

    # 2. neutrality by leg count
    while True:
        longs = [g for g in book.values() if g.side == 1]
        shorts = [g for g in book.values() if g.side == -1]
        if len(longs) == len(shorts):
            break
        heavy, light = (1, -1) if len(longs) > len(shorts) else (-1, 1)
        side_legs = longs if heavy == 1 else shorts
        worst = min(side_legs, key=lambda g: (x.c0(g.asset, heavy), g.asset))
        u_cost = worst.notional * (x.cost(worst.asset) if math.isfinite(x.cost(worst.asset)) else 0.0)
        u_value = -worst.notional * x.c0(worst.asset, heavy) * x.H - x.mult * u_cost
        cand = x.best(light, set(book) | used | set(inp.blocked)) if opens else None
        r_value = -math.inf
        if cand is not None:
            r_value = x.N * x.c(cand, light) * x.H - x.mult * 2 * x.N * x.cost(cand) - x.buf
        if cand is not None and r_value > u_value:
            act(enter_one(cand, light, "neutrality_restore", r_value))
        else:
            act(exit_action(worst, "neutrality_unwind", forced=True, value=u_value))

    if cfg.decision_mode == "rebalance_to_target":
        if inp.allow_rebalance:
            _rebalance_to_target(x, book, used, inp, act, exit_action)
    elif opens:
        _cost_aware(x, book, used, inp, act, records)

    _hold_records(x, book, used, inp, records)
    _trim(x, book, inp, act)
    return Decision(actions, records, x.ideal[1], x.ideal[-1], book_carry_h)


def _rebalance_to_target(x: _Ctx, book, used, inp, act, exit_action) -> None:
    target = {**{a: 1 for a in x.ideal[1]}, **{a: -1 for a in x.ideal[-1]}}
    for lg in sorted(list(book.values()), key=lambda g: g.asset):
        if lg.asset not in used and target.get(lg.asset) != lg.side:
            act(exit_action(lg, "rebalance", forced=False))
    if not inp.opens_allowed:
        return
    new = {side: [a for a in x.ideal[side] if a not in book and a not in used] for side in (1, -1)}
    while new[1] or new[-1]:
        legs, assets = [], []
        for side in (1, -1):
            if new[side]:
                a = new[side].pop(0)
                legs.append(LegOrder(a, side, "open", x.N))
                assets.append(a)
        carry = sum(x.N * x.c(o.asset, o.side) for o in legs) * x.H
        cost = sum(2 * x.N * x.cost(o.asset) for o in legs)
        act(
            Action(
                "ENTER",
                tuple(legs),
                "rebalance",
                None,
                None,
                "+".join(assets),
                carry,
                0.0,
                carry,
                cost,
                0.0,
                math.nan,
            )
        )


def _cost_aware(x: _Ctx, book, used, inp, act, records) -> None:
    """Greedy, best value first. Only the final round's HOLD records are kept (earlier rounds are
    re-evaluated after each applied action); a round cut by ``max_actions_per_cycle`` keeps its
    unchosen positive alternatives as ``max_actions_reached:<reason>``."""
    cfg = x.cfg
    pending: list[Record] = []
    for i in range(max(0, cfg.max_actions_per_cycle) + 1):
        cands: list[Action] = []
        pending = []
        excl = set(book) | used | set(inp.blocked)
        # a) ENTER pair
        n_long = sum(1 for g in book.values() if g.side == 1)
        n_short = sum(1 for g in book.values() if g.side == -1)
        if n_long < x.k and n_short < x.k:
            xs = x.best(-1, excl)
            xl = x.best(1, excl | ({xs} if xs else set()))
            if xs is not None and xl is not None:
                gain = x.N * (x.c(xs, -1) + x.c(xl, 1)) * x.H
                cost = 2 * x.N * (x.cost(xs) + x.cost(xl))
                buf = 2 * x.buf
                value = gain - x.mult * cost - buf
                a = Action(
                    "ENTER",
                    (LegOrder(xl, 1, "open", x.N), LegOrder(xs, -1, "open", x.N)),
                    "pair_entry",
                    None,
                    None,
                    f"{xl}+{xs}",
                    gain,
                    0.0,
                    gain,
                    cost,
                    buf,
                    value,
                )
                if value > 0:
                    cands.append(a)
                else:
                    pending.append(
                        Record(
                            "HOLD",
                            "entry_carry_below_cost",
                            None,
                            None,
                            f"{xl}+{xs}",
                            gain,
                            0.0,
                            gain,
                            cost,
                            buf,
                            value,
                        )
                    )
        # b) SWITCH
        for side in (-1, 1):
            cand = x.best(side, excl)
            if cand is None:
                continue
            for lg in sorted(
                (g for g in book.values() if g.side == side and g.asset not in used), key=lambda g: g.asset
            ):
                a, rec = _switch(x, lg, cand, inp)
                if a is not None:
                    cands.append(a)
                else:
                    pending.append(rec)
        # c) EXIT pair on funding-edge reversal
        old = [
            g
            for g in book.values()
            if g.asset not in used and (inp.now - g.entry_ts) >= pd.Timedelta(hours=cfg.min_hold_hours)
        ]
        olds, oldl = [g for g in old if g.side == -1], [g for g in old if g.side == 1]
        if olds and oldl:
            s = min(olds, key=lambda g: (x.c0(g.asset, -1), g.asset))
            lo = min(oldl, key=lambda g: (x.c0(g.asset, 1), g.asset))
            carry = (s.notional * x.c0(s.asset, -1) + lo.notional * x.c0(lo.asset, 1)) * x.H
            cost = s.notional * x.cost(s.asset) + lo.notional * x.cost(lo.asset)
            value = -carry - x.mult * cost - 2 * x.buf
            if value > 0:
                cands.append(
                    Action(
                        "EXIT",
                        (
                            LegOrder(s.asset, -1, "close", s.notional, s.position_id),
                            LegOrder(lo.asset, 1, "close", lo.notional, lo.position_id),
                        ),
                        "funding_edge_reversed",
                        None,
                        f"{lo.asset}+{s.asset}",
                        None,
                        0.0,
                        carry,
                        -carry,
                        cost,
                        2 * x.buf,
                        value,
                    )
                )
        if not cands:
            break
        ranked = sorted(
            cands, key=lambda a: (a.value, a.decision, a.asset_out or "", a.asset_in or ""), reverse=True
        )
        if i == cfg.max_actions_per_cycle:  # budget spent: report what was left undone
            pending += [
                Record(
                    "HOLD",
                    "max_actions_reached:" + a.reason,
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
                for a in ranked
            ]
            break
        act(ranked[0])
    records.extend(pending)


def _switch(x: _Ctx, lg: HeldLeg, cand: str, inp: DecisionInput) -> tuple[Action | None, Record]:
    cfg, side = x.cfg, lg.side
    c_new, c_old = x.c(cand, side), x.c0(lg.asset, side)
    expected = x.N * c_new * x.H
    current = lg.notional * c_old * x.H
    incremental = expected - current
    cost_old = x.cost(lg.asset)
    cost = (lg.notional * cost_old if math.isfinite(cost_old) else math.nan) + x.N * x.cost(cand)
    value = incremental - x.mult * cost - x.buf
    rank = x.side_rank(lg.asset, side)

    def hold(reason: str) -> tuple[None, Record]:
        return None, Record(
            "HOLD", reason, side, lg.asset, cand, expected, current, incremental, cost, x.buf, value
        )

    if (inp.now - lg.entry_ts) < pd.Timedelta(hours=cfg.min_hold_hours):
        return hold("min_hold")
    if rank is not None and rank <= x.k + cfg.hold_rank_buffer:
        return hold("in_target_band")
    if cand not in x.ideal[side]:
        return hold("candidate_not_in_target")
    if not math.isfinite(cost):
        return hold("no_exit_quote")
    if not (c_new - c_old) * x.H >= cfg.min_carry_improvement_bps * BPS:
        return hold("below_min_improvement")
    if not value > 0:
        return hold("cost_exceeds_advantage")
    a = Action(
        "SWITCH",
        (LegOrder(lg.asset, side, "close", lg.notional, lg.position_id), LegOrder(cand, side, "open", x.N)),
        "carry_improvement",
        side,
        lg.asset,
        cand,
        expected,
        current,
        incremental,
        cost,
        x.buf,
        value,
    )
    return a, _rec(a)


def _hold_records(x: _Ctx, book, used, inp, records: list[Record]) -> None:
    """A HOLD record for every held leg the decision did not evaluate or touch."""
    seen = {r.asset_out for r in records}
    for lg in sorted(book.values(), key=lambda g: g.asset):
        if lg.asset in used or lg.asset in seen or not lg.position_id:
            continue
        carry = lg.notional * x.c0(lg.asset, lg.side) * x.H
        records.append(
            Record(
                "HOLD",
                "no_better_candidate" if inp.allow_rebalance else "not_rebalance_time",
                lg.side,
                lg.asset,
                None,
                carry,
                carry,
                0.0,
                0.0,
                0.0,
                0.0,
            )
        )


def _trim(x: _Ctx, book, inp, act) -> None:
    net = sum(g.side * g.notional for g in book.values())
    cap = x.cfg.max_net_frac * inp.equity
    if abs(net) <= cap:
        return
    heavy = 1 if net > 0 else -1
    legs = [g for g in book.values() if g.side == heavy and g.position_id]
    if not legs:
        return
    big = max(legs, key=lambda g: (g.notional, g.asset))
    amount = min(abs(net) - cap, big.notional)
    intent = "close" if amount >= big.notional * (1 - 1e-9) else "reduce"
    cost = amount * x.cost(big.asset)
    carry = amount * x.c0(big.asset, heavy) * x.H
    act(
        Action(
            "EXIT",
            (LegOrder(big.asset, heavy, intent, amount, big.position_id),),
            "net_exposure_trim",
            heavy,
            big.asset,
            None,
            0.0,
            carry,
            -carry,
            cost,
            0.0,
            math.nan,
            True,
        )
    )
