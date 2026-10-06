"""Kraken spot public REST: trades gap-fill (aggregated to 15m bars), OHLC and depth."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from luna.data.store import BAR_COLUMNS, _empty_bars, _normalize
from luna.http import get_json

SPOT_BASE = "https://api.kraken.com/0/public"


def _result(payload: dict) -> dict:
    if payload.get("error"):
        raise RuntimeError(f"Kraken error: {payload['error']}")
    return payload["result"]


def _pair_key(result: dict) -> str:
    return next(k for k in result if k != "last")


def fetch_trades_page(client, pair: str, since_ns: int) -> tuple[pd.DataFrame, int]:
    """One /Trades page (<=1000 trades) after cursor `since_ns`; returns (trades, next cursor)."""
    res = _result(get_json(client, f"{SPOT_BASE}/Trades", {"pair": pair, "since": int(since_ns)}))
    rows = res[_pair_key(res)]
    df = pd.DataFrame(
        {
            "price": pd.Series([float(r[0]) for r in rows], dtype="float64"),
            "volume": pd.Series([float(r[1]) for r in rows], dtype="float64"),
            "time": pd.to_datetime([r[2] for r in rows], unit="s", utc=True),
        }
    )
    return df, int(res["last"])


def aggregate_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Trades -> 15m bar frame; open/close by time order, empty bars omitted."""
    if trades.empty:
        return _empty_bars()
    t = trades.sort_values("time", kind="stable")
    g = t.groupby(t["time"].dt.floor("15min"))
    out = pd.DataFrame(
        {
            "open": g["price"].first(),
            "high": g["price"].max(),
            "low": g["price"].min(),
            "close": g["price"].last(),
            "volume": g["volume"].sum(),
            "trades": g["price"].count().astype("int64"),
        }
    )
    out.index.name = None
    return _normalize(out)


def backfill_trades(
    client,
    pair: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    checkpoint: Path,
    sleep_s: float = 1.0,
) -> pd.DataFrame:
    """Page /Trades from the checkpoint (or `start`) until trades reach `end`; 15m bars for [start, end)."""
    checkpoint = Path(checkpoint)
    since = int(start.value)
    if checkpoint.exists():
        since = int(json.loads(checkpoint.read_text())["since"])
    pages: list[pd.DataFrame] = []
    while True:
        df, last = fetch_trades_page(client, pair, since)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps({"since": last}))
        if df.empty:
            break
        pages.append(df)
        if (df["time"] >= end).any() or last <= since:
            break
        since = last
        if sleep_s:
            time.sleep(sleep_s)
    if not pages:
        return _empty_bars()
    all_trades = pd.concat(pages, ignore_index=True)
    all_trades = all_trades[(all_trades["time"] >= start) & (all_trades["time"] < end)]
    return aggregate_trades(all_trades)


def fetch_ohlc(client, pair: str) -> pd.DataFrame:
    """Last <=720 closed 15m bars (the still-forming last row is dropped)."""
    res = _result(get_json(client, f"{SPOT_BASE}/OHLC", {"pair": pair, "interval": 15}))
    rows = res[_pair_key(res)][:-1]
    if not rows:
        return _empty_bars()
    df = pd.DataFrame(
        {
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[6]) for r in rows],
            "trades": [int(r[7]) for r in rows],
        },
        index=pd.to_datetime([int(r[0]) for r in rows], unit="s", utc=True),
    )
    return _normalize(df)[BAR_COLUMNS]


def fetch_depth(
    client, pair: str, count: int = 10
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Order book (bids, asks) as (price, qty) lists, best first."""
    res = _result(get_json(client, f"{SPOT_BASE}/Depth", {"pair": pair, "count": count}))
    book = res[_pair_key(res)]
    bids = [(float(r[0]), float(r[1])) for r in book["bids"]]
    asks = [(float(r[0]), float(r[1])) for r in book["asks"]]
    return bids, asks
