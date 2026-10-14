"""Task 2 endpoint-allowlist tests: one refusal test per class of S-1.7 'Guard tests' (L10, DEC-41/46)."""

from __future__ import annotations

import pytest

from luna.v7 import guard, netguard
from luna.v7.netguard import EndpointRefused, Request, check
from luna.v7.timeutil import to_ns

FAPI = "https://fapi.binance.com/fapi/v1/"
BVIS = "https://data.binance.vision/data/futures/um"
MS_OK = str(to_ns("2025-06-30T23:59:59") // 10**6)
MS_BAD = str(to_ns("2025-07-01") // 10**6)


def test_refuses_cms_catalog_and_detail():
    for u in ("https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query",
              "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query",
              "https://www.binance.com/en/support/announcement"):
        with pytest.raises(EndpointRefused):
            check(Request(u, {"catalogId": "161", "pageNo": "1"}))


def test_refuses_current_state_fapi_with_old_endtime():
    for p in ("premiumIndex", "ticker/24hr", "openInterest", "fundingInfo", "klines"):
        with pytest.raises(EndpointRefused):
            check(Request(FAPI + p, {"symbol": "BTCUSDT", "endTime": MS_OK}))


def test_fundingrate_requires_bounded_endtime():
    base = {"symbol": "BTCUSDT", "startTime": "1700000000000"}
    with pytest.raises(EndpointRefused):
        check(Request(FAPI + "fundingRate", base))
    with pytest.raises(EndpointRefused):
        check(Request(FAPI + "fundingRate", {**base, "endTime": MS_BAD}))
    with pytest.raises(EndpointRefused):
        check(Request(FAPI + "fundingRate", {**base, "endTime": "abc"}))
    with pytest.raises(EndpointRefused):  # startTime missing
        check(Request(FAPI + "fundingRate", {"symbol": "BTCUSDT", "endTime": MS_OK}))
    with pytest.raises(EndpointRefused):  # unknown parameter
        check(Request(FAPI + "fundingRate", {**base, "endTime": MS_OK, "foo": "1"}))
    assert check(Request(FAPI + "fundingRate", {**base, "endTime": MS_OK, "limit": 1000})).kind == "N3"
    assert check(Request(FAPI + "fundingRate", {**base, "endTime": int(MS_OK)})).kind == "N3"


def test_exchangeinfo_n4_no_params():
    assert check(Request(FAPI + "exchangeInfo", {})).kind == "N4"
    with pytest.raises(EndpointRefused):
        check(Request(FAPI + "exchangeInfo", {"symbol": "BTCUSDT"}))


def test_cdx_requires_to_bound():
    u = "https://web.archive.org/cdx/search/cdx"
    p = {"url": "binance.com/x", "matchType": "prefix", "fl": "timestamp,original"}
    with pytest.raises(EndpointRefused):
        check(Request(u, p))
    with pytest.raises(EndpointRefused):
        check(Request(u, {**p, "to": "20250701000000"}))
    with pytest.raises(EndpointRefused):
        check(Request(u, {**p, "to": "2025"}))  # truncated timestamps widen the bound
    with pytest.raises(EndpointRefused):
        check(Request(u, {**p, "to": "20250630235959", "output": "json"}))
    assert check(Request(u, {**p, "to": "20250630235959"})).kind == "N5"


def test_wayback_only_registered_pairs():
    orig = "https://www.binance.com/en/support/announcement/x"
    url = f"https://web.archive.org/web/20250101000000id_/{orig}"
    reg = netguard.cdx_pair_registry
    reg.clear()
    with pytest.raises(EndpointRefused):
        check(Request(url, {}))
    reg.register_cdx_result([{"timestamp": "20250101000000", "original": orig}])
    assert check(Request(url, {})).kind == "N6"
    with pytest.raises(EndpointRefused):  # not id_
        check(Request(f"https://web.archive.org/web/20250101000000/{orig}", {}))
    with pytest.raises(EndpointRefused):  # other ts
        check(Request(f"https://web.archive.org/web/20250101000001id_/{orig}", {}))
    with pytest.raises(EndpointRefused):  # other original
        check(Request(f"https://web.archive.org/web/20250101000000id_/{orig}2", {}))
    with pytest.raises(guard.GuardFailure):  # a CDX row beyond the bound is itself a failure
        reg.register_cdx_result([("20250701000000", orig)])
    reg.clear()


def test_wayback_redirect_or_memento_mismatch_is_guard_failure():
    ts = "20250101000000"
    ok = {"Memento-Datetime": "Wed, 01 Jan 2025 00:00:00 GMT"}
    netguard.check_wayback_response(ts, 200, ok, False)
    netguard.check_wayback_response(ts, 200, {"memento-datetime": ok["Memento-Datetime"]}, False)
    for args in ((ts, 302, ok, True), (ts, 200, ok, True), (ts, 301, ok, False), (ts, 200, {}, False),
                 (ts, 200, {"Memento-Datetime": "Thu, 02 Jan 2025 00:00:00 GMT"}, False),
                 (ts, 200, {"Memento-Datetime": "garbage"}, False)):
        with pytest.raises(guard.GuardFailure):
            netguard.check_wayback_response(*args)


def test_archive_file_whole_period_before_2025_07():
    ok = f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip"
    assert check(Request(ok, {})).kind == "N1"
    assert check(Request(ok + ".CHECKSUM", {})).kind == "N1"
    assert check(Request(f"{BVIS}/daily/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06-30.zip", {})).kind == "N1"
    fr = f"{BVIS}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-06.zip"
    assert check(Request(fr, {})).kind == "N1"
    for bad in (f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-07.zip",
                f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-07.zip.CHECKSUM",
                f"{BVIS}/daily/klines/BTCUSDT/15m/BTCUSDT-15m-2025-07-01.zip",
                f"{BVIS}/monthly/klines/BTCUSDT/15m/..%2F..%2FBTCUSDT-15m-2025-06.zip",
                f"{BVIS}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025.zip",
                "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip",
                "http://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip"):
        with pytest.raises(EndpointRefused):
            check(Request(bad, {}))
    with pytest.raises(EndpointRefused):
        check(Request(ok, {"x": "1"}))


def test_n2_listing_params():
    u = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    p = {"delimiter": "/", "prefix": "data/futures/um/monthly/klines/BTCUSDT/15m/"}
    assert check(Request(u, p)).kind == "N2"
    with pytest.raises(EndpointRefused):
        check(Request(u, {"prefix": p["prefix"]}))
    with pytest.raises(EndpointRefused):
        check(Request(u, {**p, "prefix": "data/spot/"}))
    with pytest.raises(EndpointRefused):
        check(Request(u, {**p, "marker": "x"}))


def test_n2_drops_m6_keys():
    d = "data/futures/um/monthly/klines/BTCUSDT/15m/"
    keys = [d + "BTCUSDT-15m-2025-06.zip", d + "BTCUSDT-15m-2025-08.zip", d + "BTCUSDT-15m-2026-06.zip",
            d + "BTCUSDT-15m-2026-07.zip", d + "BTCUSDT-15m-2026-07.zip.CHECKSUM",
            d + "BTCUSDT-15m-2026-09.zip", "data/futures/um/monthly/klines/BTCUSDT/"]
    out = netguard.drop_m6_keys(keys)
    assert out == [keys[0], keys[1], keys[2], keys[6]]
    assert keys[1] in out  # W_HOLD key kept (name only)
    # a daily file the day before M6 stays, the first M6 day goes
    assert netguard.drop_m6_keys(["X-2026-06-30.zip", "X-2026-07-01.zip"]) == ["X-2026-06-30.zip"]


def test_exchangeinfo_status_fields_refused_as_history():
    for f in ("status", "onboardDate", "deliveryDate", "filters", "maxQty"):
        with pytest.raises(guard.GuardFailure):
            guard.historical_field(f)


def test_refuses_other_hosts_and_shapes():
    for u in ("https://evil.example/fapi/v1/exchangeInfo", "https://fapi.binance.com:8443/fapi/v1/exchangeInfo",
              "https://user@fapi.binance.com/fapi/v1/exchangeInfo", "ftp://fapi.binance.com/fapi/v1/exchangeInfo",
              "https://fapi.binance.com/fapi/v1/exchangeInfo#x", "", "not a url"):
        with pytest.raises(EndpointRefused):
            check(Request(u, {}))


def test_query_in_url_is_checked_like_params():
    with pytest.raises(EndpointRefused):
        check(Request(FAPI + "fundingRate?symbol=BTCUSDT&startTime=1&endTime=" + MS_BAD, {}))
    assert check(Request(FAPI + f"fundingRate?symbol=BTCUSDT&startTime=1&endTime={MS_OK}", {})).kind == "N3"
    with pytest.raises(EndpointRefused):  # same key twice
        both = {"symbol": "B", "startTime": 1, "endTime": MS_OK}
        check(Request(FAPI + f"fundingRate?endTime={MS_BAD}", both))
