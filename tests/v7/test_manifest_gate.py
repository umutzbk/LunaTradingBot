"""Task 29c (red-team r2 MJ4 / MJ2; controller rulings R56 / R55; AMB-44 / AMB-45): the DEC-19 pinned W_HOLD
listing manifest is built from single-page N2 listings, required (by content) at the opening and enforced on
every post-opening W_HOLD N1 fetch; the ingest-completeness gate refuses an unresolved ingest gap, a DEC-33
conflict or an unledgered chunk in the data used. Fake transports, tmp_path, synthetic data only."""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from luna.v7 import fetch, guard, ingest, k9, manifest, metaingest, rawstore, runner, windows
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.fetch import Fetcher, ListingTruncated, ManifestMismatch, Response
from luna.v7.netguard import Request
from luna.v7.rawstore import RawStore
from luna.v7.timeutil import MIN, to_ns
from tests.v7 import auth_fixtures as af
from tests.v7.net_fixtures import FakeTransport, ok
from tests.v7.test_ingest import empty_listings, kline_rows, serve, zip_of
from tests.v7.test_rawstore import bars, month_of

RECV = to_ns("2026-10-15")
BVIS = "https://data.binance.vision/"
UM = "data/futures/um"
MONTHS6 = ["2023-01", "2023-02", "2023-03", "2023-04", "2023-05", "2023-06"]


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


def _rules(lg: k9.K9Log) -> list[str]:
    return [r["rule"] for r in lg.records()]


# ------------------------------------------------------------------------------- R56: the builder


def _xml(prefix: str, keys: list[tuple[str, int, str]], prefixes: list[str] = (), truncated: str = "false"):
    items = "".join(f"<Contents><Key>{k}</Key><Size>{s}</Size><ETag>\"{e}\"</ETag></Contents>"
                    for k, s, e in keys)
    cps = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
    return (f"<ListBucketResult><Prefix>{prefix}</Prefix><IsTruncated>{truncated}</IsTruncated>{items}{cps}"
            "</ListBucketResult>").encode()


def _bucket(truncate: str | None = None) -> tuple[dict, FakeTransport]:
    """A synthetic bucket: BTCUSDT and XYZUSDT; W_HOLD and W_DEV keys; daily files of a W_HOLD month."""
    k15 = f"{UM}/monthly/klines/BTCUSDT/15m/"
    files = {
        f"{UM}/monthly/klines/": ([], [f"{UM}/monthly/klines/BTCUSDT/", f"{UM}/monthly/klines/XYZUSDT/"]),
        f"{UM}/daily/klines/": ([], [f"{UM}/daily/klines/BTCUSDT/"]),
        f"{UM}/monthly/fundingRate/": ([], [f"{UM}/monthly/fundingRate/BTCUSDT/"]),
        k15: ([(k15 + "BTCUSDT-15m-2025-06.zip", 5, "a" * 32),  # W_DEV: not in the manifest
               (k15 + "BTCUSDT-15m-2025-07.zip", 7, "b" * 32),
               (k15 + "BTCUSDT-15m-2025-07.zip.CHECKSUM", 3, "c" * 32)], []),
        f"{UM}/monthly/klines/XYZUSDT/1m/": ([(f"{UM}/monthly/klines/XYZUSDT/1m/XYZUSDT-1m-2026-01.zip", 9,
                                               "d" * 32 + "-3")], []),
        f"{UM}/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-08-": (
            [(f"{UM}/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-08-02.zip", 4, "e" * 32)], []),
        f"{UM}/monthly/fundingRate/BTCUSDT/": (
            [(f"{UM}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-06.zip", 2, "f" * 32)], []),
        f"{UM}/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-08-": (  # R56b
            [(f"{UM}/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-08-03.zip", 6, "1" * 32)], []),
    }

    def route(url, params):
        p = params["prefix"]
        keys, cps = files.get(p, ([], []))
        return ok(_xml(p, keys, cps, "true" if p == truncate else "false"))

    t = FakeTransport({manifest.S3: route})
    return files, t


