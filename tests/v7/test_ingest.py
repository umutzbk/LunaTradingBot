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

from luna.v7 import fetch, guard, ingest, k9, rawstore, windows
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
    assert k9.current() is None  # the run's K9 log was used for the call only (the previous one restored)
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
        ingest.ingest_bars(fetcher(t, out), st, "R3", "BTCUSDT", "2025-07", daily=())
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
    (rec,) = ingest.ingest_funding_archive(fetcher(t, out), st, "BTCUSDT", ["2024-01"])
    assert rec["status"] == "stored" and rec["rows"] == 91
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
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01", daily=())
    with pytest.raises(ValueError, match="daily"):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R4_mark", "BTCUSDT", "2024-01",
                           daily=("2024-01-31",))
    with pytest.raises(ValueError):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01",
                           daily=("2024-02-01",))
    with pytest.raises(ValueError):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01", monthly=False,
                           daily=())


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


# ------------------------------------------------------------------------------- Task 26a fix round 1


def listing_xml(prefix: str, keys: list[str], truncated: str = "false") -> bytes:
    items = "".join(f"<Contents><Key>{k}</Key><Size>10</Size><ETag>\"e{i}\"</ETag></Contents>"
                    for i, k in enumerate(keys))
    return (f"<ListBucketResult><Prefix>{prefix}</Prefix><IsTruncated>{truncated}</IsTruncated>{items}"
            "</ListBucketResult>").encode()


def empty_listings(t: FakeTransport) -> None:
    t.routes[ingest.S3] = lambda u, p: ok(listing_xml(p["prefix"], []))


def test_resumed_ingest_heals_a_missing_table_manifest(tmp_path, no_k9):
    """Review I-2 probe: crash after the symbol manifest, before the (first) table manifest."""
    t = FakeTransport()
    jan_15m(t)
    st = RawStore(tmp_path / "raw")
    f = fetcher(t, tmp_path / "run")
    ingest.ingest_bars(f, st, "R3", "BTCUSDT", "2024-01", daily=DAYS)
    tman = st.table_dir("R3") / "MANIFEST.json"
    tman.unlink()
    n = len(t.calls)
    ingest.ingest_bars(f, st, "R3", "BTCUSDT", "2024-01", daily=DAYS)
    assert len(t.calls) == n and tman.exists()  # no fetch, healed
    with k9.using(k9.K9Log(tmp_path / "rk9")):
        assert len(TableLoader(st, "R3").frame("BTCUSDT", JAN, FEB)) == 31 * 96


def test_daily_files_come_from_one_n2_listing(tmp_path, no_k9):
    t = FakeTransport()
    m_url, _, d_urls = jan_15m(t)
    prefix = "data/futures/um/daily/klines/BTCUSDT/15m/BTCUSDT-15m-2024-01-"
    keys = [u.split("data.binance.vision/", 1)[1] for u in d_urls]
    keys += [k + ".CHECKSUM" for k in keys]
    t.routes[ingest.S3] = lambda u, p: ok(listing_xml(p["prefix"], keys if p["prefix"] == prefix else []))
    st = RawStore(tmp_path / "raw")
    e = ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01")
    assert t.calls[0][0] == ingest.S3 and t.calls[0][1] == {"delimiter": "/", "prefix": prefix}
    assert sorted(s["url"] for s in e["sources"]) == sorted([m_url, *d_urls]) and e["rows"] == 31 * 96
    with pytest.raises(ValueError, match="R47"):  # R4: monthly only
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R4_mark", "BTCUSDT", "2024-01", daily=DAYS)


def test_empty_file_is_refused(tmp_path, no_k9):
    t = FakeTransport()
    url = ingest.n1_url("R3", "BTCUSDT", "2024-01")
    serve(t, url, zip_of("BTCUSDT-15m-2024-01.zip", [], header=True))
    st = RawStore(tmp_path / "raw")
    with pytest.raises(IntegrityBlocker, match="empty file"):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01", daily=())
    assert st.entry("R3", "BTCUSDT", "2024-01") is None


