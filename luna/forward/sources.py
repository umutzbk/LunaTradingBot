"""One function per source id (spec §3): each returns the requests of one scheduled round."""

from __future__ import annotations

from functools import partial
from typing import Any

from luna.forward.config import (
    ANALYTICS_INTERVAL_S,
    ANALYTICS_LOOKBACK_S,
    ANALYTICS_TYPES,
    DEPTH_COUNT,
    FUNDING_KEEP_S,
    FUTURES_BASE,
    PERP_SYMBOLS,
    SPOT_BASE,
    SPOT_PAIRS,
)
from luna.forward.envelope import NS, Request, iso_ns, ns_from_iso

_PERPS = frozenset(PERP_SYMBOLS)
FUNDING_URL = f"{FUTURES_BASE}/derivatives/api/v4/historicalfundingrates"


def filter_tickers(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only universe perps in ``tickers`` (rows untouched, missing ones not invented)."""
    out = dict(payload)
    out["tickers"] = [t for t in payload.get("tickers") or [] if t.get("symbol") in _PERPS]
    return out


def filter_instruments(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only universe perps in ``instruments`` (rows untouched)."""
    out = dict(payload)
    out["instruments"] = [i for i in payload.get("instruments") or [] if i.get("symbol") in _PERPS]
    return out


def fut_tickers() -> list[Request]:
    return [Request("fut_tickers", f"{FUTURES_BASE}/derivatives/api/v3/tickers", None, filter_tickers, True)]


def spot_ticker() -> list[Request]:
    return [Request("spot_ticker", f"{SPOT_BASE}/Ticker", {"pair": ",".join(SPOT_PAIRS)})]


def fut_book() -> list[Request]:
    url = f"{FUTURES_BASE}/derivatives/api/v3/orderbook"
    return [Request("fut_book", url, {"symbol": s}) for s in PERP_SYMBOLS]


def spot_depth() -> list[Request]:
    url = f"{SPOT_BASE}/Depth"
    return [Request("spot_depth", url, {"pair": p, "count": DEPTH_COUNT}) for p in SPOT_PAIRS]


def filter_rates_since(since_ns: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only funding rows with ``timestamp >= since`` (rows unmodified; selection only)."""
    out = dict(payload)
    out["rates"] = [r for r in payload.get("rates") or []
                    if r.get("timestamp") and ns_from_iso(r["timestamp"]) >= since_ns]
    return out


def fut_funding(slot_s: int) -> list[Request]:
    """Hourly: the endpoint returns the whole history; only rows of the last 48 h are stored."""
    since_ns = (slot_s - FUNDING_KEEP_S) * NS
    extra = {"filtered_since_ts": iso_ns(since_ns)}
    flt = partial(filter_rates_since, since_ns)
    return [Request("fut_funding", FUNDING_URL, {"symbol": s}, flt, extra=extra) for s in PERP_SYMBOLS]


def fut_funding_full() -> list[Request]:
    """Daily audit trail: the full, unfiltered funding history of every perp."""
    extra = {"filtered_since_ts": None}
    return [Request("fut_funding_full", FUNDING_URL, {"symbol": s}, extra=extra) for s in PERP_SYMBOLS]


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


def fut_instruments() -> list[Request]:
    url = f"{FUTURES_BASE}/derivatives/api/v3/instruments"
    return [Request("fut_instruments", url, None, filter_instruments, True)]


def spot_assetpairs() -> list[Request]:
    return [Request("spot_assetpairs", f"{SPOT_BASE}/AssetPairs", {"pair": ",".join(SPOT_PAIRS)})]