def test_builder_keeps_w_hold_listing_fields_only(tmp_path, log):
    _files, t = _bucket()
    doc = manifest.build_document(Fetcher(t, tmp_path / "run", clock=lambda: RECV))
    assert doc["symbols"] == ["BTCUSDT", "XYZUSDT"] and doc["hedge"] == "BTCUSDT"
    assert doc["entries"] == [
        [f"{UM}/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-08-03.zip", 6, "1" * 32],
        [f"{UM}/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-08-02.zip", 4, "e" * 32],
        [f"{UM}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-06.zip", 2, "f" * 32],
        [f"{UM}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-07.zip", 7, "b" * 32],
        [f"{UM}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-07.zip.CHECKSUM", 3, "c" * 32],
        [f"{UM}/monthly/klines/XYZUSDT/1m/XYZUSDT-1m-2026-01.zip", 9, "d" * 32 + "-3"]]
    assert doc["w_hold"] == list(windows.W_HOLD) and doc["run_id"] == guard.RUN_ID
    prefixes = sorted({c[1]["prefix"] for c in t.calls})
    # daily files: one listing per (symbol, W_HOLD month), never the whole daily directory (DK-01)
    assert f"{UM}/daily/klines/BTCUSDT/15m/" not in prefixes
    assert f"{UM}/daily/klines/BTCUSDT/15m/BTCUSDT-15m-2026-06-" in prefixes
    assert not any("XYZUSDT" in p for p in prefixes if "/daily/klines/" in p)  # no daily klines directory
    # R56b: aggTrades of every in-scope symbol, one listing per (symbol, W_HOLD month)
    agg = {f"{UM}/daily/aggTrades/{s}/{s}-aggTrades-2026-06-" for s in ("BTCUSDT", "XYZUSDT")}
    assert agg <= set(prefixes)
    assert manifest.counts(doc)["R10/daily"] == 1
    assert manifest.validate(doc).symbols == ("BTCUSDT", "XYZUSDT")
    again = manifest.build_document(Fetcher(_bucket()[1], tmp_path / "run2", clock=lambda: RECV))
    assert manifest.canonical_bytes(again) == manifest.canonical_bytes(doc)  # deterministic


def test_builder_fails_closed_on_a_truncated_listing_and_without_the_hedge(tmp_path, log):
    _f, t = _bucket(truncate=f"{UM}/monthly/klines/BTCUSDT/15m/")
    with pytest.raises(ListingTruncated):
        manifest.build_document(Fetcher(t, tmp_path / "run", clock=lambda: RECV))
    with pytest.raises(manifest.ManifestError, match="hedge"):
        manifest.document([(f"{UM}/monthly/klines/XYZUSDT/1m/XYZUSDT-1m-2026-01.zip", 9, "d" * 32)])
    for bad in (f"{UM}/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2025-06.zip",  # W_DEV
                f"{UM}/monthly/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-08.zip",  # out of scope
                f"{UM}/monthly/klines/BTCUSDT/15m/ETHUSDT-15m-2025-08.zip"):  # not of its directory
        with pytest.raises(manifest.ManifestError, match="in-scope"):
            manifest.document([(bad, 1, "a" * 32)])


def test_scope_matches_the_raw_store_tables():
    for table, freq, dataset, interval in manifest.SCOPE:
        if table == "R10":  # aggTrades (R56b): not a raw-store table
            assert (freq, dataset, interval) == ("daily", "aggTrades", None)
            continue
        t = rawstore.TABLES[table]
        assert (t.dataset, t.interval) == (dataset, interval)
        assert freq == "monthly" or t.daily_union


# ------------------------------------------------------------------------- R56: the opening check


def test_preflight_requires_the_canonical_manifest_by_content(tmp_path, monkeypatch):
    repo = af.frozen_repo(tmp_path / "repo")
    home = tmp_path / "home"
    af.roots(monkeypatch, repo, home)
    rec = af.pre_opening(tmp_path / "pre.json")
    with af.verify_spec_off():
        assert guard.preflight(pre_opening=rec, manifest_sha256=af.MANIFEST)["head"] == af.HEAD_COMMIT
        for free in ("c" * 64, "0" * 64, None, "xyz"):  # a free 64-hex string no longer passes
            with pytest.raises(guard.HoldoutBlocked, match="manifest"):
                guard.preflight(pre_opening=rec, manifest_sha256=free)
    p = manifest.manifest_path()
    good = p.read_bytes()
    doc = json.loads(good)
    p.write_bytes(json.dumps(doc, indent=1).encode())  # same content, not canonical bytes
    with pytest.raises(manifest.ManifestError, match="sha256|canonical"):
        manifest.verify_pinned(_sha(p.read_bytes()))
    tampered = dict(doc, symbols=["BTCUSDT", "ZZZUSDT"])  # canonical bytes, but not a valid document
    p.write_bytes(manifest.canonical_bytes(tampered))
    with pytest.raises(manifest.ManifestError, match="differ"):
        manifest.verify_pinned(_sha(p.read_bytes()))
    p.unlink()
    with pytest.raises(manifest.ManifestError, match="missing"):
        manifest.verify_pinned(af.MANIFEST)
    p.write_bytes(good)
    assert manifest.verify_pinned(af.MANIFEST).sha256 == af.MANIFEST


def test_written_manifest_is_immutable_once_opened(tmp_path, monkeypatch):
    o = af.authorize(tmp_path, monkeypatch)
    assert guard.verify_authorization(o.auth)["manifest_sha256"] == af.MANIFEST
    with pytest.raises(guard.HoldoutBlocked, match="immutable"):
        manifest.write_document(af.MANIFEST_DOC)


