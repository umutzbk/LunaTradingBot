"""S0-2: archive monthly fundingRate files (W_DEV months only), checksum-verified; one CSV per symbol."""
import hashlib
import io
import json
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIST, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
W_DEV_END_MS = 1751328000000


def fetch(url):
    for a in range(6):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception:
            time.sleep(2 ** a)
    raise RuntimeError(url)


def one(item):
    sym, months = item
    target = OUT / f"{sym}.csv"
    if target.exists():
        return sym, "cached"
    lines, audit, header = [], [], None
    for ym, _ in months:
        url = f"{BASE}/{sym}/{sym}-fundingRate-{ym}.zip"
        b = fetch(url)
        ok = hashlib.sha256(b).hexdigest() == fetch(url + ".CHECKSUM").decode().split()[0]
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            rows = z.read(z.namelist()[0]).decode().splitlines()
        if rows and not rows[0][:1].isdigit():
            header = rows[0]
            rows = rows[1:]
        rows = [r for r in rows if r and int(r.split(",")[0]) < W_DEV_END_MS]
        lines += rows
        audit.append({"month": ym, "checksum_ok": ok, "rows": len(rows), "sha256": hashlib.sha256(b).hexdigest()})
    target.with_suffix(".audit.json").write_text(json.dumps({"symbol": sym, "header": header, "files": audit}))
    target.write_text((header or "calc_time,funding_interval_hours,last_funding_rate") + "\n" + "\n".join(lines) + "\n")
    return sym, len(lines)


def main():
    d = json.loads((LIST / "monthly_fundingRate.json").read_text())["symbols"]
    jobs = {s: [f for f in v["files"] if f[0] < "2025-07"] for s, v in d.items() if s.endswith("USDT") and "_" not in s}
    jobs = [(s, m) for s, m in jobs.items() if m]
    print(len(jobs), "symbols", flush=True)
    with ThreadPoolExecutor(6) as ex:
        for i, (s, r) in enumerate(ex.map(one, jobs)):
            if i % 50 == 0:
                print(i, s, r, time.strftime("%H:%M:%S"), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
