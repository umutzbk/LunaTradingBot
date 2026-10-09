"""One function per source id (spec §3): each returns the requests of one scheduled round."""

from __future__ import annotations

from typing import Any

from luna.forward.config import (
    ANALYTICS_INTERVAL_S,
    ANALYTICS_LOOKBACK_S,
    ANALYTICS_TYPES,
    DEPTH_COUNT,
    FUTURES_BASE,
    PERP_SYMBOLS,
    SPOT_BASE,
    SPOT_PAIRS,
)
from luna.forward.envelope import Request

_PERPS = frozenset(PERP_SYMBOLS)


def filter_tickers(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only universe perps in ``tickers`` (rows untouched, missing ones not invented)."""
    out = dict(payload)
    out["tickers"] = [t for t in payload.get("tickers") or [] if t.get("symbol") in _PERPS]
    return out


def fut_tickers() -> list[Request]:
    return [Request("fut_tickers", f"{FUTURES_BASE}/derivatives/api/v3/tickers", None, filter_tickers)]


def spot_ticker() -> list[Request]:
    return [Request("spot_ticker", f"{SPOT_BASE}/Ticker", {"pair": ",".join(SPOT_PAIRS)})]


def fut_book() -> list[Request]:
    url = f"{FUTURES_BASE}/derivatives/api/v3/orderbook"
    return [Request("fut_book", url, {"symbol": s}) for s in PERP_SYMBOLS]


def spot_depth() -> list[Request]:
    url = f"{SPOT_BASE}/Depth"
    return [Request("spot_depth", url, {"pair": p, "count": DEPTH_COUNT}) for p in SPOT_PAIRS]


def fut_funding() -> list[Request]:
    url = f"{FUTURES_BASE}/derivatives/api/v4/historicalfundingrates"
    return [Request("fut_funding", url, {"symbol": s}) for s in PERP_SYMBOLS]


def fut_analytics(now_s: int) -> list[Request]:
    """Every analytics type of every perp over the last 3 hours at 5-minute resolution."""
    params = {"since": now_s - ANALYTICS_LOOKBACK_S, "to": now_s, "interval": ANALYTICS_INTERVAL_S}
    return [
        Request("fut_analytics", f"{FUTURES_BASE}/api/charts/v1/analytics/{s}/{t}", dict(params))
        for s in PERP_SYMBOLS
        for t in ANALYTICS_TYPES
    ]


def spot_time() -> list[Request]:
    return [Request("spot_time", f"{SPOT_BASE}/Time", None)]
