"""W parity verification (V5 historical backtest protocol §5). Replay-only; paper never imports it.

**Layer A — W-V4** (``run_w_v4``): the frozen V4 H2 rules re-implemented on V5 components:
- daily step at 00:00; rebalance on Mondays only; V4 ``elig_h2_30`` (listed >= 30 days, 30-day median
  perp volume >= $1M, >= 90 % of 720 hours of funding); fewer than 2k eligible -> flat week;
- long the k lowest FL30, short the k highest (ties alphabetical);
- fill = close of the first real trade bar in [t, t + 1h) at mid; fee and half spread booked
  separately (V4 analytics window [t - 1h, t), else 10 bps); all-or-nothing execution;
- held legs re-sized to 0.5 / k x E with the V4 minimum-order rule;
- daily margin buffer and liquidation check; valuation at the d 23:45 trade-bar close;
- no leg stop, no risk engine.

V5 components used: ``FundingStore`` (FL30, coverage), ``FundingLedger`` (pro-rata settlement, missing
periods pay 0), ``Portfolio`` (cash, avg-entry, spread_slip = s x (fill - ref) x qty). ``CarryCore``
is not used and not changed.

**Tooling:** ``compare_daily`` / ``compare_fills`` produce first-divergence reports (day / index,
asset, component, expected, actual, delta, state before and after) for §5.4.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from luna.carry import frozen
from luna.carry.funding import HOURS_PER_YEAR, FundingLedger, FundingStore
from luna.carry.history import ParityQuote, parity_quote
from luna.portfolio.portfolio import Portfolio, Position
from luna.types import ExitSpec, Fill, Leg, StopSpec

DAY = pd.Timedelta("1D")
BAR = pd.Timedelta("15min")
EPS = 1e-12

# V4 frozen constants (spec §3, §5.2; luna/v4 at b074b69)
E0 = 265.0
SIGNAL_DAYS = 30
MIN_VOLUME_USD = 1_000_000.0
VOLUME_DAYS, VOLUME_MIN_DAYS = 30, 27
H2_COVERAGE = 0.90
H2_MIN_AGE_DAYS = 30
MARGIN_MIN, MARGIN_TARGET = 0.50, 1.00
LIQ_RATIO, LIQ_PENALTY = 0.05, 0.005
PERP_FEE = 0.0005
FALLBACK_FULL = 0.0010

COUNTERS = (
    "decisions", "flat_weeks", "skipped_actions", "min_order_skips", "perp_spread_fallbacks",
    "margin_reductions", "liquidations", "margin_check_skipped", "valuation_fallbacks",
    "funding_unknown_hours", "funding_held_hours", "forced_closes",
)


@dataclass(frozen=True)
class BridgeFlags:
    """§5.5 bridge steps 1-4, each one rule of the V4 engine replaced (all off = W-V4):
    1 ``exec_v5``: §3 snapshot (mark mid at t) and §3.2 executor (fill at t + 15m on the mid of the
      snapshot at t + 15m, non-overlapping 5-minute fill window, fallback 10 bps);
    2 ``slip2``: 2 bps slippage on every fill;
    3 ``gates_v5``: V5 gates instead of ``elig_h2_30``; k = min(2, eligible // 2) instead of a flat week;
    4 ``no_resize``: held legs keep their size (R10)."""

    exec_v5: bool = False
    slip2: bool = False
    gates_v5: bool = False
    no_resize: bool = False


BRIDGE_STEPS = ("exec_v5", "slip2", "gates_v5", "no_resize")
SLIP_BPS = 2.0


class ParityUnsupported(RuntimeError):
    """An input or ordering whose V4 semantics W-V4 does not reproduce: stop, never guess."""


# --- inputs -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LegRule:
    step: float = 0.0
    min_qty: float = 0.0
    min_cost: float = 0.0


@dataclass
class ParityInputs:
    perp: dict[str, pd.DataFrame]
    mark: dict[str, pd.DataFrame]
    funding: dict[str, pd.DataFrame]
    spreads: dict[str, pd.DataFrame]
    instruments: dict[str, LegRule]

    @property
    def assets(self) -> list[str]:
        return sorted(self.perp)


def _read(root: Path, rel: str, bars: bool) -> pd.DataFrame | None:
    p = root / rel
    if not p.exists():
        return None
    df = frozen._utc(pd.read_parquet(p))
    return frozen.assert_bars_before(df, rel) if bars else frozen.assert_series_before(df, rel)


def load_parity_inputs(root: Path, expected_manifest_sha256: str | None = None) -> ParityInputs:
    """Inputs from the frozen copy only, verified against its manifest first."""
    root = frozen.open_frozen(root, expected_manifest_sha256)
    man = json.loads((root / frozen.MANIFEST).read_text(encoding="utf-8"))
    inst_raw = json.loads((root / frozen.INSTRUMENTS_REL).read_text(encoding="utf-8"))
    out = ParityInputs({}, {}, {}, {}, {})
    for a in man["assets"]:
        perp = _read(root, f"bars/kraken_futures/{a}.parquet", True)
        if perp is None:
            continue
        out.perp[a] = perp
        for kind, dst, bars in (("mark", out.mark, True), ("funding", out.funding, False),
                                ("spreads", out.spreads, False)):
            df = _read(root, f"history_v4/{kind}/{a}.parquet", bars)
            if df is not None:
                dst[a] = df
        leg = ((inst_raw.get("assets") or {}).get(a) or {}).get("perp")
        if leg:
            out.instruments[a] = LegRule(float(leg.get("step", 0.0)), float(leg.get("min_qty", 0.0)),
                                         float(leg.get("min_cost", 0.0)))
    return out


def funding_store(inp: ParityInputs) -> FundingStore:
    """V5 store of the frozen funding. A NaN row is refused: V4 keeps it (counts it in FL30_n, pays
    a finite abs) while ``FundingStore.merge_rows`` drops it, so parity would silently diverge."""
    store = FundingStore()
    for a, df in inp.funding.items():
        nan = df[["funding_abs", "funding_rel"]].isna().any(axis=1)
        if nan.any():
            raise ParityUnsupported(f"{a}: {int(nan.sum())} NaN funding rows (first {df.index[nan][0]})")
        store.merge_rows(a, [
            {"timestamp": ts.isoformat(), "fundingRate": fa, "relativeFundingRate": fr}
            for ts, fa, fr in zip(df.index, df["funding_abs"].to_numpy(), df["funding_rel"].to_numpy(), strict=True)
        ])
    return store


# --- V4 primitives -----------------------------------------------------------------------------------


def round_qty_v4(q: float, rule: LegRule | None) -> float:
    if rule is None or rule.step <= 0:
        return q
    n = math.floor(abs(q) / rule.step + 1e-9)
    return math.copysign(n * rule.step, q) if n else 0.0


def order_ok_v4(q: float, px: float, rule: LegRule | None) -> bool:
    if abs(q) <= EPS:
        return False
    if rule is None:
        return True
    return abs(q) >= rule.min_qty - EPS and abs(q) * px >= rule.min_cost - EPS


# --- signals (V4 elig_h2_30 + FL30 from the V5 FundingStore) -----------------------------------------


@dataclass(frozen=True)
class V4Signal:
    asset: str
    eligible: bool
    fl_annual: float
    n: int
    listed: bool
    age_days: float
    volume_median: float


class _AssetStatic:
    def __init__(self, bars: pd.DataFrame) -> None:
        real = bars[~bars["filled"].astype(bool)]
        self.first = real.index.min() if len(real) else None
        self.daily_vol = (real["volume"] * real["close"]).groupby(real.index.floor("D")).sum()

    def volume_median(self, t: pd.Timestamp) -> float:
        d = self.daily_vol
        w = d[(d.index >= t.floor("D") - VOLUME_DAYS * DAY) & (d.index < t.floor("D"))].dropna()
        return float(w.median()) if len(w) >= VOLUME_MIN_DAYS else math.nan


def v4_signals(inp: ParityInputs, store: FundingStore, t: pd.Timestamp,
               static: dict[str, _AssetStatic] | None = None) -> dict[str, V4Signal]:
    static = static if static is not None else {a: _AssetStatic(b) for a, b in inp.perp.items()}
    hours = SIGNAL_DAYS * 24
    out = {}
    for a in inp.assets:
        st = static[a]
        listed = st.first is not None and st.first <= t - BAR
        age = (t - st.first) / DAY if listed else math.nan
        vol = st.volume_median(t)
        m, n = store.trailing(a, t, hours)
        fl = m * HOURS_PER_YEAR
        ok = bool(listed and age >= H2_MIN_AGE_DAYS and vol >= MIN_VOLUME_USD and n >= H2_COVERAGE * hours)
        out[a] = V4Signal(a, ok, fl, n, bool(listed), age, vol)
    return out


def v4_h2_sides(sig: dict[str, V4Signal], k: int) -> dict[str, int] | None:
    """Long the k lowest FL30, short the k highest (ties alphabetical); None = flat week."""
    ok = [(s.fl_annual, a) for a, s in sig.items() if s.eligible and not math.isnan(s.fl_annual)]
    if len(ok) < 2 * k:
        return None
    order = [a for _, a in sorted(ok)]
    return {**{a: 1 for a in order[:k]}, **{a: -1 for a in order[-k:]}}


# --- W-V4 replay --------------------------------------------------------------------------------------


@dataclass
class WV4Result:
    daily: pd.DataFrame
    fills: list[dict]
    counters: dict[str, float]
    decisions: list[dict] = field(default_factory=list)
    valuations: dict[str, dict[str, float]] = field(default_factory=dict)


class _WV4:
    def __init__(self, inp: ParityInputs, fee_rate: float, spread_mult: float, k: int, gross: float,
                 bridge: BridgeFlags | None = None, hist=None, cfg=None) -> None:
        self.inp, self.fee_rate, self.spread_mult, self.k, self.gross = inp, fee_rate, spread_mult, k, gross
        self.bridge = bridge or BridgeFlags()
        self.cfg = cfg
        self.builder = None
        if self.bridge.exec_v5 or self.bridge.gates_v5:
            from luna.carry.history import SnapshotBuilder

            if hist is None or cfg is None:
                raise ValueError("bridge steps 1 / 3 need the historical data and the carry config")
            self.builder = SnapshotBuilder(hist)
            self.hist = hist
        self.pf = Portfolio("perp", E0)
        self.ledger = FundingLedger()
        self.store = funding_store(inp)
        self.static = {a: _AssetStatic(b) for a, b in inp.perp.items()}
        self.c: dict[str, float] = {k: 0.0 for k in COUNTERS}
        self.pos_of: dict[str, str] = {}
        self.all_pos: dict[str, Position] = {}
        self.n_pos = 0
        self.last_px: dict[str, float] = {}
        self.last_obs: dict[str, float] = {}
        self.quotes: dict[tuple[str, pd.Timestamp], ParityQuote | None] = {}
        self.costs = {"fees": 0.0, "spread": 0.0, "penalty": 0.0}
        self.fills: list[dict] = []
        self.decisions: list[dict] = []
        self.rows: list[dict] = []
        self.held_today: set[str] = set()
        self.prev_equity = E0
        self.valuations: dict[str, dict[str, float]] = {}
        self.value_override: dict[str, dict[str, float]] | None = None

    # quotes ----------------------------------------------------------------------------------------
    def quote(self, a: str, t: pd.Timestamp) -> ParityQuote | None:
        key = (a, t)
        if key not in self.quotes:
            self.quotes[key] = parity_quote(self.inp.perp.get(a), self.inp.spreads.get(a), t)
        q = self.quotes[key]
        if q is not None:
            self.last_px[a] = q.mid
            self.last_obs[a] = q.half
        return q

    def dmid(self, a: str, t: pd.Timestamp) -> float | None:
        """Decision price: V4 quote mid, or (bridge step 1) the §3 snapshot mid at t."""
        if self.bridge.exec_v5:
            m, src = self.builder.mid(a, t)
            return m if src is not None else None
        q = self.quote(a, t)
        return q.mid if q is not None else None

    def half(self, a: str, obs: float | None) -> float:
        if obs is None:
            obs = self.last_obs.get(a, FALLBACK_FULL / 2)
        return obs * self.spread_mult

    def value(self, a: str, day: pd.Timestamp) -> float | None:
        df = self.inp.perp.get(a)
        if df is None or not len(df):
            return None
        i = df.index.searchsorted(day + DAY - BAR, "right")
        return float(df["close"].iloc[i - 1]) if i > 0 else None

    def mark(self, a: str, t: pd.Timestamp) -> float | None:
        for src in (self.inp.mark.get(a), self.inp.perp.get(a)):
            if src is not None and len(src) and (t - BAR) in src.index:
                return float(src.at[t - BAR, "close"])
        return None

    def extremes(self, a: str, day: pd.Timestamp) -> tuple[float | None, float | None]:
        lo, hi = day + BAR, day + DAY
        for src in (self.inp.mark.get(a), self.inp.perp.get(a)):
            if src is not None and len(src):
                w = src.iloc[src.index.searchsorted(lo, "left") : src.index.searchsorted(hi, "left")]
                if len(w):
                    return float(w["high"].max()), float(w["low"].min())
        return None, None

    # book ------------------------------------------------------------------------------------------
    def held(self) -> dict[str, float]:
        return dict(self.pf.holdings)

    def equity_at(self, px: dict[str, float]) -> float:
        pf = self.pf
        return pf.cash + sum(q * (px.get(a, pf._mark(a)) - pf.avg_entry[a]) for a, q in pf.holdings.items())

    def fill(self, a: str, dq: float, px: float, ts: pd.Timestamp, obs: float | None, source: str,
             penalty: float = 0.0, *, fee_rate: float | None = None, half: float | None = None) -> None:
        """``half`` / ``fee_rate`` override the quote-derived values (B1 replays a logged fill)."""
        self.held_today.add(a)
        h = self.half(a, obs) if half is None else half
        s = 1.0 if dq > 0 else -1.0
        fee = (self.fee_rate if fee_rate is None else fee_rate) * abs(dq) * px
        pen = penalty * abs(dq) * px
        price = px * (1 + s * h)
        if self.bridge.slip2:
            price *= 1 + s * SLIP_BPS * 1e-4
        pid = self.pos_of.get(a)
        if pid is None:
            self.n_pos += 1
            pid = f"wv4:{a}:{self.n_pos:05d}"
            pos = Position(
                id=pid, strategy="w-v4", key=a, legs=(Leg(a, 1.0),), direction=int(s), qty={a: 0.0},
                entry_price={}, entry_ts=ts, entry_ref=px, stop=StopSpec("price", 0.0, 0.0),
                exit=ExitSpec(max_hold_bars=0), risk_amount=0.0, regime="w-v4",
            )
            self.pf.open_position(pos)
            self.pos_of[a], self.all_pos[pid] = pid, pos
        segs = self.ledger.path.get(a)
        if segs and ts.value < segs[-1][0]:
            raise ParityUnsupported(f"{a}: fill at {ts} before the last recorded change; V4 would "
                                    "double-count the overlap in its funding path")
        q0 = self.pf.holdings.get(a, 0.0)
        self.pf.apply_fill(Fill(f"{pid}:{len(self.fills)}", ts, a, "buy" if s > 0 else "sell", abs(dq), price,
                                fee, "taker"), pid, px)
        self.pf.cash -= pen
        spread = s * (price - px) * abs(dq)
        self.costs["fees"] += fee
        self.costs["spread"] += spread
        self.costs["penalty"] += pen
        q1 = self.pf.holdings.get(a, 0.0)
        self.ledger.on_fill(a, ts, q1, pid)
        if q1 == 0.0:
            self.pf.close_position(pid, ts, source)
            del self.pos_of[a]
        self.fills.append({
            "source": source, "ts": ts.isoformat(), "asset": a, "dq": float(dq), "px": float(px),
            "half_spread": float(h), "fee": fee, "spread": spread, "penalty": pen, "penalty_rate": penalty,
            "q_before": float(q0), "q_after": float(q1),
        })

    def execute(self, orders: list[tuple[str, float, bool]], t: pd.Timestamp, source: str) -> list | None:
        if self.bridge.exec_v5:
            return self._execute_v5(orders, t, source)
        fills = []
        for a, dq, full_close in orders:
            q = self.quote(a, t)
            if q is None:
                self.c["skipped_actions"] += 1
                return None
            rule = self.inp.instruments.get(a)
            qty = dq if full_close else round_qty_v4(dq, rule)
            if not full_close and not order_ok_v4(qty, q.mid, rule):
                self.c["min_order_skips"] += 1
                return None
            fills.append((a, qty, q))
        for a, qty, q in fills:
            if q.fallback:
                self.c["perp_spread_fallbacks"] += 1
            self.fill(a, qty, q.mid, q.ts, q.half, source)
        return fills

    def _execute_v5(self, orders: list[tuple[str, float, bool]], t: pd.Timestamp, source: str) -> list | None:
        """Bridge step 1: all-or-nothing as V4, filled at t + 15m (§3.2)."""
        planned = []
        for a, dq, full_close in orders:
            m = self.dmid(a, t)
            fq = self.builder.fill_quote(a, t)
            if m is None or fq is None:
                self.c["skipped_actions"] += 1
                return None
            rule = self.inp.instruments.get(a)
            qty = dq if full_close else round_qty_v4(dq, rule)
            if not full_close and not order_ok_v4(qty, m, rule):
                self.c["min_order_skips"] += 1
                return None
            planned.append((a, qty, fq))
        for a, qty, fq in planned:
            if fq.fallback:
                self.c["perp_spread_fallbacks"] += 1
            self.last_obs[a] = fq.half
            self.fill(a, qty, fq.mid, fq.ts, fq.half, source)
        return planned

    def sides_v5(self, t: pd.Timestamp) -> dict[str, int] | None:
        """Bridge step 3: V5 gates and ideal book at t; k = min(2, eligible // 2)."""
        from luna.carry.history import FundingPIT
        from luna.carry.signals import compute_signals
        from luna.carry.strategy import DecisionInput, decide

        snap, _ = self.builder.snapshot(t)
        eq = self.pf.equity()
        sig = compute_signals(snap, FundingPIT(self.store, t), t, self.cfg, self.cfg.leg_frac * eq, self.fee_rate)
        dec = decide(DecisionInput(t, eq, sig, (), {}, frozenset(), True, True), self.cfg)
        if not dec.ideal_long:
            return None
        return {**{a: 1 for a in dec.ideal_long}, **{a: -1 for a in dec.ideal_short}}

    # daily steps -----------------------------------------------------------------------------------
    def margin_check(self, t: pd.Timestamp) -> None:
        held = self.held()
        if not held:
            return
        px = {}
        for a in held:
            p = self.mark(a, t)
            if p is None:
                self.c["margin_check_skipped"] += 1
                return
            px[a] = p
        eq = self.equity_at(px)
        notional = sum(abs(q) * px[a] for a, q in held.items())
        if notional <= EPS or eq / notional >= MARGIN_MIN:
            return
        kk = {a: self.fee_rate + self.half(a, self.last_obs.get(a)) for a in px}
        c = sum(abs(q) * px[a] * kk[a] for a, q in held.items())
        denom = MARGIN_TARGET * notional - c
        f = (eq - c) / denom if denom > EPS else 0.0
        f = min(max(f, 0.0), 1.0)
        orders = [(a, -(1 - f) * q, f == 0.0) for a, q in sorted(held.items())]
        if self.execute(orders, t, "margin") is not None:
            self.c["margin_reductions"] += 1

    def liquidation_check(self, t: pd.Timestamp) -> None:
        held = self.held()
        if not held:
            return
        adv = {}
        for a, q in held.items():
            hi, lo = self.extremes(a, t.floor("D"))
            p = hi if q < 0 else lo
            if p is None or not math.isfinite(p):
                return
            adv[a] = p
        eq = self.equity_at(adv)
        notional = sum(abs(q) * adv[a] for a, q in held.items())
        if notional <= EPS or eq > LIQ_RATIO * notional:
            return
        for a, q in sorted(held.items()):
            self.fill(a, -q, adv[a], t + BAR, self.last_obs.get(a), "liquidation", LIQ_PENALTY)
        self.c["liquidations"] += 1

    def rebalance(self, sides: dict[str, int], t: pd.Timestamp) -> list | None:
        held = self.held()
        names = sorted(set(held) | set(sides))
        px = {}
        for a in names:
            m = self.dmid(a, t)
            if m is None:
                self.c["skipped_actions"] += 1
                return None
            px[a] = m
        k = sum(1 for s in sides.values() if s > 0)
        eq = self.equity_at(px)
        leg = self.gross * 0.5 / k * eq if k else 0.0
        orders = []
        for a in names:
            cur = held.get(a, 0.0)
            target = sides.get(a, 0) * leg / px[a]
            dq = target - cur
            if abs(dq) <= EPS:
                continue
            if target == 0:
                orders.append((a, dq, True))
            elif cur == 0:
                orders.append((a, dq, False))
            elif self.bridge.no_resize and (target > 0) == (cur > 0):
                continue  # bridge step 4: a held leg keeps its size
            else:
                r = round_qty_v4(dq, self.inp.instruments.get(a))
                if order_ok_v4(r, px[a], self.inp.instruments.get(a)):
                    orders.append((a, dq, False))
                else:
                    self.c["min_order_skips"] += 1
        return self.execute(orders, t, "rebalance") if orders else []

    def settle_funding(self, until: pd.Timestamp) -> float:
        total = 0.0
        for s in self.ledger.settle(until, self.store, pd.Timedelta(0)):
            self.c["funding_held_hours"] += s.held_hours
            if not s.known:
                self.c["funding_unknown_hours"] += s.held_hours
            for pid, amt in s.amounts.items():
                self.pf.cash += amt
                self.all_pos[pid].pnl["funding"] += amt
                total += amt
        return total

    def close_day(self, day: pd.Timestamp, start: dict, active: bool) -> None:
        funding = self.settle_funding(day + DAY)
        px = {}
        for a in self.pf.holdings:
            if self.value_override is not None:
                v = self.value_override[day.isoformat()][a]
                px[a] = v
                continue
            v = self.value(a, day)
            if v is None or not math.isfinite(v):
                self.c["valuation_fallbacks"] += 1
                v = self.last_px.get(a)
            if v is None:
                raise RuntimeError(f"no price ever seen for {a}")
            px[a] = v
            self.last_px[a] = v
        if px:
            self.pf.mark(px)
        self.valuations[day.isoformat()] = dict(px)
        eq = self.pf.equity()
        fees = self.costs["fees"] - start["fees"]
        spread = self.costs["spread"] - start["spread"]
        pen = self.costs["penalty"] - start["penalty"]
        prev = self.prev_equity
        self.rows.append({
            "day": day, "equity": eq, "ret": eq / prev - 1.0 if prev else 0.0,
            "perp_price_pnl": eq - start["eq"] - funding + fees + spread + pen,
            "funding_cf": funding, "fees": fees, "spread_slippage": spread, "liquidation_penalty": pen,
            "carry_net": (funding - fees - spread) / prev if prev else 0.0,
            "active": active, "n_positions": len(self.pf.holdings),
            "held": ",".join(sorted(self.held_today | set(self.pf.holdings))),
            "gross": sum(abs(q) * px[a] for a, q in self.pf.holdings.items()),
        })
        self.prev_equity = eq

    def step(self, t: pd.Timestamp) -> None:
        start = {"eq": self.pf.equity(), **self.costs}
        self.held_today = set(self.pf.holdings)
        self.margin_check(t)
        if t.weekday() == 0:
            sig = v4_signals(self.inp, self.store, t, self.static)
            sides = self.sides_v5(t) if self.bridge.gates_v5 else v4_h2_sides(sig, self.k)
            self.c["decisions"] += 1
            if sides is None:
                self.c["flat_weeks"] += 1
            self.decisions.append({
                "t": t.isoformat(), "flat": sides is None, "sides": sides or {},
                "eligible": sorted(a for a, s in sig.items() if s.eligible),
            })
            self.rebalance(sides or {}, t)
        active = bool(self.pf.holdings)
        self.liquidation_check(t)
        self.close_day(t, start, active)


def run_w_v4(inp: ParityInputs, days: list[pd.Timestamp], *, fee_rate: float = PERP_FEE, spread_mult: float = 1.0,
             k: int = 2, gross: float = 1.0, bridge: BridgeFlags | None = None, hist=None, cfg=None) -> WV4Result:
    sim = _WV4(inp, fee_rate, spread_mult, k, gross, bridge, hist, cfg)
    for t in days:
        sim.step(t)
    daily = pd.DataFrame(sim.rows).set_index("day")
    return WV4Result(daily, sim.fills, dict(sim.c), sim.decisions, sim.valuations)


def run_bridge(inp: ParityInputs, hist, cfg, days: list[pd.Timestamp], *, order: tuple[str, ...] = BRIDGE_STEPS,
               auth=None) -> list[tuple[str, WV4Result]]:
    """§5.5: W-V4, then the steps switched on cumulatively in ``order`` (forward or reverse)."""
    from luna.carry.backtest import _require_authorization

    _require_authorization(hist, auth)
    if sorted(order) != sorted(BRIDGE_STEPS):
        raise ValueError(f"order must be a permutation of {BRIDGE_STEPS}")
    out = [("W-V4", run_w_v4(inp, days))]
    on: dict[str, bool] = {}
    for step in order:
        on[step] = True
        out.append((f"+{step}", run_w_v4(inp, days, bridge=BridgeFlags(**on), hist=hist, cfg=cfg)))
    return out


def replay_fill_log(log: list[dict], valuations: dict[str, dict[str, float]], store: FundingStore,
                    days: list[pd.Timestamp], e0: float = E0) -> pd.DataFrame:
    """B1 (§5.3): book a logged fill sequence (ts, asset, dq, px, half_spread, fee_rate, penalty_rate)
    through V5 ``Portfolio`` + ``FundingLedger`` + cost arithmetic; value each day at the logged
    valuation prices. Returns the daily components (same columns as ``run_w_v4``)."""
    empty = ParityInputs({}, {}, {}, {}, {})
    sim = _WV4(empty, 0.0, 1.0, 2, 1.0)
    sim.store = store
    sim.pf.cash = sim.prev_equity = float(e0)
    sim.value_override = valuations
    by_day: dict[pd.Timestamp, list[dict]] = {}
    for f in log:
        by_day.setdefault(pd.Timestamp(f["ts"]).floor("D"), []).append(f)
    for t in days:
        start = {"eq": sim.pf.equity(), **sim.costs}
        sim.held_today = set(sim.pf.holdings)
        for f in by_day.get(t, []):
            sim.fill(f["asset"], f["dq"], f["px"], pd.Timestamp(f["ts"]), None, f.get("source", "log"),
                     f.get("penalty_rate", 0.0), fee_rate=f["fee_rate"], half=f["half_spread"])
        sim.close_day(t, start, bool(sim.pf.holdings))
    return pd.DataFrame(sim.rows).set_index("day")


# --- metrics (V4 formulas, re-implemented) -----------------------------------------------------------


def sharpe(r: pd.Series) -> float:
    r = pd.Series(r).dropna()
    sd = r.std(ddof=1) if len(r) > 1 else math.nan
    return float(r.mean() / sd * math.sqrt(365)) if sd and sd > 0 else math.nan


def max_drawdown(r: pd.Series) -> float:
    eq = np.cumprod(1 + pd.Series(r).fillna(0.0).to_numpy())
    if not len(eq):
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    return float(np.max(1 - eq / peak))


# --- first-divergence reports -------------------------------------------------------------------------


def _iso(x) -> str:
    return x.isoformat() if hasattr(x, "isoformat") else str(x)


def compare_daily(ref: pd.DataFrame, got: pd.DataFrame, tol: dict[str, float]) -> dict:
    """Per component: max |delta|, first day over tolerance; overall first divergence (earliest day,
    then component order) with the equity state before and after when available."""
    by = {}
    first = None
    days = ref.index
    if not got.index.equals(days):
        missing = sorted(set(days) ^ set(got.index))
        return {"pass": False, "first": {"day": _iso(missing[0]) if missing else None, "component": "index",
                                         "expected": len(days), "actual": len(got.index), "delta": None},
                "by_component": {}}
    for c, t in tol.items():
        if c not in ref or c not in got:
            first = first or {"day": None, "component": c, "expected": c in ref, "actual": c in got,
                              "delta": None, "note": "column missing"}
            by[c] = {"max_abs_delta": math.inf, "tol": t, "n_bad": -1, "first_day": None}
            continue
        r, g = ref[c].astype(float), got[c].astype(float)
        d = (g - r).abs()
        nan_bad = r.isna() != g.isna()
        d = d.where(~(r.isna() & g.isna()), 0.0)  # both NaN: same value
        bad = d[(d > t) | nan_bad]
        by[c] = {"max_abs_delta": float(np.fmax.reduce(d.fillna(np.inf).to_numpy())) if len(d) else 0.0, "tol": t, "n_bad": int(len(bad)),
                 "first_day": _iso(bad.index[0]) if len(bad) else None}
        if len(bad):
            day = bad.index[0]
            i = days.get_loc(day)
            cand = {"day": _iso(day), "component": c, "expected": float(ref.at[day, c]),
                    "actual": float(got.at[day, c]), "delta": float(got.at[day, c] - ref.at[day, c])}
            if "equity" in ref and "equity" in got:
                cand["state_before"] = {
                    "expected_equity": float(ref["equity"].iloc[i - 1]) if i else None,
                    "actual_equity": float(got["equity"].iloc[i - 1]) if i else None,
                }
                cand["state_after"] = {"expected_equity": float(ref.at[day, "equity"]),
                                       "actual_equity": float(got.at[day, "equity"])}
            if "held" in ref and "held" in got:
                cand["held_expected"], cand["held_actual"] = str(ref.at[day, "held"]), str(got.at[day, "held"])
            if first is None or (first["day"] is not None
                                 and (cand["day"], list(tol).index(c)) < (first["day"], list(tol).index(first["component"]))):
                first = cand
    return {"pass": first is None, "first": first, "by_component": by}


def compare_fills(expected: list[dict], actual: list[dict], rel_tol: float,
                  exact: tuple[str, ...] = ("ts", "asset"), numeric: tuple[str, ...] = ("dq", "px")) -> dict:
    """Fill-by-fill: exact keys must match, numeric keys within relative tolerance."""
    first = None
    max_rel = 0.0
    for i in range(max(len(expected), len(actual))):
        if i >= len(expected) or i >= len(actual):
            first = first or {"index": i, "field": "count", "expected": len(expected), "actual": len(actual),
                              "delta": len(actual) - len(expected)}
            break
        e, a = expected[i], actual[i]
        for f in exact:
            if e[f] != a[f] and first is None:
                first = {"index": i, "field": f, "expected": e[f], "actual": a[f], "delta": None}
        for f in numeric:
            den = max(abs(e[f]), EPS)
            rel = abs(a[f] - e[f]) / den
            if math.isnan(e[f]) and math.isnan(a[f]):
                rel = 0.0
            elif math.isnan(rel):
                rel = math.inf
            max_rel = max(max_rel, rel)
            if rel > rel_tol and first is None:
                first = {"index": i, "field": f, "expected": e[f], "actual": a[f], "delta": a[f] - e[f]}
        if first is not None and "state_before" not in first:
            first["asset"] = e.get("asset")
            first["ts"] = e.get("ts")
            first["state_before"] = expected[i - 1] if i else None
            first["expected_fill"], first["actual_fill"] = e, a
    return {"pass": first is None, "n_expected": len(expected), "n_actual": len(actual),
            "max_rel_delta": max_rel, "first": first}
