"""Historical perp spread backfill from Kraken analytics `spreads` (V5 backtest protocol §2.1 S0.2).

Replay / research only; paper never imports it. V4 code is not imported.

- One request window per (asset, UTC day) following ``more`` pages; requests never reach the sealed
  holdout (``to <= cutoff - interval``), returned rows at or after the cut are dropped and counted.
- Resume-safe: every finished day is an atomic checkpoint ``parts/<asset>/<YYYYMMDD>.parquet`` with a
  sibling meta (params, rows, pages, dropped rows, sha256); a checkpoint whose sha does not match is
  fetched again. Re-running is idempotent.
- Bounded exponential backoff (1, 2, 4, 8 s, 5 attempts) on transport errors, 429 and 5xx.
- ``assemble`` writes ``<asset>.parquet`` + provenance including the **observed cadence** (median
  spacing of value changes): the Stage 0 probe found the 60 s series to be 5-minute snapshots
  carried forward, so a file never claims a resolution its values do not have.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from luna.carry.frozen import CUTOFF
from luna.instruments import perp_symbol
from luna.v2.data import HoldoutLeak

BASE = "https://futures.kraken.com/api/charts/v1/analytics"
DAY = pd.Timedelta("1D")
ATTEMPTS = 5
BACKOFF_S = (1.0, 2.0, 4.0, 8.0)
SPACING_S = 0.3


class FetchFailed(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _prices(x) -> list[float] | None:
    if not isinstance(x, list):
        return None
    try:
        return [math.nan if v is None else float(v) for v in x]
    except (TypeError, ValueError):
        return None


def parse_spreads(payload: dict) -> pd.DataFrame:
    """``{"result": {"timestamp": [...], "data": {"bid": {"best_price": [...]}, "ask": {...}}}}`` ->
    ``spread_full`` = (ask - bid) / mid by UTC timestamp. Any other shape raises."""
    res = payload.get("result") if isinstance(payload, dict) else None
    data = res.get("data") if isinstance(res, dict) else None
    ts = res.get("timestamp") if isinstance(res, dict) else None
    side = lambda k: _prices((data.get(k) or {}).get("best_price")) if isinstance(data, dict) and isinstance(data.get(k), dict) else None  # noqa: E731
    bid, ask = side("bid"), side("ask")
    if not isinstance(ts, list) or bid is None or ask is None or not (len(ts) == len(bid) == len(ask)):
        raise ValueError("unknown analytics spreads payload shape")
    idx = pd.DatetimeIndex(pd.to_datetime([int(t) for t in ts], unit="s", utc=True))
    s = [(a - b) / ((a + b) / 2) if b > 0 and a > 0 else math.nan for b, a in zip(bid, ask, strict=True)]
    df = pd.DataFrame({"spread_full": s}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _get(client, url: str, params: dict, sleep) -> dict:
    for attempt in range(ATTEMPTS):
        try:
            r = client.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if attempt == ATTEMPTS - 1:
                raise FetchFailed(f"{url} {params}: {e}") from e
            sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
    raise FetchFailed(url)  # pragma: no cover


def fetch_day(client, asset: str, day: pd.Timestamp, interval_s: int, *, cutoff: pd.Timestamp = CUTOFF,
              sleep=time.sleep) -> tuple[pd.DataFrame, dict]:
    if cutoff > CUTOFF:
        raise HoldoutLeak(f"cutoff {cutoff} after the holdout start {CUTOFF}")
    day = pd.Timestamp(day).floor("D")
    if day >= cutoff:
        raise HoldoutLeak(f"{asset}: day {day} is at or after the cut {cutoff}")
    url = f"{BASE}/{perp_symbol(asset)}/spreads"
    lo = int(day.timestamp())
    hi = min(int((day + DAY).timestamp()), int(cutoff.timestamp())) - interval_s
    parts, pages, cursor = [], 0, lo
    while cursor <= hi:
        if pages:
            sleep(SPACING_S)
        payload = _get(client, url, {"since": cursor, "to": hi, "interval": interval_s}, sleep)
        pages += 1
        df = parse_spreads(payload)
        parts.append(df)
        if not (payload.get("result") or {}).get("more") or df.empty:
            break
        nxt = int(df.index.max().timestamp()) + interval_s
        if nxt <= cursor:
            break
        cursor = nxt
    df = pd.concat(parts) if parts else parse_spreads({"result": {"timestamp": [], "data": {"bid": {"best_price": []}, "ask": {"best_price": []}}}})
    df = df[~df.index.duplicated(keep="last")].sort_index()
    n0 = len(df)
    df = df[(df.index >= day) & (df.index < min(day + DAY, cutoff))]
    leaked = int((parts and sum(int((p.index >= cutoff).sum()) for p in parts)) or 0)
    meta = {"endpoint": url, "asset": asset, "day": day.isoformat(), "interval_s": interval_s,
            "pages": pages, "rows": len(df), "rows_dropped_outside_day": n0 - len(df) - leaked,
            "rows_dropped_at_or_after_cutoff": leaked, "cutoff": cutoff.isoformat()}
    return df, meta


def _part(root: Path, asset: str, day: pd.Timestamp) -> Path:
    return root / "parts" / asset / f"{day:%Y%m%d}.parquet"


def _checkpoint_ok(p: Path) -> bool:
    m = p.with_suffix(".json")
    if not (p.exists() and m.exists()):
        return False
    try:
        return json.loads(m.read_text(encoding="utf-8"))["sha256"] == sha256_file(p)
    except (ValueError, KeyError, OSError):
        return False


def backfill(client, root: Path, assets: list[str], days: list[pd.Timestamp], interval_s: int, *,
             cutoff: pd.Timestamp = CUTOFF, sleep=time.sleep) -> dict[str, int]:
    """Fetch every missing (asset, day) checkpoint. Returns days fetched per asset."""
    root = Path(root)
    done: dict[str, int] = {}
    for a in assets:
        done[a] = 0
        for d in days:
            p = _part(root, a, d)
            if _checkpoint_ok(p):
                continue
            df, meta = fetch_day(client, a, d, interval_s, cutoff=cutoff, sleep=sleep)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            df.to_parquet(tmp)
            os.replace(tmp, p)
            meta["sha256"] = sha256_file(p)
            meta["fetched_at"] = pd.Timestamp.now(tz="UTC").isoformat()
            mt = p.with_suffix(".json.tmp")
            mt.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            os.replace(mt, p.with_suffix(".json"))
            done[a] += 1
            sleep(SPACING_S)
    return done


def observed_cadence_s(df: pd.DataFrame) -> int | None:
    """Median seconds between value changes (on consecutive samples): the real observation cadence."""
    v = df["spread_full"].to_numpy()
    t = df.index.asi8 // 10**9 if df.index.unit == "ns" else df.index.as_unit("s").asi8
    chg = t[1:][(v[1:] != v[:-1]) & ~(np.isnan(v[1:]) & np.isnan(v[:-1]))]
    if len(chg) < 3:
        return None
    return int(np.median(np.diff(chg)))


def assemble(root: Path, asset: str, interval_s: int, cutoff: pd.Timestamp = CUTOFF) -> Path:
    root = Path(root)
    parts = sorted((root / "parts" / asset).glob("*.parquet"))
    bad = [p.name for p in parts if not _checkpoint_ok(p)]
    if bad:
        raise ValueError(f"{asset}: checkpoints failing their sha: {bad[:5]}")
    df = pd.concat([pd.read_parquet(p) for p in parts]).sort_index()
    if df.index.duplicated().any():
        raise ValueError(f"{asset}: duplicate timestamps across checkpoints")
    if len(df) and df.index.max() >= cutoff:
        raise HoldoutLeak(f"{asset}: row {df.index.max()} at or after {cutoff}")
    out = root / f"{asset}.parquet"
    tmp = out.with_name(out.name + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, out)
    cad = observed_cadence_s(df)
    prov = {
        "asset": asset, "requested_interval_s": interval_s, "observed_cadence_s": cad,
        "real_resolution_matches_interval": cad == interval_s,
        "rows": len(df), "first": df.index.min().isoformat() if len(df) else None,
        "last": df.index.max().isoformat() if len(df) else None, "cutoff": cutoff.isoformat(),
        "checkpoints": len(parts), "sha256": sha256_file(out),
    }
    out.with_name(f"{asset}.provenance.json").write_text(json.dumps(prov, indent=1), encoding="utf-8")
    return out
