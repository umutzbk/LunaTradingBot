"""Task 25 (SC-03, DK-01): the S-1.7 fetch layer. ``netguard.check`` runs before the transport is touched;
every refusal is a K9 record (UTC time, request, rule, reason) plus an incident-report stub; N1 is
checksum-verified; N2 listings fail closed on truncation and drop M6 keys; N6 needs an exact registered N5
pair, HTTP 200, no redirect and ``Memento-Datetime`` == ts; the post-opening E6 mode needs the hardened
authorization.

No real network: a fake ``Transport`` (and ``httpx.MockTransport`` for the default httpx transport)."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from luna.v7 import fetch, guard, k9, netguard, windows
from luna.v7.fetch import Fetcher, Response
from luna.v7.netguard import EndpointRefused, Request
from luna.v7.timeutil import to_ns
from tests.v7 import auth_fixtures as af
from tests.v7.net_fixtures import CDX, FakeTransport, cdx_req, ok, pairs_via_n5

FAPI = "https://fapi.binance.com/fapi/v1/"
BVIS = "https://data.binance.vision/data/futures/um"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ORIG = "https://www.binance.com/en/support/announcement/x"
MS_OK = str(to_ns("2025-06-30T23:59:59") // 10**6)
MS_BAD = str(to_ns("2025-07-01") // 10**6)
ZIP_DEV = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip"
ZIP_HOLD = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-08.zip"
ZIP_M6 = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-07.zip"
RECV = to_ns("2026-10-15T12:00:00")
MS_HOLD = str(to_ns("2026-06-30T23:59:59") // 10**6)  # R46: inside W_HOLD
MS_M6 = str(to_ns("2026-07-01") // 10**6)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def out(tmp_path) -> Path:
    return tmp_path / "run_out"


def fetcher(transport, out: Path, **kw) -> Fetcher:
    return Fetcher(transport, out, clock=lambda: RECV, **kw)


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
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            Fetcher(FakeTransport(), tmp_path, max_bytes=bad)  # type: ignore[arg-type]


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


class _Unreadable(bytes):
    """A body whose reading would fail the test: a 3xx must be refused before the body is touched."""

    def __bytes__(self):
        raise AssertionError("the body of a redirect was read")

    def __len__(self):
        raise AssertionError("the body of a redirect was read")


def test_redirect_is_refused_everywhere_before_the_body_is_read(out):
    t = FakeTransport({ZIP_DEV: Response(302, {"Location": "https://evil.example/x.zip"}, _Unreadable(b"x"),
                                         ZIP_DEV),
                       FAPI + "exchangeInfo": Response(307, {}, _Unreadable(b"{}"), FAPI + "exchangeInfo"),
                       FAPI + "fundingRate": Response(200, {}, _Unreadable(b"[]"), "", redirected=True)})
    f = fetcher(t, out)
    for req in (Request(ZIP_DEV), Request(FAPI + "exchangeInfo"),
                Request(FAPI + "fundingRate", {"symbol": "B", "startTime": "1", "endTime": MS_OK})):
        with pytest.raises(guard.GuardFailure, match="redirect"):
            f.fetch(req)
    recs = k9_records(out)
    assert [r["rule"] for r in recs] == [fetch.RULE_REDIRECT] * 3
    assert all("response_sha256" not in r for r in recs)  # never read, never hashed


def test_response_over_the_size_cap_is_refused(out):
    t = FakeTransport({FAPI + "exchangeInfo": ok(b"x" * 11)})
    with pytest.raises(fetch.ResponseTooLarge):
        fetcher(t, out, max_bytes=10).fetch(Request(FAPI + "exchangeInfo"))
    assert not (out / fetch.FETCH_LOG).exists()
    assert fetch.MAX_RESPONSE_BYTES == 1 << 30


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
    assert t.calls[0][2] == fetch.DEFAULT_TIMEOUT_S


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
    other = fetcher(FakeTransport(), out)  # another fetcher (run) has its own, empty pairs
    with pytest.raises(EndpointRefused):
        other.fetch(Request(wayback("20250101000000")))


def test_n5_default_field_order_is_parsed(out):
    cdx = f"com,binance)/x 20250101000000 {ORIG} text/html 200 ABC 123\n".encode()
    r = fetcher(FakeTransport({CDX: ok(cdx)}), out).fetch(
        Request(CDX, {"url": "binance.com/x", "to": "20250630235959"}))
    assert r.cdx_pairs == (("20250101000000", ORIG),)


def test_n5_row_beyond_the_bound_is_a_logged_guard_failure(out):
    cdx = f"20250101000000 {ORIG}\n20250701000000 {ORIG}\n".encode()
    f = fetcher(FakeTransport({CDX: ok(cdx)}), out)
    with pytest.raises(guard.GuardFailure):
        f.fetch(cdx_req())
    assert len(f.cdx_pairs) == 0  # nothing from the batch kept
    assert [r["rule"] for r in k9_records(out)] == [fetch.RULE_N5_BOUND]


def test_n5_unparseable_rows_register_nothing(out):
    f = fetcher(FakeTransport({CDX: ok(f"20250101000000 {ORIG}\nonly-one-field\n".encode())}), out)
    with pytest.raises(fetch.FetchError):
        f.fetch(cdx_req())
    assert len(f.cdx_pairs) == 0


@pytest.mark.parametrize("resp,rule", [
    (Response(302, {"Location": "https://web.archive.org/web/20250105000000id_/x", **MEMENTO}, b"", ""),
     fetch.RULE_REDIRECT),
    (Response(200, MEMENTO, b"followed", "", redirected=True), fetch.RULE_REDIRECT),
    (Response(200, {"Memento-Datetime": "Thu, 02 Jan 2025 00:00:00 GMT"}, b"substituted", ""), fetch.RULE_N6),
    (Response(200, {}, b"no memento", ""), fetch.RULE_N6),
    (Response(404, MEMENTO, b"", ""), fetch.RULE_N6),
])
def test_n6_redirect_or_timestamp_substitution_discards_content(resp, rule, out):
    f = fetcher(FakeTransport(), out)
    pairs_via_n5(f, [("20250101000000", ORIG)])
    f.transport.routes[wayback("20250101000000")] = resp
    with pytest.raises(guard.GuardFailure):
        f.fetch(Request(wayback("20250101000000")))
    (rec,) = k9_records(out)
    assert rec["rule"] == rule
    if rule == fetch.RULE_N6:  # hashed for the incident, never parsed or returned
        assert rec["response_sha256"] == _sha(resp.body)
    else:  # a redirect is refused before its body is read
        assert "response_sha256" not in rec


# --------------------------------------------------------------------------------- default transport


def test_default_transport_never_follows_redirects():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("a.zip"):
            return httpx.Response(302, headers={"Location": "https://data.binance.vision/b.zip"})
        return httpx.Response(200, content=b"followed")

    tr = fetch.HttpxTransport(_mock=httpx.MockTransport(handler))
    r = tr.get("https://data.binance.vision/a.zip", {}, timeout=5.0, max_bytes=100)
    assert r.status == 302 and seen == ["https://data.binance.vision/a.zip"] and r.body == b""
    r2 = tr.get("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol": "B", "endTime": "1"}, timeout=5.0,
                max_bytes=100)
    assert r2.status == 200 and seen[-1].endswith("fundingRate?symbol=B&endTime=1") and r2.body == b"followed"
    with pytest.raises(fetch.ResponseTooLarge):  # streamed body over the cap
        tr.get("https://fapi.binance.com/fapi/v1/exchangeInfo", {}, timeout=5.0, max_bytes=4)


def test_default_transport_reuses_one_pooled_client_across_calls_and_threads(tmp_path):
    """Task 26d (C6 phase A): one keep-alive ``httpx.Client`` per transport, shared by worker threads, closed
    with the fetcher; the redirect / timeout / cap behaviour is unchanged (tests above)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("i", ""))
        return httpx.Response(200, content=b"ok")

    tr = fetch.HttpxTransport(_mock=httpx.MockTransport(handler))
    assert tr._client is None  # created lazily on the first GET
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    tr.get(url, {"i": "0"}, timeout=5.0, max_bytes=100)
    first = tr._client
    assert isinstance(first, httpx.Client) and not first.is_closed and first.follow_redirects is False
    tr.get(url, {"i": "1"}, timeout=3.0, max_bytes=100)
    assert tr._client is first
    with ThreadPoolExecutor(8) as ex:
        bodies = list(ex.map(lambda i: tr.get(url, {"i": str(i)}, timeout=5.0, max_bytes=100).body,
                             range(2, 34)))
    assert bodies == [b"ok"] * 32 and tr._client is first
    assert sorted(seen, key=int) == [str(i) for i in range(34)]
    f = Fetcher(tr, tmp_path)
    with f:
        assert f.fetch(Request(url)).body == b"ok" and tr._client is first  # through netguard, same client
    assert first.is_closed and tr._client is None
    with pytest.raises(RuntimeError, match="closed"):
        tr.get(url, {}, timeout=5.0, max_bytes=100)
    Fetcher(FakeTransport({}), tmp_path).close()  # a transport without close(): nothing to close


