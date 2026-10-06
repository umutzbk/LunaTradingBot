"""Pessimistic simulated broker. Orders live exactly one bar and fill only on the next bar."""

from __future__ import annotations

import pandas as pd

from luna.execution.costs import CostModel, Liquidity
from luna.types import AccountKind, Fill, Instrument, OrderRequest, Side


class SimBroker:
    def __init__(
        self,
        kind: AccountKind,
        costs: CostModel,
        instruments: dict[str, Instrument],
        cost_mult: float = 1.0,
    ) -> None:
        self.kind = kind
        self.costs = costs
        self.instruments = instruments
        self.cost_mult = cost_mult
        self._pending: list[OrderRequest] = []

    def submit(self, order: OrderRequest) -> None:
        self._pending.append(order)

    def _frictions(self, asset: str) -> float:
        return (self.costs.half_spread(asset) + self.costs.slippage) * self.cost_mult

    def _fee(self, notional: float, liquidity: Liquidity) -> float:
        return self.costs.fee(notional, liquidity) * self.cost_mult

    def on_bar(self, ts: pd.Timestamp, bars: dict[str, pd.Series]) -> list[Fill]:
        """Process orders for bar `ts`.

        Only orders decided strictly before `ts` are eligible (an order is never filled on the
        bar it was decided on; it stays pending until the next call). Every eligible order is
        consumed: it either fills on this bar or is cancelled at its end. Bars with
        filled=True (forward-filled) never fill anything.
        """
        fills: list[Fill] = []
        keep: list[OrderRequest] = []
        for o in self._pending:
            if o.ts >= ts:
                keep.append(o)
                continue
            bar = bars.get(o.asset)
            if bar is None or bool(bar.get("filled", False)):
                continue
            fill = self._try_fill(o, ts, bar)
            if fill is not None:
                fills.append(fill)
        self._pending = keep
        return fills

    def _try_fill(self, o: OrderRequest, ts: pd.Timestamp, bar: pd.Series) -> Fill | None:
        buy = o.side == "buy"
        liquidity: Liquidity
        if o.tif == "IOC":
            f = self._frictions(o.asset)
            price = float(bar["open"]) * (1.0 + f if buy else 1.0 - f)
            if (buy and price > o.limit_price) or (not buy and price < o.limit_price):
                return None
            liquidity = "taker"
        else:  # POST_ONLY: must trade strictly through the limit
            if buy and not float(bar["low"]) < o.limit_price:
                return None
            if not buy and not float(bar["high"]) > o.limit_price:
                return None
            price = o.limit_price
            liquidity = "maker"
        return Fill(
            client_id=o.client_id,
            ts=ts,
            asset=o.asset,
            side=o.side,
            qty=o.qty,
            price=price,
            fee=self._fee(o.qty * price, liquidity),
            liquidity=liquidity,
        )

    def stop_fill(
        self, asset: str, side: Side, qty: float, stop_price: float, bar: pd.Series
    ) -> Fill:
        """Emergency stop at the worse of stop_price and bar open, plus costs, taker.

        Precondition: `bar` is a real bar (filled=False); the engine must not call this on
        forward-filled bars.
        """
        f = self._frictions(asset)
        open_ = float(bar["open"])
        if side == "sell":
            price = min(stop_price, open_) * (1.0 - f)
        else:
            price = max(stop_price, open_) * (1.0 + f)
        ts = pd.Timestamp(bar.name)
        return Fill(
            client_id=f"stop:{asset}:{ts:%Y%m%d%H%M}",
            ts=ts,
            asset=asset,
            side=side,
            qty=qty,
            price=price,
            fee=self._fee(qty * price, "taker"),
            liquidity="taker",
        )
