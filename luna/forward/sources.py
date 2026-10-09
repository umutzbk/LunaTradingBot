"""One function per source id (spec §3): each returns the requests of one scheduled round."""

from __future__ import annotations

from typing import Any

from luna.forward.config import FUTURES_BASE, PERP_SYMBOLS, SPOT_BASE, SPOT_PAIRS
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