def test_default_transport_refuses_a_declared_oversized_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"0123456789")  # content-length 10

    tr = fetch.HttpxTransport(_mock=httpx.MockTransport(handler))
    with pytest.raises(fetch.ResponseTooLarge, match="declared"):
        tr.get("https://fapi.binance.com/fapi/v1/exchangeInfo", {}, timeout=5.0, max_bytes=9)


# ---------------------------------------------------------------------------- post-opening (E6) mode


@pytest.fixture(scope="module")
def _opening(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("opening"))


@pytest.fixture
def opened(_opening, monkeypatch):
    """The module's opening, with ``guard._roots`` pointed at it for THIS test only (review m-7)."""
    return af.use_opening(monkeypatch, _opening)


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
                Request(FAPI + "fundingRate", {"symbol": "BTCUSDT", "startTime": "1", "endTime": MS_M6}),
                cdx_req(to="20260701000000")):
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


def test_n3_post_opening_bound_r46(opened, out):
    """Controller ruling R46: with the verified authorization N3 endTime < 2026-07-01 (W_HOLD funding, the
    S-2.6 REST fill); >= 2026-07-01 refused; without it the pre-opening rule; forged refused."""
    url = FAPI + "fundingRate"
    t = FakeTransport({url: ok(b"[]")})
    f = fetcher(t, out)
    for end in (MS_BAD, MS_HOLD):
        req = Request(url, {"symbol": "BTCUSDT", "startTime": "1", "endTime": end})
        with pytest.raises(EndpointRefused):
            f.fetch(req)  # without the authorization: endTime < 2025-07-01 only
        assert f.fetch(req, auth=opened.auth).kind == "N3"
    with pytest.raises(EndpointRefused):
        f.fetch(Request(url, {"symbol": "BTCUSDT", "startTime": "1", "endTime": MS_M6}), auth=opened.auth)
    n = len(t.calls)
    for forged in (object(), object.__new__(guard.HoldoutAuthorization)):
        with pytest.raises(guard.HoldoutBlocked):
            f.fetch(Request(url, {"symbol": "BTCUSDT", "startTime": "1", "endTime": MS_HOLD}), auth=forged)
    assert len(t.calls) == n


