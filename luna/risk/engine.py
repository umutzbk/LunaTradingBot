"""Deterministic, pure risk engine (spec §6). Nothing may bypass `check`.

`check` evaluates orders sequentially against rules R1-R19 in spec order; the first failing rule
vetoes the order with reason "<rule id>: <detail>". Approved orders update a running copy of the
exposure (gross notional, open risk, open positions, orders today, holdings, spot cash), so a batch
cannot jointly exceed a limit. Vetoed orders consume nothing.

An intent outside {"open", "close", "reduce"} is vetoed first with R0_bad_intent.

Exemptions (spec §6.1, rev4): orders with intent "close"/"reduce" pass R1, R2, R3, R4, R5-R9, R11,
R12, R14, R16 and R18. They remain subject to R10 (a "close" that grows a position is mislabeled
pyramiding; R10_close_invalid: a close/reduce must be opposite in sign to the asset's net signed
holding with qty <= |held|, so it can never open or flip exposure), R13 (a sell may not take holdings
below zero), R15 (price band), R17 (minimums) and R19 (live disabled).

All comparisons are written so that NaN inputs fail closed (`not (x <= limit)` rather than
`x > limit`). Unknown reference values (missing day start / peak equity, missing per-asset
minimums or liquidation distance) also veto opens.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd

from luna.config import RiskLimits
from luna.risk.state import RiskState
from luna.types import AccountKind, OrderRequest

Action = Literal["FLATTEN_AND_KILL", "BLOCK_ENTRIES_TODAY", "PAUSE_RECONCILIATION"]

BAR = pd.Timedelta("15min")
STALE_AFTER = 2 * BAR  # R16: last closed bar older than 2 bars
_QTY_TOL = 1e-9  # relative tolerance on holdings (R10 close, R13), absorbs float drift from fills
_BAND_TOL = 1e-12  # R15: a limit exactly on the band edge must pass despite float rounding
_INTENTS = frozenset({"open", "close", "reduce"})


@dataclass(frozen=True)
class OrderCheck:
    """Per-order facts computed by the caller (backtest/live engine). Kept deliberately dumb.

    notional: |qty| * price in USD (positive).
    new_position_risk: USD loss if the stop is hit (0 for close/reduce).
    new_position_risk_frac: new_position_risk / equity.
    stop_distance_frac: loss fraction of notional at the stop (None if no stop).
    increases_abs_position: True iff the order grows the absolute size of a position that already
        exists before this order (pyramiding/martingale). False for a fresh open.
    mid: current mid price of the asset.
    """

    order: OrderRequest
    notional: float
    new_position_risk: float
    new_position_risk_frac: float
    stop_distance_frac: float | None
    has_stop: bool
    increases_abs_position: bool
    mid: float


@dataclass(frozen=True)
class RiskContext:
    """Account snapshot at decision time. `holdings` is net signed qty per asset;
    `liq_distance[asset]` is the fractional price move to liquidation of the post-order position
    (perp only); `last_bar_ts` is the open time of the last closed 15-minute bar;
    `open_position_ids` are the ids of positions open before this batch (R7: an open order on one
    of them, e.g. a retried pair leg, does not take a new slot)."""

    limits: RiskLimits
    kind: AccountKind
    mode: Literal["backtest", "paper", "live"]
    state: RiskState
    equity: float
    open_positions: int
    open_risk: float
    gross_notional: float
    holdings: dict[str, float]
    last_bar_ts: pd.Timestamp
    now: pd.Timestamp
    liq_distance: dict[str, float]
    min_qty: dict[str, float]
    min_notional: dict[str, float]
    available_cash: float
    reconciliation_ok: bool = True
    open_position_ids: frozenset[str] = frozenset()


@dataclass
class RiskResult:
    approved: list[OrderRequest] = field(default_factory=list)
    vetoed: list[tuple[OrderRequest, str]] = field(default_factory=list)


@dataclass
class _Running:
    """Running exposure for one batch; starts as a copy of the context, updated on approval."""

    open_positions: int
    open_risk: float
    gross_notional: float
    orders_today: int
    holdings: dict[str, float]
    available_cash: float
    new_position_ids: set[str] = field(default_factory=set)


# --- portfolio-level conditions ---------------------------------------------------------------


def _day_loss_breached(ctx: RiskContext) -> bool | None:
    """R3: loss vs UTC day-start equity >= daily_loss_limit. None if not computable."""
    dse = ctx.state.day_start_equity
    if dse is None or not (math.isfinite(dse) and dse > 0) or not math.isfinite(ctx.equity):
        return None
    return bool((dse - ctx.equity) >= ctx.limits.daily_loss_limit * dse)  # plain bool, never np.bool_


def _drawdown_breached(ctx: RiskContext) -> bool | None:
    """R4: drawdown from peak equity >= max_drawdown. None if not computable."""
    peak = ctx.state.peak_equity
    if peak is None or not (math.isfinite(peak) and peak > 0) or not math.isfinite(ctx.equity):
        return None
    return bool((peak - ctx.equity) >= ctx.limits.max_drawdown * peak)  # plain bool, never np.bool_


def portfolio_actions(ctx: RiskContext) -> list[Action]:
    """Portfolio-level actions, in fixed order. Pure: returns actions, never mutates state.

    FLATTEN_AND_KILL     R4 drawdown from peak >= max_drawdown (only when computable: broken
                         marks never trigger a permanent kill).
    BLOCK_ENTRIES_TODAY  R3 day loss >= daily_loss_limit, or day loss not computable (fail closed;
                         `check` vetoes opens with R3 in the same situations).
    PAUSE_RECONCILIATION R18 reconciliation mismatch.
    """
    actions: list[Action] = []
    if _drawdown_breached(ctx):  # unknown (None) never kills
        actions.append("FLATTEN_AND_KILL")
    day_loss = _day_loss_breached(ctx)
    if day_loss is None or day_loss:  # unknown blocks (fail closed)
        actions.append("BLOCK_ENTRIES_TODAY")
    if not ctx.reconciliation_ok:
        actions.append("PAUSE_RECONCILIATION")
    return actions


def _utc_date(ts: pd.Timestamp) -> date:
    if ts.tzinfo is None:
        raise ValueError("risk engine requires tz-aware timestamps (UTC)")
    return ts.tz_convert("UTC").date()


def apply_actions(state: RiskState, actions: list[Action], now: pd.Timestamp) -> RiskState:
    """Return a new state with the actions' persistent flags set (pure).

    FLATTEN_AND_KILL sets kill_switch, PAUSE_RECONCILIATION sets paused, BLOCK_ENTRIES_TODAY sets
    entries_blocked_day to the UTC date of `now` (sticky until roll_day starts a new day)."""
    out = dataclasses.replace(state)
    if "BLOCK_ENTRIES_TODAY" in actions:
        out.entries_blocked_day = _utc_date(now)
    if "FLATTEN_AND_KILL" in actions:
        out.kill_switch = True
    if "PAUSE_RECONCILIATION" in actions:
        out.paused = True
    return out


# --- per-order rules ---------------------------------------------------------------------------


def _check_one(oc: OrderCheck, ctx: RiskContext, run: _Running) -> str | None:
    order = oc.order
    lim = ctx.limits
    if order.intent not in _INTENTS:
        return f"R0_bad_intent: {order.intent!r}"
    is_open = order.intent == "open"
    is_new_position = (
        is_open
        and order.position_id not in ctx.open_position_ids
        and order.position_id not in run.new_position_ids
    )
    restrict_short = ctx.kind == "spot" or not lim.allow_short

    if is_open:
        if ctx.state.kill_switch:
            return "R1_kill: kill switch active"
        if ctx.state.paused:
            return "R2_pause: trading paused"
        if ctx.state.entries_blocked_day == _utc_date(ctx.now):
            return "R3_daily_loss: entries blocked for the rest of the UTC day"
        day_loss = _day_loss_breached(ctx)
        if day_loss is None or day_loss:
            return f"R3_daily_loss: equity {ctx.equity} vs day start {ctx.state.day_start_equity}"
        drawdown = _drawdown_breached(ctx)
        if drawdown is None or drawdown:
            return f"R4_drawdown: equity {ctx.equity} vs peak {ctx.state.peak_equity}"
        if not (0 <= oc.new_position_risk_frac <= lim.max_risk_per_trade):
            return f"R5_risk_per_trade: {oc.new_position_risk_frac} not in [0, {lim.max_risk_per_trade}]"
        total_risk = run.open_risk + oc.new_position_risk
        if not (oc.new_position_risk >= 0 and total_risk <= lim.max_open_risk * ctx.equity):
            return f"R6_open_risk: {total_risk} > {lim.max_open_risk} x {ctx.equity}"
        positions = run.open_positions + (1 if is_new_position else 0)
        if not (positions <= lim.max_positions):
            return f"R7_max_positions: {positions} > {lim.max_positions}"
        gross = run.gross_notional + oc.notional
        if not (oc.notional >= 0 and gross <= lim.max_gross_leverage * ctx.equity):
            return f"R8_gross_leverage: {gross} > {lim.max_gross_leverage} x {ctx.equity}"
        if not (oc.notional <= lim.max_leg_notional_frac * ctx.equity):
            return f"R9_leg_notional: {oc.notional} > {lim.max_leg_notional_frac} x {ctx.equity}"

    if oc.increases_abs_position:
        return "R10_pyramiding: order grows an existing position"

    if not is_open:
        held = run.holdings.get(order.asset, 0.0)
        opposite = held > 0 if order.side == "sell" else held < 0
        if not (opposite and order.qty <= abs(held) * (1 + _QTY_TOL)):
            return f"R10_close_invalid: {order.side} {order.qty} against net holding {held}"

    if is_open:
        stop = oc.stop_distance_frac
        if not (oc.has_stop and stop is not None and math.isfinite(stop) and stop > 0):
            return f"R11_no_stop: open order without a valid stop ({oc.has_stop}, {stop})"
        if ctx.kind == "perp":
            liq = ctx.liq_distance.get(order.asset)
            if liq is None:
                return f"R12_liquidation: unknown liq distance for {order.asset}"
            if not (liq >= lim.liq_distance_mult * stop):
                return f"R12_liquidation: {liq} < {lim.liq_distance_mult} x {stop}"

    if restrict_short and order.side == "sell":
        held = run.holdings.get(order.asset, 0.0)
        if not (held - order.qty >= -_QTY_TOL * order.qty):
            return f"R13_spot_short: sell {order.qty} with holdings {held}"

    if is_open and not (run.orders_today < lim.max_orders_per_day):
        return f"R14_orders_per_day: {run.orders_today} >= {lim.max_orders_per_day}"

    if not (oc.mid > 0 and abs(order.limit_price / oc.mid - 1.0) <= lim.max_price_band + _BAND_TOL):
        return f"R15_price_band: limit {order.limit_price} vs mid {oc.mid}"

    if is_open:
        if pd.isna(ctx.last_bar_ts) or not (ctx.now - (ctx.last_bar_ts + BAR) <= STALE_AFTER):
            return f"R16_stale_data: last bar {ctx.last_bar_ts} at {ctx.now}"

    min_qty = ctx.min_qty.get(order.asset)
    min_notional = ctx.min_notional.get(order.asset)
    if min_qty is None or min_notional is None:
        return f"R17_minimums_balance: unknown exchange minimums for {order.asset}"
    if not (order.qty >= min_qty and order.qty > 0):
        return f"R17_minimums_balance: qty {order.qty} < {min_qty}"
    if not (oc.notional >= min_notional):
        return f"R17_minimums_balance: notional {oc.notional} < {min_notional}"
    if ctx.kind == "spot" and order.side == "buy" and not (oc.notional <= run.available_cash):
        return f"R17_minimums_balance: notional {oc.notional} > cash {run.available_cash}"

    if is_open and not ctx.reconciliation_ok:
        return "R18_reconciliation: reconciliation mismatch"

    if ctx.mode == "live" and not lim.live_enabled:
        return "R19_live_disabled: live mode with live_enabled false"

    return None


def _apply(oc: OrderCheck, ctx: RiskContext, run: _Running) -> None:
    order = oc.order
    run.orders_today += 1
    signed = order.qty if order.side == "buy" else -order.qty
    run.holdings[order.asset] = run.holdings.get(order.asset, 0.0) + signed
    if ctx.kind == "spot" and order.side == "buy":
        run.available_cash -= oc.notional
    # Conservative: closes/reduces never free budget within the batch (they may not fill).
    if order.intent == "open":
        run.gross_notional += oc.notional
        run.open_risk += oc.new_position_risk
        pid = order.position_id
        if pid not in ctx.open_position_ids and pid not in run.new_position_ids:
            run.new_position_ids.add(pid)
            run.open_positions += 1


def check(orders: list[OrderCheck], ctx: RiskContext) -> RiskResult:
    """Evaluate `orders` in sequence; pure (ctx and its dicts are not modified).

    R7 counts an "open" order as a new position unless its position_id is in
    `ctx.open_position_ids` or was already approved earlier in this batch (pair legs and retried
    legs share one slot). Growing an existing position is still vetoed by R10
    (`increases_abs_position`).
    """
    run = _Running(
        open_positions=ctx.open_positions,
        open_risk=ctx.open_risk,
        gross_notional=ctx.gross_notional,
        orders_today=ctx.state.orders_today,
        holdings=dict(ctx.holdings),
        available_cash=ctx.available_cash,
    )
    result = RiskResult()
    for oc in orders:
        reason = _check_one(oc, ctx, run)
        if reason is None:
            result.approved.append(oc.order)
            _apply(oc, ctx, run)
        else:
            result.vetoed.append((oc.order, reason))
    return result