def test_write_document_returns_the_sha_to_pin(tmp_path, monkeypatch):
    af.roots(monkeypatch, af.frozen_repo(tmp_path / "repo"), tmp_path / "home")
    manifest.manifest_path().unlink()
    p, sha = manifest.write_document(af.MANIFEST_DOC)
    assert p == manifest.manifest_path() and sha == af.MANIFEST == _sha(p.read_bytes())


# -------------------------------------------------------------------- R56: post-opening consumer


@pytest.fixture(scope="module")
def _opening(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("opening"))


@pytest.fixture
def opened(_opening, monkeypatch):
    return af.use_opening(monkeypatch, _opening)


ZIP = BVIS + af.HOLD_ZIP_KEY


def _k9(out) -> list[dict]:
    return k9.K9Log(out / "k9").records()


def test_post_opening_w_hold_files_must_match_the_pinned_manifest(opened, tmp_path):
    good = {ZIP: ok(af.HOLD_ZIP_BODY), ZIP + ".CHECKSUM": ok(af.HOLD_CK_BODY)}
    out = tmp_path / "run"
    f = Fetcher(FakeTransport(dict(good)), out, clock=lambda: RECV)
    assert f.fetch(Request(ZIP), auth=opened.auth).body == af.HOLD_ZIP_BODY
    same_size = b"W_HOLD zap"  # republished content of the same size: md5 differs from the pinned ETag
    ck = f"{_sha(same_size)}  BTCUSDT-15m-2025-08.zip".encode()
    unlisted = BVIS + af.HOLD_ZIP_KEY.replace("2025-08", "2025-09")
    for routes, req in (({ZIP: ok(same_size), ZIP + ".CHECKSUM": ok(ck)}, Request(ZIP)),
                        ({ZIP: ok(af.HOLD_ZIP_BODY + b"!"), ZIP + ".CHECKSUM": ok(af.HOLD_CK_BODY)},
                         Request(ZIP)),
                        ({unlisted: ok(b"x"), unlisted + ".CHECKSUM": ok(b"y")}, Request(unlisted))):
        with pytest.raises(ManifestMismatch):
            Fetcher(FakeTransport(routes), out, clock=lambda: RECV).fetch(req, auth=opened.auth)
        assert _k9(out)[-1]["rule"] == fetch.RULE_MANIFEST and _k9(out)[-1]["exception"] == "ManifestMismatch"
    assert issubclass(ManifestMismatch, fetch.FetchError)  # non-fatal for the driver: an ingest gap (R55)


def test_match_entry_etag_forms():
    m = manifest.validate(manifest.document([
        (af.HOLD_ZIP_KEY, 3, "0" * 32 + "-2"), (af.HOLD_ZIP_KEY + ".CHECKSUM", 3, "0" * 32 + "-2")]))
    assert manifest.match_entry(m, af.HOLD_ZIP_KEY, b"abc") is None  # multipart zip: size (+ .CHECKSUM)
    assert "bytes" in manifest.match_entry(m, af.HOLD_ZIP_KEY, b"abcd")
    assert "cannot be verified" in manifest.match_entry(m, af.HOLD_ZIP_KEY + ".CHECKSUM", b"abc")
    m2 = manifest.validate(manifest.document([(af.HOLD_ZIP_KEY, 3, _md5(b"abc"))]))
    assert manifest.match_entry(m2, af.HOLD_ZIP_KEY, b"abc") is None
    assert "md5" in manifest.match_entry(m2, af.HOLD_ZIP_KEY, b"abd")


def _hold_chunk(store: RawStore, auth, sources) -> None:
    lo, hi = rawstore.month_period("2025-08")
    store.put_chunk("R3", "BTCUSDT", "2025-08", month_of(bars(lo, 31 * 96), lo, hi), sources=sources,
                    auth=auth)


