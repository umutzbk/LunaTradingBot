"""Task 25 (SC-03, DK-01): the S-1.7 fetch layer. ``netguard.check`` runs before the transport is touched;
every refusal is a K9 record (UTC time, request, rule, reason) plus an incident-report stub; N1 is
checksum-verified; N2 listings fail closed on truncation and drop M6 keys; N6 needs an exact registered N5
pair, HTTP 200, no redirect and ``Memento-Datetime`` == ts; the post-opening E6 mode needs the hardened
authorization.

No real network: a fake ``Transport`` (and ``httpx.MockTransport`` for the default httpx transport)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from luna.v7 import fetch, guard, k9, netguard, windows
from luna.v7.fetch import Fetcher, Response
from luna.v7.netguard import EndpointRefused, Request
from luna.v7.timeutil import to_ns
from tests.v7 import auth_fixtures as af

FAPI = "https://fapi.binance.com/fapi/v1/"
BVIS = "https://data.binance.vision/data/futures/um"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDX = "https://web.archive.org/cdx/search/cdx"
ORIG = "https://www.binance.com/en/support/announcement/x"
MS_OK = str(to_ns("2025-06-30T23:59:59") // 10**6)
MS_BAD = str(to_ns("2025-07-01") // 10**6)
ZIP_DEV = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip"
ZIP_HOLD = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-08.zip"
ZIP_M6 = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-07.zip"
RECV = to_ns("2026-10-15T12:00:00")


@dataclass
class FakeTransport:
    """Serves fixed responses by URL and records every call (a refused request must never get here)."""

    routes: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def get(self, url, params, *, timeout):
        self.calls.append((url, dict(params), timeout))
        if url not in self.routes:
            raise AssertionError(f"unexpected transport call {url}")
        r = self.routes[url]
        return r(url, params) if callable(r) else r


def ok(body: bytes, url: str = "", headers: dict | None = None) -> Response:
    return Response(200, dict(headers or {}), body, url)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(autouse=True)
def _clean_registry():
    netguard.cdx_pair_registry.clear()
    yield
    netguard.cdx_pair_registry.clear()


@pytest.fixture
def out(tmp_path) -> Path:
    return tmp_path / "run_out"


def fetcher(transport, out: Path) -> Fetcher:
    return Fetcher(transport, out, clock=lambda: RECV)


def k9_records(out: Path) -> list[dict]:
    p = out / "k9" / k9.REFUSALS
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def incidents(out: Path) -> list[Path]:
    d = out / "k9" / k9.INCIDENTS
    return sorted(d.glob("K9-*.md")) if d.exists() else []


# ------------------------------------------------------------------------------- refusal before any I/O


REFUSED = {
    "cms catalog": Request("https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query",
                           {"catalogId": "161", "pageNo": "1"}),
    "cms detail": Request("https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query",
                          {"articleCode": "x"}),
    "current-state fapi, old endTime": Request(FAPI + "premiumIndex",
                                               {"symbol": "BTCUSDT", "endTime": MS_OK}),
    "fundingRate without endTime": Request(FAPI + "fundingRate", {"symbol": "BTCUSDT", "startTime": "1"}),
    "fundingRate reaching W_HOLD": Request(FAPI + "fundingRate",
                                           {"symbol": "BTCUSDT", "startTime": "1", "endTime": MS_BAD}),
    "N5 without to": Request(CDX, {"url": "binance.com/x"}),
    "N5 to >= 20250701000000": Request(CDX, {"url": "binance.com/x", "to": "20250701000000"}),
    "N6 not an N5 pair": Request(f"https://web.archive.org/web/20250101000000id_/{ORIG}"),
    "N6 not id_": Request(f"https://web.archive.org/web/20250101000000/{ORIG}"),
    "W_HOLD archive file": Request(ZIP_HOLD),
    "M6 archive file": Request(ZIP_M6),
    "M6 archive checksum": Request(ZIP_M6 + ".CHECKSUM"),
    "N2 with paging marker": Request(S3, {"delimiter": "/", "prefix": "data/futures/um/", "marker": "x"}),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refused_class_never_reaches_the_transport_and_is_k9_logged(name, out):
    t = FakeTransport()
    req = REFUSED[name]
    with pytest.raises(EndpointRefused):
        fetcher(t, out).fetch(req)
    assert t.calls == []  # netguard.check ran before any network I/O
    (rec,) = k9_records(out)
    assert rec["request"] == {"url": req.url, "params": {k: str(v) for k, v in req.params.items()}}
    assert rec["rule"].startswith("S-1.7") and rec["reason"] and rec["utc"].endswith("+00:00")
    assert rec["exception"] == "EndpointRefused"
    (inc,) = incidents(out)
    text = inc.read_text(encoding="utf-8")
    assert "K9" in text and req.url in text and rec["sha256"] in text


def test_refusal_logs_append_and_incidents_accumulate(out):
    f = fetcher(FakeTransport(), out)
    for _ in range(2):
        with pytest.raises(EndpointRefused):
            f.fetch(REFUSED["cms catalog"])
    assert len(k9_records(out)) == 2 and len(incidents(out)) == 2


def test_log_and_incident_directories_are_injectable(tmp_path):
    log = k9.K9Log(tmp_path / "logs", tmp_path / "reports")
    f = Fetcher(FakeTransport(), tmp_path / "out", k9_log=log)
    with pytest.raises(EndpointRefused):
        f.fetch(REFUSED["cms detail"])
    assert len(log.records()) == 1 and len(list((tmp_path / "reports").glob("K9-*.md"))) == 1
    assert not (tmp_path / "out" / "k9").exists()


def test_fetcher_requires_a_location_and_a_sane_timeout(tmp_path):
    with pytest.raises(TypeError):
        Fetcher(FakeTransport())  # type: ignore[call-arg]
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            Fetcher(FakeTransport(), tmp_path, timeout=bad)


# ------------------------------------------------------------------------------------ N1 / N3 / N4


def test_n1_is_checksum_verified_and_recorded(out):
    body = b"PK\x03\x04 synthetic zip"
    t = FakeTransport({ZIP_DEV: ok(body), ZIP_DEV + ".CHECKSUM": ok(f"{_sha(body)}  BTCUSDT-15m-2025-06.zip\n"
                                                                         .encode())})
    r = fetcher(t, out).fetch(Request(ZIP_DEV))
    assert r.kind == "N1" and r.body == body and r.sha256 == _sha(body) and r.recv_ts == RECV
    assert r.checksum_sha256 == _sha(body) and r.url == ZIP_DEV
    assert [c[0] for c in t.calls] == [ZIP_DEV, ZIP_DEV + ".CHECKSUM"]
    assert all(c[2] == fetch.DEFAULT_TIMEOUT_S for c in t.calls)
    log = [json.loads(x) for x in (out / fetch.FETCH_LOG).read_text(encoding="utf-8").splitlines()]
    by_url = {e["url"]: e for e in log}
    assert set(by_url) == {ZIP_DEV, ZIP_DEV + ".CHECKSUM"}
    e = by_url[ZIP_DEV]
    assert (e["sha256"], e["recv_ts"], e["kind"]) == (_sha(body), RECV, "N1")
    assert k9_records(out) == []


@pytest.mark.parametrize("checksum", [b"0" * 64 + b"  BTCUSDT-15m-2025-06.zip\n", b"garbage\n",
                                      b"%s  OTHER-2025-06.zip\n"])
def test_n1_checksum_mismatch_is_refused(checksum, out):
    body = b"zip bytes"
    checksum = checksum.replace(b"%s", _sha(body).encode())
    t = FakeTransport({ZIP_DEV: ok(body), ZIP_DEV + ".CHECKSUM": ok(checksum)})
    with pytest.raises(fetch.IntegrityError):
        fetcher(t, out).fetch(Request(ZIP_DEV))


def test_redirect_is_refused_everywhere_and_logged(out):
    t = FakeTransport({ZIP_DEV: Response(302, {"Location": "https://evil.example/x.zip"}, b"", ZIP_DEV),
                       FAPI + "exchangeInfo": Response(200, {}, b"{}", FAPI + "exchangeInfo",
                                                       redirected=True)})
    f = fetcher(t, out)
    for req in (Request(ZIP_DEV), Request(FAPI + "exchangeInfo")):
        with pytest.raises(guard.GuardFailure, match="redirect"):
            f.fetch(req)
    assert [r["rule"] for r in k9_records(out)] == [fetch.RULE_REDIRECT] * 2


def test_non_200_is_an_error_without_content(out):
    t = FakeTransport({FAPI + "exchangeInfo": Response(503, {}, b"down", FAPI + "exchangeInfo")})
    with pytest.raises(fetch.FetchError, match="503"):
        fetcher(t, out).fetch(Request(FAPI + "exchangeInfo"))


def test_n3_and_n4_pass_through(out):
    p = {"symbol": "BTCUSDT", "startTime": "1", "endTime": MS_OK}
    t = FakeTransport({FAPI + "fundingRate": ok(b"[]"), FAPI + "exchangeInfo": ok(b"{}")})
    f = fetcher(t, out)
    assert f.fetch(Request(FAPI + "fundingRate", p)).kind == "N3"
    assert f.fetch(Request(FAPI + "exchangeInfo")).kind == "N4"
    assert t.calls[0][1] == p


# ------------------------------------------------------------------------------------------------- N2


def listing_xml(keys, *, truncated="false", prefix="data/futures/um/monthly/klines/BTCUSDT/15m/",
                prefixes=()) -> bytes:
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    trunc = "" if truncated is None else f"<IsTruncated>{truncated}</IsTruncated>"
    items = "".join(f"<Contents><Key>{k}</Key><LastModified>2026-10-01T00:00:00.000Z</LastModified>"
                    f"<ETag>&quot;{i:032x}&quot;</ETag><Size>{100 + i}</Size>"
                    f"<StorageClass>STANDARD</StorageClass></Contents>" for i, k in enumerate(keys))
    cps = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
    return (f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="{ns}"><Name>data.binance.vision'
            f"</Name><Prefix>{prefix}</Prefix><Marker></Marker><MaxKeys>1000</MaxKeys><Delimiter>/</Delimiter>"
            f"{trunc}{items}{cps}</ListBucketResult>").encode()


N2_REQ = Request(S3, {"delimiter": "/", "prefix": "data/futures/um/monthly/klines/BTCUSDT/15m/"})


def test_n2_listing_drops_m6_keys_in_memory(out):
    d = N2_REQ.params["prefix"]
    keys = [d + "BTCUSDT-15m-2025-06.zip", d + "BTCUSDT-15m-2025-08.zip.CHECKSUM",
            d + "BTCUSDT-15m-2026-07.zip", d + "BTCUSDT-15m-2026-08.zip.CHECKSUM", d + "junk.zip"]
    body = listing_xml(keys, prefixes=(d + "sub/",))
    r = fetcher(FakeTransport({S3: ok(body)}), out).fetch(N2_REQ)
    assert r.kind == "N2" and r.body is None and r.sha256 == _sha(body)
    lst = r.listing
    assert [e.key for e in lst.entries] == keys[:2]  # M6 (and unparseable) keys dropped before returning
    assert lst.entries[1] == fetch.ListingEntry(keys[1], 101, f"{1:032x}")  # W_HOLD: name, size, checksum
    assert set(fetch.ListingEntry.__dataclass_fields__) == {"key", "size", "checksum"}
    assert lst.prefixes == (d + "sub/",) and lst.n_dropped_m6 == 2 and lst.n_dropped_unparseable == 1
    assert "2026-07" not in repr(r) and "2026-08" not in repr(r)
    log = (out / fetch.FETCH_LOG).read_text(encoding="utf-8")
    assert "2026-07" not in log and "2026-08" not in log


@pytest.mark.parametrize("truncated", ["true", None, "TRUE ", "maybe"])
def test_n2_truncated_or_unknown_listing_fails_closed(truncated, out):
    d = N2_REQ.params["prefix"]
    body = listing_xml([d + "BTCUSDT-15m-2025-06.zip"], truncated=truncated)
    with pytest.raises(fetch.ListingTruncated):
        fetcher(FakeTransport({S3: ok(body)}), out).fetch(N2_REQ)
    (rec,) = k9_records(out)
    assert rec["rule"] == fetch.RULE_TRUNCATED


@pytest.mark.parametrize("body", [b"<html>not a listing</html>", b"not xml at all",
                                  listing_xml([], prefix="data/futures/um/daily/")])
def test_n2_unparseable_or_foreign_listing_is_refused(body, out):
    with pytest.raises(fetch.FetchError):
        fetcher(FakeTransport({S3: ok(body)}), out).fetch(N2_REQ)


# ------------------------------------------------------------------------------------------- N5 / N6


def cdx_req(to="20250630235959", **extra) -> Request:
    return Request(CDX, {"url": "binance.com/en/support/announcement/*", "fl": "timestamp,original", "to": to,
                         **extra})


def wayback(ts: str, original: str = ORIG) -> str:
    return f"https://web.archive.org/web/{ts}id_/{original}"


MEMENTO = {"Memento-Datetime": "Wed, 01 Jan 2025 00:00:00 GMT"}


def test_n5_registers_pairs_then_n6_fetches_exactly_them(out):
    cdx = f"20250101000000 {ORIG}\n20250102000000 {ORIG}2\n".encode()
    page = b"<html>archived</html>"
    t = FakeTransport({CDX: ok(cdx), wayback("20250101000000"): ok(page, headers=MEMENTO)})
    f = fetcher(t, out)
    r5 = f.fetch(cdx_req())
    assert r5.kind == "N5" and r5.cdx_pairs == (("20250101000000", ORIG), ("20250102000000", ORIG + "2"))
    r6 = f.fetch(Request(wayback("20250101000000")))
    assert r6.kind == "N6" and r6.body == page and r6.sha256 == _sha(page) and r6.recv_ts == RECV
    with pytest.raises(EndpointRefused):
        f.fetch(Request(wayback("20250101000001")))  # not a returned pair


def test_n5_default_field_order_is_parsed(out):
    cdx = f"com,binance)/x 20250101000000 {ORIG} text/html 200 ABC 123\n".encode()
    r = fetcher(FakeTransport({CDX: ok(cdx)}), out).fetch(
        Request(CDX, {"url": "binance.com/x", "to": "20250630235959"}))
    assert r.cdx_pairs == (("20250101000000", ORIG),)


def test_n5_row_beyond_the_bound_is_a_logged_guard_failure(out):
    cdx = f"20250101000000 {ORIG}\n20250701000000 {ORIG}\n".encode()
    with pytest.raises(guard.GuardFailure):
        fetcher(FakeTransport({CDX: ok(cdx)}), out).fetch(cdx_req())
    assert not netguard.cdx_pair_registry.contains("20250101000000", ORIG)  # nothing from the batch kept
    assert [r["rule"] for r in k9_records(out)] == [fetch.RULE_N5_BOUND]


def test_n5_unparseable_rows_register_nothing(out):
    with pytest.raises(fetch.FetchError):
        fetcher(FakeTransport({CDX: ok(f"20250101000000 {ORIG}\nonly-one-field\n".encode())}), out
                ).fetch(cdx_req())
    assert not netguard.cdx_pair_registry.contains("20250101000000", ORIG)


@pytest.mark.parametrize("resp", [
    Response(302, {"Location": "https://web.archive.org/web/20250105000000id_/x", **MEMENTO}, b"", ""),
    Response(200, {"Memento-Datetime": "Thu, 02 Jan 2025 00:00:00 GMT"}, b"substituted", ""),
    Response(200, {}, b"no memento", ""),
    Response(200, MEMENTO, b"followed", "", redirected=True),
    Response(404, MEMENTO, b"", ""),
])
def test_n6_redirect_or_timestamp_substitution_discards_content(resp, out):
    netguard.cdx_pair_registry.register_cdx_result([("20250101000000", ORIG)])
    with pytest.raises(guard.GuardFailure):
        f = fetcher(FakeTransport({wayback("20250101000000"): resp}), out)
        f.fetch(Request(wayback("20250101000000")))
    (rec,) = k9_records(out)
    assert rec["rule"] in (fetch.RULE_N6, fetch.RULE_REDIRECT)
    assert rec["response_sha256"] == _sha(resp.body)  # hashed for the incident, never parsed or returned


# --------------------------------------------------------------------------------- default transport


def test_default_transport_never_follows_redirects():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("a.zip"):
            return httpx.Response(302, headers={"Location": "https://data.binance.vision/b.zip"})
        return httpx.Response(200, content=b"followed")

    tr = fetch.HttpxTransport(_mock=httpx.MockTransport(handler))
    r = tr.get("https://data.binance.vision/a.zip", {}, timeout=5.0)
    assert r.status == 302 and seen == ["https://data.binance.vision/a.zip"] and r.body == b""
    r2 = tr.get("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol": "B", "endTime": "1"}, timeout=5.0)
    assert r2.status == 200 and seen[-1].endswith("fundingRate?symbol=B&endTime=1")


# ---------------------------------------------------------------------------- post-opening (E6) mode


@pytest.fixture(scope="module")
def opened(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    try:
        yield af.authorize(tmp_path_factory.mktemp("opening"), mp)
    finally:
        mp.undo()


E6_TS = "20260115000000"
E6_MEMENTO = {"Memento-Datetime": "Thu, 15 Jan 2026 00:00:00 GMT"}


def test_e6_mode_needs_the_verified_authorization(opened, out):
    body = b"W_HOLD zip"
    t = FakeTransport({ZIP_HOLD: ok(body),
                       ZIP_HOLD + ".CHECKSUM": ok(f"{_sha(body)}  BTCUSDT-15m-2025-08.zip".encode()),
                       CDX: ok(f"{E6_TS} {ORIG}\n".encode()),
                       wayback(E6_TS): ok(b"capture", headers=E6_MEMENTO)})
    f = fetcher(t, out)
    with pytest.raises(EndpointRefused):  # without the authorization: pre-opening bounds
        f.fetch(Request(ZIP_HOLD))
    assert f.fetch(Request(ZIP_HOLD), auth=opened.auth).body == body  # W_HOLD N1 content after the opening
    for bad in (Request(ZIP_M6), Request(ZIP_M6 + ".CHECKSUM"), REFUSED["cms catalog"], REFUSED["cms detail"],
                REFUSED["fundingRate reaching W_HOLD"], cdx_req(to="20260701000000")):
        with pytest.raises(EndpointRefused):  # M6 stays refused; the research CMS stays blocked
            f.fetch(bad, auth=opened.auth)
    with pytest.raises(EndpointRefused):
        f.fetch(cdx_req(to="20260630235959"))  # the raised bound needs the authorization
    r5 = f.fetch(cdx_req(to="20260630235959"), auth=opened.auth)
    assert r5.cdx_pairs == ((E6_TS, ORIG),)
    with pytest.raises(EndpointRefused):  # an E6 pair is fetchable only in the E6 mode
        f.fetch(Request(wayback(E6_TS)))
    assert f.fetch(Request(wayback(E6_TS)), auth=opened.auth).body == b"capture"
    n = len(t.calls)
    for forged in (object(), object.__new__(guard.HoldoutAuthorization)):
        with pytest.raises(guard.HoldoutBlocked):
            f.fetch(Request(ZIP_HOLD), auth=forged)
    assert len(t.calls) == n  # a forged authorization never reaches the transport
    assert k9_records(out)[-1]["exception"] == "HoldoutBlocked"


def test_pre_opening_cdx_rows_in_w_hold_fail_without_authorization(opened, out):
    cdx = f"{E6_TS} {ORIG}\n".encode()
    with pytest.raises(guard.GuardFailure):
        fetcher(FakeTransport({CDX: ok(cdx)}), out).fetch(cdx_req())


def test_e6_capture_is_pinned_and_refetch_must_be_byte_identical(opened, out):
    ts = "20260201000000"
    netguard.cdx_pair_registry.register_cdx_result([(ts, ORIG + "/pin")], auth=opened.auth)
    url = wayback(ts, ORIG + "/pin")
    mem = {"Memento-Datetime": "Sun, 01 Feb 2026 00:00:00 GMT"}
    body = {"v": b"first capture"}
    t = FakeTransport({url: lambda u, p: ok(body["v"], headers=mem)})
    f = fetcher(t, out)
    f.fetch(Request(url), auth=opened.auth)
    caps = [c for c in guard.e6_captures() if c["original"] == ORIG + "/pin"]
    assert len(caps) == 1 and caps[0]["sha256"] == _sha(b"first capture") and caps[0]["ts"] == ts
    assert caps[0]["opening_id"] == opened.auth.opening_id
    f.fetch(Request(url), auth=opened.auth)  # byte-identical: allowed, nothing new pinned
    assert len([c for c in guard.e6_captures() if c["original"] == ORIG + "/pin"]) == 1
    body["v"] = b"a different capture"
    with pytest.raises(guard.GuardFailure, match="E6"):
        f.fetch(Request(url), auth=opened.auth)
    assert k9_records(out)[-1]["rule"] == fetch.RULE_E6_CAPTURE


def test_after_the_verdict_nothing_opens(tmp_path, monkeypatch, out):
    o = af.authorize(tmp_path / "o", monkeypatch)
    t = FakeTransport({ZIP_HOLD: ok(b"x")})
    guard.mark_verdict_produced(o.auth, "d" * 64)
    with pytest.raises(guard.HoldoutBlocked):
        fetcher(t, out).fetch(Request(ZIP_HOLD), auth=o.auth)
    assert t.calls == []
    with pytest.raises(guard.HoldoutBlocked):
        netguard.cdx_pair_registry.register_cdx_result([(E6_TS, ORIG)], auth=o.auth)


def test_netguard_e6_bounds_follow_the_frozen_windows():
    assert netguard.bounds(None) == ("20250630235959", "20250701000000", windows.W_HOLD[0])
    assert netguard.post_opening_bounds() == ("20260630235959", "20260701000000", windows.W_HOLD[1])
