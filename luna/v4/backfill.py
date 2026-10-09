"""`luna backfill-v4` (spec §7.1): historical V4 inputs that really exist, hard-cut before the
sealed holdout (2026-07-01):

- real funding (`historicalfundingrates`): both `fundingRate` (abs, USD/unit/h) and
  `relativeFundingRate` (rel, decimal/h). The API returns its whole history in one response, so rows
  at or after the cut are dropped in memory before anything is written;
- mark and index (`charts/v1/mark|spot/<sym>/15m`) candles;
- perp analytics `spreads` (hourly by default), full quoted spread as a fraction of mid.

Requests never ask for a time at or after the cut. Every written file gets a sibling
`<asset>.provenance.json` (endpoint, params, fetched_at, sha256 of the parquet, rows, cutoff).
No signal, return or P&L is computed here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from luna.http import get_json
from luna.instruments import perp_symbol
from luna.v2.data import HoldoutLeak
from luna.v4.constants import BAR, V4_DATA_END
from luna.v4.funding import parse_funding

log = logging.getLogger(__name__)

BASE = "https://futures.kraken.com"
FUNDING_URL = f"{BASE}/derivatives/api/v4/historicalfundingrates"
HISTORY_START = pd.Timestamp("2023-01-01T00:00:00Z")
SPREAD_INTERVAL_S = 3600
SPREAD_CHUNK = pd.Timedelta(days=30)
TICK_DIRS = {"mark": "mark", "spot": "index"}  # Kraken "spot" chart = its index price


def _check_cutoff(cutoff: pd.Timestamp) -> None:
    if cutoff > V4_DATA_END:
        raise HoldoutLeak(f"cutoff {cutoff} is after the sealed holdout start {V4_DATA_END}")


def _last_request_s(end: pd.Timestamp, cutoff: pd.Timestamp) -> int:
    """Last epoch second a request may cover: strictly before the cut."""
    _check_cutoff(cutoff)
    if end > cutoff:
        raise HoldoutLeak(f"requested end {end} is after the cutoff {cutoff}")
    return int(end.timestamp()) - 1


def fetch_funding_history(client: httpx.Client, asset: str, cutoff: pd.Timestamp = V4_DATA_END):
    _check_cutoff(cutoff)
    params = {"symbol": perp_symbol(asset)}
    df = parse_funding(get_json(client, FUNDING_URL, params))
    kept = df[df.index < cutoff]
    if len(kept) and kept.index.max() >= cutoff:  # pragma: no cover - defensive
        raise HoldoutLeak(f"{asset}: funding at or after {cutoff}")
    meta = {"endpoint": FUNDING_URL, "params": params, "rows_dropped_at_or_after_cutoff": len(df) - len(kept)}
    return kept, meta


def fetch_chart_candles(
    client: httpx.Client,
    tick_type: str,
    asset: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cutoff: pd.Timestamp = V4_DATA_END,
):
    """15m `mark` or `spot` (index) candles with start <= open_ts and close <= min(end, cutoff)."""
    if tick_type not in TICK_DIRS:
        raise ValueError(f"tick_type must be one of {sorted(TICK_DIRS)}")
    to = _last_request_s(end, cutoff) - (int(BAR.total_seconds()) - 1)  # last open_ts whose bar closes by end
    url = f"{BASE}/api/charts/v1/{tick_type}/{perp_symbol(asset)}/15m"
    cursor = int(start.timestamp())
    rows: list[dict] = []
    while cursor <= to:
        data = get_json(client, url, {"from": cursor, "to": to})
        candles = data.get("candles") or []
        rows.extend(candles)
        if not data.get("more_candles") or not candles:
            break
        nxt = max(int(c["time"]) for c in candles) // 1000 + int(BAR.total_seconds())
        if nxt <= cursor:
            break
        cursor = nxt
    cols = ("open", "high", "low", "close")
    if rows:
        raw = pd.DataFrame(rows)
        idx = pd.DatetimeIndex(pd.to_datetime(raw["time"].astype("int64"), unit="ms", utc=True))
        df = pd.DataFrame({c: raw[c].astype("float64").to_numpy() for c in cols}, index=idx)
        df = df[~df.index.duplicated(keep="last")].sort_index()
    else:
        df = pd.DataFrame({c: pd.Series(dtype="float64") for c in cols}, index=pd.DatetimeIndex([], tz="UTC"))
    df = df[(df.index >= start) & (df.index <= min(end, cutoff) - BAR)]
    meta = {"endpoint": url, "params": {"from": int(start.timestamp()), "to": to}, "tick_type": tick_type}
    return df, meta


def _num_list(x: Any) -> list[float] | None:
    if isinstance(x, list) and all(v is None or isinstance(v, (int, float, str)) for v in x):
        return [float("nan") if v is None else float(v) for v in x]
    return None


def parse_spreads(payload: dict) -> pd.DataFrame:
    """Analytics `spreads` -> frame indexed by UTC timestamp with `spread_full` (fraction of mid).

    Accepted shapes of `result.data` (the exact shape must be confirmed on the live API in Stage 0;
    an unknown shape fails loudly rather than being guessed): a list of spread values, a dict with a
    `spread` list, or a dict with `bid` / `ask` lists (spread = (ask - bid) / mid)."""
    res = payload.get("result") or {}
    ts = res.get("timestamp") or []
    data = res.get("data")
    idx = pd.DatetimeIndex(pd.to_datetime([int(t) for t in ts], unit="s", utc=True))
    spread: list[float] | None = _num_list(data)
    if spread is None and isinstance(data, dict):
        if "spread" in data:
            spread = _num_list(data["spread"])
        elif "bid" in data and "ask" in data:
            bid, ask = _num_list(data["bid"]), _num_list(data["ask"])
            if bid is not None and ask is not None:
                spread = [(a - b) / ((a + b) / 2) for b, a in zip(bid, ask, strict=True)]
    if spread is None or len(spread) != len(idx):
        raise ValueError("unknown analytics spreads payload shape")
    df = pd.DataFrame({"spread_full": spread}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_spreads(
    client: httpx.Client,
    asset: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cutoff: pd.Timestamp = V4_DATA_END,
    interval_s: int = SPREAD_INTERVAL_S,
):
    last = _last_request_s(end, cutoff)
    url = f"{BASE}/api/charts/v1/analytics/{perp_symbol(asset)}/spreads"
    parts = []
    lo = int(start.timestamp())
    while lo <= last:
        hi = min(lo + int(SPREAD_CHUNK.total_seconds()) - 1, last)
        parts.append(parse_spreads(get_json(client, url, {"since": lo, "to": hi, "interval": interval_s})))
        lo = hi + 1
    df = pd.concat(parts) if parts else parse_spreads({"result": {"timestamp": [], "data": []}})
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= start) & (df.index < min(end, cutoff))]
    meta = {"endpoint": url, "params": {"since": int(start.timestamp()), "to": last, "interval": interval_s}}
    return df, meta


def _write(root: Path, kind: str, asset: str, df: pd.DataFrame, meta: dict, now: pd.Timestamp,
           cutoff: pd.Timestamp) -> None:
    if len(df) and df.index.max() >= cutoff:
        raise HoldoutLeak(f"{kind}/{asset}: row {df.index.max()} at or after {cutoff}")
    d = root / "history_v4" / kind
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{asset}.parquet"
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)
    prov = {
        **meta,
        "asset": asset,
        "kind": kind,
        "fetched_at": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "rows": len(df),
        "first": df.index.min().isoformat() if len(df) else None,
        "last": df.index.max().isoformat() if len(df) else None,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    (d / f"{asset}.provenance.json").write_text(json.dumps(prov, indent=1), encoding="utf-8")


def backfill_v4(
    client: httpx.Client,
    var_dir: Path,
    assets: list[str],
    *,
    start: pd.Timestamp = HISTORY_START,
    now: pd.Timestamp | None = None,
    cutoff: pd.Timestamp = V4_DATA_END,
) -> list[str]:
    """Download every V4 historical input for `assets` into `var_dir/history_v4/<kind>/`."""
    _check_cutoff(cutoff)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    root = Path(var_dir)
    done = []
    for asset in assets:
        df, meta = fetch_funding_history(client, asset, cutoff)
        _write(root, "funding", asset, df, meta, now, cutoff)
        for tick, kind in TICK_DIRS.items():
            df, meta = fetch_chart_candles(client, tick, asset, start, cutoff, cutoff)
            _write(root, kind, asset, df, meta, now, cutoff)
        df, meta = fetch_spreads(client, asset, start, cutoff, cutoff)
        _write(root, "spreads", asset, df, meta, now, cutoff)
        log.info("backfill-v4 %s done", asset)
        done.append(asset)
    return done
