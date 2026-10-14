"""DEC-36 feasibility (outcome-free): for each stored W_DEV futures notice, the earliest Wayback capture (HTTP 200)
of its announcement page. CDX queries are server-side bounded with to=20250630235959 (S-1.7 allowlist).
Only capture metadata (timestamp, status) is read; no capture content is downloaded."""
import calendar, json, sys, time, urllib.request, urllib.parse
from pathlib import Path
V = Path(sys.argv[1]); out = Path(sys.argv[2])
rows = []
for n in json.loads((V / "delist/futures_delist_announcements.json").read_text(encoding="utf-8")):
    q = urllib.parse.urlencode({"url": "binance.com/en/support/announcement/", "matchType": "prefix",
                                "to": "20250630235959", "filter": [f"original:.*{n['code']}.*", "statuscode:200"],
                                "fl": "timestamp,original", "limit": "2000"}, doseq=True)
    for a in range(4):
        try:
            with urllib.request.urlopen("https://web.archive.org/cdx/search/cdx?" + q, timeout=180) as r:
                rowsx = sorted(ln.split(" ") for ln in r.read().decode().splitlines() if ln.strip()); ts = [x[0] for x in rowsx]; break
        except Exception:
            time.sleep(5 * (a + 1)); ts = None
    rel = int(n["releaseDate"]) // 1000
    first = None
    if ts:
        t0 = ts[0]; first = calendar.timegm(time.strptime(t0, "%Y%m%d%H%M%S"))  # UTC (was mktime - timezone: 1 h off in BST)
    rows.append({"code": n["code"], "title": n["title"], "releaseDate": rel, "first_capture": first, "first_capture_ts": ts[0] if ts else None, "first_capture_url": rowsx[0][1] if ts else None, "n_captures": len(ts) if ts else 0,
                 "lag_h": None if first is None else round((first - rel) / 3600, 1)})
    time.sleep(1)
out.write_text(json.dumps(rows, indent=1))
lags = sorted(r["lag_h"] for r in rows if r["lag_h"] is not None)
print(len(rows), "notices;", len(lags), "with a capture < 2025-07-01;",
      "median lag h:", lags[len(lags)//2] if lags else None,
      "<=24h:", sum(l <= 24 for l in lags), "<=72h:", sum(l <= 72 for l in lags), "<=7d:", sum(l <= 168 for l in lags))