def test_gate_with_the_authorization_needs_every_pinned_w_hold_file_stored(opened, tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = windows.W_HOLD
    with pytest.raises(IntegrityBlocker, match="pinned W_HOLD file"):
        st.check_ingest_complete(lo, hi, auth=opened.auth)
    st.check_ingest_complete(lo, hi)  # pre-opening view: no manifest coverage asked
    st.check_ingest_complete(lo, hi, tables=("R5",), auth=opened.auth)  # not in use
    _hold_chunk(st, opened.auth, [{"url": "https://data.binance.vision/elsewhere.zip"}])
    with pytest.raises(IntegrityBlocker, match="pinned W_HOLD file"):  # stored, but not from the pinned file
        st.check_ingest_complete(lo, hi, auth=opened.auth)
    st2 = RawStore(tmp_path / "raw2")
    _hold_chunk(st2, opened.auth, [{"url": ZIP, "bytes": len(af.HOLD_ZIP_BODY), "md5": _md5(b"other")}])
    with pytest.raises(IntegrityBlocker, match="differs from the pinned"):  # round 1 m5: size / md5 compared
        st2.check_ingest_complete(lo, hi, auth=opened.auth)
    st3 = RawStore(tmp_path / "raw3")
    good = {"url": ZIP, "bytes": len(af.HOLD_ZIP_BODY), "md5": _md5(af.HOLD_ZIP_BODY)}
    _hold_chunk(st3, opened.auth, [good])
    st3.check_ingest_complete(lo, hi, auth=opened.auth)  # the aggTrades key (R10) is not a store table
    assert rawstore.RULE_INGEST in _rules(log)


# --------------------------------------------------------------------------- R55: the gate (probe)


def _six_months(t: FakeTransport, fail: str | None = None) -> None:
    empty_listings(t)
    for i, m in enumerate(MONTHS6):
        url = ingest.n1_url("R3", "BTCUSDT", m)
        lo, hi = rawstore.month_period(m)
        body = zip_of(url.rsplit("/", 1)[-1], kline_rows(lo, (hi - lo) // (15 * MIN), 15 * MIN, seed=i), True)
        serve(t, url, body)
        if m == fail:
            t.routes[url] = Response(503, {}, b"", url)


def test_probe_gap_instance_refuses_instead_of_splitting(tmp_path, log):
    """red-team r2 probe_gap_instance.py: one R3 month of six missing after a ledgered transport failure. The
    instance is no longer split silently (phantom instance end + restart): the run refuses until a later
    successful re-ingest stores the month (the resolution is in the ledger, append-only)."""
    t = FakeTransport()
    _six_months(t, fail="2023-04")
    st = RawStore(tmp_path / "raw")
    out = tmp_path / "run"
    recs = ingest.ingest_months(Fetcher(t, out, clock=lambda: RECV), st, "R3", "BTCUSDT", MONTHS6)
    assert [r["status"] for r in recs] == ["stored"] * 3 + ["gap"] + ["stored"] * 2
    hi = rawstore.month_period("2023-06")[1]
    with k9.using(log), pytest.raises(IntegrityBlocker, match="2023-04"):
        metaingest.instances_from_store(st, ["BTCUSDT"], hi)
    assert _rules(log)[-1] == rawstore.RULE_INGEST
    # the hole itself (what the gate prevents): read anyway, it would split the live instance
    raw = metaingest.build_instances
    jan = rawstore.month_period("2023-01")[0]
    a = rawstore.TableLoader(st, "R3").arrays("BTCUSDT", jan, hi, ["vol_base"])
    assert len(raw(pd.DataFrame({"open_ts": a["open_ts"], "vol_base": a["vol_base"]}), hi)) == 2
    # a later successful re-ingest resolves the gap
    _six_months(t)
    rec = ingest.ingest_months(Fetcher(t, out, clock=lambda: RECV), st, "R3", "BTCUSDT", ["2023-04"])
    assert rec[0]["status"] == "stored"
    with k9.using(log):
        insts = metaingest.instances_from_store(st, ["BTCUSDT"], hi)
    assert [(i.first_bar_ts, i.last_trade_ts) for i in insts["BTCUSDT"]] == [
        (rawstore.month_period("2023-01")[0], None)]
    lines = [json.loads(x) for x in st.ledger_path.read_text(encoding="utf-8").splitlines()]
    apr = [r["status"] for r in lines if r["month"] == "2023-04"]
    assert apr == ["attempt", "gap", "attempt", "stored"]  # append-only: the gap stays, resolved after it


def test_an_unledgered_exception_leaves_an_unresolved_attempt(tmp_path, log):
    t = FakeTransport()
    _six_months(t)
    url = ingest.n1_url("R3", "BTCUSDT", "2023-02")

    def boom(u, p):
        raise ConnectionError("reset by peer")

    t.routes[url] = boom
    st = RawStore(tmp_path / "raw")
    f = Fetcher(t, tmp_path / "run", clock=lambda: RECV)
    with pytest.raises(ConnectionError):  # not a ledgered failure: the driver sees the exception
        ingest.ingest_months(f, st, "R3", "BTCUSDT", ["2023-01", "2023-02"])
    lo, hi = rawstore.month_period("2023-01")[0], rawstore.month_period("2023-03")[0]
    with pytest.raises(IntegrityBlocker, match="attempt"):
        st.check_ingest_complete(lo, hi)
    st.check_ingest_complete(lo, rawstore.month_period("2023-02")[0])  # January alone is complete


def test_conflict_blocks_and_scope_filters(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.put_chunk("R3", "BTCUSDT", "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    st.check_ingest_complete(lo, hi)
    st.append_ledger(rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "gap",
                                            reason="IntegrityBlocker: kline conflict monthly vs daily in c"))
    with pytest.raises(IntegrityBlocker, match="conflict"):  # DEC-33: a stored chunk does not resolve it
        st.check_ingest_complete(lo, hi)
    st.check_ingest_complete(lo, hi, symbols=["ETHUSDT"])  # other symbol
    st.check_ingest_complete(lo, hi, tables=("R5",))  # other table
    st.check_ingest_complete(hi, hi + 31 * 86_400 * 10**9)  # other period
    st.append_ledger(rawstore.ledger_record("R5", "ETHUSDT", "2024-01", "spill_not_requested", reason="x"))
    with pytest.raises(IntegrityBlocker, match="spill_not_requested"):
        st.check_ingest_complete(lo, hi, tables=("R5",))


def test_unledgered_chunk_and_unreadable_ledger_fail_closed(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.put_chunk("R3", "BTCUSDT", "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    led = st.ledger_path.read_bytes()
    st.ledger_path.unlink()  # a store without its ledger (or a pre-R55 store)
    with pytest.raises(IntegrityBlocker, match="without a stored ledger record"):
        st.check_ingest_complete(lo, hi)
    st.resume_chunk("R3", "BTCUSDT", "2024-01")  # a resumed ingest re-records the verified chunk
    st.check_ingest_complete(lo, hi)
    st.ledger_path.write_bytes(led + b'{"table": "R3", "sym')  # a torn last line
    with pytest.raises(IntegrityBlocker, match="unreadable"):
        st.check_ingest_complete(lo, hi)
    st.append_ledger(rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "stored", sha256="0" * 64))
    lines = st.ledger_path.read_bytes().split(b"\n")  # the new record is never glued to the torn line
    assert lines[-1] == b"" and json.loads(lines[-2])["sha256"] == "0" * 64 and lines[-3].endswith(b'"sym')


def test_research_entry_points_run_the_gate(tmp_path, log):
    calls = []

    class Gated(runner.InMemorySource):
        def check_ingest(self, lo, hi):
            calls.append((lo, hi))
            if len(calls) > 1:
                raise IntegrityBlocker("gap")

    class Never(runner.InMemorySource):
        def check_ingest(self, lo, hi):
            raise IntegrityBlocker("gap")

        def load(self, *a):  # pragma: no cover - the gate runs before any load
            raise AssertionError("loaded despite the gate")

    with pytest.raises(IntegrityBlocker):
        runner.ResearchUniverse.from_source(Never({}), 0, 10)
    with pytest.raises(IntegrityBlocker):
        runner.ResearchUniverse.for_decisions(Never({}), 5, 10, history_lo=0)

    class U:
        def __init__(self):
            self._ingest_check = (Gated({}).check_ingest, 1, 2)

        check_ingest = runner.ResearchUniverse.check_ingest

        def check_data(self, auth):
            return None

    calls.append(None)  # the next check raises
    with pytest.raises(IntegrityBlocker):
        runner.run_book("A0", U(), object(), (0, 1), 72, 0)

    class P:
        with_features = True

    t0 = to_ns("2024-01-10")
    with pytest.raises(IntegrityBlocker):
        runner.label_panel(U(), P(), [t0], 72, (t0, t0 + 10**15), t0 + 10**15)
    assert calls[-1] == (1, 2)


def test_store_source_checks_every_table_of_the_store(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.append_ledger(rawstore.ledger_record("R2_rest", "XYZUSDT", "2024-01", "gap", reason="FetchError"))
    src = rawstore.StoreSource(st, {})
    with pytest.raises(IntegrityBlocker, match="R2_rest"):  # a symbol without metadata is still in use
        src.check_ingest(lo, hi)
    with pytest.raises(IntegrityBlocker):
        runner.ResearchUniverse.from_source(src, lo, hi)


def test_post_opening_mismatch_is_an_unresolved_ingest_gap(opened, tmp_path, log):
    same_size = b"W_HOLD zap"
    t = FakeTransport({ZIP: ok(same_size),
                       ZIP + ".CHECKSUM": ok(f"{_sha(same_size)}  BTCUSDT-15m-2025-08.zip".encode())})
    empty_listings(t)
    st = RawStore(tmp_path / "raw")
    f = Fetcher(t, tmp_path / "run", clock=lambda: RECV)
    rec = ingest.ingest_months(f, st, "R3", "BTCUSDT", ["2025-08"], auth=opened.auth)
    assert rec[0]["status"] == "gap" and "ManifestMismatch" in rec[0]["reason"]
    with pytest.raises(IntegrityBlocker, match="ManifestMismatch"):
        st.check_ingest_complete(*windows.W_HOLD, auth=opened.auth)


def test_ledger_state_is_part_of_the_data_identity(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.put_chunk("R3", "BTCUSDT", "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    src = rawstore.StoreSource(st, {})
    clean, ident = st.ingest_state_sha256(), src.identity()
    assert ident["ingest"] == clean and st.data_manifests() == {**st.manifest_sha256s(),
                                                                 "ingest_ledger_state": clean}
    st.append_ledger(rawstore.ledger_record("R3", "BTCUSDT", "2024-02", "gap", reason="FetchError: 503"))
    gap = st.ingest_state_sha256()
    assert gap != clean and src.identity() != ident and src.identity()["raw"] == ident["raw"]
    lo2, hi2 = rawstore.month_period("2024-02")
    st.put_chunk("R3", "BTCUSDT", "2024-02", month_of(bars(lo2, 29 * 96), lo2, hi2), sources=[{"url": "s://2"}])
    assert st.ingest_state_sha256() == clean  # resolved: the same completeness state again


def test_adopt_legacy_fetcher_ledgers_as_they_are(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.put_chunk("R3", "BTCUSDT", "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    e = st.entry("R3", "BTCUSDT", "2024-01")
    st.ledger_path.unlink()  # a pre-R55 store: its records live only in the fetcher ledgers
    old = tmp_path / "w1" / ingest.LEDGER
    old.parent.mkdir()
    recs = [{"table": "R3", "symbol": "BTCUSDT", "month": "2024-01", "status": "stored",
             "sha256": e["sha256"], "rows": e["rows"], "reason": None},
            {"kind": "wayback_capture", "query": "q", "status": "cdx_failed", "reason": "x"},
            {"table": "R3", "symbol": "ETHUSDT", "month": "2024-01", "status": "gap", "sha256": None,
             "rows": None, "reason": "FetchError: HTTP 503"}]
    old.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    assert ingest.adopt_ledgers(st, [old]) == 2  # the metadata record is not the raw store's
    st.check_ingest_complete(lo, hi, symbols=["BTCUSDT"])
    with pytest.raises(IntegrityBlocker, match="ETHUSDT"):
        st.check_ingest_complete(lo, hi)
    assert ingest.adopt_ledgers(st, [old]) == 2  # idempotent in effect (order-independent gate)
    st.check_ingest_complete(lo, hi, symbols=["BTCUSDT"])
    # an old record lacking a field is an unresolved gap: a stored record without its sha256
    st2 = RawStore(tmp_path / "raw2")
    st2.put_chunk("R3", "BTCUSDT", "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    st2.ledger_path.unlink()
    old.write_text(json.dumps({"table": "R3", "symbol": "BTCUSDT", "month": "2024-01", "status": "stored"})
                   + "\n{torn", encoding="utf-8")
    ingest.adopt_ledgers(st2, [old])
    with pytest.raises(IntegrityBlocker, match="without a stored ledger record"):
        st2.check_ingest_complete(lo, hi)
    with pytest.raises(IntegrityBlocker, match="unreadable"):  # the torn legacy line blocks every use
        st2.check_ingest_complete(lo, hi, symbols=["ZZZUSDT"])


def test_decision_pass_runs_the_gate_first(tmp_path, log):
    class Never(runner.InMemorySource):
        def check_ingest(self, lo, hi):
            raise IntegrityBlocker("gap")

    class NoStore:
        def bind(self, body):  # pragma: no cover - the gate runs first
            raise AssertionError("bound despite the gate")

    with pytest.raises(IntegrityBlocker):
        runner.decision_pass(Never({}), [10**18], NoStore(), history_lo=0)


# ------------------------------------------------------------------------------------- round 1 (29b/29c)


AGG = BVIS + af.HOLD_AGG_KEY


def test_aggtrades_w_hold_files_are_accepted_when_listed_and_refused_when_not(opened, tmp_path):
    out = tmp_path / "run"
    f = Fetcher(FakeTransport({AGG: ok(af.HOLD_AGG_BODY), AGG + ".CHECKSUM": ok(af.HOLD_AGG_CK)}), out,
                clock=lambda: RECV)
    assert f.fetch(Request(AGG), auth=opened.auth).body == af.HOLD_AGG_BODY  # R56b: listed, matching
    other = AGG.replace("2025-08-03", "2025-08-04")
    body = b"x"
    g = Fetcher(FakeTransport({other: ok(body), other + ".CHECKSUM": ok(
        f"{_sha(body)}  BTCUSDT-aggTrades-2025-08-04.zip".encode())}), out, clock=lambda: RECV)
    with pytest.raises(ManifestMismatch, match="not in the pinned"):
        g.fetch(Request(other), auth=opened.auth)


def test_live_listing_must_equal_the_pinned_manifest(opened, tmp_path):
    prefix = f"{UM}/monthly/klines/BTCUSDT/15m/"
    keys = [(af.HOLD_ZIP_KEY, len(af.HOLD_ZIP_BODY), _md5(af.HOLD_ZIP_BODY)),
            (af.HOLD_ZIP_KEY + ".CHECKSUM", len(af.HOLD_CK_BODY), _md5(af.HOLD_CK_BODY)),
            (prefix + "BTCUSDT-15m-2025-06.zip", 1, "a" * 32)]  # W_DEV: not compared
    out = tmp_path / "run"

    def fetch_listing(ks):
        t = FakeTransport({manifest.S3: lambda u, p: ok(_xml(p["prefix"], ks))})
        return Fetcher(t, out, clock=lambda: RECV).fetch(Request(manifest.S3, {"delimiter": "/",
                                                                               "prefix": prefix}),
                                                         auth=opened.auth)

    assert len(fetch_listing(keys).listing.entries) == 3
    extra = keys + [(prefix + "BTCUSDT-15m-2025-09.zip", 5, "b" * 32)]  # a W_HOLD key the manifest lacks
    for ks in (extra, keys[1:], [(keys[0][0], keys[0][1] + 1, keys[0][2])] + keys[1:]):
        with pytest.raises(ManifestMismatch, match="differs from the pinned"):
            fetch_listing(ks)
        assert _k9(out)[-1]["rule"] == fetch.RULE_MANIFEST
    assert fetch_listing(keys).listing is not None  # pre-opening (no auth): no comparison either way
    t = FakeTransport({manifest.S3: lambda u, p: ok(_xml(p["prefix"], extra))})
    Fetcher(t, out, clock=lambda: RECV).fetch(Request(manifest.S3, {"delimiter": "/", "prefix": prefix}))


def test_store_less_universes_are_refused(opened, tmp_path, log):
    from luna.v7 import runner_select

    u = runner.ResearchUniverse.__new__(runner.ResearchUniverse)  # a universe built from inputs: no check
    u._ingest_check = None
    for call in (lambda: runner.run_book("A0", u, object(), windows.W_HOLD, 72, 0, auth=opened.auth),
                 lambda: runner.label_panel(u, object(), [1], 72, windows.W_HOLD, 0, auth=opened.auth),
                 lambda: runner_select.dev_selection(u, object(), {}, 0),
                 lambda: runner.ResearchUniverse.from_source(runner.InMemorySource({}), 0, 10),
                 lambda: runner.decision_pass(runner.InMemorySource({}), [], object(), history_lo=0)):
        with pytest.raises(IntegrityBlocker, match="no verified ingest ledger"):
            call()
    assert _rules(log)[-1] == runner.RULE_NO_LEDGER


def test_run_book_binds_the_provider_and_the_model_type(tmp_path, log):
    from tests.v7 import fixtures_v7 as fx

    u = runner.ResearchUniverse.__new__(runner.ResearchUniverse)
    u._ingest_check = (fx.LedgeredSource({}).check_ingest, 0, 1)
    u.inputs = {}
    u.engine_inputs = lambda *a: None  # the universe span check is tested elsewhere

    class P:
        with_features = True

    with pytest.raises(ValueError, match="provider.u is not u"):
        runner.run_book("A0", u, P(), (0, 1), 72, 0)
    p = P()
    p.u = u
    with pytest.raises(TypeError, match="FoldScoreModel"):
        runner.run_book("A2", u, p, (0, 1), 72, 0, model=object())
    with pytest.raises(IntegrityBlocker, match="in-memory ledger"):  # the test-only ledger is a real check
        rec = rawstore.ledger_record("R3", "X", "1970-01", "gap", reason="r")
        fx.LedgeredSource({}, [rec]).check_ingest(0, 1)


def test_only_label_panel_and_run_book_issue_into_the_holdout_registries():
    from pathlib import Path

    src = Path(runner.__file__).read_text(encoding="utf-8")
    body = src.split("def label_panel(", 1)[1]
    assert src.count("_issue_holdout_panel(") == 2 and "_issue_holdout_panel(panel, auth" in body
    assert src.count("_issue_holdout_run(") == 2 and "_issue_holdout_run(run, auth" in src.split(
        "def run_book(", 1)[1]
    others = [p.name for p in Path(runner.__file__).parent.rglob("*.py") if p.name != "runner.py"
              and ("_issue_holdout_panel" in p.read_text(encoding="utf-8")
                   or "_issue_holdout_run" in p.read_text(encoding="utf-8"))]
    assert others == []


def test_note_holdout_universe_refusal_is_k9_logged(opened, tmp_path, log):
    panel = runner.LabelPanel(72, pd.DataFrame(), pd.DataFrame(), 0, 0, 0, 0.0, ())
    with pytest.raises(guard.HoldoutBlocked, match="not issued"):
        runner._note_holdout_universe(panel, opened.auth)
    assert log.records()[-1]["exception"] == "HoldoutBlocked"


def test_structured_conflict_field(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.append_ledger({**rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "gap", reason="FetchError: x"),
                      "conflict": True})
    st.append_ledger({**rawstore.ledger_record("R3", "ETHUSDT", "2024-01", "gap",
                                               reason="the word conflict in a 503"), "conflict": False})
    for sym in ("BTCUSDT", "ETHUSDT"):
        st.put_chunk("R3", sym, "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    with pytest.raises(IntegrityBlocker, match="BTCUSDT"):  # conflict: true is never resolved
        st.check_ingest_complete(lo, hi)
    st.check_ingest_complete(lo, hi, symbols=["ETHUSDT"])  # the field decides, not the reason text
    assert ingest._err_gap("R3", "X", "2024-01", IntegrityBlocker("kline conflict"))["conflict"] is False
    from luna.v7.data.schema import IntegrityConflict

    assert ingest._err_gap("R3", "X", "2024-01", IntegrityConflict("x"))["conflict"] is True


def test_planned_listing_coverage_and_torn_line_repair(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    lo, hi = rawstore.month_period("2024-01")
    st.put_chunk("R3", "BTCUSDT", "2024-01", month_of(bars(lo, 31 * 96), lo, hi), sources=[{"url": "s://1"}])
    st.check_ingest_complete(lo, hi, planned=[("R3", "BTCUSDT", "2024-01")])
    with pytest.raises(IntegrityBlocker, match="planned"):  # listed but never ingested, no ledger record
        st.check_ingest_complete(lo, hi, planned=[("R3", "BTCUSDT", "2024-01"), ("R3", "ETHUSDT", "2024-01")])
    with open(st.ledger_path, "ab") as f:
        f.write(b'{"table": "R3", "sym')  # a torn last line
    with pytest.raises(IntegrityBlocker, match="unreadable"):
        st.check_ingest_complete(lo, hi)
    assert st.quarantine_unreadable() == 1
    st.check_ingest_complete(lo, hi)  # repaired, append-only
    assert b"sym" in (tmp_path / "raw" / "INGEST_LEDGER.quarantine.jsonl").read_bytes()
    assert log.records()[-1]["rule"] == rawstore.RULE_REPAIR
    assert st.quarantine_unreadable() == 0


def test_manifest_entries_are_indexed_by_parent_prefix():
    """Task 29e: ``check_listing`` reads the pinned entries under a prefix from a per-manifest parent index;
    same result as the scan (direct children only, daily-stem prefixes included)."""
    d = f"{UM}/daily/klines/BTCUSDT/15m/"
    ents = {d + "BTCUSDT-15m-2025-08-01.zip": (1, "a" * 32), d + "BTCUSDT-15m-2025-09-01.zip": (2, "b" * 32),
            d + "sub/BTCUSDT-15m-2025-08-02.zip": (3, "c" * 32), f"{UM}/daily/klines/X.zip": (4, "d" * 32)}
    m = manifest.Manifest("0" * 64, ("BTCUSDT",), ents)
    for prefix in (d, d + "BTCUSDT-15m-2025-08-", f"{UM}/daily/klines/", f"{UM}/nothing/"):
        scan = {k: v for k, v in ents.items() if k.startswith(prefix) and "/" not in k[len(prefix):]}
        assert m.under(prefix) == scan
    assert m.under(d + "BTCUSDT-15m-2025-08-") == {d + "BTCUSDT-15m-2025-08-01.zip": (1, "a" * 32)}


def test_under_auth_only_a_store_source_with_that_auth_passes(opened, tmp_path, log):
    """Hardening (Tasks 29b / 29c re-review): under the authorization, run_book / label_panel need the
    universe's ingest check to be a rawstore.StoreSource's, opened with the same auth; an in-memory,
    duck-typed or subclassed source is refused (IntegrityBlocker, K9-logged) even when its own
    check passes."""
    from tests.v7 import fixtures_v7 as fx

    class Duck:
        auth = opened.auth

        def check_ingest(self, lo, hi):
            return None

    class Sub(rawstore.StoreSource):
        def check_ingest(self, lo, hi):
            return None

    st = RawStore(tmp_path / "raw")
    good = rawstore.StoreSource(st, {}, auth=opened.auth)
    other = rawstore.StoreSource(st, {}, auth=None)

    def uni(chk):
        u = runner.ResearchUniverse.__new__(runner.ResearchUniverse)
        u._ingest_check = (chk, 0, 1)
        return u

    runner._require_store_source(uni(good.check_ingest), opened.auth)  # passes
    runner._require_store_source(uni(fx.LedgeredSource({}).check_ingest), None)  # no auth: no requirement
    for chk in (fx.LedgeredSource({}).check_ingest, Duck().check_ingest,
                Sub(st, {}, auth=opened.auth).check_ingest, other.check_ingest):
        with pytest.raises(IntegrityBlocker, match="StoreSource"):
            runner._require_store_source(uni(chk), opened.auth)
    assert _rules(log)[-1] == runner.RULE_AUTH_STORE
    u = uni(fx.LedgeredSource({}).check_ingest)  # the entry points run it right after the R55 check
    for call in (lambda: runner.run_book("A0", u, object(), windows.W_HOLD, 72, 0, auth=opened.auth),
                 lambda: runner.label_panel(u, object(), [1], 72, windows.W_HOLD, 0, auth=opened.auth)):
        with pytest.raises(IntegrityBlocker, match="StoreSource"):
            call()