def test_pre_opening_cdx_rows_in_w_hold_fail_without_authorization(opened, out):
    cdx = f"{E6_TS} {ORIG}\n".encode()
    with pytest.raises(guard.GuardFailure):
        fetcher(FakeTransport({CDX: ok(cdx)}), out).fetch(cdx_req())


def test_e6_capture_is_pinned_and_refetch_must_be_byte_identical(opened, out):
    ts = "20260201000000"
    url = wayback(ts, ORIG + "/pin")
    mem = {"Memento-Datetime": "Sun, 01 Feb 2026 00:00:00 GMT"}
    body = {"v": b"first capture"}
    t = FakeTransport({url: lambda u, p: ok(body["v"], headers=mem)})
    f = fetcher(t, out)
    pairs_via_n5(f, [(ts, ORIG + "/pin")], to="20260630235959", auth=opened.auth)
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


def test_corrupt_e6_pin_file_is_a_logged_guard_failure(tmp_path, monkeypatch, out):
    o = af.authorize(tmp_path / "o", monkeypatch)
    ts = "20260301000000"
    url = wayback(ts, ORIG + "/c")
    t = FakeTransport({url: ok(b"cap", headers={"Memento-Datetime": "Sun, 01 Mar 2026 00:00:00 GMT"})})
    f = fetcher(t, out)
    pairs_via_n5(f, [(ts, ORIG + "/c")], to="20260630235959", auth=o.auth)
    pin_file = guard.marker_paths()[0].parent / guard.E6_CAPTURES
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    pin_file.write_text('{"ts": "x"\n', encoding="utf-8")  # a torn / corrupt line
    with pytest.raises(guard.GuardFailure, match="corrupt"):
        f.fetch(Request(url), auth=o.auth)
    assert k9_records(out)[-1]["rule"] == fetch.RULE_E6_CAPTURE


def test_after_the_verdict_nothing_opens(tmp_path, monkeypatch, out):
    o = af.authorize(tmp_path / "o", monkeypatch)
    t = FakeTransport({ZIP_HOLD: ok(b"x"), CDX: ok(f"{E6_TS} {ORIG}\n".encode())})
    f = fetcher(t, out)
    guard.mark_verdict_produced(o.auth, "d" * 64)
    for req in (Request(ZIP_HOLD), cdx_req(to="20260630235959"),
                Request(FAPI + "fundingRate", {"symbol": "BTCUSDT", "startTime": "1", "endTime": MS_HOLD})):
        with pytest.raises(guard.HoldoutBlocked):
            f.fetch(req, auth=o.auth)
    assert t.calls == [] and len(f.cdx_pairs) == 0


