"""S0-2: REST /fapi/v1/fundingRate for every reconstructed symbol, W_DEV timestamps only (endTime < 2025-07-01).

Writes one CSV per symbol (fundingTime, fundingRate, markPrice) and a summary JSON. Resumable.
Rate: the endpoint shares 500 requests / 5 min / IP; we stay at <= 1.2 req/s.
"""
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LIST = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
W_DEV_END_MS = 1751328000000  # 2025-07-01T00:00:00Z
END_MS = W_DEV_END_MS - 1
START_MS = 1567296000000  # 2019-09-01


def symbols():
    s = set()
    for name in ("monthly_klines_15m", "monthly_fundingRate"):
        d = json.loads((LIST / f"{name}.json").read_text())["symbols"]
        for sym, v in d.items():
            if sym.endswith("USDT") and "_" not in sym and v["files"] and v["files"][0][0] < "2025-07":
                s.add(sym)
    return sorted(s)


def get(params):
    url = "https://fapi.binance.com/fapi/v1/fundingRate?" + urllib.parse.urlencode(params)
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                time.sleep(60 * (attempt + 1))
            else:
                time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    raise RuntimeError(url)


def main():
    syms = symbols()
    print(len(syms), "symbols", flush=True)
    for i, sym in enumerate(syms):
        f = OUT / f"{sym}.csv"
        if f.exists():
            continue
        rows, start = [], START_MS
        while True:
            t0 = time.time()
            data = get({"symbol": sym, "startTime": start, "endTime": END_MS, "limit": 1000})
            data = [d for d in data if int(d["fundingTime"]) <= END_MS]  # belt and braces: W_DEV only
            rows += data
            time.sleep(max(0.0, 0.85 - (time.time() - t0)))
            if len(data) < 1000:
                break
            start = int(data[-1]["fundingTime"]) + 1
        tmp = f.with_suffix(".tmp")
        with tmp.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["fundingTime", "fundingRate", "markPrice"])
            for d in rows:
                w.writerow([d["fundingTime"], d["fundingRate"], d.get("markPrice", "")])
        tmp.replace(f)
        if i % 50 == 0:
            print(i, sym, len(rows), time.strftime("%H:%M:%S"), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
