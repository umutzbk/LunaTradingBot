"""Task 26a (SC-01 part 1): guarded ingestion through the one S-1.7 fetch layer (fake transport, tmp_path).
N1 zips verified against their .CHECKSUM, parsed with the existing parsers (header auto-detection, ms / us ->
ns), DEC-33 monthly union daily, written once to the raw store; resumable, idempotent; N2 truncation and
W_HOLD refused (K9-logged in the ingest run's own output directory)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from luna.v7 import fetch, guard, ingest, k9, rawstore
from luna.v7.data import funding as fund_mod
from luna.v7.data import klines
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.fetch import Fetcher, Response
from luna.v7.netguard import EndpointRefused
from luna.v7.rawstore import FundingLoader, RawStore, TableLoader
from luna.v7.timeutil import MIN, D, to_ns
from tests.v7.net_fixtures import FakeTransport, ok

JAN = to_ns("2024-01-01")
FEB = to_ns("2024-02-01")
RECV = to_ns("2026-10-15")
DAYS = ("2024-01-30", "2024-01-31")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def kline_rows(lo: int, n: int, step: int, unit: str = "ms", seed: int = 0, bump: float = 0.0) -> list[list]:
    g = np.random.default_rng(seed)
    div = {"ms": 10**6, "us": 10**3}[unit]
    out = []
    for i in range(n):
        t = lo + i * step
        c = round(100 + float(g.normal(0, 1)) + bump, 2)
        out.append([t // div, c, c + 0.5, c - 0.5, c, 10.0 + i, (t + step - 10**6) // div, (10.0 + i) * c,
                    50 + i % 7, 5.0, 5.0 * c, 0])
    return out


def zip_of(name: str, rows: list[list], header: bool) -> bytes:
    lines = (["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
              "taker_buy_quote_volume,ignore"] if header else [])
    lines += [",".join(str(x) for x in r) for r in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name.replace(".zip", ".csv"), "\n".join(lines) + "\n")
    return buf.getvalue()


def serve(t: FakeTransport, url: str, body: bytes) -> None:
    t.routes[url] = ok(body, url)
    t.routes[url + ".CHECKSUM"] = ok(f"{_sha(body)}  {url.rsplit('/', 1)[-1]}\n".encode(), url)


def fetcher(t: FakeTransport, out) -> Fetcher:
    return Fetcher(t, out, clock=lambda: RECV)


@pytest.fixture
def no_k9():
    """Driver tests: no process K9 log; the ingest entry point must configure its own (out_dir/k9)."""
    with k9.using(None):
        yield


def jan_15m(t: FakeTransport, *, conflict: bool = False):
    """Monthly 15m file truncated after Jan 30 (header, ms) + daily files for Jan 30 and 31 (no header,
    us)."""
    m_url = ingest.n1_url("R3", "BTCUSDT", "2024-01")
    m_rows = kline_rows(JAN, 30 * 96, 15 * MIN)
    serve(t, m_url, zip_of(m_url.rsplit("/", 1)[-1], m_rows, header=True))
    d_urls = []
    for day, seed in (("2024-01-30", 0), ("2024-01-31", 1)):
        u = ingest.n1_url("R3", "BTCUSDT", day)
        lo = to_ns(day)
        if day == "2024-01-30":  # overlaps the monthly file: must agree (DEC-33)
            rows = [list(r) for r in m_rows[29 * 96:]]
            for r in rows:
                r[0], r[6] = r[0] * 1000, r[6] * 1000
            if conflict:
                rows[3][4] = rows[3][4] + 1.0
        else:
            rows = kline_rows(lo, 96, 15 * MIN, unit="us", seed=seed)
        serve(t, u, zip_of(u.rsplit("/", 1)[-1], rows, header=False))
        d_urls.append(u)
    return m_url, m_rows, d_urls


def test_ingest_monthly_union_daily_round_trip(tmp_path, no_k9):
    t = FakeTransport()
    m_url, m_rows, d_urls = jan_15m(t)
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    e = ingest.ingest_bars(fetcher(t, out), st, "R3", "BTCUSDT", "2024-01", daily=DAYS)
    assert [c[0] for c in t.calls] == [m_url, m_url + ".CHECKSUM", d_urls[0], d_urls[0] + ".CHECKSUM",
                                       d_urls[1], d_urls[1] + ".CHECKSUM"]
    assert e["rows"] == 31 * 96 and e["period"] == [JAN, FEB]
    srcs = {s["url"]: s for s in e["sources"]}
    assert set(srcs) == {m_url, *d_urls}
    assert srcs[m_url]["checksum_sha256"] == srcs[m_url]["sha256"] and srcs[m_url]["recv_ts"] == RECV
    assert srcs[m_url]["kind"] == "N1" and srcs[m_url]["bytes"] > 0
    assert not list(tmp_path.rglob("*.zip"))  # no zip is kept on disk
    with k9.using(k9.K9Log(tmp_path / "read_k9")):
        got = TableLoader(st, "R3").frame("BTCUSDT", JAN, FEB)
    raw = pd.DataFrame(m_rows, columns=list(klines.RAW_COLUMNS)).astype(str)
    want_m = klines.normalise(raw, "ms")
    d31 = pd.DataFrame(kline_rows(to_ns("2024-01-31"), 96, 15 * MIN, unit="us", seed=1),
                       columns=list(klines.RAW_COLUMNS)).astype(str)
    want = klines.union(want_m, klines.normalise(d31, "us")).reset_index(drop=True)
    pd.testing.assert_frame_equal(got, want)
    assert k9.current() is not None and k9.current().log_dir == out / "k9"  # the run's own K9 log
    assert (out / fetch.FETCH_LOG).exists()


def test_ingest_is_idempotent_and_resumable(tmp_path, no_k9):
    t = FakeTransport()
    jan_15m(t)
    st = RawStore(tmp_path / "raw")
    f = fetcher(t, tmp_path / "run")
    e = ingest.ingest_bars(f, st, "R3", "BTCUSDT", "2024-01", daily=("2024-01-30", "2024-01-31"))
    n = len(t.calls)
    assert ingest.ingest_bars(f, st, "R3", "BTCUSDT", "2024-01", daily=("2024-01-30", "2024-01-31")) == e
    assert len(t.calls) == n  # completed and verified: skipped without a fetch
    p = st.chunk_path("R3", "BTCUSDT", "2024-01")
    sha = _sha(p.read_bytes())
    p.unlink()  # a lost file is re-created only with identical content
    assert ingest.ingest_bars(f, st, "R3", "BTCUSDT", "2024-01", daily=("2024-01-30", "2024-01-31")) == e
    assert len(t.calls) > n and _sha(p.read_bytes()) == sha
    p.write_bytes(b"corrupt")
    with pytest.raises(IntegrityBlocker):
        ingest.ingest_bars(f, st, "R3", "BTCUSDT", "2024-01", daily=("2024-01-30", "2024-01-31"))
    assert any(r["rule"].startswith("§3") for r in k9.K9Log(tmp_path / "run" / "k9").records())


def test_conflicting_monthly_and_daily_is_an_integrity_blocker(tmp_path, no_k9):
    t = FakeTransport()
    jan_15m(t, conflict=True)
    st = RawStore(tmp_path / "raw")
    with pytest.raises(IntegrityBlocker, match="conflict"):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01",
                           daily=("2024-01-30", "2024-01-31"))
    assert st.entry("R3", "BTCUSDT", "2024-01") is None
    assert not st.chunk_path("R3", "BTCUSDT", "2024-01").exists()


def test_changed_source_content_is_refused_and_logged(tmp_path, no_k9):
    t = FakeTransport()
    jan_15m(t)
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    ingest.ingest_bars(fetcher(t, out), st, "R3", "BTCUSDT", "2024-01", daily=("2024-01-30",))
    p = st.chunk_path("R3", "BTCUSDT", "2024-01")
    before = p.read_bytes()
    with pytest.raises(rawstore.ImmutabilityViolation):  # adding Jan 31 would change a written file
        ingest.ingest_bars(fetcher(t, out), st, "R3", "BTCUSDT", "2024-01", daily=DAYS)
    assert p.read_bytes() == before
    recs = k9.K9Log(out / "k9").records()
    assert [r["exception"] for r in recs] == ["ImmutabilityViolation"]


def test_w_hold_is_never_fetched_nor_stored(tmp_path, no_k9):
    t = FakeTransport()
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    with pytest.raises(EndpointRefused):
        ingest.ingest_bars(fetcher(t, out), st, "R3", "BTCUSDT", "2025-07")
    with pytest.raises(EndpointRefused):
        ingest.ingest_funding_rest(fetcher(t, out), st, "BTCUSDT", "2025-07")
    assert t.calls == [] and not list(tmp_path.rglob("*.parquet"))
    assert len(k9.K9Log(out / "k9").records()) == 2


def test_listing_single_page_and_truncation(tmp_path, no_k9):
    t = FakeTransport()
    d = "data/futures/um/monthly/klines/BTCUSDT/15m/"

    def xml(keys, truncated):
        items = "".join(f"<Contents><Key>{k}</Key><Size>10</Size><ETag>\"e{i}\"</ETag></Contents>"
                        for i, k in enumerate(keys))
        return (f"<ListBucketResult><Prefix>{d}</Prefix><IsTruncated>{truncated}</IsTruncated>{items}"
                "</ListBucketResult>").encode()

    keys = [d + "BTCUSDT-15m-2024-01.zip", d + "BTCUSDT-15m-2024-01.zip.CHECKSUM",
            d + "BTCUSDT-15m-2023-12.zip"]
    t.routes[ingest.S3] = ok(xml(keys, "false"))
    out = tmp_path / "run"
    got = ingest.list_archive(fetcher(t, out), "R3", "BTCUSDT")
    assert [(m, e.key) for m, e in got] == [("2023-12", keys[2]), ("2024-01", keys[0])]
    assert t.calls[-1][1] == {"delimiter": "/", "prefix": d}
    t.routes[ingest.S3] = ok(xml(keys, "true"))
    with pytest.raises(fetch.ListingTruncated):
        ingest.list_archive(fetcher(t, out), "R3", "BTCUSDT")
    assert [r["exception"] for r in k9.K9Log(out / "k9").records()] == ["ListingTruncated"]


def test_funding_archive_and_rest(tmp_path, no_k9):
    t = FakeTransport()
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    ts = JAN + np.arange(0, 93, dtype=np.int64) * 8 * 3600 * 10**9
    url = ingest.n1_url("R2_archive", "BTCUSDT", "2024-01")
    rows = "calc_time,funding_interval_hours,last_funding_rate\n" + "".join(
        f"{x // 10**6},8,{1e-4 + i * 1e-7:.10f}\n" for i, x in enumerate(ts[:-2]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BTCUSDT-fundingRate-2024-01.csv", rows)
    serve(t, url, buf.getvalue())
    ingest.ingest_funding_archive(fetcher(t, out), st, "BTCUSDT", "2024-01")
    rest = [{"symbol": "BTCUSDT", "fundingTime": int(x // 10**6) + 4,
             "fundingRate": f"{1e-4 + i * 1e-7:.10f}", "markPrice": "100.1"}
            for i, x in enumerate(ts) if i >= 88]

    def n3(u, params):
        assert params == {"symbol": "BTCUSDT", "startTime": str(JAN // 10**6),
                          "endTime": str(FEB // 10**6 - 1), "limit": str(ingest.N3_LIMIT)}
        return Response(200, {}, json.dumps(rest).encode(), u)

    t.routes[ingest.FAPI_FUNDING] = n3
    e = ingest.ingest_funding_rest(fetcher(t, out), st, "BTCUSDT", "2024-01")
    assert e["rows"] == 5 and e["sources"][0]["kind"] == "N3"
    with k9.using(k9.K9Log(tmp_path / "read_k9")):
        got = FundingLoader(st).frame("BTCUSDT", JAN, FEB)
    assert len(got) == 93 and got["source"].tolist().count("rest") == 2
    assert got["mark_at_settle"].notna().sum() == 5  # agreeing overlap fills the archive's null mark
    raw_a = fund_mod.parse_archive_csv(rows, "ms")
    raw_r = fund_mod.normalise_rest(pd.DataFrame(rest).drop(columns="symbol"), "ms")
    pd.testing.assert_frame_equal(got, fund_mod.merge(raw_a, raw_r))
    # a full page may be truncated: fails closed
    t.routes[ingest.FAPI_FUNDING] = lambda u, p: Response(200, {}, json.dumps(rest * 400).encode(), u)
    with pytest.raises(IntegrityBlocker, match="limit"):
        ingest.ingest_funding_rest(fetcher(t, out), st, "BTCUSDT", "2023-12")
    t.routes[ingest.FAPI_FUNDING] = lambda u, p: Response(
        200, {}, json.dumps([{**rest[0], "symbol": "ETHUSDT"}]).encode(), u)
    with pytest.raises(IntegrityBlocker, match="symbol"):
        ingest.ingest_funding_rest(fetcher(t, out), st, "BTCUSDT", "2023-11")


def test_unit_must_be_unambiguous_and_r4_takes_no_daily_files(tmp_path, no_k9):
    t = FakeTransport()
    st = RawStore(tmp_path / "raw")
    url = ingest.n1_url("R3", "BTCUSDT", "2024-01")
    rows = kline_rows(JAN, 4, 15 * MIN)
    for r in rows:
        r[0] = 12345  # plausible in no unit
    serve(t, url, zip_of("BTCUSDT-15m-2024-01.zip", rows, header=False))
    with pytest.raises(IntegrityBlocker, match="unit"):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01")
    with pytest.raises(ValueError, match="daily"):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R4_mark", "BTCUSDT", "2024-01",
                           daily=("2024-01-31",))
    with pytest.raises(ValueError):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01",
                           daily=("2024-02-01",))
    with pytest.raises(ValueError):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01", monthly=False)


def test_urls():
    assert ingest.n1_url("R5", "BTCUSDT", "2024-01") == (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip")
    assert ingest.n1_url("R4_premium", "BTCUSDT", "2024-01-05") == (
        "https://data.binance.vision/data/futures/um/daily/premiumIndexKlines/BTCUSDT/15m/"
        "BTCUSDT-15m-2024-01-05.zip")
    assert ingest.n1_url("R2_archive", "BTCUSDT", "2024-01") == (
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip")
    with pytest.raises(ValueError):
        ingest.n1_url("R2_rest", "BTCUSDT", "2024-01")


def test_ingest_never_writes_w_hold_even_if_the_fetch_layer_were_wider(tmp_path):
    """Defence in depth: the store applies the fetch layer's bound itself (refused, K9-logged)."""
    st = RawStore(tmp_path / "raw")
    with k9.using(k9.K9Log(tmp_path / "k9")) as lg:
        df = klines.normalise(pd.DataFrame(kline_rows(to_ns("2025-07-01"), 2, 15 * MIN),
                                           columns=list(klines.RAW_COLUMNS)).astype(str), "ms")
        with pytest.raises(guard.HoldoutBlocked):
            st.put_chunk("R3", "BTCUSDT", "2025-07", df, sources=[])
        assert len(lg.records()) == 1


def test_month_helpers():
    assert rawstore.month_period("2024-02") == (FEB, to_ns("2024-03-01"))
    assert rawstore.months_between(JAN + D, FEB + D) == ["2024-01", "2024-02"]
    assert rawstore.months_between(JAN, FEB) == ["2024-01"]
