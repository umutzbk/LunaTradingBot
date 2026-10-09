"""Synthetic builders for the carry tests (no network, no collector)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from luna.carry.config import CarryConfig, load_carry_config
from luna.types import Instrument

T0 = pd.Timestamp("2026-10-12T00:00:00Z")  # a Monday


def carry_config(**overrides: Any) -> CarryConfig:
    """The default.yaml carry section with top-level overrides (``gates__x`` / ``risk__x`` /
    ``evaluation__x`` override nested fields)."""
    base = load_carry_config()
    data = base.model_dump()
    for key, value in overrides.items():
        if "__" in key:
            group, field = key.split("__", 1)
            data[group][field] = value
        else:
            data[key] = value
    return CarryConfig(**data)


def instrument(asset: str, step: float = 0.0001) -> Instrument:
    return Instrument(
        asset=asset,
        symbol=f"PF_{asset}USD",
        venue="kraken_futures",
        kind="perp",
        qty_step=step,
        price_tick=0.0001,
        min_qty=step,
        min_notional=0.0,
        maker_fee=0.0002,
        taker_fee=0.0005,
        initial_margin=0.02,
        maintenance_margin=0.01,
    )


def ticker(
    asset: str,
    mid: float = 100.0,
    spread_bps: float = 2.0,
    *,
    size: float = 1000.0,
    volume_usd: float = 50e6,
    rel: float = 1e-5,
    suspended: bool = False,
    post_only: bool = False,
    mark: float | None = None,
) -> dict[str, Any]:
    """One Kraken futures v3 ticker row (the real field names)."""
    half = mid * spread_bps / 2e4
    sym = "PF_XBTUSD" if asset == "BTC" else f"PF_{asset}USD"
    return {
        "symbol": sym,
        "tag": "perpetual",
        "pair": f"{asset}:USD",
        "bid": mid - half,
        "ask": mid + half,
        "bidSize": size,
        "askSize": size,
        "markPrice": mid if mark is None else mark,
        "indexPrice": mid,
        "last": mid,
        "vol24h": volume_usd / mid,
        "volumeQuote": volume_usd,
        "openInterest": 1e6,
        "fundingRate": rel * mid,
        "relativeFundingRate": rel,
        "fundingRatePrediction": rel * mid,
        "relativeFundingRatePrediction": rel,
        "suspended": suspended,
        "postOnly": post_only,
    }


def tickers_envelope(ts: pd.Timestamp, rows: list[dict[str, Any]], ok: bool = True) -> dict[str, Any]:
    iso = ts.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
    return {
        "source": "fut_tickers",
        "seq": 1,
        "req_ts": iso,
        "recv_ts": iso,
        "latency_ms": 10.0,
        "attempts": 1,
        "ok": ok,
        "error": None if ok else "boom",
        "url": "u",
        "params": None,
        "filtered_to_universe": True,
        "payload": {"result": "success", "serverTime": iso, "tickers": rows} if ok else None,
    }


def funding_envelope(
    asset: str,
    rates: list[tuple[pd.Timestamp, float, float]],
    source: str = "fut_funding",
    recv: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """``rates`` = [(period start, abs, rel), ...]."""
    sym = "PF_XBTUSD" if asset == "BTC" else f"PF_{asset}USD"
    r = recv if recv is not None else (rates[-1][0] if rates else T0)
    iso = r.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
    return {
        "source": source,
        "seq": 1,
        "req_ts": iso,
        "recv_ts": iso,
        "latency_ms": 10.0,
        "attempts": 1,
        "ok": True,
        "error": None,
        "url": "u",
        "params": {"symbol": sym},
        "filtered_to_universe": False,
        "payload": {
            "rates": [
                {
                    "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "fundingRate": a,
                    "relativeFundingRate": rl,
                }
                for t, a, rl in rates
            ]
        },
    }


def flat_history(
    rel: float, end: pd.Timestamp, hours: int = 720, price: float = 100.0
) -> list[tuple[pd.Timestamp, float, float]]:
    """``hours`` hourly periods ending at ``end`` (last period start = end - 1h), constant rate."""
    starts = pd.date_range(end - pd.Timedelta(hours=hours), end - pd.Timedelta(hours=1), freq="1h")
    return [(t, rel * price, rel) for t in starts]
