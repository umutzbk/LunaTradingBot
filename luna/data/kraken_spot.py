"""Kraken spot public REST: trades gap-fill (aggregated to 15m bars), OHLC and depth."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from luna.config import CostConfig
from luna.data.store import BAR_COLUMNS, _empty_bars, _normalize
from luna.http import get_json
from luna.instruments import asset_from_spot
from luna.types import Instrument

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


def _write_checkpoint(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _empty_trades() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "price": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
            "time": pd.Series(dtype="datetime64[ns, UTC]"),
        }
    )


def backfill_trades(
    client,
    pair: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    checkpoint: Path,
    on_bars: Callable[[pd.DataFrame], None],
    sleep_s: float = 1.0,
) -> int:
    """Page /Trades over [start, end), emitting only COMPLETED 15m bars through `on_bars`.

    The bar containing the latest trade is held back; the checkpoint stores that bar's
    (bar-aligned) start as `since`, written only after the completed bars were emitted, so a
    resume rebuilds the held bar from all its trades. Kraken's `since` is exclusive, so
    requests use `since - 1`. Returns the number of bars emitted in this run.
    """
    checkpoint = Path(checkpoint)
    start_ns, end_ns = int(start.value), int(end.value)
    ident = {"pair": pair, "start": start_ns, "end": end_ns}
    since = start_ns
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if {k: saved.get(k) for k in ident} != ident:
            raise ValueError(f"checkpoint {checkpoint} belongs to a different pair/start/end")
        since = int(saved["since"])
    since = min(since, end_ns)
    emitted = 0
    buffer = _empty_trades()

    def emit(bars: pd.DataFrame) -> None:
        nonlocal emitted
        if not bars.empty:
            on_bars(bars)
            emitted += len(bars)

    cursor = since - 1
    while True:
        page, last = fetch_trades_page(client, pair, cursor)
        done = page.empty or last <= cursor or bool((page["time"] >= end).any())
        keep = page[(page["time"] >= start) & (page["time"] < end)]
        buffer = pd.concat([buffer, keep], ignore_index=True)
        if done:
            emit(aggregate_trades(buffer))
            _write_checkpoint(checkpoint, {**ident, "since": end_ns})
            return emitted
        held = page["time"].max().floor("15min")
        bars = aggregate_trades(buffer)
        emit(bars[bars.index < held])
        buffer = buffer[buffer["time"] >= held]
        _write_checkpoint(checkpoint, {**ident, "since": min(int(held.value), end_ns)})
        cursor = last
        if sleep_s:
            time.sleep(sleep_s)


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


def fetch_spot_instruments(client, pairs: list[str], costs: CostConfig) -> dict[str, Instrument]:
    """Instruments (by asset) from /AssetPairs for the given altnames, e.g. ["XBTUSD", "ETHUSD"]."""
    res = _result(get_json(client, f"{SPOT_BASE}/AssetPairs", {"pair": ",".join(pairs)}))
    by_alt = {v["altname"]: v for v in res.values()}
    out = {}
    for pair in pairs:
        meta = by_alt.get(pair)
        if meta is None:
            continue
        asset = asset_from_spot(pair)
        out[asset] = Instrument(
            asset=asset,
            symbol=pair,
            venue="kraken_spot",
            kind="spot",
            qty_step=10.0 ** -int(meta["lot_decimals"]),
            price_tick=float(meta["tick_size"]),
            min_qty=float(meta["ordermin"]),
            min_notional=float(meta["costmin"]),
            maker_fee=costs.maker_fee,
            taker_fee=costs.taker_fee,
        )
    return out
