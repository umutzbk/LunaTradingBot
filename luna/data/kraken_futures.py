"""Kraken Futures public history: 15m trade candles, funding rates, perp listing."""

from __future__ import annotations

import re

import httpx
import pandas as pd

from luna.config import CostConfig
from luna.data.store import BAR_COLUMNS, _empty_bars, _normalize
from luna.http import get_json
from luna.instruments import perp_instrument, perp_symbol
from luna.types import Instrument

BASE = "https://futures.kraken.com"
BAR_SECONDS = 900
_PERP_RE = re.compile(r"^PF_[A-Z0-9]+USD$")


def fetch_candles(client: httpx.Client, asset: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """15m trade bars with start <= open time <= end (inclusive), paginating while more_candles."""
    url = f"{BASE}/api/charts/v1/trade/{perp_symbol(asset)}/15m"
    to = int(end.timestamp())
    cursor = int(start.timestamp())
    rows: list[dict] = []
    while cursor <= to:
        data = get_json(client, url, {"from": cursor, "to": to})
        candles = data.get("candles") or []
        rows.extend(candles)
        if not data.get("more_candles") or not candles:
            break
        nxt = max(int(c["time"]) for c in candles) // 1000 + BAR_SECONDS
        if nxt <= cursor:  # no progress: stop instead of looping forever
            break
        cursor = nxt
    if not rows:
        return _empty_bars()
    df = pd.DataFrame(rows)
    idx = pd.DatetimeIndex(pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True))
    out = pd.DataFrame(
        {c: df[c].astype("float64").to_numpy() for c in ("open", "high", "low", "close", "volume")},
        index=idx,
    )
    out["trades"] = pd.Series(0, index=idx, dtype="int64")
    out["filled"] = False
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out[(out.index >= start) & (out.index <= end)]
    return _normalize(out)[BAR_COLUMNS]


def fetch_funding(client: httpx.Client, asset: str) -> pd.DataFrame:
    """Hourly funding history; `rate` = relativeFundingRate (per-hour fraction)."""
    url = f"{BASE}/derivatives/api/v4/historicalfundingrates"
    data = get_json(client, url, {"symbol": perp_symbol(asset)})
    rates = data.get("rates") or []
    idx = pd.DatetimeIndex(pd.to_datetime([r["timestamp"] for r in rates], utc=True))
    df = pd.DataFrame({"rate": [float(r["relativeFundingRate"]) for r in rates]}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


def list_perps(client: httpx.Client, costs: CostConfig) -> list[tuple[Instrument, float]]:
    """(instrument, 24h quote volume USD) for every PF_*USD perp that has ticker volume."""
    instruments = get_json(client, f"{BASE}/derivatives/api/v3/instruments").get("instruments", [])
    tickers = get_json(client, f"{BASE}/derivatives/api/v3/tickers").get("tickers", [])
    volume = {t["symbol"]: float(t["volumeQuote"]) for t in tickers if t.get("volumeQuote") is not None}
    out = []
    for meta in instruments:
        sym = meta.get("symbol", "")
        if _PERP_RE.match(sym) and sym in volume:
            out.append((perp_instrument(meta, costs), volume[sym]))
    return out
