"""Task 29e: amendment A-6 (AMENDMENTS-2026-10-16) - a DEC-33 monthly / daily kline conflict is resolved by
removing the conflicting keys from both sources (missing bars) and recording the resolution; R55 treats it as
resolved, an unresolved conflict still blocks; DT4 lists the resolutions of its window; the dry-run driver's
not-applicable N3 keys (S-2.1). Fake transports, tmp_path, synthetic data only."""

from __future__ import annotations

import json

import pytest

from luna.v7 import beta, ingest, k9, rawstore, runrecord
from luna.v7.data import klines
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.fetch import Fetcher
from luna.v7.rawstore import RawStore, TableLoader
from luna.v7.timeutil import MIN, H, to_ns
from tests.v7.net_fixtures import FakeTransport, ok
from tests.v7.test_ingest import kline_rows, listing_xml, serve, zip_of
from tests.v7.test_rawstore import bars, month_of

JAN, FEB = to_ns("2024-01-01"), to_ns("2024-02-01")
RECV = to_ns("2026-10-15")
DAY = "2024-01-30"
BAD = (60, 61, 62, 63, 70)  # Jan 30 daily rows that disagree on open (5 keys: 15:00 .. 15:45, 17:30)


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


def _serve_jan(t: FakeTransport, *, bad=BAD) -> tuple[list, list]:
    """Monthly file for the whole of January + the Jan 30 daily file (overlapping), ``bad`` rows' open +1."""
    m_url = ingest.n1_url("R3", "BTCUSDT", "2024-01")
    m_rows = kline_rows(JAN, 31 * 96, 15 * MIN)
    serve(t, m_url, zip_of(m_url.rsplit("/", 1)[-1], m_rows, header=True))
    d_rows = [list(r) for r in m_rows[29 * 96:30 * 96]]
    for i in bad:
        d_rows[i][1] = round(d_rows[i][1] + 1.0, 2)
    u = ingest.n1_url("R3", "BTCUSDT", DAY)
    serve(t, u, zip_of(u.rsplit("/", 1)[-1], d_rows, header=False))
    return m_rows, d_rows


def _ingest(t, st, out):
    return ingest.ingest_months(Fetcher(t, out, clock=lambda: RECV), st, "R3", "BTCUSDT", ["2024-01"])


def test_union_a6_drops_conflicting_keys_from_both_sources():
    m = bars(JAN, 8)
    d = m.iloc[2:6].copy()
    d.loc[d.index[1], "o"] += 1.0
    d.loc[d.index[3], "vol_base"] += 1.0
    out, conf = klines.union_a6(m, d)
    assert conf["keys"] == [int(m["open_ts"].iloc[3]), int(m["open_ts"].iloc[5])]
    o3 = float(m["o"].iloc[3])
    assert conf["monthly"][0]["o"] == o3 and conf["daily"][0]["o"] == o3 + 1
    assert list(out["open_ts"]) == [int(x) for i, x in enumerate(m["open_ts"]) if i not in (3, 5)]
    json.dumps(conf, allow_nan=False)
    same, none = klines.union_a6(m, m.iloc[2:6])
    assert none is None and len(same) == 8
    with pytest.raises(IntegrityBlocker, match="conflict"):  # the strict union is unchanged
        klines.union(m, d)


