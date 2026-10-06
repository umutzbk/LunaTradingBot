"""Paper broker: simulates fills against LIVE public Kraken market data (no keys, no real orders).

Every order is persisted (status NEW) before it is simulated; status transitions
(OPEN / PARTIAL / FILLED / CANCELED / REJECTED) and fills are written to the Db.

- IOC (perp; also accepted for spot): at submit, walk the current book from the best level up to
  the limit; fill min(qty, depth within the limit) at the VWAP, taker fee; the remainder is
  cancelled. Slippage is implicit in the walk (no extra slippage/half-spread). A book that
  cannot be fetched rejects the order. Fills are delivered by the next `poll()`.
- POST_ONLY (spot only): rests at its limit from `order.ts` (the submission time). Each poll reads
  public trades since the order's cursor; a trade strictly through the limit (buy: price < limit,
  sell: price > limit) fills min(trade volume, remaining) at the limit, maker fee. Repriced to the
  best bid (buy) / ask (sell) at +5 min and +10 min (at most 2 times, on the first poll at/after
  each mark, judged against the limit active when the trades are read). Cancelled 15 min after
  submission; trades printed at/after expiry are ignored. Feed errors leave the order resting.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

import pandas as pd

from luna.engine.feeds import FeedError, LiveFeed
from luna.execution.costs import CostModel
from luna.store.db import Db
from luna.types import AccountKind, Fill, OrderRequest

log = logging.getLogger("luna.paper")

REPRICE_EVERY = pd.Timedelta("5min")
MAX_REPRICES = 2
POST_ONLY_TTL = pd.Timedelta("15min")
SNAPSHOT_LEVELS = 10
_EPS = 1e-12


@dataclass
class _Resting:
    order: OrderRequest
    submitted: pd.Timestamp
    cursor_ns: int
    filled: float = 0.0
    reprices: int = 0


class PaperBroker:
    def __init__(self, kind: AccountKind, costs: CostModel, feed: LiveFeed, db: Db, account: str) -> None:
        self.kind = kind
        self.costs = costs
        self.feed = feed
        self.db = db
        self.account = account
        self._ready: list[Fill] = []  # IOC fills awaiting the next poll
        self._resting: dict[str, _Resting] = {}
        self._seen: set[str] = set()

    # --- submission ------------------------------------------------------------------------------

    def submit(self, order: OrderRequest) -> None:
        """Persist (NEW), then simulate. A client_id already known (in memory or in the Db, e.g.
        after a restart) is ignored, so a resubmission never duplicates an order."""
        if order.tif == "POST_ONLY" and self.kind != "spot":
            raise ValueError("POST_ONLY is only simulated for spot; perp orders must be IOC")
        if order.qty <= 0:
            raise ValueError(f"order qty must be positive, got {order.qty}")
        if order.client_id in self._seen or self.db.order_status(self.account, order.client_id) is not None:
            log.warning("duplicate client_id %s ignored", order.client_id)
            return
        self._seen.add(order.client_id)
        self.db.record_order(self.account, order, "NEW")
        if order.tif == "IOC":
            self._simulate_ioc(order)
        else:
            self._resting[order.client_id] = _Resting(order, order.ts, int(order.ts.value) - 1)
            self._status(order.client_id, "OPEN")

    def _status(self, client_id: str, status: str) -> None:
        self.db.set_order_status(self.account, client_id, status)

    def _simulate_ioc(self, o: OrderRequest) -> None:
        try:
            bids, asks = self.feed.book(o.asset)
        except FeedError as e:
            log.warning("IOC %s rejected: no book (%s)", o.client_id, e)
            self._status(o.client_id, "REJECTED")
            return
        buy = o.side == "buy"
        levels = asks if buy else bids
        remaining, qty, notional = o.qty, 0.0, 0.0
        for price, size in levels:
            if (buy and price > o.limit_price) or (not buy and price < o.limit_price):
                break
            take = min(size, remaining)
            qty += take
            notional += take * price
            remaining -= take
            if remaining <= o.qty * _EPS:
                break
        if qty <= 0:
            self._status(o.client_id, "CANCELED")
            return
        price = notional / qty
        fill = Fill(client_id=o.client_id, ts=o.ts, asset=o.asset, side=o.side, qty=qty, price=price,
                    fee=self.costs.fee(qty * price, "taker"), liquidity="taker")
        self.db.record_fill(self.account, fill, position_id=o.position_id)
        self._status(o.client_id, "FILLED" if qty >= o.qty * (1 - 1e-9) else "CANCELED")
        self._ready.append(fill)

    def cancel(self, client_id: str) -> None:
        """Cancel a resting order (also one known only from the Db, e.g. after a restart)."""
        self._resting.pop(client_id, None)
        if self.db.order_status(self.account, client_id) in ("NEW", "OPEN", "PARTIAL"):
            self._status(client_id, "CANCELED")

    # --- polling ---------------------------------------------------------------------------------

    def poll(self, now: pd.Timestamp) -> list[Fill]:
        """Fills of IOC orders submitted before this call, then POST_ONLY progress up to `now`."""
        now = pd.Timestamp(now)
        fills, self._ready = self._ready, []
        for cid in list(self._resting):
            fill = self._poll_resting(self._resting[cid], now)
            if fill is not None:
                fills.append(fill)
        return fills

    def _poll_resting(self, r: _Resting, now: pd.Timestamp) -> Fill | None:
        o = r.order
        expiry = r.submitted + POST_ONLY_TTL
        fill = None
        try:
            trades, cursor = self.feed.trades_since(o.asset, r.cursor_ns)  # type: ignore[attr-defined]
        except FeedError as e:
            log.warning("trades for %s unavailable: %s", o.client_id, e)
        else:
            r.cursor_ns = max(r.cursor_ns, int(cursor))
            fill = self._match(r, trades, expiry)
        cid = o.client_id
        if r.filled >= o.qty * (1 - 1e-9):
            self._status(cid, "FILLED")
            del self._resting[cid]
        elif now >= expiry:
            self._status(cid, "CANCELED")
            del self._resting[cid]
        else:
            if fill is not None:
                self._status(cid, "PARTIAL")
            self._maybe_reprice(r, now)
        return fill

    def _match(self, r: _Resting, trades: pd.DataFrame, expiry: pd.Timestamp) -> Fill | None:
        o = r.order
        if trades.empty:
            return None
        live = (trades["time"] >= r.submitted) & (trades["time"] < expiry)
        t = trades[live].sort_values("time", kind="stable")
        through = t["price"] < o.limit_price if o.side == "buy" else t["price"] > o.limit_price
        qty, last_ts = 0.0, None
        for vol, ts in zip(t.loc[through, "volume"], t.loc[through, "time"], strict=True):
            take = min(float(vol), o.qty - r.filled - qty)
            if take <= 0:
                break
            qty += take
            last_ts = ts
        if qty <= 0:
            return None
        r.filled += qty
        price = o.limit_price
        fill = Fill(client_id=o.client_id, ts=pd.Timestamp(last_ts), asset=o.asset, side=o.side, qty=qty,
                    price=price, fee=self.costs.fee(qty * price, "maker"), liquidity="maker")
        self.db.record_fill(self.account, fill, position_id=o.position_id)
        return fill

    def _maybe_reprice(self, r: _Resting, now: pd.Timestamp) -> None:
        due = min(MAX_REPRICES, int((now - r.submitted) // REPRICE_EVERY))
        if r.reprices >= due:
            return
        try:
            bids, asks = self.feed.book(r.order.asset)
        except FeedError as e:
            log.warning("reprice of %s skipped: %s", r.order.client_id, e)
            return
        side = bids if r.order.side == "buy" else asks
        if not side:
            return
        r.order = dataclasses.replace(r.order, limit_price=side[0][0])
        r.reprices = due
        status = "PARTIAL" if r.filled > 0 else "OPEN"
        self.db.record_order(self.account, r.order, status)  # upsert: new limit price

    # --- calibration -----------------------------------------------------------------------------

    def snapshot_books(self, assets: list[str], now: pd.Timestamp | None = None) -> None:
        """Store the top-10 levels of each asset's live book in `book_snapshots` (skips feed errors)."""
        ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        for asset in assets:
            try:
                bids, asks = self.feed.book(asset)
            except FeedError as e:
                log.warning("book snapshot %s skipped: %s", asset, e)
                continue
            self.db.record_book_snapshot(ts, asset, bids[:SNAPSHOT_LEVELS], asks[:SNAPSHOT_LEVELS])
