"""Order planner: turns candidates / open positions into OrderRequests."""

from __future__ import annotations

import math

import pandas as pd

from luna.execution.costs import CostModel
from luna.portfolio.portfolio import Position
from luna.types import AccountKind, Candidate, Instrument, OrderRequest, Side


def round_qty(qty: float, step: float) -> float:
    """Round |qty| toward zero to a multiple of step, keeping the sign."""
    if step <= 0:
        return qty
    n = math.floor(abs(qty) / step + 1e-9)
    out = round(n * step, 12)
    return math.copysign(out, qty) if out else 0.0


def _client_id(position_id: str, leg_group: str, asset: str, intent: str, ts: pd.Timestamp) -> str:
    return f"{position_id}:{leg_group}:{asset}:{intent}:{ts:%Y%m%d%H%M}"


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
    intent: str,
    position_id: str,
    leg_group: str,
) -> OrderRequest | None:
    qty = round_qty(abs(qty), inst.qty_step)
    if qty <= 0 or qty < inst.min_qty:
        return None
    sign = 1.0 if side == "buy" else -1.0
    if kind == "perp":
        tif = "IOC"
        limit = mid * (1.0 + sign * band)
    else:
        if costs is None:
            raise ValueError("costs is required to price spot POST_ONLY orders")
        tif = "POST_ONLY"
        limit = mid * (1.0 - sign * costs.half_spread(asset))
    return OrderRequest(
        client_id=_client_id(position_id, leg_group, asset, intent, ts),
        ts=ts,
        asset=asset,
        side=side,
        qty=qty,
        limit_price=limit,
        tif=tif,
        intent=intent,  # type: ignore[arg-type]
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
    costs: CostModel | None = None,
) -> list[OrderRequest]:
    """Per-leg notional = gross * |w| / sum|w|. Legs rounding to zero / below min_qty are dropped.

    `costs` is required for spot (POST_ONLY limit = mid * (1 -/+ half_spread)).
    """
    total = sum(abs(leg.weight) for leg in cand.legs)
    position_id = f"{cand.strategy}:{cand.key}:{ts:%Y%m%d%H%M}"
    orders: list[OrderRequest] = []
    for leg in cand.legs:
        if leg.weight == 0 or total == 0:
            continue
        mid = mids[leg.asset]
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
        )
        if o is not None:
            orders.append(o)
    return orders


def plan_exit(
    pos: Position,
    mids: dict[str, float],
    instruments: dict[str, Instrument],
    kind: AccountKind,
    ts: pd.Timestamp,
    band: float,
    costs: CostModel | None = None,
) -> list[OrderRequest]:
    """Flatten every non-zero leg: sell if qty > 0, buy if qty < 0 (intent "close")."""
    orders: list[OrderRequest] = []
    for asset, q in pos.qty.items():
        if q == 0:
            continue
        side: Side = "sell" if q > 0 else "buy"
        o = _make_order(
            asset=asset,
            side=side,
            qty=q,
            mid=mids[asset],
            inst=instruments[asset],
            kind=kind,
            ts=ts,
            band=band,
            costs=costs,
            intent="close",
            position_id=pos.id,
            leg_group="exit",
        )
        if o is not None:
            orders.append(o)
    return orders
