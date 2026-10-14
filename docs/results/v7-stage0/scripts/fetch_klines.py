"""S0-9 / S0-3 / S0-7 input: download W_DEV monthly USD-M klines (interval arg, default 15m) for USDT symbols,
verify each zip against its published .CHECKSUM, audit it, and keep a compact per-symbol parquet.

W_DEV only: months < 2025-07 (the file for 2025-06 ends 2025-06-30T23:45). Rows with open_time >= 2025-07-01 are dropped.
Raw zips are deleted after a successful parse (disk is tight); the audit keeps their sha256.
"""
import hashlib
import io
import json
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

LIST, OUT = Path(sys.argv[1]), Path(sys.argv[2])
KIND = sys.argv[3] if len(sys.argv) > 3 else "klines"  # klines | markPriceKlines | indexPriceKlines | premiumIndexKlines
IV = "15m"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://data.binance.vision/data/futures/um/monthly"
W_DEV_END_MS = 1751328000000
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
        "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def fetch(url):
    for a in range(6):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception:
            time.sleep(2 ** a)
    raise RuntimeError(url)


def one_symbol(sym, months):
    target = OUT / f"{sym}.parquet"
    audit_f = OUT / f"{sym}.audit.json"
    if target.exists() and audit_f.exists():
        return sym, "cached"
    frames, audit = [], []
    for ym, size in months:
        name = f"{sym}-{IV}-{ym}.zip"
        url = f"{BASE}/{KIND}/{sym}/{IV}/{name}"
        b = fetch(url)
        sha = hashlib.sha256(b).hexdigest()
        pub = fetch(url + ".CHECKSUM").decode().split()[0]
        a = {"month": ym, "bytes": len(b), "listed_size": size, "sha256": sha, "checksum_ok": sha == pub}
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            raw = z.read(z.namelist()[0])
        first = raw[:9].decode(errors="replace")
        a["header"] = first.startswith("open_time")
        df = pd.read_csv(io.BytesIO(raw), header=0 if a["header"] else None, names=None if a["header"] else COLS)
        df.columns = COLS[:len(df.columns)]
        t = df["open_time"].astype("int64")
        unit = "us" if t.max() > 1e14 else "ms"
        a["ts_unit"] = unit
        if unit == "us":
            t = t // 1000
        df["open_time"] = t
        a["rows"] = len(df)
        a["dupes"] = int(t.duplicated().sum())
        a["misaligned"] = int((t % 900000 != 0).sum())
        a["non_monotonic"] = int((t.diff().dropna() <= 0).sum())
        a["post_wdev_rows_dropped"] = int((t >= W_DEV_END_MS).sum())
        df = df[t < W_DEV_END_MS]
        audit.append(a)
        keep = df[["open_time", "high", "low", "close", "volume", "quote_volume"] + (["count"] if KIND == "klines" else [])]
        frames.append(keep)
    df = pd.concat(frames).drop_duplicates("open_time").sort_values("open_time")
    for c in df.columns:
        if c != "open_time":
            df[c] = df[c].astype("float64" if c in ("high", "low", "close") else "float32")
    df.to_parquet(target, index=False)
    t = df["open_time"].to_numpy()
    gaps = np.diff(t) // 900000 - 1 if len(t) > 1 else np.array([0])
    nz = df.loc[df["volume"] > 0, "open_time"]
    summ = {"symbol": sym, "kind": KIND, "first_bar": int(t[0]), "last_bar": int(t[-1]),
            "last_bar_volume_gt0": int(nz.iloc[-1]) if len(nz) else None, "bars": int(len(t)),
            "expected_bars_first_to_last": int((t[-1] - t[0]) // 900000 + 1),
            "missing_bars": int(gaps.sum()), "max_gap_bars": int(gaps.max()), "n_gaps": int((gaps > 0).sum()),
            "zero_volume_bars": int((df["volume"] == 0).sum()), "files": audit}
    audit_f.write_text(json.dumps(summ))
    return sym, len(t)


def main():
    name = {"klines": "monthly_klines_15m", "markPriceKlines": "monthly_markPriceKlines_15m",
            "indexPriceKlines": "monthly_indexPriceKlines_15m", "premiumIndexKlines": "monthly_premiumIndexKlines_15m"}[KIND]
    d = json.loads((LIST / f"{name}.json").read_text())["symbols"]
    jobs = {s: [f for f in v["files"] if f[0] < "2025-07"] for s, v in d.items()
            if s.endswith("USDT") and "_" not in s}
    jobs = {s: m for s, m in jobs.items() if m}
    print(len(jobs), "symbols", sum(len(m) for m in jobs.values()), "files", flush=True)
    done = 0
    with ThreadPoolExecutor(8) as ex:
        for sym, r in ex.map(lambda kv: one_symbol(*kv), jobs.items()):
            done += 1
            if done % 25 == 0:
                print(done, sym, r, time.strftime("%H:%M:%S"), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
