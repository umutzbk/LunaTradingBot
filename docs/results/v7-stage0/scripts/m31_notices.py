"""M3-1 step 2 (outcome-free, W_DEV only): reconstruct official Binance termination notices from immutable,
timestamped Wayback captures.

Guard discipline (the M3-4 / DEC-46 rule, applied here):
  - every CDX query carries a server-side `to` bound <= 20250630235959;
  - captures are fetched only at exact (timestamp, original URL) pairs returned by such a query, in raw `id_` form,
    with redirects disabled; any non-200 status, redirect or Memento-Datetime mismatch / >= 2025-07-01 is refused
    and the response discarded unparsed. Accepted raw bytes are stored as captures/<ts>_<sha256[:16]>.bin.
No price, return or P&L is read.

Discovery per event (last traded bar close L):
  (a) stored W_DEV catalog-161 entries whose title names the symbol or base asset in a futures context -> their code;
  (b) slug search: CDX prefix query on binance.com/en/support/announcement/ in [L - 120 d, min(L + 30 d, 2025-06-30)]
      filtered on the base asset, post-filtered to whole slug tokens plus a futures / delisting / swap word.
Slug-less announcement URLs (used until about late 2022) can only be found through route (a).
"""
import calendar, gzip, hashlib, html, json, re, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

V = Path(sys.argv[1]); OUT = Path(sys.argv[2]); CAP = V / "m31" / "captures"; CAP.mkdir(parents=True, exist_ok=True)
BOUND = "20250630235959"
ERR = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPEN = urllib.request.build_opener(NoRedirect)


def get(url, tries=6):
    for a in range(tries):
        try:
            with OPEN.open(url, timeout=120) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.code, dict(e.headers), b""
            if e.code == 404:
                return 404, {}, b""
            time.sleep(15 * (a + 1))
        except Exception:
            time.sleep(15 * (a + 1))
    ERR.append(url)
    return None, {}, b""


def cdx(params):
    assert params.get("to", BOUND) <= BOUND, params
    params = dict(params, to=min(params.get("to", BOUND), BOUND))
    st, _, b = get("https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(params, doseq=True))
    time.sleep(2)
    if st != 200:
        return None
    return [ln.split(" ") for ln in b.decode().splitlines() if ln.strip()]


def ep_to_ts(e):
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(e))


def decode(b):
    try:
        return gzip.decompress(b).decode("utf-8", "replace")
    except OSError:
        return b.decode("utf-8", "replace")


def fetch_capture(ts, original):
    assert ts <= BOUND
    cached = sorted(CAP.glob(f"{ts}_*.bin"))
    if cached:  # stored only after the guard checks passed
        b = cached[0].read_bytes()
        return {"status": 200, "memento": ts, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b), "text": decode(b)}
    st, hd, b = get(f"https://web.archive.org/web/{ts}id_/{original}")
    time.sleep(2)
    md = {k.lower(): v for k, v in hd.items()}.get("memento-datetime")
    if st != 200 or not md:
        return {"status": st, "refused": "non-200 or redirect"}
    mts = time.strftime("%Y%m%d%H%M%S", time.strptime(md, "%a, %d %b %Y %H:%M:%S GMT"))
    if mts != ts or mts > BOUND:
        return {"status": st, "refused": f"memento {mts} != requested {ts}"}
    h = hashlib.sha256(b).hexdigest()
    (CAP / f"{ts}_{h[:16]}.bin").write_bytes(b)
    return {"status": 200, "memento": mts, "sha256": h, "bytes": len(b), "text": decode(b)}


TIME_RE = re.compile(r"(20\d\d-\d\d-\d\d),? (\d{1,2}:\d\d)(?::\d\d)?\s*(AM|PM|am|pm)?\s*\(UTC\)")


def analyse(raw, sym):
    base = sym[:-4]
    m = re.search(r"<title[^>]*>([^<]{0,400})", raw)
    title = html.unescape(m.group(1)).split(" | ")[0] if m else None
    md = re.search(r'<meta name="description" content="([^"]{0,1500})', raw)
    t = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&").replace("\\n", " ").replace('\\"', '"')
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t)
    sym_hits = len(re.findall(r"(?<![A-Z0-9])" + re.escape(sym) + r"(?![A-Z0-9])", t))
    base_ctx = len(re.findall(r"(?<![A-Z0-9])" + re.escape(base) + r"(?![A-Z0-9])[^.]{0,40}?(USDT-Margined|USD.-M|USDT Perpetual|Perpetual Contract|USDT-M)", t))
    times = []
    for mm in TIME_RE.finditer(t):
        a = max(0, mm.start() - 300)
        times.append({"utc": mm.group(0), "context": t[a:mm.end() + 40]})
    return {"title": title, "meta_description": html.unescape(md.group(1)) if md else None,
            "exact_symbol_token_hits": sym_hits, "base_with_usdt_margined_context": base_ctx,
            "futures_context": bool(re.search(r"USD.-M|USDT-Margined|Perpetual|Binance Futures", t)),
            "mentions_automatic_settlement": bool(re.search(r"(?i)automatic settlement|settle", t)),
            "states_index_average_30min": bool(re.search(r"(?i)average[^.]{0,120}index price[^.]{0,120}30 minutes|30 minutes[^.]{0,120}average[^.]{0,120}index", t)),
            "utc_times": times[:30]}


