"""Market snapshots from the collector's ``fut_tickers`` envelopes (Kraken futures v3 tickers)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from luna.instruments import asset_from_perp

NAN = math.nan
Book = tuple[list[tuple[float, float]], list[tuple[float, float]]]  # (bids, asks) price, size


def _f(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return NAN
    return v if math.isfinite(v) else NAN


@dataclass(frozen=True)
class Quote:
    asset: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    mark: float
    index: float
    funding_abs: float  # current period (fundingRate), USD per unit per hour
    funding_rel: float  # current period (relativeFundingRate), fraction per hour
    pred_rel: float  # relativeFundingRatePrediction (next period, estimate)
    volume_usd: float  # 24h volumeQuote
    open_interest: float
    suspended: bool
    post_only: bool

    @property
    def valid(self) -> bool:
        """A takeable two-sided quote: bid > 0, ask > 0, bid < ask, not suspended / post-only."""
        ok = self.bid > 0 and self.ask > 0 and self.bid < self.ask  # NaN compares False
        return bool(ok) and not self.suspended and not self.post_only

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.valid else NAN

    @property
    def spread_full(self) -> float:
        return (self.ask - self.bid) / self.mid if self.valid else NAN

    @property
    def half_spread(self) -> float:
        return self.spread_full / 2

    @property
    def top_book_usd(self) -> float:
        """The thinner side of the top of book, USD."""
        return min(self.bid * self.bid_size, self.ask * self.ask_size) if self.valid else NAN


@dataclass(frozen=True)
class MarketSnapshot:
    ts: pd.Timestamp  # collector recv_ts of the tickers call
    quotes: dict[str, Quote]
    books: dict[str, tuple[pd.Timestamp, Book]] = field(default_factory=dict)  # latest fut_book per asset

    def depth_usd(self, asset: str, band: float, max_age_s: float, now: pd.Timestamp) -> float:
        """USD resting within ``mid x (1 +/- band)`` on the thinner side of the newest order book;
        the top of book (a lower bound of that depth) when no fresh book is known."""
        q = self.quotes.get(asset)
        if q is None or not q.valid:
            return NAN
        b = self.books.get(asset)
        if b is None or (pd.Timestamp(now) - b[0]).total_seconds() > max_age_s:
            return q.top_book_usd
        bids, asks = b[1]
        lo, hi = q.mid * (1 - band), q.mid * (1 + band)
        bid_usd = sum(p * s for p, s in bids if lo <= p <= q.mid)
        ask_usd = sum(p * s for p, s in asks if q.mid <= p <= hi)
        return max(min(bid_usd, ask_usd), q.top_book_usd)

    def age_s(self, now: pd.Timestamp) -> float:
        return (pd.Timestamp(now) - self.ts).total_seconds()


def parse_tickers(env: dict[str, Any]) -> MarketSnapshot | None:
    """One ``fut_tickers`` envelope -> snapshot (None if the call failed). Perpetual PF_ rows only;
    rows are kept even when their quote is unusable (``Quote.valid`` says so)."""
    if not env.get("ok") or not isinstance(env.get("payload"), dict):
        return None
    ts = pd.Timestamp(env["recv_ts"])
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    quotes: dict[str, Quote] = {}
    for t in env["payload"].get("tickers") or []:
        sym = t.get("symbol") or ""
        if not sym.startswith("PF_") or t.get("tag") != "perpetual":
            continue
        a = asset_from_perp(sym)
        quotes[a] = Quote(
            asset=a,
            bid=_f(t.get("bid")),
            ask=_f(t.get("ask")),
            bid_size=_f(t.get("bidSize")),
            ask_size=_f(t.get("askSize")),
            mark=_f(t.get("markPrice")),
            index=_f(t.get("indexPrice")),
            funding_abs=_f(t.get("fundingRate")),
            funding_rel=_f(t.get("relativeFundingRate")),
            pred_rel=_f(t.get("relativeFundingRatePrediction")),
            volume_usd=_f(t.get("volumeQuote")),
            open_interest=_f(t.get("openInterest")),
            suspended=bool(t.get("suspended", False)),
            post_only=bool(t.get("postOnly", False)),
        )
    return MarketSnapshot(ts, quotes)


def parse_book(env: dict[str, Any]) -> tuple[str, pd.Timestamp, Book] | None:
    """One collector ``fut_book`` envelope -> (asset, recv ts, (bids, asks)); None if unusable."""
    if not env.get("ok") or not isinstance(env.get("payload"), dict):
        return None
    sym = (env.get("params") or {}).get("symbol") or ""
    ob = env["payload"].get("orderBook")
    if not sym.startswith("PF_") or not isinstance(ob, dict):
        return None
    try:
        bids = [(float(p), float(s)) for p, s in ob.get("bids") or []]
        asks = [(float(p), float(s)) for p, s in ob.get("asks") or []]
    except (TypeError, ValueError):
        return None
    ts = pd.Timestamp(env["recv_ts"])
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return asset_from_perp(sym), ts, (bids, asks)
