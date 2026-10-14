"""S0-1/S0-3/S0-11: list the Binance USD-M public archive (names, sizes, dates only; no file content).

Entries dated >= 2026-07-01 (M6 start) are not recorded: only a per-symbol flag "has files at/after M6 start".
Output: one JSON per dataset in OUT: {symbol: {"files": [[date, size], ...], "post_m6": bool}}.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
M6 = "2026-07-01"
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)


def s3list(prefix, delimiter=True):
    keys, prefixes, marker = [], [], ""
    while True:
        q = {"prefix": prefix}
        if delimiter:
            q["delimiter"] = "/"
        if marker:
            q["marker"] = marker
        url = BASE + "?" + urllib.parse.urlencode(q)
        for attempt in range(6):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    root = ET.fromstring(r.read())
                break
            except Exception:
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"listing failed: {url}")
        for c in root.iter(NS + "Contents"):
            keys.append((c.find(NS + "Key").text, int(c.find(NS + "Size").text)))
        for p in root.iter(NS + "CommonPrefixes"):
            prefixes.append(p.find(NS + "Prefix").text)
        if root.find(NS + "IsTruncated").text != "true":
            return keys, prefixes
        marker = (keys[-1][0] if keys else prefixes[-1])


DATE = re.compile(r"-(\d{4}-\d{2}(?:-\d{2})?)\.zip$")


def symbol_files(prefix):
    keys, _ = s3list(prefix, delimiter=False)
    files, post = [], False
    for k, size in keys:
        m = DATE.search(k)
        if not m:
            continue
        d = m.group(1)
        if (d + "-01")[:10] >= M6 if len(d) == 7 else d >= M6:
            post = True
            continue
        files.append([d, size])
    return {"files": sorted(files), "post_m6": post}


DATASETS = {  # name: (prefix template, interval subdir or None)
    "monthly_klines_15m": ("data/futures/um/monthly/klines/", "15m"),
    "monthly_klines_1d": ("data/futures/um/monthly/klines/", "1d"),
    "monthly_klines_1m": ("data/futures/um/monthly/klines/", "1m"),
    "monthly_fundingRate": ("data/futures/um/monthly/fundingRate/", None),
    "monthly_markPriceKlines_15m": ("data/futures/um/monthly/markPriceKlines/", "15m"),
    "monthly_indexPriceKlines_15m": ("data/futures/um/monthly/indexPriceKlines/", "15m"),
    "monthly_premiumIndexKlines_15m": ("data/futures/um/monthly/premiumIndexKlines/", "15m"),
    "monthly_bookTicker": ("data/futures/um/monthly/bookTicker/", None),
    "monthly_aggTrades": ("data/futures/um/monthly/aggTrades/", None),
    "daily_bookTicker": ("data/futures/um/daily/bookTicker/", None),
    "daily_bookDepth": ("data/futures/um/daily/bookDepth/", None),
    "daily_metrics": ("data/futures/um/daily/metrics/", None),
    "daily_klines_15m": ("data/futures/um/daily/klines/", "15m"),
}


def run(name):
    root, iv = DATASETS[name]
    target = OUT / f"{name}.json"
    if target.exists():
        return name, "cached"
    _, syms = s3list(root)
    syms = [p[len(root):].strip("/") for p in syms]

    def one(sym):
        pre = f"{root}{sym}/" + (f"{iv}/" if iv else "")
        return sym, symbol_files(pre)

    with ThreadPoolExecutor(12) as ex:
        res = dict(ex.map(one, syms))
    res = {k: v for k, v in res.items() if v["files"] or v["post_m6"]}
    target.write_text(json.dumps({"dataset": name, "prefix": root, "interval": iv,
                                  "listed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "m6_truncation": M6, "symbols": res}))
    return name, len(res)


if __name__ == "__main__":
    names = sys.argv[2:] or list(DATASETS)
    for n in names:
        t = time.time()
        print(run(n), f"{time.time() - t:.0f}s", flush=True)