def test_daily_rows_must_lie_in_their_named_day(tmp_path, no_k9):
    t = FakeTransport()
    jan_15m(t)
    u31 = ingest.n1_url("R3", "BTCUSDT", "2024-01-31")
    rows = kline_rows(to_ns("2024-01-30"), 96 * 2, 15 * MIN, unit="us", seed=1)  # Jan 30 rows in Jan 31's
    serve(t, u31, zip_of(u31.rsplit("/", 1)[-1], rows, header=False))
    st = RawStore(tmp_path / "raw")
    with pytest.raises(IntegrityBlocker, match="named day"):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01",
                           daily=("2024-01-31",))
    assert st.entry("R3", "BTCUSDT", "2024-01") is None


def test_bad_checksum_writes_no_chunk_and_no_manifest(tmp_path, no_k9):
    t = FakeTransport()
    url = ingest.n1_url("R3", "BTCUSDT", "2024-01")
    body = zip_of("BTCUSDT-15m-2024-01.zip", kline_rows(JAN, 4, 15 * MIN), header=False)
    t.routes[url] = ok(body, url)
    t.routes[url + ".CHECKSUM"] = ok(f"{'0' * 64}  BTCUSDT-15m-2024-01.zip\n".encode(), url)
    st = RawStore(tmp_path / "raw")
    with pytest.raises(fetch.IntegrityError):
        ingest.ingest_bars(fetcher(t, tmp_path / "run"), st, "R3", "BTCUSDT", "2024-01", daily=())
    assert not list(tmp_path.rglob("*.parquet")) and not list(tmp_path.rglob("MANIFEST.json"))


def test_ingest_restores_the_previous_k9_log(tmp_path):
    prev = k9.K9Log(tmp_path / "prev")
    t = FakeTransport()
    with k9.using(prev):
        with pytest.raises(EndpointRefused):
            ingest.ingest_bars(fetcher(t, tmp_path / "run"), RawStore(tmp_path / "raw"), "R3", "BTCUSDT",
                               "2025-07", daily=())
        assert k9.current() is prev
    assert len(k9.K9Log(tmp_path / "run" / "k9").records()) == 1 and prev.records() == []


def _funding_zip(rows: list[tuple[int, float]]) -> bytes:
    text = "calc_time,funding_interval_hours,last_funding_rate\n" + "".join(
        f"{ts // 10**6},8,{r:.10f}\n" for ts, r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("f.csv", text)
    return buf.getvalue()


def _settlements(lo: int, hi: int) -> list[int]:
    return list(range(lo, hi, 8 * 3600 * 10**9))


def test_funding_rows_are_repartitioned_by_event_month_r48(tmp_path, no_k9):
    t = FakeTransport()
    jan, feb = _settlements(JAN, FEB), _settlements(FEB, to_ns("2024-03-01"))
    rate = {ts: 1e-4 + (i % 7) * 1e-6 for i, ts in enumerate(jan + feb)}
    u_jan, u_feb = (ingest.n1_url("R2_archive", "BTCUSDT", m) for m in ("2024-01", "2024-02"))
    # Jan's file also holds Feb 1 00:00:00.000 (agreeing with Feb's own row at +3 ms) and Feb 1 08:00, which
    # Feb's file lacks
    spill = [(feb[0], rate[feb[0]]), (feb[1], rate[feb[1]])]
    serve(t, u_jan, _funding_zip([(ts, rate[ts]) for ts in jan] + spill))
    serve(t, u_feb, _funding_zip([(feb[0] + 3 * 10**6, rate[feb[0]])] + [(ts, rate[ts]) for ts in feb[2:]]))
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    recs = ingest.ingest_funding_archive(fetcher(t, out), st, "BTCUSDT", ["2024-01", "2024-02"])
    assert [(r["month"], r["status"], r["rows"]) for r in recs] == [("2024-01", "stored", len(jan)),
                                                                     ("2024-02", "stored", len(feb))]
    with k9.using(k9.K9Log(tmp_path / "rk9")):
        a = TableLoader(st, "R2_archive").frame("BTCUSDT", JAN, to_ns("2024-03-01"))
    got = a["funding_ts"].to_numpy()
    assert feb[0] + 3 * 10**6 in got and feb[0] not in got and feb[1] in got  # own-month row kept
    assert len(got) == len(jan) + len(feb)
    srcs = {s["url"] for s in st.entry("R2_archive", "BTCUSDT", "2024-02")["sources"]}
    assert srcs == {u_jan, u_feb}
    lines = (out / ingest.LEDGER).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["status"] for x in lines] == ["stored", "stored"]


