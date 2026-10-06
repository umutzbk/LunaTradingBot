"""Portfolio accounting for perp (cross margin, avg-entry) and spot (cash + holdings) accounts.

Account level (per asset): ``holdings`` is the signed net quantity and ``avg_entry`` the average
entry price of that net quantity.

* Perp: opening/increasing moves cash only by the fee; reducing realizes
  ``(fill - avg_entry) * closed_qty * sign(old)`` into cash; a sign flip closes the old side at
  ``avg_entry`` and opens the remainder at the fill price. ``equity = cash + sum qty*(mark-avg)``.
* Spot: buy ``cash -= price*qty + fee``; sell ``cash += price*qty - fee`` (never more than held).
  ``equity = cash + sum qty*mark``.

Position level (P&L attribution). Every fill is booked to one position. Signs: ``gross`` and
``funding`` are P&L (positive = gain), ``fees`` and ``spread_slip`` are costs (positive = cost):

* ``gross``       realized price P&L measured at *reference* prices (mid at decision), booked on
                  reduce with the same avg-entry rule (``entry_ref_price`` holds the avg ref entry).
* ``spread_slip`` ``s * (fill - ref) * qty`` per fill, ``s = +1`` buy / ``-1`` sell. Equals
                  ``|fill - ref| * qty`` for adverse fills; negative for fills better than ref.
* ``fees``        sum of ``Fill.fee``.
* ``funding``     sum of funding P&L on the position's quantity.

Identity (tested): ``net = gross - fees - spread_slip + funding``; once a position is flat its
``net`` equals the change in account equity caused by it, because for every fill the actual
realized P&L at fill prices equals the ref-price P&L minus the spread/slippage cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.types import AccountKind, ExitSpec, Fill, Leg, StopSpec

_REL_TOL = 1e-9  # quantities within this relative tolerance of zero are flat (float dust)


def _empty_pnl() -> dict[str, float]:
    return {"gross": 0.0, "fees": 0.0, "spread_slip": 0.0, "funding": 0.0, "funding_unknown_bars": 0.0}


@dataclass
class Position:
    id: str
    strategy: str
    key: str
    legs: tuple[Leg, ...]
    direction: int
    qty: dict[str, float]  # signed qty per asset
    entry_price: dict[str, float]  # avg entry at fill prices per asset
    entry_ts: pd.Timestamp
    entry_ref: float
    stop: StopSpec
    exit: ExitSpec
    risk_amount: float
    regime: str
    trail_level: float | None = None
    bars_held: int = 0
    pnl: dict[str, float] = field(default_factory=_empty_pnl)
    entry_ref_price: dict[str, float] = field(default_factory=dict)  # avg entry at ref prices

    def net_pnl(self) -> float:
        """Realized net P&L: gross - fees - spread_slip + funding."""
        p = self.pnl
        return p["gross"] - p["fees"] - p["spread_slip"] + p["funding"]

    def is_flat(self) -> bool:
        return all(self.qty.get(leg.asset, 0.0) == 0.0 for leg in self.legs)


def _trade(qty: float, avg: float, delta: float, price: float) -> tuple[float, float, float]:
    """Apply signed ``delta`` at ``price`` to (qty, avg). Returns (new_qty, new_avg, realized)."""
    tol = _REL_TOL * max(abs(qty), abs(delta))
    if qty == 0.0 or (qty > 0) == (delta > 0):  # open / increase
        new = qty + delta
        return new, (qty * avg + delta * price) / new, 0.0
    remainder = abs(delta) - abs(qty)
    if abs(remainder) <= tol:  # exact close (snap float dust)
        return 0.0, 0.0, (price - avg) * qty
    if remainder < 0:  # partial reduce, avg unchanged
        return qty + delta, avg, (price - avg) * -delta
    # sign flip: close the old side at avg, open the remainder at price
    return math.copysign(remainder, delta), price, (price - avg) * qty


class Portfolio:
    def __init__(self, kind: AccountKind, capital_usd: float) -> None:
        if kind not in ("perp", "spot"):
            raise ValueError(f"unknown account kind {kind!r}")
        self.kind: AccountKind = kind
        self.cash: float = float(capital_usd)
        self.holdings: dict[str, float] = {}  # signed net qty per asset (spot: >= 0); flat -> absent
        self.avg_entry: dict[str, float] = {}  # avg entry price of the net qty per asset
        self.marks: dict[str, float] = {}
        self.positions: dict[str, Position] = {}
        self.closed: list[tuple[Position, pd.Timestamp, str]] = []
        self.funding_log: list[tuple[pd.Timestamp, str, float | None, float]] = []  # ts, asset, rate, amt

    # --- lifecycle ------------------------------------------------------------------------------

    def open_position(self, position: Position) -> None:
        """Register a position so fills can be booked to it."""
        if position.id in self.positions:
            raise ValueError(f"position {position.id!r} already open")
        self.positions[position.id] = position

    def close_position(self, position_id: str, ts: pd.Timestamp, reason: str) -> Position:
        pos = self.positions[position_id]
        if not pos.is_flat():
            raise ValueError(f"position {position_id!r} still has open legs: {pos.qty}")
        del self.positions[position_id]
        self.closed.append((pos, ts, reason))
        return pos

    # --- fills ----------------------------------------------------------------------------------

    def apply_fill(self, fill: Fill, position_id: str, ref_price: float) -> None:
        pos = self.positions[position_id]
        asset = fill.asset
        if asset not in {leg.asset for leg in pos.legs}:
            raise ValueError(f"fill asset {asset} is not a leg of position {position_id!r}")
        if fill.side not in ("buy", "sell"):
            raise ValueError(f"bad side {fill.side!r}")
        finite = all(math.isfinite(x) for x in (fill.qty, fill.price, ref_price, fill.fee))
        if not (finite and fill.qty > 0 and fill.price > 0 and ref_price > 0):
            raise ValueError(f"invalid fill {fill} / ref_price {ref_price}")
        s = 1.0 if fill.side == "buy" else -1.0
        delta = s * fill.qty

        held = self.holdings.get(asset, 0.0)
        pos_q = pos.qty.get(asset, 0.0)
        if self.kind == "spot" and delta < 0:
            for have, what in ((held, "account"), (pos_q, f"position {position_id!r}")):
                if fill.qty - have > _REL_TOL * fill.qty:
                    raise ValueError(f"spot sell of {fill.qty} {asset} exceeds {what} holding {have}")

        # account
        new_held, new_avg, realized = _trade(held, self.avg_entry.get(asset, 0.0), delta, fill.price)
        if self.kind == "perp":
            self.cash += realized - fill.fee
        else:
            self.cash += -delta * fill.price - fill.fee
        self._set(self.holdings, self.avg_entry, asset, new_held, new_avg)
        # First fill seeds the mark if none exists yet. Existing marks are not overwritten by fill
        # prices, so equity reflects the spread/slippage paid only after the next mark().
        self.marks.setdefault(asset, fill.price)

        # position attribution
        new_q, new_entry, _ = _trade(pos_q, pos.entry_price.get(asset, 0.0), delta, fill.price)
        _, new_ref, gross = _trade(pos_q, pos.entry_ref_price.get(asset, 0.0), delta, ref_price)
        pos.qty[asset] = new_q
        if new_q == 0.0:
            pos.entry_price.pop(asset, None)
            pos.entry_ref_price.pop(asset, None)
        else:
            pos.entry_price[asset] = new_entry
            pos.entry_ref_price[asset] = new_ref
        pos.pnl["gross"] += gross
        pos.pnl["fees"] += fill.fee
        pos.pnl["spread_slip"] += s * (fill.price - ref_price) * fill.qty

    @staticmethod
    def _set(qtys: dict[str, float], avgs: dict[str, float], asset: str, q: float, avg: float) -> None:
        if q == 0.0:
            qtys.pop(asset, None)
            avgs.pop(asset, None)
        else:
            qtys[asset] = q
            avgs[asset] = avg

    # --- funding / marks ------------------------------------------------------------------------

    def apply_funding(self, asset: str, rate: float | None, mark: float, ts: pd.Timestamp) -> None:
        """Hourly perp funding. P&L = -rate * qty * mark (long pays when rate > 0).

        ``rate is None`` (funding unknown): no cash change, each position holding the asset gets
        ``funding_unknown_bars += 1``.
        """
        if self.kind != "perp":
            raise ValueError("funding applies to perp accounts only")
        holders = [p for p in self.positions.values() if p.qty.get(asset, 0.0) != 0.0]
        if rate is None:
            for p in holders:
                p.pnl["funding_unknown_bars"] += 1
            if holders:
                self.funding_log.append((ts, asset, None, 0.0))
            return
        if not holders:
            return
        # Book per holder; the account amount is their sum (== -rate * net holding * mark), so
        # exactly offsetting positions each carry their own funding while cash nets to zero.
        amount = 0.0
        for p in holders:
            share = -rate * p.qty[asset] * mark
            p.pnl["funding"] += share
            amount += share
        self.cash += amount
        self.funding_log.append((ts, asset, rate, amount))

    def mark(self, prices: dict[str, float]) -> None:
        bad = {a: p for a, p in prices.items() if not (math.isfinite(p) and p > 0)}
        if bad:
            raise ValueError(f"invalid mark prices: {bad}")
        self.marks.update(prices)

    # --- metrics --------------------------------------------------------------------------------

    def _mark(self, asset: str) -> float:
        return self.marks.get(asset, self.avg_entry[asset])

    def equity(self) -> float:
        if self.kind == "spot":
            return self.cash + sum(q * self._mark(a) for a, q in self.holdings.items())
        return self.cash + sum(q * (self._mark(a) - self.avg_entry[a]) for a, q in self.holdings.items())

    def notional(self, asset: str) -> float:
        """Absolute notional of the net holding in ``asset`` at the last mark (0 if flat)."""
        q = self.holdings.get(asset, 0.0)
        return abs(q) * self._mark(asset) if q else 0.0

    def gross_notional(self) -> float:
        return sum(self.notional(a) for a in self.holdings)

    def gross_leverage(self) -> float:
        eq = self.equity()
        gn = self.gross_notional()
        if eq <= 0:
            return np.inf if gn > 0 else 0.0
        return gn / eq

    def open_risk(self) -> float:
        return sum(p.risk_amount for p in self.positions.values())

    def liquidation_distance(self, asset: str, maintenance_margin: float) -> float:
        """Approx. fractional adverse move in ``asset`` to liquidation (cross margin).

        ``(equity - gross_notional * mm) / |notional(asset)|``, floored at 0. ``inf`` for spot
        accounts or when the asset has no position.
        """
        n = self.notional(asset)
        if self.kind != "perp" or n == 0.0:
            return np.inf
        return max(0.0, (self.equity() - self.gross_notional() * maintenance_margin) / n)
