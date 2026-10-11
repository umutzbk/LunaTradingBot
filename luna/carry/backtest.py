"""V5 historical backtest (frozen protocol rev 3, §3-§10). Replay-only: paper never imports it.

Every arm runs the unchanged ``CarryCore.run_cycle`` on the 15-minute historical snapshots of
``history.SnapshotBuilder``; only this adapter is replay-specific:

- ``DelayedExecutor`` (§3.2): an order sent at t fills at t + 15m at
  ``mid_{t+15m} x (1 +/- fill_half) x (1 +/- slip)`` (no IOC band), taker fee on the notional;
  without a t + 15m price it does not fill. Fills are booked inside the cycle at t with their own
  timestamp t + 15m (declared dependency M6(b)); the daily accounting assigns every fill to the
  day of its timestamp.
- ``Schedule`` (§3.2): a scheduled rebalance (W, D, C) stays permitted until it completes or has
  been tried in 4 counted cycles (``opens_allowed`` and a valid snapshot); the core's own trigger
  is not used. E is permitted at every snapshot.
- Daily valuation (§3.2): E_d at d + 1 00:00 after mark and settlement, before the fills stamped at
  that instant (those belong to d + 1).

Every per-fill attribute the protocol reports (decision / fill fallback, spread_slip, delay_drift,
hour, asset) is recorded in the fill log, from which the fallback exposure (§10) is computed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from luna.carry.config import CarryConfig
from luna.carry.core import CarryCore, CycleResult
from luna.carry.frozen import CUTOFF
from luna.carry.history import (
    KNOWN_AFTER,
    FundingPIT,
    HistoricalData,
    SnapshotBuilder,
    lookback_p90,
)
from luna.types import Fill

OUTCOME_START = pd.Timestamp("2025-11-10T00:00Z")
M15 = pd.Timedelta("15min")
DAY = pd.Timedelta("1D")
MAX_SCHEDULED_ATTEMPTS = 4
FEE_RATE = 0.0005

ARMS: dict[str, dict] = {
    "W": {"decision_mode": "rebalance_to_target", "evaluation": {"trigger": "weekly", "weekday": 0, "at_hour": 0}},
    "D": {"decision_mode": "rebalance_to_target", "evaluation": {"trigger": "daily", "at_hour": 0}},
    "C": {"decision_mode": "cost_aware", "evaluation": {"trigger": "daily", "at_hour": 0}},
    "E": {"decision_mode": "cost_aware", "evaluation": {"trigger": "event"}},
}
VARIANTS: dict[str, tuple[str, dict]] = {
    "P-TIGHT": ("E", {"switch_horizon_hours": 72, "min_carry_improvement_bps": 10, "switch_cost_multiplier": 2.0,
                      "safety_buffer_bps": 10, "min_hold_hours": 48}),
    "P-LOOSE": ("E", {"switch_horizon_hours": 336, "min_carry_improvement_bps": 2.5, "switch_cost_multiplier": 1.0,
                      "safety_buffer_bps": 2.5, "min_hold_hours": 12}),
    "P-C8": ("C", {"evaluation": {"trigger": "interval", "interval_hours": 8}}),
}


@dataclass(frozen=True)
class RowSpec:
    """A sensitivity row (§9): only the fill-side cost multiplier, the spread source, the known-time
    rule or the R4 kill differ."""

    name: str
    cost_mult: float = 1.0
    spread_mode: str = "primary"
    known_after: pd.Timedelta = KNOWN_AFTER
    fb_cons: bool = False
    nokill: bool = False


ROWS: dict[str, RowSpec] = {
    "PRIMARY": RowSpec("PRIMARY"),
    "S-COST2X": RowSpec("S-COST2X", cost_mult=2.0),
    "S-SPREAD-FIX": RowSpec("S-SPREAD-FIX", spread_mode="fixed"),
    "S-NOKILL": RowSpec("S-NOKILL", nokill=True),
    "S-PIT10": RowSpec("S-PIT10", known_after=pd.Timedelta("10min")),
    "S-FB-CONS": RowSpec("S-FB-CONS", fb_cons=True),
    "S-PIT10/NOKILL": RowSpec("S-PIT10/NOKILL", known_after=pd.Timedelta("10min"), nokill=True),
    "S-FB-CONS/NOKILL": RowSpec("S-FB-CONS/NOKILL", fb_cons=True, nokill=True),
}


def arm_config(base: CarryConfig, arm: str, *, variant: str | None = None, nokill: bool = False) -> CarryConfig:
    """``base`` with only ``decision_mode`` and ``trigger`` set for the arm (R6); a §7 variant
    changes its listed parameters; S-NOKILL disables only the R4 drawdown kill."""
    data = base.model_dump()
    spec = ARMS[arm]
    data["decision_mode"] = spec["decision_mode"]
    data["evaluation"] = {**data["evaluation"], **spec["evaluation"]}
    if variant is not None:
        varm, over = VARIANTS[variant]
        if varm != arm:
            raise ValueError(f"variant {variant} belongs to arm {varm}")
        for k, v in over.items():
            data[k] = {**data[k], **v} if isinstance(v, dict) else v
    if nokill:
        data["risk"] = {**data["risk"], "max_drawdown": 1.0}
    return CarryConfig(**data)


def builder_for(data: HistoricalData, row: RowSpec) -> SnapshotBuilder:
    fb = lookback_p90(data.spreads) if row.fb_cons else None
    return SnapshotBuilder(data, known_after=row.known_after, spread_mode=row.spread_mode, fallback_full=fb)


# --- outcome guard -----------------------------------------------------------------------------------


class OutcomeBlocked(RuntimeError):
    """A run on the real frozen inputs without the outcome authorization."""


class OutcomeAuthorization:
    """Produced only by ``luna.carry.outcome_guard.authorize`` (all §12 checks passed)."""

    _secret = object()

    def __init__(self, key: object, record: dict) -> None:
        if key is not OutcomeAuthorization._secret:
            raise OutcomeBlocked("an OutcomeAuthorization can only come from outcome_guard.authorize")
        self.record = record


def _require_authorization(data: HistoricalData, auth: OutcomeAuthorization | None) -> None:
    if data.real and not isinstance(auth, OutcomeAuthorization):
        raise OutcomeBlocked(
            "real frozen inputs: the single historical outcome run needs the user's explicit approval "
            "(outcome_guard.authorize); dry runs use synthetic data"
        )


# --- execution ---------------------------------------------------------------------------------------


class DelayedExecutor:
    """§3.2 fills at t + 15m. ``info`` is the snapshot's per-asset ``QuoteInfo`` (set by the runner)."""

    def __init__(self, builder: SnapshotBuilder, fee_rate: float, slippage_bps: float, cost_mult: float = 1.0):
        self.builder, self.fee, self.slip, self.mult = builder, fee_rate, slippage_bps * 1e-4, cost_mult
        self.info: dict = {}
        self.log: list[dict] = []
        self.unfilled: list[dict] = []

    def execute(self, batch, snap) -> list[tuple[Fill, str]]:
        t = snap.ts
        out = []
        for o, ref in batch:
            fq = self.builder.fill_quote(o.asset, t)
            if fq is None:
                self.unfilled.append({"decision_ts": t, "asset": o.asset, "intent": o.intent, "qty": o.qty})
                continue
            s = 1.0 if o.side == "buy" else -1.0
            half, slip = fq.half * self.mult, self.slip * self.mult
            px = fq.mid * (1 + s * half) * (1 + s * slip)
            fee = self.fee * self.mult * o.qty * px
            info = self.info.get(o.asset)
            self.log.append({
                "decision_ts": t, "ts": fq.ts, "asset": o.asset, "side": o.side, "intent": o.intent,
                "qty": o.qty, "dq": s * o.qty, "px": px, "mid_t": ref, "mid_fill": fq.mid, "half": half,
                "fee": fee, "spread_slip": s * o.qty * (px - fq.mid), "delay_drift": s * o.qty * (fq.mid - ref),
                "notional": o.qty * fq.mid, "dec_fallback": bool(info.dec_fallback) if info else True,
                "fill_fallback": bool(fq.fallback), "position_id": o.position_id,
            })
            out.append((Fill(o.client_id, fq.ts, o.asset, o.side, o.qty, px, fee, "taker"), o.position_id))
        return out