def test_conflicting_spill_is_a_ledgered_gap_r48(tmp_path, no_k9):
    t = FakeTransport()
    jan, feb = _settlements(JAN, FEB), _settlements(FEB, to_ns("2024-03-01"))
    u_jan, u_feb = (ingest.n1_url("R2_archive", "BTCUSDT", m) for m in ("2024-01", "2024-02"))
    serve(t, u_jan, _funding_zip([(ts, 1e-4) for ts in jan] + [(feb[0], 9e-4)]))  # conflicts with Feb's row
    serve(t, u_feb, _funding_zip([(ts, 1e-4) for ts in feb]))
    st = RawStore(tmp_path / "raw")
    recs = ingest.ingest_funding_archive(fetcher(t, tmp_path / "run"), st, "BTCUSDT", ["2024-01", "2024-02"])
    assert [(r["month"], r["status"]) for r in recs] == [("2024-01", "stored"), ("2024-02", "gap")]
    assert "conflicting" in recs[1]["reason"] and st.entry("R2_archive", "BTCUSDT", "2024-02") is None


def test_funding_rows_at_the_bound_are_dropped_and_logged_r48(tmp_path, no_k9):
    t = FakeTransport()
    jun = _settlements(to_ns("2025-06-01"), windows.W_HOLD[0])
    u = ingest.n1_url("R2_archive", "BTCUSDT", "2025-06")
    serve(t, u, _funding_zip([(ts, 1e-4) for ts in jun] + [(windows.W_HOLD[0], 1e-4)]))
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    (rec,) = ingest.ingest_funding_archive(fetcher(t, out), st, "BTCUSDT", ["2025-06"])
    assert rec["status"] == "stored" and rec["rows"] == len(jun)
    (k,) = k9.K9Log(out / "k9").records()
    assert k["rule"] == ingest.RULE_DROPPED and k["request"]["rows"] == 1


def test_driver_ledgers_gaps_and_continues_but_raises_guard_refusals(tmp_path, no_k9):
    t = FakeTransport()
    empty_listings(t)
    u_jan = ingest.n1_url("R3", "BTCUSDT", "2024-01")
    t.routes[u_jan] = ok(b"zip", u_jan)
    t.routes[u_jan + ".CHECKSUM"] = ok(f"{'0' * 64}  BTCUSDT-15m-2024-01.zip\n".encode(), u_jan)
    u_feb = ingest.n1_url("R3", "BTCUSDT", "2024-02")
    serve(t, u_feb, zip_of("BTCUSDT-15m-2024-02.zip", kline_rows(FEB, 29 * 96, 15 * MIN), header=False))
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    recs = ingest.ingest_months(fetcher(t, out), st, "R3", "BTCUSDT", ["2024-01", "2024-02"])
    assert [(r["month"], r["status"]) for r in recs] == [("2024-01", "gap"), ("2024-02", "stored")]
    assert "IntegrityError" in recs[0]["reason"]
    assert len((out / ingest.LEDGER).read_text(encoding="utf-8").splitlines()) == 2
    with pytest.raises(EndpointRefused):
        ingest.ingest_months(fetcher(t, out), st, "R3", "BTCUSDT", ["2025-07"])