def test_conflict_on_five_keys_stores_the_chunk_minus_those_keys(tmp_path, log):
    t = FakeTransport()
    t.routes[ingest.S3] = lambda u, p: ok(listing_xml(
        p["prefix"], [p["prefix"] + "30.zip"] if p["prefix"].endswith("2024-01-") else []))
    m_rows, _ = _serve_jan(t)
    st = RawStore(tmp_path / "raw")
    lo, hi = JAN, FEB
    (rec,) = _ingest(t, st, tmp_path / "run")
    assert rec["status"] == "stored" and rec["rows"] == 31 * 96 - 5
    day0 = to_ns(DAY)
    keys = [day0 + i * 15 * MIN for i in BAD]
    with k9.using(k9.K9Log(tmp_path / "rk9")):
        got = TableLoader(st, "R3").frame("BTCUSDT", lo, hi)
    assert not set(keys) & set(got["open_ts"].tolist()) and len(got) == 31 * 96 - 5
    lines = [json.loads(x) for x in st.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in lines] == ["attempt", "conflict", "stored", rawstore.A6_RESOLVED]
    a6 = lines[-1]
    assert a6["keys"] == keys and a6["rule"] == rawstore.RULE_A6 and a6["sha256"] == lines[2]["sha256"]
    assert [v["o"] for v in a6["daily_values"]] == [round(v["o"] + 1.0, 2) for v in a6["monthly_values"]]
    assert {s["name"] for s in a6["sources"]} == {"BTCUSDT-15m-2024-01.zip", "BTCUSDT-15m-2024-01-30.zip"}
    assert all(len(s["sha256"]) == 64 for s in a6["sources"])
    st.check_ingest_complete(lo, hi)  # R55: resolved by A-6
    # downstream: the hour closed by the dropped 15:45 bar (16:00) has no hourly close for beta
    hc = beta.hourly_close_from_15m(got)
    assert day0 + 16 * H not in hc.index and day0 + 15 * H in hc.index
    # DT4 lists the resolution inside the window, not outside
    assert st.a6_resolutions(lo, hi) == [json.loads(json.dumps(a6, sort_keys=True))]
    assert st.a6_resolutions(day0 + 18 * H, hi) == []
    # idempotent: a resumed run fetches no file again (only the N2 daily listing) and records no new A-6
    n = len(t.calls)
    _ingest(t, st, tmp_path / "run")
    assert [c[0] for c in t.calls[n:]] == [ingest.S3]
    again = [json.loads(x) for x in st.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert sum(r["status"] == rawstore.A6_RESOLVED for r in again) == 1
    st.check_ingest_complete(lo, hi)


def _chunk(st, sym="BTCUSDT"):
    """January without its first bar (the key ``_a6`` records as dropped)."""
    lo, hi = JAN, FEB
    df = month_of(bars(lo, 31 * 96), lo, hi).iloc[1:].reset_index(drop=True)
    return st.put_chunk("R3", sym, "2024-01", df, sources=[{"url": "s://1"}])


def _a6(e, **kw):
    return {"table": "R3", "symbol": "BTCUSDT", "month": "2024-01", "status": rawstore.A6_RESOLVED,
            "sha256": e["sha256"], "rows": e["rows"], "conflict": False, "reason": "A-6", "keys": [JAN],
            "monthly_values": [{"o": 1.0}], "daily_values": [{"o": 2.0}], "rule": rawstore.RULE_A6,
            "sources": [{"name": "a.zip", "url": "u", "sha256": "0" * 64}], **kw}


def test_r55_blocks_before_and_passes_after_the_a6_resolution(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    e = _chunk(st)
    st.append_ledger({**rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "conflict", reason="x"),
                      "conflict": True})
    with pytest.raises(IntegrityBlocker, match="conflict"):  # unresolved conflict: blocks
        st.check_ingest_complete(JAN, FEB)
    st.append_ledger(_a6(e, sha256="f" * 64))  # a resolution of another chunk content: still blocks
    with pytest.raises(IntegrityBlocker, match="conflict"):
        st.check_ingest_complete(JAN, FEB)
    st.append_ledger(_a6(e, rule="something else"))  # malformed (no A-6 rule): resolves nothing
    with pytest.raises(IntegrityBlocker, match="conflict"):
        st.check_ingest_complete(JAN, FEB)
    st.append_ledger(_a6(e, keys=[JAN + 15 * 60 * 10**9]))  # a "dropped" key the chunk still holds: nothing
    with pytest.raises(IntegrityBlocker, match="conflict"):
        st.check_ingest_complete(JAN, FEB)
    st.append_ledger(_a6(e, keys=[FEB]))  # a key outside the chunk's month: nothing
    with pytest.raises(IntegrityBlocker, match="conflict"):
        st.check_ingest_complete(JAN, FEB)
    st2 = RawStore(tmp_path / "raw2")
    e2 = _chunk(st2)
    st2.append_ledger({**rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "gap", reason="c"),
                       "conflict": True})
    before = st2.ingest_state_sha256()
    st2.append_ledger(_a6(e2))
    st2.check_ingest_complete(JAN, FEB)  # resolved by A-6
    assert st2.ingest_state_sha256() != before


def test_legacy_reason_only_conflict_blocks_unless_resolved_by_a6(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    e = _chunk(st)
    st.append_ledger(rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "gap",
                                            reason="IntegrityConflict: kline conflict monthly vs daily in o"))
    with pytest.raises(IntegrityBlocker, match="conflict"):
        st.check_ingest_complete(JAN, FEB)
    st.append_ledger(_a6(e))
    st.check_ingest_complete(JAN, FEB)


def test_a6_never_resolves_a_funding_conflict(tmp_path, log):
    """A-6 covers DEC-33 (monthly union daily, R3 / R5); an S-2.6 funding conflict still blocks."""
    st = RawStore(tmp_path / "raw")
    st.append_ledger({**rawstore.ledger_record("R2_archive", "BTCUSDT", "2024-01", "gap", reason="c"),
                      "conflict": True})
    st.append_ledger({**_a6({"sha256": "a" * 64, "rows": 1}), "table": "R2_archive"})
    with pytest.raises(IntegrityBlocker):
        st.check_ingest_complete(JAN, FEB)