def main():
    events = json.loads((OUT / "events.json").read_text())
    cat = json.loads((V / "delist/catalog161_index.json").read_text(encoding="utf-8"))["articles"]
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    prev = {}
    pf = OUT / "notices.json"
    if pf.exists():
        prev = {(r["symbol"], r["instance"]): r for r in json.loads(pf.read_text(encoding="utf-8"))}
    results = []
    for ev in events:
        key = (ev["symbol"], ev["instance"])
        if only and ev["symbol"] not in only and key in prev:
            results.append(prev[key]); continue
        sym = ev["symbol"]; base = sym[:-4]
        L = calendar.timegm(time.strptime(ev["last_trade_bar_close"][:19], "%Y-%m-%dT%H:%M:%S"))
        lo, hi = ep_to_ts(L - 120 * 86400), min(ep_to_ts(L + 30 * 86400), BOUND)
        cands = {}
        for a in cat:
            ti = a["title"]
            if re.search(r"\b(" + re.escape(sym) + "|" + re.escape(base) + r")\b", ti) and re.search(r"Futures|Perpetual|USD.?-M|Margined", ti):
                rows = cdx({"url": "binance.com/en/support/announcement/", "matchType": "prefix", "to": BOUND,
                            "filter": [f"original:.*{a['code']}.*", "statuscode:200"], "fl": "timestamp,original", "limit": "500"})
                for ts, orig in (rows or []):
                    cands.setdefault(orig.split("?")[0].lower(), []).append((ts, orig, "catalog161:" + a["code"]))
        rows = cdx({"url": "binance.com/en/support/announcement/", "matchType": "prefix", "from": lo, "to": hi,
                    "filter": [f"original:.*{base.lower()}.*", "statuscode:200"], "fl": "timestamp,original",
                    "collapse": "urlkey", "limit": "3000"})
        slug_query_failed = rows is None
        for ts, orig in (rows or []):
            toks = orig.split("?")[0].lower().rsplit("/", 1)[-1].split("-")
            if base.lower() in toks or any(x.startswith(base.lower() + "usdt") for x in toks):
                if any(w in toks for w in ("futures", "perpetual", "contract", "contracts", "margined", "delist", "swap",
                                           "migration", "rebranding", "redenomination", "settlement")):
                    cands.setdefault(orig.split("?")[0].lower(), []).append((ts, orig, "slug"))
        notices = []
        for k, lst in sorted(cands.items()):
            rows = cdx({"url": k.replace("https://", ""), "matchType": "prefix", "to": BOUND,
                        "filter": ["statuscode:200"], "fl": "timestamp,original", "limit": "50"})
            rows = sorted(rows or []) or sorted((ts, orig) for ts, orig, _ in lst)
            got = None
            for ts, orig in rows[:4]:
                c = fetch_capture(ts, orig)
                if c.get("status") == 200 and "text" in c:
                    got = c; break
            entry = {"url": k, "found_by": sorted({s for _, _, s in lst}), "n_captures_bounded": len(rows)}
            if got:
                entry.update({"capture_ts": got["memento"], "sha256": got["sha256"], "bytes": got["bytes"],
                              **analyse(got.pop("text"), sym)})
            else:
                entry["capture"] = "not retrievable under the guard"
            notices.append(entry)
        r = {**ev, "search_window": [lo, hi], "slug_query_failed": slug_query_failed, "notices": notices}
        results.append(r)
        print(sym, ev["last_trade_bar_close"][:16], "slugfail" if slug_query_failed else "", len(notices),
              [(n.get("capture_ts"), n.get("exact_symbol_token_hits"), n.get("base_with_usdt_margined_context"), (n.get("title") or "")[:70]) for n in notices], flush=True)
        pf.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    (OUT / "notices_errors.json").write_text(json.dumps(ERR, indent=1))


if __name__ == "__main__":
    main()
