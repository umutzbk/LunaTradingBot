"""Live public market-data feeds (Kraken Futures perps, Kraken spot). No API keys.

`refresh(now)` appends newly *closed* 15m bars (index + 15min <= now) to the BarStore and
returns a rolling in-memory buffer per asset. Every network or payload problem surfaces as
`FeedError` (after `luna.http.get_json`'s retries), so the engine can skip the tick.

Funding (perp): the tickers endpoint's `fundingRate` is the *absolute* hourly rate (USD per
contract); the relative per-hour fraction comparable to historicalfundingrates'
`relativeFundingRate` (what the backtest uses) is `fundingRate / markPrice`.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import httpx
import pandas as pd

from luna.data.kraken_futures import BASE as FUTURES_BASE
from luna.data.kraken_futures import fetch_candles
from luna.data.kraken_spot import SPOT_BASE, fetch_depth, fetch_ohlc, fetch_trades_page
from luna.data.store import BarStore, _empty_bars, fill_missing
from luna.http import get_json, make_client
from luna.instruments import SPOT_ALIAS, perp_symbol, spot_pair
from luna.types import Venue

log = logging.getLogger("luna.feeds")

BAR = pd.Timedelta("15min")
BUFFER_BARS = 3000
BOOK_DEPTH = 10  # spot Depth levels requested

Book = tuple[list[tuple[float, float]], list[tuple[float, float]]]


class FeedError(Exception):
    """Market data unavailable (retries exhausted) or malformed."""


class _guard:
    """Context manager turning transport / HTTP / payload errors into FeedError."""

    _ERRORS = (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError, StopIteration,
               RuntimeError, AttributeError)

    def __init__(self, what: str) -> None:
        self.what = what

    def __enter__(self) -> None:
        return None

    def __exit__(self, et, ev, tb) -> bool:
        if ev is not None and isinstance(ev, self._ERRORS) and not isinstance(ev, FeedError):
            raise FeedError(f"{self.what}: {type(ev).__name__}: {ev}") from ev
        return False


@runtime_checkable
class LiveFeed(Protocol):
    def refresh(self, now: pd.Timestamp) -> dict[str, pd.DataFrame]: ...
    def funding_rates(self) -> dict[str, float | None]: ...
    def book(self, asset: str) -> Book: ...
    def mids(self) -> dict[str, float]: ...


class _BarFeed:
    venue: Venue

    def __init__(
        self,
        assets: list[str],
        store: BarStore,
        client: httpx.Client | None = None,
        buffer_bars: int = BUFFER_BARS,
    ) -> None:
        self.assets = list(assets)
        self.store = store
        self.client = client if client is not None else make_client()
        self.buffer_bars = buffer_bars
        self._cache: dict[str, pd.DataFrame] = {}  # raw stored bars (tail), not gap-filled

    def _fetch_new(self, asset: str, last: pd.Timestamp | None, last_closed: pd.Timestamp) -> pd.DataFrame:
        raise NotImplementedError

    def _cached(self, asset: str) -> pd.DataFrame:
        if asset not in self._cache:
            self._cache[asset] = self.store.read(self.venue, asset).tail(self.buffer_bars)
        return self._cache[asset]

    def refresh(self, now: pd.Timestamp) -> dict[str, pd.DataFrame]:
        """Append bars closed since the last stored bar; return the last `buffer_bars` closed
        bars per asset (gaps forward-filled with filled=True). Raises FeedError."""
        now = pd.Timestamp(now)
        last_closed = now.floor("15min") - BAR  # open time of the newest closed bar
        out: dict[str, pd.DataFrame] = {}
        for asset in self.assets:
            cached = self._cached(asset)
            last = cached.index[-1] if len(cached) else None
            if last is None or last < last_closed:
                with _guard(f"{self.venue} bars {asset}"):
                    new = self._fetch_new(asset, last, last_closed)
                new = new[new.index + BAR <= now]  # never the still-forming bar
                if last is not None:
                    new = new[new.index > last]  # never rewrite stored history
                if len(new):
                    if last is not None and new.index[0] - last > BAR:
                        log.warning("%s %s: hole %s -> %s (forward-filled in buffer)",
                                    self.venue, asset, last, new.index[0])
                    self.store.append(self.venue, asset, new)
                    cached = pd.concat([cached, new]) if len(cached) else new
                    self._cache[asset] = cached = cached.tail(self.buffer_bars)
            out[asset] = fill_missing(cached).tail(self.buffer_bars)
        return out


class KrakenFuturesLiveFeed(_BarFeed):
    venue: Venue = "kraken_futures"

    def _fetch_new(self, asset: str, last: pd.Timestamp | None, last_closed: pd.Timestamp) -> pd.DataFrame:
        start = last + BAR if last is not None else last_closed - (self.buffer_bars - 1) * BAR
        if start > last_closed:
            return _empty_bars()
        return fetch_candles(self.client, asset, start, last_closed)

    def _tickers(self) -> dict[str, dict]:
        with _guard("futures tickers"):
            rows = get_json(self.client, f"{FUTURES_BASE}/derivatives/api/v3/tickers")["tickers"]
            return {t["symbol"]: t for t in rows}

    def funding_rates(self) -> dict[str, float | None]:
        """Relative hourly funding per asset (fundingRate / markPrice); None if unavailable."""
        tickers = self._tickers()
        out: dict[str, float | None] = {}
        with _guard("futures funding"):
            for asset in self.assets:
                t = tickers.get(perp_symbol(asset)) or {}
                rate, mark = t.get("fundingRate"), t.get("markPrice")
                out[asset] = (float(rate) / float(mark)
                              if rate is not None and mark is not None and float(mark) > 0 else None)
        return out

    def book(self, asset: str) -> Book:
        with _guard(f"futures orderbook {asset}"):
            data = get_json(self.client, f"{FUTURES_BASE}/derivatives/api/v3/orderbook",
                            {"symbol": perp_symbol(asset)})
            ob = data["orderBook"]
            bids = sorted(((float(p), float(q)) for p, q in ob["bids"]), key=lambda x: -x[0])
            asks = sorted(((float(p), float(q)) for p, q in ob["asks"]), key=lambda x: x[0])
        return bids, asks

    def mids(self) -> dict[str, float]:
        """(bid + ask) / 2 from tickers; markPrice when bid/ask is missing; absent assets omitted."""
        tickers = self._tickers()
        out: dict[str, float] = {}
        with _guard("futures mids"):
            for asset in self.assets:
                t = tickers.get(perp_symbol(asset))
                if t is None:
                    continue
                bid, ask = t.get("bid"), t.get("ask")
                if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                    out[asset] = (float(bid) + float(ask)) / 2.0
                elif t.get("markPrice") is not None:
                    out[asset] = float(t["markPrice"])
        return out


class KrakenSpotLiveFeed(_BarFeed):
    venue: Venue = "kraken_spot"

    def _fetch_new(self, asset: str, last: pd.Timestamp | None, last_closed: pd.Timestamp) -> pd.DataFrame:
        return fetch_ohlc(self.client, spot_pair(asset))  # last <=720 closed bars

    def funding_rates(self) -> dict[str, float | None]:
        return {}

    def book(self, asset: str) -> Book:
        with _guard(f"spot depth {asset}"):
            return fetch_depth(self.client, spot_pair(asset), count=BOOK_DEPTH)

    def trades_since(self, asset: str, since_ns: int) -> tuple[pd.DataFrame, int]:
        """Public trades after cursor `since_ns` (columns price, volume, time) and the next cursor."""
        with _guard(f"spot trades {asset}"):
            return fetch_trades_page(self.client, spot_pair(asset), since_ns)

    def mids(self) -> dict[str, float]:
        """(bid + ask) / 2 from one Ticker call. Kraken keys legacy pairs as X<code>Z<quote>
        (XXBTZUSD); both that and the altname are matched."""
        pairs = {asset: spot_pair(asset) for asset in self.assets}
        with _guard("spot ticker"):
            payload = get_json(self.client, f"{SPOT_BASE}/Ticker", {"pair": ",".join(pairs.values())})
            if payload.get("error"):
                raise RuntimeError(f"Kraken error: {payload['error']}")
            res = payload["result"]
            out: dict[str, float] = {}
            for asset, pair in pairs.items():
                code = SPOT_ALIAS.get(asset, asset)
                t = res.get(pair) or res.get(f"X{code}ZUSD")
                if t is None:
                    continue
                out[asset] = (float(t["a"][0]) + float(t["b"][0])) / 2.0
        return out