def test_dt4_lists_the_a6_resolutions(tmp_path, log):
    from tests.v7.test_round2_minors import funding_run

    st = RawStore(tmp_path / "raw")
    e = _chunk(st)
    st.append_ledger({**rawstore.ledger_record("R3", "BTCUSDT", "2024-01", "conflict", reason="x"),
                      "conflict": True})
    st.append_ledger(_a6(e))
    base = dict(commit="a" * 40, dirty=False, config={"h": 24}, data_manifests=st.data_manifests(),
                seeds=runrecord.default_seeds(), uv_lock_sha256="c" * 64, libraries={"python": "3.12"},
                convergence=(), runs=[funding_run([0.0] * 3)])
    with pytest.raises(ValueError, match="a6_resolutions"):  # a raw-store record must list them
        runrecord.run_record(**base)
    with pytest.raises(ValueError, match="store and data_window"):  # bound to the store (fix round 1)
        runrecord.run_record(**base, a6_resolutions=st.a6_resolutions(JAN, FEB))
    bound = dict(base, store=st, data_window=(JAN, FEB))
    rec = runrecord.run_record(**bound, a6_resolutions=st.a6_resolutions(JAN, FEB))
    assert rec["a6_resolutions"][0]["keys"] == [JAN] and rec["a6_resolutions"][0]["rule"] == rawstore.RULE_A6
    with pytest.raises(ValueError, match="mismatch"):  # an omitted resolution is refused
        runrecord.run_record(**bound, a6_resolutions=[])
    altered = [{**st.a6_resolutions(JAN, FEB)[0], "keys": [JAN + 15 * MIN]}]
    with pytest.raises(ValueError, match="mismatch"):  # other keys
        runrecord.run_record(**bound, a6_resolutions=altered)
    with pytest.raises(ValueError, match="conflict_resolved_A6"):
        runrecord.run_record(**bound, a6_resolutions=[{"status": "stored"}])
    later = dict(bound, data_window=(FEB, FEB + 86_400 * 10**9))  # outside the window: none to list
    assert runrecord.run_record(**later, a6_resolutions=[])["a6_resolutions"] == []
    st.append_ledger(rawstore.ledger_record("R3", "ETHUSDT", "2024-01", "attempt", reason="x"))
    with pytest.raises(ValueError, match="ingest ledger state"):  # the store changed since the manifests
        runrecord.run_record(**bound, a6_resolutions=rec["a6_resolutions"])


def test_not_applicable_n3_keys_of_non_perpetual_symbols(tmp_path, log):
    """Dry-run driver (Task 29e, fix round 1): an N3 key netguard refused as a non-alphanumeric symbol
    (delivery / settled names), with that refusal as evidence and no R1 PERPETUAL identity, is recorded as not
    applicable (S-2.1) and stops blocking; a perpetual's gap, a key without the refusal evidence or with an R1
    PERPETUAL identity is never hidden, and hand-written records that lack these are ignored."""
    from luna.v7 import netguard

    refusal = f"EndpointRefused: {netguard.N3_SYMBOL_REFUSAL}"
    st = RawStore(tmp_path / "raw")
    for sym in ("BTCUSDT_240628", "ICPUSDT_SETTLED", "ETHUSDT", "XRPUSDT_250328"):
        st.append_ledger(rawstore.ledger_record("R2_rest", sym, "2024-01", "attempt", reason="started"))
    with pytest.raises(IntegrityBlocker, match="attempt"):
        st.check_ingest_complete(JAN, FEB)
    for sym in ("BTCUSDT_240628", "ICPUSDT_SETTLED"):
        rec = ingest.mark_not_applicable(st, "R2_rest", sym, "2024-01", evidence=refusal,
                                         r1_contract_type="CURRENT_QUARTER" if "_2" in sym else None)
        assert rec["status"] == rawstore.NOT_APPLICABLE and netguard.N3_SYMBOL_REFUSAL in rec["evidence"]
    refused = [dict(table="R2_rest", symbol="ETHUSDT", evidence=refusal, r1_contract_type=None),  # alnum
               dict(table="R3", symbol="BTCUSDT_240628", evidence=refusal, r1_contract_type=None),
               dict(table="R2_rest", symbol="XRPUSDT_250328", evidence="HTTP 503", r1_contract_type=None),
               dict(table="R2_rest", symbol="XRPUSDT_250328", evidence=refusal, r1_contract_type="PERPETUAL")]
    for kw in refused:
        with pytest.raises(ValueError):
            ingest.mark_not_applicable(st, kw["table"], kw["symbol"], "2024-01", evidence=kw["evidence"],
                                       r1_contract_type=kw["r1_contract_type"])
    # hand-written records: no evidence / an R1 PERPETUAL / an alphanumeric symbol -> ignored
    st.append_ledger(rawstore.ledger_record("R2_rest", "ETHUSDT", "2024-01", rawstore.NOT_APPLICABLE))
    na = rawstore.ledger_record("R2_rest", "XRPUSDT_250328", "2024-01", rawstore.NOT_APPLICABLE)
    st.append_ledger({**na,
                      "evidence": refusal, "r1_contract_type": "PERPETUAL"})
    for sym in ("ETHUSDT", "XRPUSDT_250328"):
        with pytest.raises(IntegrityBlocker, match=sym):  # a real gap still blocks
            st.check_ingest_complete(JAN, FEB, symbols=[sym])
    st.check_ingest_complete(JAN, FEB, symbols=["BTCUSDT_240628", "ICPUSDT_SETTLED"])
    assert rawstore.perpetual_shaped("1000PEPEUSDT") and not rawstore.perpetual_shaped("BTCUSDT_240628")
