"""Download and load the sealed holdout window (spec S-GUARD-1: part of the single, user-approved
opening). Every function needs the ``HoldoutAuthorization``. Files are written once (exclusive) with
a sha256 manifest; nothing outside [2026-07-01, collector_start) is kept; nothing is filled in.

Sources (public Kraken endpoints, as for the frozen development inputs):
- perp trade / mark / index 15-minute candles: futures charts API (trade, mark, spot tick types);
- funding: derivatives historicalfundingrates (rel and abs per hourly period);
- perp spreads: analytics spreads, interval 300 (5-minute snapshots, the V5 Stage 0 source);
- spot 15-minute bars: public spot Trades aggregated to 15-minute bars.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from luna.data.kraken_spot import aggregate_trades, fetch_trades_page
from luna.http import get_json
from luna.instruments import perp_symbol, spot_pair
from luna.v6 import guard
from luna.v6.data import BarPanel, FundingPanel
from luna.v6.devdata import Inputs, hourly_prices, load_dev

FUT = "https://futures.kraken.com"
ANALYTICS = f"{FUT}/api/charts/v1/analytics"
DAY = pd.Timedelta("1D")


def _require(auth) -> None:
    if not isinstance(auth, guard.HoldoutAuthorization):
        raise guard.HoldoutBlocked("holdout data needs the user's explicit approval (authorize_holdout)")


def _window():
    return guard.HOLDOUT_START, guard.HOLDOUT_END


def _write_once(path: Path, df: pd.DataFrame) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    df.to_parquet(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_candles(client, tick: str, asset: str, auth) -> pd.DataFrame:
    _require(auth)
    lo, hi = _window()
    url = f"{FUT}/api/charts/v1/{tick}/{perp_symbol(asset)}/15m"
    rows, cursor, to = [], int(lo.timestamp()), int(hi.timestamp())
    while cursor < to:
        data = get_json(client, url, {"from": cursor, "to": to})
        c = data.get("candles") or []
        rows += c
        if not data.get("more_candles") or not c:
            break
        nxt = max(int(x["time"]) for x in c) // 1000 + 900
        if nxt <= cursor:
            break
        cursor = nxt
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    idx = pd.DatetimeIndex(pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)).as_unit("ns")
    out = pd.DataFrame({c: df[c].astype(float).to_numpy() for c in ("open", "high", "low", "close")}, index=idx)
    out["volume"] = df["volume"].astype(float).to_numpy() if "volume" in df else np.nan
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out[(out.index >= lo) & (out.index + pd.Timedelta("15min") <= hi)]  # bars closed before the end


def fetch_funding(client, asset: str, auth) -> pd.DataFrame:
    _require(auth)
    lo, hi = _window()
    data = get_json(client, f"{FUT}/derivatives/api/v4/historicalfundingrates", {"symbol": perp_symbol(asset)})
    rates = data.get("rates") or []
    idx = pd.DatetimeIndex(pd.to_datetime([r["timestamp"] for r in rates], utc=True)).as_unit("ns")
    df = pd.DataFrame({"funding_abs": [float(r["fundingRate"]) for r in rates],
                       "funding_rel": [float(r["relativeFundingRate"]) for r in rates]}, index=idx)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[(df.index >= lo) & (df.index + pd.Timedelta("1h") <= hi)]


def fetch_spreads(client, asset: str, auth) -> pd.DataFrame:
    _require(auth)
    lo, hi = _window()
    url = f"{ANALYTICS}/{perp_symbol(asset)}/spreads"
    out, day = [], lo.floor("D")
    while day < hi:
        cur, end = int(max(day, lo).timestamp()), int(min(day + DAY, hi).timestamp()) - 300
        while cur <= end:
            p = get_json(client, url, {"since": cur, "to": end, "interval": 300})
            res = p.get("result") or {}
            ts = res.get("timestamp") or []
            data = res.get("data") or {}
            bid = (data.get("bid") or {}).get("best_price") or []
            ask = (data.get("ask") or {}).get("best_price") or []
            if not ts:
                break
            t = pd.to_datetime(ts, unit="s", utc=True).as_unit("ns")
            b, a = np.asarray(bid, float), np.asarray(ask, float)
            mid = (a + b) / 2
            out.append(pd.Series(np.where(mid > 0, (a - b) / mid, np.nan), index=t))
            if not res.get("more"):
                break
            nxt = int(t.max().timestamp()) + 300
            if nxt <= cur:
                break
            cur = nxt
        day += DAY
    if not out:
        return pd.DataFrame({"spread_full": []}, index=pd.DatetimeIndex([], tz="UTC"))
    s = pd.concat(out)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[(s.index >= lo) & (s.index < hi)]
    if (s.index.as_unit("ns").asi8 % pd.Timedelta("5min").value).any():
        raise ValueError(f"{asset}: spread labels not on the 5-minute grid (not interval-300 snapshots)")
    return s.to_frame("spread_full")


def fetch_spot(client, asset: str, auth, sleep=None) -> pd.DataFrame:
    _require(auth)
    lo, hi = _window()
    pair = spot_pair(asset)
    since, parts = int(lo.value) - 1, []
    while True:
        df, last = fetch_trades_page(client, pair, since)
        if df.empty:
            break
        parts.append(df[(df["time"] >= lo) & (df["time"] < hi)])
        if df["time"].max() >= hi or last <= since:
            break
        since = last
        if sleep:
            sleep(1.0)
    trades = pd.concat(parts) if parts else pd.DataFrame(columns=["price", "volume", "time"])
    bars = aggregate_trades(trades)
    bars.index = bars.index.as_unit("ns")
    return bars[(bars.index >= lo) & (bars.index + pd.Timedelta("15min") <= hi)]


def download(client, root: Path, assets: list, auth) -> dict:
    """The holdout download (once). Returns the manifest {relative path: sha256}."""
    _require(auth)
    root = Path(root)
    man = {}
    for a in assets:
        for name, df in (("trade", fetch_candles(client, "trade", a, auth)), ("mark", fetch_candles(client, "mark", a, auth)),
                         ("index", fetch_candles(client, "spot", a, auth)), ("funding", fetch_funding(client, a, auth)),
                         ("spreads", fetch_spreads(client, a, auth)), ("spot", fetch_spot(client, a, auth))):
            rel = f"{name}/{a}.parquet"
            man[rel] = _write_once(root / rel, df)
    mp = root / "manifest.json"
    fd = os.open(mp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"window": [str(guard.HOLDOUT_START), str(guard.HOLDOUT_END)], "files": man}, f, indent=1)
    return man


def load_holdout(frozen_root: Path, root: Path, auth) -> Inputs:
    """Development inputs (history for the models) + the verified holdout download, scope holdout."""
    _require(auth)
    root = Path(root)
    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["files"]
    for rel, sha in man.items():
        if hashlib.sha256((root / rel).read_bytes()).hexdigest() != sha:
            raise ValueError(f"holdout file {rel} changed after download")
    dev = load_dev(frozen_root)
    assets = list(dev.mark.close.columns)

    def cat(panel: BarPanel, name: str, col: str, usd: bool = False) -> BarPanel:
        add = {}
        for a in assets:
            d = pd.read_parquet(root / name / f"{a}.parquet")
            add[a] = d["volume"] * d["close"] if usd else d[col]
        lo, hi = panel.close.index.min(), guard.HOLDOUT_END.floor("15min")
        idx = pd.date_range(lo, hi - pd.Timedelta("15min"), freq="15min").as_unit("ns")
        full = pd.concat([panel.close, pd.DataFrame(add)]).sort_index()
        full = full[~full.index.duplicated(keep="first")].reindex(idx)
        return BarPanel(full)

    mark = cat(dev.mark, "mark", "close")
    funding_rel, funding_abs = dev.funding.rel.copy(), dev.funding.abs.copy()
    fr = {a: pd.read_parquet(root / "funding" / f"{a}.parquet") for a in assets}
    R = pd.concat([funding_rel, pd.DataFrame({a: f["funding_rel"] for a, f in fr.items()})]).sort_index()
    A = pd.concat([funding_abs, pd.DataFrame({a: f["funding_abs"] for a, f in fr.items()})]).sort_index()
    R, A = R[~R.index.duplicated(keep="first")], A[~A.index.duplicated(keep="first")]
    spreads = {a: pd.concat([dev.spreads[a], pd.read_parquet(root / "spreads" / f"{a}.parquet")["spread_full"]]).sort_index()
               for a in assets}
    inp = Inputs("holdout", mark, cat(dev.trade, "trade", "close"), cat(dev.volume_usd, "trade", "", usd=True),
                 cat(dev.spot, "spot", "close"), cat(dev.spot_volume_usd, "spot", "", usd=True),
                 cat(dev.index, "index", "close"), FundingPanel(R, abs_=A), spreads, dev.insts, hourly_prices(mark))
    guard.require_window(guard.HOLDOUT_START, inp.mark.close.index.max(), "holdout", auth)
    return inp
