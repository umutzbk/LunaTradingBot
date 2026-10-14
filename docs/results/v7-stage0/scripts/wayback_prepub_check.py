"""m3-8 follow-up (outcome-free, W_DEV only): for notices whose first capture lies within 1 h of releaseDate (the set
that showed negative lags before the UTC fix), fetch that exact capture (bounded CDX pair, id_, no redirects,
Memento-Datetime check) and verify that the captured page is the same Binance futures notice: title equality with the
stored W_DEV catalog title and article-code presence."""
import calendar, gzip, hashlib, html, json, re, sys, time, urllib.error, urllib.request
from pathlib import Path
R = Path(sys.argv[1]); OUTF = Path(sys.argv[2]); CAP = Path(sys.argv[3]); CAP.mkdir(parents=True, exist_ok=True)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
OPEN = urllib.request.build_opener(NoRedirect)
rows = json.loads(R.read_text(encoding="utf-8"))
out = []
for r in rows:
    if r["lag_h"] is None or r["lag_h"] >= 1.0:
        continue
    ts, url = r["first_capture_ts"], r["first_capture_url"]
    assert ts <= "20250630235959"
    cached = sorted(CAP.glob(f"{ts}_*.bin"))
    if cached:
        b = cached[0].read_bytes(); status = 200; mts = ts
    else:
        try:
            with OPEN.open(f"https://web.archive.org/web/{ts}id_/{url}", timeout=120) as resp:
                status = resp.status; md = resp.headers.get("Memento-Datetime"); b = resp.read()
            mts = time.strftime("%Y%m%d%H%M%S", time.strptime(md, "%a, %d %b %Y %H:%M:%S GMT"))
        except urllib.error.HTTPError as e:
            out.append({"code": r["code"], "refused": e.code}); continue
        if status != 200 or mts != ts:
            out.append({"code": r["code"], "refused": f"{status} {mts}"}); continue
        CAP.joinpath(f"{ts}_{hashlib.sha256(b).hexdigest()[:16]}.bin").write_bytes(b)
        time.sleep(2)
    try: t = gzip.decompress(b).decode("utf-8", "replace")
    except OSError: t = b.decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>([^<]{0,400})", t)
    cap_title = html.unescape(m.group(1)).split(" | ")[0].strip() if m else None
    out.append({"code": r["code"], "catalog_title": r["title"], "capture_ts": ts, "lag_h": r["lag_h"],
                "sha256": hashlib.sha256(b).hexdigest(), "captured_title": cap_title,
                "title_equal": (cap_title or "").strip() == r["title"].strip(), "code_in_page": r["code"] in t})
OUTF.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
for o in out: print(o.get("lag_h"), o.get("title_equal"), o.get("code_in_page"), (o.get("captured_title") or str(o.get("refused")))[:80])
