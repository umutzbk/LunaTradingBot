"""V4 accounting (spec §5.1 carry account, §5.2 H2 account; frozen).

Execution is at the mid of the executing quote; every trade additionally pays
``fee = rate * |q| * mid`` and ``spread = |q| * half_spread * mid`` (T4) from the leg's own cash.
The perp sleeve is marked to market: each perp position has a reference price; trades realise
``q * (px - ref)`` into perp cash and every daily valuation settles the open P&L. Funding settles into
perp cash (``luna.v4.funding.accrue_funding``: pro rata, settles at period end or position change,
missing periods pay 0 and are counted).

Daily P&L decomposition (exact by construction):
``ΔE = spot_price_pnl + perp_price_pnl + funding_cf - fees - spread_slippage - liquidation_penalty``.

Carry account (H1, H5, N-RANDENTRY, N-ALWAYS): E0 530 = spot sleeve 265 + perp sleeve 265, no sleeve
transfer; slot N = 0.30 x E_spot at entry, at most 3 slots and <= 90% of the spot sleeve; USD hedge
q_s = N / P_spot, q_p = -N / P_perp. Daily 00:00: margin buffer (perp_equity / perp_notional < 0.50
-> both legs reduced proportionally so the ratio returns to 1.00), hedge drift (> 5% -> perp leg
re-sized). Intraday liquidation when perp_equity <= 0.05 x perp_notional at the day's adverse marks
(both legs closed at the adverse prices, fees + spread + 0.5% penalty). Multi-leg actions are
all-or-nothing: a missing price or a leg below the minimum order skips the whole action (counted).

H2 account: E0 265 (perp only); dollar-neutral rebalance (each leg 0.5 / k x E) when the decider
says so (Mondays 00:00); a flat week closes everything; same margin / liquidation rules.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from luna.v4.constants import DAY
from luna.v4.costs import PERP_FALLBACK_FULL, CostRow
from luna.v4.funding import accrue_funding

EPS = 1e-12
LEGS = ("spot", "perp")


# --- primitives ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    mid: float
    half_spread_obs: float | None  # perp (historical analytics / forward) or forward spot; None: hist spot
    ts: pd.Timestamp
    fallback: bool = False  # perp spread fallback (10 bps full) used


@dataclass(frozen=True)
class LegMeta:
    """Freeze-time instrument metadata: quantity step, minimum quantity, minimum cost (spot costmin)."""

    step: float = 0.0
    min_qty: float = 0.0
    min_cost: float = 0.0


def round_qty(q: float, meta: LegMeta | None) -> float:
    if meta is None or meta.step <= 0:
        return q
    n = math.floor(abs(q) / meta.step + 1e-9)
    return math.copysign(n * meta.step, q) if n else 0.0


def order_ok(q: float, px: float, meta: LegMeta | None) -> bool:
    if abs(q) <= EPS:
        return False
    if meta is None:
        return True
    return abs(q) >= meta.min_qty - EPS and abs(q) * px >= meta.min_cost - EPS


def hedge_qty(notional: float, spot_px: float, perp_px: float) -> tuple[float, float]:
    """USD-equal hedge (§5.1): q_s = N / P_spot, q_p = -N / P_perp."""
    return notional / spot_px, -notional / perp_px


class Book:
    """Cash and positions of the spot and perp sleeves with cumulative cost / funding counters."""

    def __init__(self, spot_cash: float, perp_cash: float) -> None:
        self.spot_cash = float(spot_cash)
        self.perp_cash = float(perp_cash)
        self.spot_q: dict[str, float] = {}
        self.perp_q: dict[str, float] = {}
        self.perp_ref: dict[str, float] = {}
        self.costs = {k: {"spot": 0.0, "perp": 0.0} for k in ("fees", "spread", "penalty")}
        self.funding = 0.0
        self.asset_pnl: dict[str, float] = defaultdict(float)  # perp price + funding - costs, by asset

    def trade(self, asset: str, leg: str, dq: float, px: float, fee_rate: float, half_spread: float,
              penalty_rate: float = 0.0) -> None:
        fee, spread, pen = fee_rate * abs(dq) * px, abs(dq) * half_spread * px, penalty_rate * abs(dq) * px
        self.costs["fees"][leg] += fee
        self.costs["spread"][leg] += spread
        self.costs["penalty"][leg] += pen
        if leg == "spot":
            self.spot_cash -= dq * px + fee + spread + pen
            q = self.spot_q.get(asset, 0.0) + dq
            self._set(self.spot_q, asset, q)
            return
        self.settle(asset, px)
        self.perp_cash -= fee + spread + pen
        self.asset_pnl[asset] -= fee + spread + pen
        self.perp_ref[asset] = px
        self._set(self.perp_q, asset, self.perp_q.get(asset, 0.0) + dq)
        if asset not in self.perp_q:
            self.perp_ref.pop(asset, None)

    @staticmethod
    def _set(d: dict[str, float], asset: str, q: float) -> None:
        if abs(q) <= EPS:
            d.pop(asset, None)
        else:
            d[asset] = q

    def settle(self, asset: str, px: float) -> None:
        q = self.perp_q.get(asset, 0.0)
        if q:
            pnl = q * (px - self.perp_ref.get(asset, px))
            self.perp_cash += pnl
            self.asset_pnl[asset] += pnl
        if asset in self.perp_q:
            self.perp_ref[asset] = px

    def add_funding(self, cf: float, asset: str | None = None) -> None:
        self.perp_cash += cf
        self.funding += cf
        if asset is not None:
            self.asset_pnl[asset] += cf

    def spot_equity(self, spot_px: dict[str, float] | None = None) -> float:
        px = spot_px or {}
        return self.spot_cash + sum(q * px[a] for a, q in self.spot_q.items())

    def perp_equity(self, perp_px: dict[str, float] | None = None) -> float:
        px = perp_px or {}
        return self.perp_cash + sum(q * (px.get(a, self.perp_ref[a]) - self.perp_ref[a])
                                    for a, q in self.perp_q.items())

    def perp_notional(self, perp_px: dict[str, float]) -> float:
        return sum(abs(q) * perp_px.get(a, self.perp_ref[a]) for a, q in self.perp_q.items())

    def equity(self, spot_px: dict[str, float] | None = None,
               perp_px: dict[str, float] | None = None) -> float:
        return self.spot_equity(spot_px) + self.perp_equity(perp_px)

    def cost_totals(self) -> dict[str, dict[str, float]]:
        return {k: dict(v) for k, v in self.costs.items()}


class Market(Protocol):
    forward: bool

    def quote(self, asset: str, leg: str, t: pd.Timestamp, qty: float = 0.0) -> Quote | None: ...
    def value(self, asset: str, leg: str, day: pd.Timestamp) -> float | None: ...
    def extremes(self, asset: str, day: pd.Timestamp) -> dict[str, float | None]: ...
    def funding(self, asset: str) -> pd.DataFrame: ...
    def suspended(self, asset: str, t: pd.Timestamp) -> bool: ...
    def outage(self, day: pd.Timestamp) -> bool: ...


# --- shared simulation machinery -------------------------------------------------------------------


COUNTERS = ("skipped_actions", "min_order_skips", "funding_unknown_hours", "funding_held_hours",
            "hedge_rebalances", "margin_reductions", "liquidations", "outage_days", "perp_spread_fallbacks",
            "valuation_fallbacks", "forced_closes", "checks_skipped_outage", "insufficient_cash",
            "margin_check_skipped", "flat_weeks", "decisions", "entries", "exits")

COMPONENTS = ("spot_price_pnl", "perp_price_pnl", "funding_cf", "fees", "spread_slippage",
              "liquidation_penalty")


@dataclass
class SimResult:
    daily: pd.DataFrame
    trades: list[dict]
    counters: dict[str, float]
    margin_ratio_after: dict[pd.Timestamp, float] = field(default_factory=dict)


class _Sim:
    """State shared by the carry and H2 simulators: book, quotes, funding paths, counters, rows."""

    def __init__(self, market: Market, row: CostRow, meta: dict, spot_cash: float, perp_cash: float) -> None:
        self.m, self.row, self.meta = market, row, meta
        self.book = Book(spot_cash, perp_cash)
        self.c: dict[str, float] = {k: 0 for k in COUNTERS}
        self.last_px: dict[tuple[str, str], float] = {}
        self.last_obs: dict[tuple[str, str], float] = {}
        self.paths: dict[str, list[tuple[pd.Timestamp, float]]] = {}
        self.rows: list[dict] = []
        self.pending = {k: 0.0 for k in COMPONENTS}
        self.prev_equity = spot_cash + perp_cash
        self.margin_after: dict[pd.Timestamp, float] = {}

    # quotes & costs
    def quote(self, asset: str, leg: str, t: pd.Timestamp, qty: float = 0.0) -> Quote | None:
        q = self.m.quote(asset, leg, t, qty)
        if q is not None:
            self.last_px[(asset, leg)] = q.mid
            if q.half_spread_obs is not None:
                self.last_obs[(asset, leg)] = q.half_spread_obs
        return q

    def half(self, asset: str, leg: str, obs: float | None) -> float:
        if leg == "perp" and obs is None:
            obs = self.last_obs.get((asset, "perp"), PERP_FALLBACK_FULL / 2)
        if leg == "spot" and self.m.forward and obs is None:
            obs = self.last_obs.get((asset, "spot"), self.row.spot_full_spread_hist / 2)
        return self.row.half_spread(leg, obs, forward=self.m.forward)

    def leg_meta(self, asset: str, leg: str) -> LegMeta | None:
        return (self.meta.get(asset) or {}).get(leg)

    def fill(self, asset: str, leg: str, dq: float, px: float, ts: pd.Timestamp, obs: float | None,
             penalty: float = 0.0) -> None:
        if leg == "perp":
            self.paths.setdefault(asset, [(ts.floor("D"), self.book.perp_q.get(asset, 0.0))])
        self.book.trade(asset, leg, dq, px, self.row.fee(leg), self.half(asset, leg, obs), penalty)
        if leg == "perp":
            self.paths[asset].append((ts, self.book.perp_q.get(asset, 0.0)))

    def execute(self, orders: list[tuple[str, str, float, bool]], t: pd.Timestamp) -> list[tuple] | None:
        """All-or-nothing: ``orders`` = [(asset, leg, dq, is_full_close)]. Quotes for every leg must exist
        and every rounded opening / resizing leg must pass the minimum order; else nothing trades."""
        fills = []
        for asset, leg, dq, full_close in orders:
            q = self.quote(asset, leg, t, dq)
            if q is None:
                self.c["skipped_actions"] += 1
                return None
            qty = dq if full_close else round_qty(dq, self.leg_meta(asset, leg))
            if not full_close and not order_ok(qty, q.mid, self.leg_meta(asset, leg)):
                self.c["min_order_skips"] += 1
                return None
            fills.append((asset, leg, qty, q))
        for asset, leg, qty, q in fills:
            if leg == "perp" and q.fallback:
                self.c["perp_spread_fallbacks"] += 1
            self.fill(asset, leg, qty, q.mid, q.ts, q.half_spread_obs)
        return fills

    def px_now(self, asset: str, leg: str, t: pd.Timestamp) -> float | None:
        q = self.quote(asset, leg, t)
        return q.mid if q is not None else None

    # end of day
    def accrue(self, day: pd.Timestamp) -> float:
        until = day + DAY
        total = 0.0
        held = set(self.book.perp_q) | set(self.paths)
        for a in held:
            path = self.paths.get(a) or [(day, self.book.perp_q.get(a, 0.0))]
            out = accrue_funding(path, self.m.funding(a), until)
            cf = float(out["cashflow"].sum()) if len(out) else 0.0
            self.c["funding_unknown_hours"] += out.attrs["unknown_hours"]
            self.c["funding_held_hours"] += out.attrs["held_hours"]
            if cf:
                self.book.add_funding(cf, a)
            total += cf
        self.paths = {}
        return total

    def valuation_prices(self, day: pd.Timestamp) -> tuple[dict[str, float], dict[str, float]]:
        out: dict[str, dict[str, float]] = {"spot": {}, "perp": {}}
        for leg, pos in (("spot", self.book.spot_q), ("perp", self.book.perp_q)):
            for a in pos:
                v = self.m.value(a, leg, day)
                if v is None or not math.isfinite(v):
                    self.c["valuation_fallbacks"] += 1
                    v = self.last_px.get((a, leg), self.book.perp_ref.get(a) if leg == "perp" else None)
                if v is None:
                    raise RuntimeError(f"no price ever seen for {a} {leg}")
                out[leg][a] = v
                self.last_px[(a, leg)] = v
        return out["spot"], out["perp"]

    def close_day(self, day: pd.Timestamp, start: dict, active: bool, extra: dict | None = None) -> None:
        funding = self.accrue(day)
        spot_px, perp_px = self.valuation_prices(day)
        for a, px in perp_px.items():
            self.book.settle(a, px)
        spot_eq, perp_eq = self.book.spot_equity(spot_px), self.book.perp_equity(perp_px)
        costs = self.book.cost_totals()
        d = {k: {leg: costs[k][leg] - start["costs"][k][leg] for leg in LEGS} for k in costs}
        comp = {
            "spot_price_pnl": spot_eq - start["spot_eq"] + d["fees"]["spot"] + d["spread"]["spot"]
            + d["penalty"]["spot"],
            "perp_price_pnl": perp_eq - start["perp_eq"] - funding + d["fees"]["perp"] + d["spread"]["perp"]
            + d["penalty"]["perp"],
            "funding_cf": funding,
            "fees": d["fees"]["spot"] + d["fees"]["perp"],
            "spread_slippage": d["spread"]["spot"] + d["spread"]["perp"],
            "liquidation_penalty": d["penalty"]["spot"] + d["penalty"]["perp"],
        }
        spot_n = sum(q * spot_px[a] for a, q in self.book.spot_q.items())
        perp_n = sum(abs(q) * perp_px[a] for a, q in self.book.perp_q.items())
        perp_net = sum(q * perp_px[a] for a, q in self.book.perp_q.items())
        equity = spot_eq + perp_eq
        if self.m.outage(day):
            self.c["outage_days"] += 1
            for k in COMPONENTS:
                self.pending[k] += comp[k]
            comp = {k: 0.0 for k in COMPONENTS}
            reported = self.prev_equity
        else:
            comp = {k: comp[k] + self.pending[k] for k in COMPONENTS}
            self.pending = {k: 0.0 for k in COMPONENTS}
            reported = equity
        prev = self.prev_equity
        gross = spot_n + perp_n
        net = spot_n + perp_net
        self.rows.append({
            "day": day, "equity": reported, "ret": reported / prev - 1.0 if prev else 0.0, **comp,
            "gross": gross, "net": net, "hedge_error": net / gross if gross > EPS else 0.0,
            "net_price_pnl": comp["spot_price_pnl"] + comp["perp_price_pnl"],
            "active": active, "n_positions": len(set(self.book.spot_q) | set(self.book.perp_q)),
            **(extra or {}),
        })
        self.prev_equity = reported

    def snapshot(self, t: pd.Timestamp) -> dict:
        spot_px = {a: self.last_px.get((a, "spot"), 0.0) for a in self.book.spot_q}
        return {"spot_eq": self.book.spot_equity(spot_px), "perp_eq": self.book.perp_equity({}),
                "costs": self.book.cost_totals()}

    def margin_check(self, t: pd.Timestamp, params, spot_legs: bool) -> None:
        if not self.book.perp_q:
            return
        px = {}
        for a in self.book.perp_q:
            p = self.px_now(a, "perp", t)
            if p is None:
                self.c["margin_check_skipped"] += 1
                return
            px[a] = p
        eq, notional = self.book.perp_equity(px), self.book.perp_notional(px)
        if notional <= EPS or eq / notional >= params.margin_min:
            return
        k = {a: self.row.fee("perp") + self.half(a, "perp", self.last_obs.get((a, "perp"))) for a in px}
        c = sum(abs(q) * px[a] * k[a] for a, q in self.book.perp_q.items())
        # reducing by (1 - f) costs (1 - f) * c; solve E - (1 - f) c = target * f * notional for f
        denom = params.margin_target * notional - c
        f = (eq - c) / denom if denom > EPS else 0.0
        f = min(max(f, 0.0), 1.0)
        orders = []
        for a, q in sorted(self.book.perp_q.items()):
            orders.append((a, "perp", -(1 - f) * q, f == 0.0))
            if spot_legs and a in self.book.spot_q:
                orders.append((a, "spot", -(1 - f) * self.book.spot_q[a], f == 0.0))
        if self.execute(orders, t) is not None:
            self.c["margin_reductions"] += 1
            self.margin_after[t] = (self.book.perp_equity(px) / self.book.perp_notional(px)
                                    if self.book.perp_q else math.inf)

    def liquidation_check(self, t: pd.Timestamp, params, spot_legs: bool) -> bool:
        if not self.book.perp_q:
            return False
        adv, ext = {}, {}
        for a, q in self.book.perp_q.items():
            e = self.m.extremes(a, t.floor("D"))
            ext[a] = e
            p = e.get("perp_high") if q < 0 else e.get("perp_low")
            if p is None or not math.isfinite(p):
                return False
            adv[a] = p
        eq, notional = self.book.perp_equity(adv), self.book.perp_notional(adv)
        if notional <= EPS or eq > params.liq_ratio * notional:
            return False
        ts = t + pd.Timedelta(minutes=15)
        for a, q in sorted(self.book.perp_q.items()):
            self.fill(a, "perp", -q, adv[a], ts, self.last_obs.get((a, "perp")), params.liq_penalty)
            if spot_legs and a in self.book.spot_q:
                sp = ext[a].get("spot_low") or self.last_px.get((a, "spot"))
                self.fill(a, "spot", -self.book.spot_q[a], sp, ts, self.last_obs.get((a, "spot")),
                          params.liq_penalty)
        self.c["liquidations"] += 1
        return True


# --- carry account --------------------------------------------------------------------------------


@dataclass(frozen=True)
class CarryParams:
    e0: float = 530.0
    spot_share: float = 0.5
    slot_frac: float = 0.30
    max_slots: int = 3
    max_spot_util: float = 0.90
    margin_min: float = 0.50
    margin_target: float = 1.00
    liq_ratio: float = 0.05
    liq_penalty: float = 0.005
    hedge_tol: float = 0.05
    delist_penalty: float = 0.005


@dataclass
class CarryDecision:
    exits: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)


@dataclass
class CarryState:
    positions: dict[str, pd.Timestamp]
    last_exit: dict[str, pd.Timestamp]
    max_slots: int

    @property
    def free_slots(self) -> int:
        return self.max_slots - len(self.positions)


class CarryDecider(Protocol):
    def decide(self, t: pd.Timestamp, state: CarryState) -> CarryDecision: ...


def run_carry(market: Market, decider: CarryDecider, days: list[pd.Timestamp], row: CostRow, meta: dict,
              params: CarryParams | None = None) -> SimResult:
    params = params or CarryParams()
    sim = _Sim(market, row, meta, params.e0 * params.spot_share, params.e0 * (1 - params.spot_share))
    b = sim.book
    state = CarryState({}, {}, params.max_slots)
    open_trades: dict[str, dict] = {}
    trades: list[dict] = []

    def close(a: str, t: pd.Timestamp, forced: bool = False) -> bool:
        orders = [(a, "spot", -b.spot_q.get(a, 0.0), True), (a, "perp", -b.perp_q.get(a, 0.0), True)]
        orders = [o for o in orders if abs(o[2]) > EPS]
        if forced:
            ts = t + pd.Timedelta(minutes=15)
            for asset, leg, dq, _ in orders:
                px = sim.last_px.get((asset, leg))
                sim.fill(asset, leg, dq, px, ts, sim.last_obs.get((asset, leg)), params.delist_penalty)
            sim.c["forced_closes"] += 1
        elif sim.execute(orders, t) is None:
            return False
        finish(a, t)
        return True

    def finish(a: str, t: pd.Timestamp) -> None:
        state.positions.pop(a, None)
        state.last_exit[a] = t
        if a in open_trades:
            open_trades.pop(a)["exit_t"] = t
        sim.c["exits"] += 1

    for t in days:
        start = sim.snapshot(t)
        outage = market.outage(t)
        for a in sorted(state.positions):
            if market.suspended(a, t):
                close(a, t, forced=True)
        dec = decider.decide(t, state)
        for a in dec.exits:
            if a in state.positions:
                close(a, t)
        if outage:
            sim.c["checks_skipped_outage"] += 1
        else:
            sim.margin_check(t, params, spot_legs=True)
            for a in sorted(set(b.spot_q) & set(b.perp_q)):
                _hedge(sim, a, t, params)
        for a in [x for x in dec.entries if x not in state.positions][: max(state.free_slots, 0)]:
            if _enter(sim, a, t, params):
                state.positions[a] = t
                tr = {"asset": a, "entry_t": t, "q_spot": b.spot_q[a], "q_perp": b.perp_q[a], "exit_t": None}
                open_trades[a] = tr
                trades.append(tr)
                sim.c["entries"] += 1
        active = bool(b.spot_q or b.perp_q)
        if not outage and sim.liquidation_check(t, params, spot_legs=True):
            for a in list(state.positions):
                finish(a, t)
        sim.close_day(t, start, active)
    return SimResult(pd.DataFrame(sim.rows).set_index("day"), trades, sim.c, sim.margin_after)


def _hedge(sim: _Sim, a: str, t: pd.Timestamp, params: CarryParams) -> None:
    b = sim.book
    ps, pp = sim.px_now(a, "spot", t), sim.px_now(a, "perp", t)
    if ps is None or pp is None:
        sim.c["skipped_actions"] += 1
        return
    s_n, p_n = b.spot_q[a] * ps, abs(b.perp_q[a]) * pp
    if s_n <= EPS or abs(s_n - p_n) / s_n <= params.hedge_tol:
        return
    if sim.execute([(a, "perp", -s_n / pp - b.perp_q[a], False)], t) is not None:
        sim.c["hedge_rebalances"] += 1


def _enter(sim: _Sim, a: str, t: pd.Timestamp, params: CarryParams) -> bool:
    b = sim.book
    qs, qp = sim.quote(a, "spot", t), sim.quote(a, "perp", t)
    if qs is None or qp is None:
        sim.c["skipped_actions"] += 1
        return False
    spot_px = {x: (sim.px_now(x, "spot", t) or sim.last_px.get((x, "spot"), 0.0)) for x in b.spot_q}
    e_spot = b.spot_equity(spot_px)
    held = sum(q * spot_px[x] for x, q in b.spot_q.items())
    # N = 0.30 x E_spot, capped so that spot notional stays <= 90% of the spot sleeve and the spot cash
    # covers the purchase and its costs (costs of earlier entries otherwise block the third slot)
    cost_rate = 1 + sim.row.fee("spot") + sim.half(a, "spot", qs.half_spread_obs)
    n = min(params.slot_frac * e_spot, params.max_spot_util * e_spot - held, b.spot_cash / cost_rate)
    if n <= EPS:
        sim.c["insufficient_cash"] += 1
        return False
    q_s, q_p = hedge_qty(n, qs.mid, qp.mid)
    return sim.execute([(a, "spot", q_s, False), (a, "perp", q_p, False)], t) is not None


# --- H2 account -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class H2Params:
    e0: float = 265.0
    gross: float = 1.0
    margin_min: float = 0.50
    margin_target: float = 1.00
    liq_ratio: float = 0.05
    liq_penalty: float = 0.005
    delist_penalty: float = 0.005


@dataclass(frozen=True)
class H2Decision:
    kind: str  # "rebalance" | "flat"
    sides: dict[str, int]

    @staticmethod
    def rebalance(sides: dict[str, int]) -> H2Decision:
        return H2Decision("rebalance", dict(sides))

    @staticmethod
    def flat() -> H2Decision:
        return H2Decision("flat", {})


class H2Decider(Protocol):
    def decide(self, t: pd.Timestamp) -> H2Decision | None: ...


def run_h2(market: Market, decider: H2Decider, days: list[pd.Timestamp], row: CostRow, meta: dict,
           params: H2Params | None = None) -> SimResult:
    params = params or H2Params()
    sim = _Sim(market, row, meta, 0.0, params.e0)
    b = sim.book
    trades: list[dict] = []
    for t in days:
        start = sim.snapshot(t)
        pnl0 = dict(b.asset_pnl)
        side_before = {a: math.copysign(1, q) for a, q in b.perp_q.items()}
        outage = market.outage(t)
        for a in sorted(b.perp_q):
            if market.suspended(a, t):
                sim.fill(a, "perp", -b.perp_q[a], sim.last_px[(a, "perp")], t + pd.Timedelta(minutes=15),
                         sim.last_obs.get((a, "perp")), params.delist_penalty)
                sim.c["forced_closes"] += 1
        if outage:
            sim.c["checks_skipped_outage"] += 1
        else:
            sim.margin_check(t, params, spot_legs=False)
        dec = decider.decide(t)
        if dec is not None:
            sim.c["decisions"] += 1
            if dec.kind == "flat":
                sim.c["flat_weeks"] += 1
            fills = _rebalance(sim, dec.sides, t, params)
            for a, _, qty, q in fills or []:
                trades.append({"asset": a, "t": t, "qty": qty, "px": q.mid})
        active = bool(b.perp_q)
        if not outage:
            sim.liquidation_check(t, params, spot_legs=False)
        side = {**side_before, **{a: math.copysign(1, q) for a, q in b.perp_q.items()}}
        prev = sim.prev_equity
        sim.close_day(t, start, active)
        d = sim.rows[-1]
        pnl = {a: b.asset_pnl[a] - pnl0.get(a, 0.0) for a in b.asset_pnl}
        perp_px = {a: sim.last_px.get((a, "perp"), 0.0) for a in b.perp_q}
        d["carry_net"] = (d["funding_cf"] - d["fees"] - d["spread_slippage"]) / prev if prev else 0.0
        d["long_pnl"] = sum(v for a, v in pnl.items() if side.get(a, 0) > 0)
        d["short_pnl"] = sum(v for a, v in pnl.items() if side.get(a, 0) < 0)
        d["net_exposure"] = (sum(q * perp_px[a] for a, q in b.perp_q.items()) / d["equity"]
                             if d["equity"] else 0.0)
    return SimResult(pd.DataFrame(sim.rows).set_index("day"), trades, sim.c, sim.margin_after)


def _rebalance(sim: _Sim, sides: dict[str, int], t: pd.Timestamp, params: H2Params) -> list[tuple] | None:
    b = sim.book
    names = sorted(set(b.perp_q) | set(sides))
    px = {}
    for a in names:
        p = sim.px_now(a, "perp", t)
        if p is None:
            sim.c["skipped_actions"] += 1
            return None
        px[a] = p
    k = sum(1 for s in sides.values() if s > 0)
    eq = b.perp_equity(px)
    leg = params.gross * 0.5 / k * eq if k else 0.0
    orders = []
    for a in names:
        cur = b.perp_q.get(a, 0.0)
        target = sides.get(a, 0) * leg / px[a]
        dq = target - cur
        if abs(dq) <= EPS:
            continue
        if target == 0:
            orders.append((a, "perp", dq, True))
        elif cur == 0:
            orders.append((a, "perp", dq, False))
        else:  # resize of a continuing leg: below minimum -> only this adjustment is skipped
            r = round_qty(dq, sim.leg_meta(a, "perp"))
            if order_ok(r, px[a], sim.leg_meta(a, "perp")):
                orders.append((a, "perp", dq, False))
            else:
                sim.c["min_order_skips"] += 1
    return sim.execute(orders, t) if orders else []