def test_netguard_e6_bounds_follow_the_frozen_windows():
    pre, post = netguard.bounds(None), netguard.post_opening_bounds()
    assert (pre.cdx_to_max, pre.n6_first_refused, pre.n1_end_ns, pre.n3_end_ms) == (
        "20250630235959", "20250701000000", windows.W_HOLD[0], windows.W_HOLD[0] // 10**6)
    assert (post.cdx_to_max, post.n6_first_refused, post.n1_end_ns, post.n3_end_ms) == (
        "20260630235959", "20260701000000", windows.W_HOLD[1], windows.W_HOLD[1] // 10**6)


# ----------------------------------------------------------------------------------- one network path


_NET_MODULES = ("httpx", "urllib.request", "urllib3", "requests", "socket", "http.client", "aiohttp", "ssl")


def test_no_other_v7_module_imports_a_network_library():
    """Review m-8: ``luna.v7.fetch`` is the only V7 module importing a network library (static check)."""
    import ast

    root = Path(fetch.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path == Path(fetch.__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                if any(n == m or n.startswith(m + ".") for m in _NET_MODULES):
                    offenders.append(f"{path.relative_to(root)}: {n}")
    assert offenders == []
    assert "httpx" in Path(fetch.__file__).read_text(encoding="utf-8")


_TRANSPORT_NAMES = ("HttpxTransport", "Transport")


def _transport_uses(tree) -> list[str]:
    """Static uses of a transport outside the fetch layer: importing / naming ``HttpxTransport`` or
    ``Transport`` (a star import from the fetch module included), reading a ``.transport`` attribute (also
    through ``getattr`` / ``setattr`` / ``hasattr`` with a string), or passing ``transport=`` to a call."""
    import ast

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out += [f"import {a.name}" for a in node.names if a.name in _TRANSPORT_NAMES]
            if (node.module or "").endswith("fetch") and any(a.name == "*" for a in node.names):
                out.append(f"from {node.module} import *")
        elif isinstance(node, ast.Name) and node.id in _TRANSPORT_NAMES:
            out.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and (node.attr in _TRANSPORT_NAMES or node.attr == "transport"):
            out.append(f"attribute .{node.attr}")
        elif isinstance(node, ast.Call):
            out += [f"{k.arg}= in a call" for k in node.keywords if k.arg == "transport"]
            if isinstance(node.func, ast.Name) and node.func.id in ("getattr", "setattr", "hasattr") and \
                    any(isinstance(a, ast.Constant) and a.value in (*_TRANSPORT_NAMES, "transport")
                        for a in node.args[1:2]):
                out.append(f"{node.func.id}(..., {node.args[1].value!r})")  # type: ignore[attr-defined]
    return out


def test_no_other_v7_module_constructs_or_uses_a_transport():
    """Task 24 / 25 carry-over n-3: only ``luna.v7.fetch`` constructs or uses ``HttpxTransport`` / a
    ``Transport``; every other module (ingestion included) goes through ``fetch.Fetcher`` (static check)."""
    import ast

    root = Path(fetch.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path == Path(fetch.__file__):
            continue
        for use in _transport_uses(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(root)}: {use}")
    assert offenders == []
    # the scan detects each form (mutation check on a synthetic module)
    probe = ast.parse("from luna.v7.fetch import HttpxTransport\nt = fetch.Transport\nf.transport.get('u')\n"
                      "fetch.fetch(r, transport=x, out_dir='o')\nHttpxTransport()\n"
                      "getattr(f, 'transport').get('u')\ngetattr(fetch, 'HttpxTransport')()\n"
                      "from luna.v7.fetch import *\n")
    assert len(_transport_uses(probe)) == 8
    assert (root / "ingest.py").exists() and _transport_uses(ast.parse((root / "ingest.py").read_text(
        encoding="utf-8"))) == []


def test_default_fetcher_owns_the_real_transport(tmp_path):
    """Drivers get the production fetcher from the fetch layer (no transport construction elsewhere)."""
    f = Fetcher.default(tmp_path, timeout=5.0)
    assert isinstance(f.transport, fetch.HttpxTransport) and f.timeout == 5.0 and f.out_dir == tmp_path