def slot(ev, t: pd.Timestamp) -> pd.Timestamp | None:
    """The scheduled rebalance slot at or before t (None for the event trigger)."""
    if ev.trigger == "event":
        return None
    day = t.floor("D")
    if ev.trigger == "interval":
        step = pd.Timedelta(hours=ev.interval_hours)
        return day + ((t - day) // step) * step
    s = day + pd.Timedelta(hours=ev.at_hour)
    if s > t:
        s -= DAY
    if ev.trigger == "weekly":
        s -= pd.Timedelta(days=(s.weekday() - ev.weekday) % 7)
    return s


def completed(res: CycleResult) -> bool:
    """§3.2 completion: every group sent and fully filled, no REJECT / close_not_filled /
    forced_exit_not_filled record."""
    if res.forced_blocked or any(r.decision == "REJECT" for r in res.records):
        return False
    got: dict[str, float] = {}
    for f, _ in res.fills:
        got[f.client_id] = got.get(f.client_id, 0.0) + f.qty
    return all(got.get(o.client_id, 0.0) >= o.qty * (1 - 1e-9) for o, _ in res.orders)


class Schedule:
    """Scheduled-rebalance permission of W / D / C (§3.2)."""

    def __init__(self, ev) -> None:
        self.ev = ev
        self.last_slot: pd.Timestamp | None = None
        self.open = False
        self.attempts = 0
        self.opened_at: pd.Timestamp | None = None
        self.log: list[dict] = []

    def allow(self, t: pd.Timestamp) -> bool:
        if self.ev.trigger == "event":
            return True
        s = slot(self.ev, t)
        if s is not None and (self.last_slot is None or s > self.last_slot):
            if self.open:  # a new slot supersedes the old permission
                self.log.append({"slot": self.last_slot, "outcome": "superseded", "attempts": self.attempts})
            self.last_slot, self.open, self.attempts, self.opened_at = s, True, 0, t
        return self.open

    def after(self, t: pd.Timestamp, res: CycleResult, allowed: bool, valid_snapshot: bool) -> None:
        if self.ev.trigger == "event" or not allowed:
            return
        if completed(res):
            self.log.append({"slot": self.last_slot, "outcome": "completed", "attempts": self.attempts + 1,
                             "done_at": t, "late": t - self.opened_at >= pd.Timedelta("1h")})
            self.open = False
            return
        if res.opens_allowed and valid_snapshot:
            self.attempts += 1
        if self.attempts >= MAX_SCHEDULED_ATTEMPTS:
            self.log.append({"slot": self.last_slot, "outcome": "exhausted", "attempts": self.attempts, "done_at": t,
                             "late": True})
            self.open = False


# --- one arm -----------------------------------------------------------------------------------------


@dataclass
class ArmResult:
    name: str
    daily: pd.DataFrame
    fills: pd.DataFrame
    records: pd.DataFrame
    settlements: pd.DataFrame
    counters: dict
    schedule: list[dict]
    kill_ts: pd.Timestamp | None
    terminal: dict = field(default_factory=dict)
    ideal: pd.DataFrame | None = None


def _fills_frame(log: list[dict]) -> pd.DataFrame:
    cols = ["decision_ts", "ts", "asset", "side", "intent", "qty", "dq", "px", "mid_t", "mid_fill", "half", "fee",
            "spread_slip", "delay_drift", "notional", "dec_fallback", "fill_fallback", "position_id"]
    return pd.DataFrame(log, columns=cols)


def run_arm(
    data: HistoricalData,
    cfg: CarryConfig,
    instruments: dict,
    *,
    name: str = "",
    row: RowSpec = ROWS["PRIMARY"],
    start: pd.Timestamp = OUTCOME_START,
    end: pd.Timestamp = CUTOFF,
    builder: SnapshotBuilder | None = None,
    fee_rate: float = FEE_RATE,
    auth: OutcomeAuthorization | None = None,
) -> ArmResult:
    """One arm x row from flat at ``start`` to the terminal valuation at ``end``."""
    _require_authorization(data, auth)
    if start.tzinfo is None or start.value % DAY.value or end.value % DAY.value or end <= start:
        raise ValueError("start / end must be UTC midnights, start < end")
    builder = builder if builder is not None else builder_for(data, row)
    core = CarryCore(cfg, instruments, fee_rate, mode="backtest")
    ex = DelayedExecutor(builder, fee_rate, cfg.slippage_bps, row.cost_mult)
    sched = Schedule(cfg.evaluation)
    recs: list[dict] = []
    settles: list[dict] = []
    vals: dict[pd.Timestamp, dict] = {}
    blocked_days: set = set()
    kill_ts = None
    ctr = {"cycles": 0, "snapshot_assets": 0, "mid_trade_fallback": 0, "no_quote": 0, "decision_fallback_points": 0}

    def settle_rows(res_settlements):
        for s in res_settlements:
            settles.append({"period_start": s.period_start, "asset": s.asset, "known": s.known,
                            "amount": sum(s.amounts.values()), "held_hours": s.held_hours})

    def valuation(t: pd.Timestamp, equity_start: float, snap) -> dict:
        at_t = [f for f in ex.log if f["ts"] == t]
        eq = equity_start + sum(f["spread_slip"] + f["fee"] for f in at_t)
        hold = dict(core.portfolio.holdings)
        for f in at_t:
            hold[f["asset"]] = hold.get(f["asset"], 0.0) - f["dq"]
        px = {a: (snap.quotes[a].mark if a in snap.quotes else core.portfolio.marks.get(a, math.nan)) for a in hold}
        gross = sum(abs(q) * px[a] for a, q in hold.items() if abs(q) > 1e-15)
        net = sum(q * px[a] for a, q in hold.items() if abs(q) > 1e-15)
        legs = sum(1 for q in hold.values() if abs(q) > 1e-15)
        return {"equity": eq, "gross": gross, "net": net, "n_legs": legs}

    last_decision = end - M15  # no decision at end - 15m: its fill would land on the cut
    for t in pd.date_range(start, end, freq="15min", inclusive="left"):
        if t >= last_decision:
            continue
        snap, info = builder.snapshot(t)
        ex.info = info
        ctr["cycles"] += 1
        ctr["snapshot_assets"] += len(info)
        ctr["mid_trade_fallback"] += sum(1 for i in info.values() if i.mid_source == "trade")
        ctr["no_quote"] += sum(1 for i in info.values() if i.mid_source is None)
        ctr["decision_fallback_points"] += sum(1 for i in info.values() if i.dec_fallback)
        allow = sched.allow(t)
        res = core.run_cycle(t, snap, FundingPIT(data.store, t), ex, allow)
        sched.after(t, res, allow, valid_snapshot=bool(snap.quotes))
        settle_rows(res.settlements)
        if t > start and t.value % DAY.value == 0:
            vals[t - DAY] = valuation(t, res.equity_start, snap)
        for r in res.records:
            recs.append({"ts": t, **r.__dict__})
        st = core.risk_state
        if st.entries_blocked_day is not None:
            blocked_days.add(st.entries_blocked_day)
        if st.kill_switch and kill_ts is None:
            kill_ts = t

    # terminal valuation at ``end``: mark and settle every ended period (unknown ones at 0), no orders
    snap, _ = builder.snapshot(end)
    core._mark(snap)
    grace_cfg = core.cfg
    core.cfg = core.cfg.model_copy(update={"funding_unknown_grace_hours": 0.0})
    try:
        out, _ = core._settle(end, FundingPIT(data.store, end))
    finally:
        core.cfg = grace_cfg
    settle_rows(out)
    vals[end - DAY] = valuation(end, core.portfolio.equity(), snap)
    hold = {a: q for a, q in core.portfolio.holdings.items() if abs(q) > 1e-15}
    unwind = 0.0
    for a, q in hold.items():
        mid = snap.quotes[a].mark if a in snap.quotes else core.portfolio.marks.get(a, math.nan)
        half = builder.spread[a].decision(end)
        h = (half.full if not half.fallback else builder.fallback_full.get(a, 0.0010)) / 2
        unwind += abs(q) * mid * (fee_rate + h + cfg.slippage_bps * 1e-4)
    terminal = {"holdings": hold, "unwind_cost": unwind}

    fills = _fills_frame(ex.log)
    sett = pd.DataFrame(settles, columns=["period_start", "asset", "known", "amount", "held_hours"])
    daily = daily_table(vals, fills, sett, cfg.capital_usd, start, end)
    ctr.update({
        "fills": len(fills), "unfilled_orders": len(ex.unfilled),
        "r3_blocked_days": len(blocked_days), "kill": kill_ts is not None,
        "funding_unknown_periods": int((~sett["known"]).sum()) if len(sett) else 0,
        "scheduled_retries": sum(max(0, x["attempts"] - 1) for x in sched.log),
        "scheduled_exhausted": sum(1 for x in sched.log if x["outcome"] == "exhausted"),
        "scheduled_late_1h": sum(1 for x in sched.log if x.get("late")),
    })
    return ArmResult(name, daily, fills, pd.DataFrame(recs), sett, ctr, sched.log, kill_ts, terminal)


def daily_table(vals: dict, fills: pd.DataFrame, sett: pd.DataFrame, e0: float, start: pd.Timestamp,
                end: pd.Timestamp) -> pd.DataFrame:
    """§6 daily rows: fills by their own timestamp's day, funding by its period's day."""
    days = pd.date_range(start, end, freq="D", inclusive="left")
    fday = fills["ts"].dt.floor("D") if len(fills) else pd.Series(dtype="datetime64[ns, UTC]")
    sday = sett["period_start"].dt.floor("D") if len(sett) else pd.Series(dtype="datetime64[ns, UTC]")
    rows = []
    prev = e0
    for d in days:
        v = vals[d]
        f = fills[fday == d] if len(fills) else fills
        funding = float(sett.loc[sday == d, "amount"].sum()) if len(sett) else 0.0
        fees = float(f["fee"].sum()) if len(f) else 0.0
        slip = float(f["spread_slip"].sum()) if len(f) else 0.0
        drift = float(f["delay_drift"].sum()) if len(f) else 0.0
        eq = v["equity"]
        rows.append({
            "day": d, "equity": eq, "ret": eq / prev - 1.0, "funding": funding, "fees": fees, "spread_slip": slip,
            "delay_drift": drift, "price": (eq - prev) - funding + fees + slip,
            "carry_net": (funding - fees - slip) / prev, "gross": v["gross"], "net": v["net"], "n_legs": v["n_legs"],
            "gross_frac": v["gross"] / prev, "net_frac": v["net"] / prev,
            "turnover": float((f["qty"] * f["px"]).sum()) if len(f) else 0.0,
        })
        prev = eq
    return pd.DataFrame(rows).set_index("day")


def fallback_exposure(fills: pd.DataFrame) -> dict:
    """Share of filled notional with a decision fallback, a fill fallback, or either (§10 F2), with
    asset and hour-of-day breakdowns (hour of the decision)."""
    if not len(fills):
        return {"notional": 0.0, "decision": 0.0, "fill": 0.0, "any": 0.0, "by_asset": {}, "by_hour": {}}
    n = fills["notional"]
    tot = float(n.sum())
    anyf = fills["dec_fallback"] | fills["fill_fallback"]

    def share(mask) -> float:
        return float(n[mask].sum()) / tot if tot > 0 else 0.0

    def brk(key) -> dict:
        out = {}
        for k, g in fills.groupby(key):
            gn = float(g["notional"].sum())
            out[str(k)] = {"notional": gn,
                           "decision": float(g.loc[g["dec_fallback"], "notional"].sum()) / gn if gn else 0.0,
                           "fill": float(g.loc[g["fill_fallback"], "notional"].sum()) / gn if gn else 0.0}
        return out

    return {"notional": tot, "decision": share(fills["dec_fallback"]), "fill": share(fills["fill_fallback"]),
            "any": share(anyf), "by_asset": brk(fills["asset"]), "by_hour": brk(fills["decision_ts"].dt.hour)}


FALLBACK_AFFECTED_PP = 0.01


def fallback_affected(exposure: dict[str, dict]) -> tuple[bool, dict]:
    """§10: any arm whose decision- or fill-fallback share of filled notional differs from W's by more
    than 1 percentage point."""
    w = exposure["W"]
    diffs = {}
    for arm, e in exposure.items():
        if arm == "W":
            continue
        diffs[arm] = {"decision": e["decision"] - w["decision"], "fill": e["fill"] - w["fill"]}
    hit = any(abs(v) > FALLBACK_AFFECTED_PP for d in diffs.values() for v in d.values())
    return hit, diffs


def equity_from_log(fills: pd.DataFrame, sett: pd.DataFrame, e0: float, at: pd.Timestamp,
                    marks: dict[str, float]) -> float:
    """Independent reconstruction (tests): E0 + funding of periods ended by ``at`` + every fill before
    ``at`` valued at ``marks`` (perp equity is linear in fills)."""
    f = fills[fills["ts"] < at]
    trades = float((f["dq"] * (f["asset"].map(marks) - f["px"]) - f["fee"]).sum()) if len(f) else 0.0
    fund = float(sett.loc[sett["period_start"] + pd.Timedelta("1h") <= at, "amount"].sum()) if len(sett) else 0.0
    return e0 + trades + fund

