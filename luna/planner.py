"""Order planner: turns candidates / open positions into OrderRequests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from luna.execution.costs import CostModel
from luna.portfolio.portfolio import Position
from luna.types import (
    AccountKind,
    Candidate,
    Instrument,
    OrderModel,
    OrderRequest,
    Side,
    default_order_model,
)

Intent = Literal["open", "close", "reduce"]


@dataclass(frozen=True)
class PlanResult:
    orders: list[OrderRequest]
    reject_reason: str | None


@dataclass(frozen=True)
class ExitPlan:
    orders: list[OrderRequest]
    dust: dict[str, float]  # signed residual qty per asset that cannot be traded


def round_qty(qty: float, step: float) -> float:
    """Round |qty| toward zero to a multiple of step, keeping the sign."""
    if not step > 0:
        raise ValueError(f"qty step must be > 0, got {step}")
    n = math.floor(abs(qty) / step + 1e-9)
    out = round(n * step, 12)
    return math.copysign(out, qty) if out else 0.0


def _client_id(
    position_id: str, leg_group: str, asset: str, intent: str, ts: pd.Timestamp, attempt: int = 0
) -> str:
    """Minute-resolution id; ``attempt > 0`` (live re-plans within the same minute) appends
    ``:a{attempt}``. ``attempt == 0`` keeps the historical ids exactly."""
    if attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt}")
    cid = f"{position_id}:{leg_group}:{asset}:{intent}:{ts:%Y%m%d%H%M}"
    return f"{cid}:a{attempt}" if attempt else cid


def _mid(mids: dict[str, float], asset: str) -> float:
    m = mids.get(asset)
    if m is None or not math.isfinite(m) or m <= 0:
        raise ValueError(f"invalid mid for {asset}: {m}")
    return m


def _make_order(
    *,
    asset: str,
    side: Side,
    qty: float,
    mid: float,
    inst: Instrument,
    kind: AccountKind,
    ts: pd.Timestamp,
    band: float,
    costs: CostModel | None,
    intent: Intent,
    position_id: str,
    leg_group: str,
    check_notional: bool,
    attempt: int = 0,
    order_model: OrderModel | None = None,
) -> OrderRequest | None:
    """None if the rounded qty is 0, below min_qty (or, for entries, below min_notional)."""
    qty = round_qty(abs(qty), inst.qty_step)
    if qty <= 0 or qty < inst.min_qty:
        return None
    if check_notional and qty * mid < inst.min_notional:
        return None
    sign = 1.0 if side == "buy" else -1.0
    if (order_model or default_order_model(kind)) == "taker":
        tif = "IOC"
        limit = mid * (1.0 + sign * band)
    else:
        if costs is None:
            raise ValueError("costs is required to price POST_ONLY orders")
        tif = "POST_ONLY"
        limit = mid * (1.0 - sign * costs.half_spread(asset))
    return OrderRequest(
        client_id=_client_id(position_id, leg_group, asset, intent, ts, attempt),
        ts=ts,
        asset=asset,
        side=side,
        qty=qty,
        limit_price=limit,
        tif=tif,
        intent=intent,
        position_id=position_id,
        leg_group=leg_group,
    )


def plan_entry(
    cand: Candidate,
    gross_notional: float,
    mids: dict[str, float],
    instruments: dict[str, Instrument],
    kind: AccountKind,
    ts: pd.Timestamp,
    band: float,
    *,
    costs: CostModel | None = None,
    attempt: int = 0,
    order_model: OrderModel | None = None,
) -> PlanResult:
    """Per-leg notional = gross * |w| / sum|w|. All-or-nothing: if any leg rounds to zero qty,
    is below min_qty or below min_notional, returns PlanResult([], "below_min_notional").

    `costs` is required for post-only orders (limit = mid * (1 -/+ half_spread)). ``order_model``:
    None = the account default (perp IOC taker, spot post-only maker).
    """
    total = sum(abs(leg.weight) for leg in cand.legs)
    position_id = f"{cand.strategy}:{cand.key}:{ts:%Y%m%d%H%M}"
    orders: list[OrderRequest] = []
    for leg in cand.legs:
        if leg.weight == 0 or total == 0:
            continue
        mid = _mid(mids, leg.asset)
        notional = gross_notional * abs(leg.weight) / total
        side: Side = "buy" if leg.weight * cand.direction > 0 else "sell"
        o = _make_order(
            asset=leg.asset,
            side=side,
            qty=notional / mid,
            mid=mid,
            inst=instruments[leg.asset],
            kind=kind,
            ts=ts,
            band=band,
            costs=costs,
            intent="open",
            position_id=position_id,
            leg_group="entry",
            check_notional=True,
            attempt=attempt,
            order_model=order_model,
        )
        if o is None:
            return PlanResult([], "below_min_notional")
        orders.append(o)
    return PlanResult(orders, None)


def plan_exit(
    pos: Position,
    mids: dict[str, float],
    instruments: dict[str, Instrument],
    kind: AccountKind,
    ts: pd.Timestamp,
    band: float,
    *,
    costs: CostModel | None = None,
    attempt: int = 0,
    order_model: OrderModel | None = None,
) -> ExitPlan:
    """Flatten every non-zero leg: sell if qty > 0, buy if qty < 0 (intent "close").

    Legs whose rounded |qty| is 0 or below min_qty (incl. float residue), or whose notional at the
    current mid is below min_notional, get no order and are reported in `dust` (signed qty) for
    Portfolio.write_off_dust: an order the risk engine's R17 would veto on every attempt would
    otherwise keep the position open forever.
    """
    orders: list[OrderRequest] = []
    dust: dict[str, float] = {}
    for asset, q in pos.qty.items():
        if q == 0:
            continue
        side: Side = "sell" if q > 0 else "buy"
        o = _make_order(
            asset=asset,
            side=side,
            qty=q,
            mid=_mid(mids, asset),
            inst=instruments[asset],
            kind=kind,
            ts=ts,
            band=band,
            costs=costs,
            intent="close",
            position_id=pos.id,
            leg_group="exit",
            check_notional=True,
            attempt=attempt,
            order_model=order_model,
        )
        if o is None:
            dust[asset] = q
        else:
            orders.append(o)
    return ExitPlan(orders, dust)
