"""Task 26c (SC-03 ingestion completeness): R11 notices from bounded Wayback captures (closed-list kind), R1
instruments with E8 identity, R12 empty-valid; the outcome-free ingest report (DK-05 / DK-06 validation C4).

Synthetic HTML captures built in code (modelled on the Binance announcement page: ``<title>`` with the
`` | Binance Support`` suffix, the rendered article and the ``__APP_DATA`` JSON with the article and related
articles, ``<``/``>``/``&`` escaped as ``\\u003c`` ...), a fake transport, tmp directories. No network,
nothing under var/, no real capture.
"""

from __future__ import annotations

import email.utils
import gzip
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from luna.v7 import guard, k9, metaingest
from luna.v7.data import captures as cap
from luna.v7.data import instruments as ins
from luna.v7.data.instances import Instance, build_instances
from luna.v7.data.notices import U6State, confident_match, known_at, scheduled_ts
from luna.v7.data.schema import IntegrityBlocker, validate
from luna.v7.data.terminal import validate_r12
from luna.v7.deploy import CurrentFiltersNotHistorical, assert_not_backdated
from luna.v7.fetch import Fetcher, Response
from luna.v7.guard import GuardFailure
from luna.v7.metastore import MetaStore
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.universe import instrument_exclusion
from tests.v7.net_fixtures import CDX, FakeTransport

CODE = "0bfb6cfd5d7b4e8b9b7c0a1e2f3d4c5b"
OTHER = "aaaaaaaabbbbbbbbccccccccdddddddd"
URL = f"https://www.binance.com/en/support/announcement/binance-futures-will-delist-xyzusdt-{CODE}"
RELEASE = to_ns("2024-03-08 09:00")
RECV = to_ns("2026-10-15")


def page(title: str, body_html: str, *, code: str = CODE, release: int | None = RELEASE,
         related: tuple = ((OTHER, "Binance Futures Will Launch USDⓈ-M DEFUSDT Perpetual Contract",
                            to_ns("2024-03-01")),), gz: bool = False) -> bytes:
    art = {"code": code, "title": title, "body": body_html}
    if release is not None:
        art["releaseDate"] = release // 10**6
    app = {"appState": {"loader": {"dataByRouteId": {"article": {
        "articleDetail": art,
        "relatedArticles": [{"code": c, "title": t, "releaseDate": r // 10**6} for c, t, r in related]}}}}}
    js = json.dumps(app, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace(
        "&", "\\u0026")
    html = (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title} | Binance Support</title>'
            f'<meta name="description" content="{title}"></head><body><header><a href="/en">Home</a>'
            f'</header><main><h1 class="css-title">{title}</h1><div class="css-article">{body_html}</div>'
            f'</main><script id="__APP_DATA" type="application/json">{js}</script></body></html>')
    b = html.encode("utf-8")
    return gzip.compress(b, mtime=0) if gz else b


DELIST_TITLE = "Binance Futures Will Delist USDⓈ-M XYZUSDT Perpetual Contract"
DELIST_BODY = ("<p>Fellow Binancians,</p><p>Binance Futures will close all positions and conduct an "
               "automatic settlement on the USDⓈ-M XYZUSDT Perpetual Contract at 2024-03-15 09:00 (UTC). "
               "After that, the contract will be delisted.</p><p>Thanks &amp; regards,</p>")


def capture(ts: str, body: bytes, original: str = URL) -> cap.Capture:
    return cap.Capture(ts, original, body, hashlib.sha256(body).hexdigest(), RECV)


# ------------------------------------------------------------------------------------------- extraction


def test_title_and_body_extraction_is_deterministic():
    raw = page(DELIST_TITLE, DELIST_BODY)
    t = cap.extract(raw, URL)
    assert t.title == DELIST_TITLE  # cut at " | " (Stage 0)
    assert t.text.startswith(DELIST_TITLE + "\n")
    assert "at 2024-03-15 09:00 (UTC). After that" in t.text  # tags stripped, whitespace collapsed
    assert "Thanks & regards," in t.text and "&amp;" not in t.text  # character references unescaped
    assert "<p>" not in t.text and "\\u003c" not in t.text  # the JSON-embedded article HTML is stripped too
    assert "  " not in t.text
    assert t.release_ts == RELEASE and t.release_reason == "code_bound_json"
    assert t.date_keys == ("releaseDate",)
    again = cap.extract(raw, URL)
    assert again == t and t.text_sha256 == hashlib.sha256(t.text.encode()).hexdigest()
    assert cap.extract(page(DELIST_TITLE, DELIST_BODY, gz=True), URL) == t  # gzip transparent


def test_release_date_only_from_inside_the_capture_and_fail_closed():
    # code-bound: the related article's releaseDate never wins
    assert cap.release_date(cap.decode(page(DELIST_TITLE, DELIST_BODY)), URL) == (RELEASE, "code_bound_json")
    no_code = "https://www.binance.com/en/support/announcement/some-slug"
    assert cap.release_date(cap.decode(page(DELIST_TITLE, DELIST_BODY)), no_code) == (
        None, "release_date_ambiguous")
    assert cap.release_date(cap.decode(page(DELIST_TITLE, DELIST_BODY, related=())), no_code) == (
        RELEASE, "unique_in_page")
    bare = cap.decode(page(DELIST_TITLE, DELIST_BODY, release=None, related=()))
    assert cap.release_date(bare, no_code) == (None, "no_release_date")
    # the article's own date missing: the related article's date is never taken
    t = cap.extract(page(DELIST_TITLE, DELIST_BODY, release=None), URL)
    assert (t.release_ts, t.release_reason) == (None, "no_code_bound_release_date")


def test_notice_identity_from_the_url():
    a = cap.notice_id(URL)
    assert a == f"en:{CODE}"
    assert cap.notice_id(f"http://binance.com/en/support/announcement/{CODE}?ref=x") == a
    assert cap.notice_id(f"https://www.binance.com/en/support/announcement/other-slug-{CODE}/") == a
    assert cap.notice_id(f"https://www.binance.com/zh-CN/support/announcement/{CODE}") == f"zh-cn:{CODE}"
    assert cap.notice_id("https://www.binance.com/en/support/announcement/Some-Slug/") == (
        "url:binance.com/en/support/announcement/some-slug")


@pytest.mark.parametrize("phrase", cap.DELISTING_PHRASES)
def test_each_delisting_phrase(phrase):
    assert cap.classify_kind(f"Binance Futures Will {phrase.title()} Something")[0] == "delisting"


@pytest.mark.parametrize("phrase", cap.POSTPONEMENT_PHRASES)
def test_each_postponement_phrase(phrase):
    assert cap.classify_kind(f"Update: Binance Futures Will {phrase.upper()} of USDⓈ-M OMGUSDT")[0] == (
        "postponement")


@pytest.mark.parametrize("phrase", cap.CANCELLATION_PHRASES)
def test_each_cancellation_phrase(phrase):
    assert cap.classify_kind(f"Binance Futures Will {phrase} of USDⓈ-M OMGUSDT")[0] == "cancellation"


def test_unclassified_and_ambiguous_titles_are_never_guessed():
    assert cap.classify_kind("Binance Futures Will Launch USDⓈ-M ABCUSDT Perpetual Contract") == (
        None, "no_kind_phrase")
    assert cap.classify_kind("Binance Will Support the XYZ Token Swap") == (None, "no_kind_phrase")
    assert cap.classify_kind(None) == (None, "no_title")
    assert cap.classify_kind("Binance Futures Will Delist AUSDT and Postpone the Delisting of BUSDT") == (
        None, "ambiguous_kind:postponement+delisting")
    assert cap.classify_kind("Postpone the Delisting and Cancel the Delisting") == (
        None, "ambiguous_kind:cancellation+postponement")


# ------------------------------------------------------------------------------- notices -> versions


INST = Instance(0, to_ns("2023-01-01"), to_ns("2024-03-15 09:00"), to_ns("2024-03-22 09:00"))
INSTS = {"XYZUSDT": [INST], "ABCUSDT": [Instance(0, to_ns("2024-02-20"), None, None)]}


def test_versions_kinds_and_unclassified_notice_in_the_report():
    a = capture("20240308095620", page(DELIST_TITLE, DELIST_BODY))
    a2 = capture("20240309000000", page(DELIST_TITLE, DELIST_BODY))  # same content: same version
    upd = DELIST_BODY.replace("2024-03-15 09:00", "2024-03-16 09:00")
    b = capture("20240310000000", page(DELIST_TITLE, upd))  # a later revision: a new version
    launch = capture("20240301120000", page("Binance Futures Will Launch USDⓈ-M ABCUSDT Perpetual Contract",
                                            "<p>USDⓈ-M ABCUSDT at 2024-03-02 08:00 (UTC)</p>", code=OTHER,
                                            release=to_ns("2024-03-01"), related=()),
                     f"https://www.binance.com/en/support/announcement/{OTHER}")
    nb = metaingest.build_notices([b, a2, launch, a], INSTS)
    assert [(v.notice_id, v.version, v.kind) for v in nb.versions] == [(f"en:{CODE}", 1, "delisting"),
                                                                       (f"en:{CODE}", 2, "delisting")]
    v1, v2 = nb.versions
    assert v1.capture_ts == to_ns("2024-03-08 09:56:20") and v1.release_ts == RELEASE
    assert v1.sha256 == a.sha256
    assert known_at(v1) == to_ns("2024-03-08 09:56:20")
    assert scheduled_ts(v1, "XYZUSDT") == to_ns("2024-03-15 09:00")
    assert scheduled_ts(v2, "XYZUSDT") == to_ns("2024-03-16 09:00")
    validate("R11", nb.table)
    assert list(nb.table["version"]) == [1, 2]
    assert json.loads(nb.table["matched_instances"].iloc[0]) == ["XYZUSDT#0"]
    assert json.loads(nb.table["scheduled_delist_ts"].iloc[0]) == {"XYZUSDT#0": to_ns("2024-03-15 09:00")}
    rep = nb.report
    assert rep["counts"] == {"captures": 4, "versions": 3, "classified": 2, "unclassified": 1, "unusable": 0,
                             "by_kind": {"cancellation": 0, "delisting": 2, "postponement": 0}}
    unc = [r for r in rep["versions"] if r["status"] == "unclassified"]
    assert len(unc) == 1 and unc[0]["kind_reason"] == "no_kind_phrase" and unc[0]["kind"] is None
    assert unc[0]["n_captures"] == 1
    assert [r["n_captures"] for r in rep["versions"] if r["status"] == "classified"] == [2, 1]
    m = rep["versions"][0]["matches"]
    assert len(m) == 1 and {k: m[0][k] for k in ("symbol", "match", "reason", "instance", "scheduled_ts",
                                                 "scheduled_reason")} == {
        "symbol": "XYZUSDT", "match": True, "reason": "usdm_context:usds-m", "instance": "XYZUSDT#0",
        "scheduled_ts": to_ns("2024-03-15 09:00"), "scheduled_reason": "mapped"}
    assert "XYZUSDT" in m[0]["evidence"] and len(m[0]["evidence"]) <= 200
    json.dumps(rep, allow_nan=False)  # machine-readable


def test_no_match_reasons_are_reported():
    coin = DELIST_BODY + "<p>COIN-M XYZUSD perpetual contracts are not affected.</p>"
    vs = [capture("20240308095620", page(DELIST_TITLE, coin)),
          capture("20240308095621", page("Binance Will Delist XYZ", "<p>Spot XYZ/USDT will be delisted.</p>",
                                         code=OTHER, related=()),
                  f"https://www.binance.com/en/support/announcement/{OTHER}")]
    rep = metaingest.build_notices(vs, INSTS).report
    by = {r["notice_id"]: r for r in rep["versions"]}
    assert by[f"en:{CODE}"]["matches"][0]["reason"] == "coin_m_veto:coin-m"
    assert by[f"en:{CODE}"]["matches"][0]["match"] is False
    assert by[f"en:{OTHER}"]["matches"] == [] and by[f"en:{OTHER}"]["match_reason"] == (
        "no_universe_symbol_token")
    assert rep["matched_instances"] == []


def test_whole_page_scope_counts_related_article_tokens_and_shows_the_evidence():
    """AMB-39 (Stage-0 port): the text is the whole page, so a universe symbol named only by a related
    article on the page matches too; the report shows where (to be judged in the DK-05 validation)."""
    rel = ((OTHER, "Binance Futures Will Launch USDⓈ-M ABCUSDT Perpetual Contract", to_ns("2024-03-01")),)
    rep = metaingest.build_notices([capture("20240308095620", page(DELIST_TITLE, DELIST_BODY, related=rel))],
                                   INSTS).report
    m = {x["symbol"]: x for x in rep["versions"][0]["matches"]}
    assert m["ABCUSDT"]["match"] is True and "Will Launch USDⓈ-M ABCUSDT" in m["ABCUSDT"]["evidence"]
    assert sorted(x["instance"] for x in rep["matched_instances"]) == ["ABCUSDT#0", "XYZUSDT#0"]


def test_capture_in_w_hold_or_m6_is_refused_before_the_opening(tmp_path):
    late = capture("20250701000000", page(DELIST_TITLE, DELIST_BODY))
    with k9.using(k9.K9Log(tmp_path / "k9")) as lg:
        with pytest.raises(guard.HoldoutBlocked):
            metaingest.build_notices([late], INSTS)
        with pytest.raises(guard.HoldoutBlocked):
            MetaStore(tmp_path / "m").put_capture(late)
        m6 = capture("20260701000000", page(DELIST_TITLE, DELIST_BODY))
        with pytest.raises(guard.EmbargoBlocked):
            MetaStore(tmp_path / "m").put_capture(m6)
        with pytest.raises(guard.EmbargoBlocked):
            metaingest.build_notices([m6], INSTS)
        # a capture before W_HOLD whose releaseDate is in W_HOLD: nothing dated >= the W_HOLD start
        pre = capture("20250630000000", page(DELIST_TITLE, DELIST_BODY, release=to_ns("2025-07-02")))
        with pytest.raises(guard.HoldoutBlocked):
            metaingest.build_notices([pre], INSTS)
        assert len(lg.records()) == 5
    assert not list((tmp_path / "m").rglob("*.bin"))


# -------------------------------------------------------------------------------- the fetch layer


def memento(ts: str) -> str:
    when = pd.Timestamp(cap.capture_ts_ns(ts), unit="ns", tz="UTC").to_pydatetime()
    return email.utils.format_datetime(when, usegmt=True)


def serve(t: FakeTransport, rows: list[tuple[str, str, bytes]], *, lie: str | None = None) -> None:
    t.routes[CDX] = Response(200, {}, "".join(f"{ts} {o}\n" for ts, o, _ in rows).encode(), CDX)
    for ts, o, body in rows:
        u = f"https://web.archive.org/web/{ts}id_/{o}"
        t.routes[u] = Response(200, {"Memento-Datetime": memento(lie or ts)}, body, u)


def test_ingest_captures_through_the_fetch_layer_with_memento_check(tmp_path):
    t = FakeTransport()
    body = page(DELIST_TITLE, DELIST_BODY)
    serve(t, [("20240308095620", URL, body)])
    out = tmp_path / "run"
    with k9.using(k9.K9Log(out / "k9")):
        st = MetaStore(tmp_path / "m")
        q = metaingest.CdxQuery(url=URL.replace("https://", ""))
        res = metaingest.ingest_captures(Fetcher(t, out, clock=lambda: RECV), st, [q])
        assert [(r.ts, r.status) for r in res] == [("20240308095620", "stored")]
        assert t.calls[0][1] == {"url": URL.replace("https://", ""), "matchType": "exact",
                                 "to": "20250630235959", "fl": "timestamp,original",
                                 "filter": "statuscode:200"}
        got = st.get_capture("20240308095620", res[0].sha256)
        assert got.body == body and got.original == URL and got.recv_ts == RECV
        # resumable: a stored pair is not fetched again
        n = len(t.calls)
        res2 = metaingest.ingest_captures(Fetcher(t, out, clock=lambda: RECV), st, [q])
        assert [r.status for r in res2] == ["already_stored"] and len(t.calls) == n + 1  # the N5 only
        # timestamp substitution: refused by the fetch layer (K9-logged), discarded, nothing stored
        t2 = FakeTransport()
        serve(t2, [("20240309000000", URL, body)], lie="20240309000001")
        res3 = metaingest.ingest_captures(Fetcher(t2, out, clock=lambda: RECV), st, [q])
        assert res3[0].status == "refused" and "Memento" in res3[0].reason
        assert len(st.captures()) == 1
        assert [r["exception"] for r in k9.K9Log(out / "k9").records()] == ["GuardFailure"]
        # a CDX row at / after 2025-07-01: the batch is a guard failure, no N6 request is made
        t3 = FakeTransport()
        serve(t3, [("20250701000000", URL, body)])
        with pytest.raises(GuardFailure):
            metaingest.ingest_captures(Fetcher(t3, out, clock=lambda: RECV), st, [q])
        assert [c[0] for c in t3.calls] == [CDX]


def test_a_cdx_query_beyond_the_bound_is_refused():
    with pytest.raises(ValueError, match="to"):
        metaingest.CdxQuery(url="binance.com/en/support/announcement/", to="20250701000000").request(None)


# ------------------------------------------------------------------------------------------------ R1


def xinfo(**syms) -> bytes:
    out = []
    for s, f in syms.items():
        d = {"symbol": s, "status": "TRADING", "onboardDate": 1_600_000_000_000, "quoteAsset": "USDT",
             "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                         {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                         {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
        d.update(f)
        out.append(d)
    return json.dumps({"timezone": "UTC", "symbols": out}).encode()


FULL = {"baseAsset": "XYZ", "contractType": "PERPETUAL", "underlyingType": "COIN", "multiplier": 1}


def src(kind: str, ts: int, body: bytes) -> ins.IdentitySource:
    return ins.identity_source(body, kind=kind, ts=ts, recv_ts=RECV, url="u",
                               sha256=hashlib.sha256(body).hexdigest())


def test_identity_source_reads_only_the_e8_whitelist():
    s = src("N4", RECV, xinfo(XYZUSDT=FULL))
    assert dict(s.symbols["XYZUSDT"]) == {"baseAsset": "XYZ", "contractType": "PERPETUAL",
                                          "underlyingType": "COIN", "multiplier": 1}
    assert set(s.symbols["XYZUSDT"]) <= guard.IDENTITY_FIELDS


def test_identity_invariance_pass_and_fail():
    ended = Instance(0, to_ns("2022-01-01"), to_ns("2023-01-01"), to_ns("2023-01-08"))
    live = Instance(1, to_ns("2023-06-01"), None, None)
    insts = {"XYZUSDT": [ended, live]}
    in_ended = src("capture", to_ns("2022-06-01"), xinfo(XYZUSDT=FULL))
    n4 = src("N4", RECV, xinfo(XYZUSDT=FULL))
    r = ins.build_instruments(insts, [in_ended, n4])
    assert [(x.instance.idx, x.included, x.reason) for x in r.rows] == [(0, True, "verified"),
                                                                       (1, True, "verified")]
    assert r.rows[0].sources == (in_ended.ref,) and r.rows[1].sources == (n4.ref,)  # N4 only for the live one
    validate("R1", r.table)
    assert r.table["multiplier"].tolist() == [1.0, 1.0]
    assert r.table["tick"].isna().all() and r.table["step"].isna().all()  # never backdated (E8)
    ident = ins.symbol_identity([x for x in r.rows if x.symbol == "XYZUSDT"])
    assert ident.invariant_verified and ident.base == "XYZ" and ident.multiplier == 1.0
    assert ident.quote_asset is None  # not on the E8 list: AMB-40 (stopped) -> the universe excludes it
    assert instrument_exclusion("XYZUSDT", ident) == "identity_unknown"
    # a conflicting source within the instance -> excluded (DK-15, DEC-31)
    other = src("capture", to_ns("2022-09-01"), xinfo(XYZUSDT={**FULL, "underlyingType": "INDEX"}))
    r2 = ins.build_instruments(insts, [in_ended, other, n4])
    assert [(x.included, x.reason) for x in r2.rows] == [(False, "conflict:underlyingType"),
                                                         (True, "verified")]
    assert r2.table["base"].isna().tolist() == [True, False]
    assert not ins.symbol_identity(list(r2.rows)).invariant_verified
    # no source dated within the ended instance (N4 is dated after its end) -> excluded
    r3 = ins.build_instruments(insts, [n4])
    assert [(x.included, x.reason) for x in r3.rows] == [(False, "no_source_within_instance"),
                                                         (True, "verified")]


def test_missing_multiplier_excludes_the_instrument():
    live = Instance(0, to_ns("2023-06-01"), None, None)
    no_mult = {k: v for k, v in FULL.items() if k != "multiplier"}
    r = ins.build_instruments({"XYZUSDT": [live]}, [src("N4", RECV, xinfo(XYZUSDT=no_mult))])
    assert [(x.included, x.reason) for x in r.rows] == [(False, "missing:multiplier")]
    ident = ins.symbol_identity(list(r.rows))
    assert not ident.invariant_verified
    assert instrument_exclusion("XYZUSDT", ident) == "identity_unverified"
    bad = src("N4", RECV, xinfo(XYZUSDT={**FULL, "multiplier": 0}))
    assert ins.build_instruments({"XYZUSDT": [live]}, [bad]).rows[0].reason == "invalid:multiplier"


def test_filters_only_from_timestamped_captures():
    body = xinfo(XYZUSDT=FULL)
    f = ins.pit_filters(body, known_at=to_ns("2022-06-01"))["XYZUSDT"]
    assert (f.step, f.min_qty, f.min_notional, f.provenance, f.known_at) == (
        0.1, 0.1, 5.0, "pit_historical", to_ns("2022-06-01"))
    cur = ins.current_filters(body)["XYZUSDT"]
    assert cur.provenance == "current_approximation" and cur.known_at is None
    with pytest.raises(CurrentFiltersNotHistorical):
        assert_not_backdated({"XYZUSDT": cur})  # the DEC-21 layer only, never R1 / gates


def test_instances_for_r1_come_from_the_bars():
    t0 = to_ns("2024-01-01")
    ts = np.arange(t0, t0 + 3 * D, 15 * MIN, dtype=np.int64)
    bars = pd.DataFrame({"open_ts": ts, "vol_base": np.ones(len(ts))})
    insts = build_instances(bars, t0 + 20 * D)
    r = ins.build_instruments({"XYZUSDT": insts}, [])
    row = r.table.iloc[0]
    assert (row["first_bar_ts"], row["last_trade_ts"], row["instance_end_known_at"]) == (
        t0, int(ts[-1]) + 15 * MIN, int(ts[-1]) + 15 * MIN + 7 * D)
    assert r.rows[0].reason == "no_source_within_instance"


# ------------------------------------------------------------------------------------------------ R12


def test_r12_is_valid_empty_and_refuses_placeholders(tmp_path):
    st = MetaStore(tmp_path / "m")
    r12 = st.load_table("R12", "W_DEV")
    assert len(r12) == 0 and list(r12.columns) == ["venue", "contract_instance_id", "settlement_ts",
                                                   "settlement_price", "source", "source_timestamp",
                                                   "known_at", "retrieved_at", "sha256"]
    validate_r12(r12)
    assert len(metaingest.r12_table()) == 0
    row = {"venue": "binance", "contract_instance_id": "XYZUSDT#0",
           "settlement_ts": to_ns("2024-03-15 09:00"), "settlement_price": 1.0, "source": "approximate",
           "source_timestamp": to_ns("2024-03-15 09:00"),
           "known_at": to_ns("2024-03-15 09:00"), "retrieved_at": RECV, "sha256": "0" * 64}
    with pytest.raises(IntegrityBlocker, match="exact official"):
        st.put_table("R12", "W_DEV", pd.DataFrame([row]))
    assert len(st.load_table("R12", "W_DEV")) == 0
    with pytest.raises(IntegrityBlocker, match="not ingested"):
        st.load_table("R11", "W_DEV")


# ------------------------------------------------------------------------------------------ end to end


def test_end_to_end_capture_to_u6_activation(tmp_path):
    t = FakeTransport()
    ts = "20240308095620"
    serve(t, [(ts, URL, page(DELIST_TITLE, DELIST_BODY))])
    out = tmp_path / "run"
    with k9.using(k9.K9Log(out / "k9")):
        st = MetaStore(tmp_path / "m")
        metaingest.ingest_captures(Fetcher(t, out, clock=lambda: RECV), st,
                                   [metaingest.CdxQuery(url=URL.replace("https://", ""))])
        n4 = src("N4", RECV, xinfo(XYZUSDT=FULL))
        live = Instance(0, to_ns("2023-01-01"), None, None)
        insts = {"XYZUSDT": [live]}
        res = metaingest.ingest_metadata(st, insts, [n4], scope="W_DEV", report_path=out / "report.json")
        v = res.notices.versions[0]
        ka = to_ns("2024-03-08 09:56:20")
        assert known_at(v) == ka and confident_match(v, "XYZUSDT")
        assert scheduled_ts(v, "XYZUSDT") == to_ns("2024-03-15 09:00")
        u6 = U6State(list(res.notices.versions), "XYZUSDT", [live])
        assert not u6.active(live, ka - 1, lambda x: False)
        assert u6.active(live, ka, lambda x: False)
        assert u6.active(live, ka + 3 * D, lambda x: False)
        assert not u6.active(live, to_ns("2024-03-15 09:00") + 72 * H + MIN, lambda x: False)  # 72 h clear
        # persisted, immutable, re-loadable (versions rebuilt from the stored captures)
        assert st.load_notice_versions("W_DEV") == res.notices.versions
        validate("R1", st.load_table("R1", "W_DEV"))
        assert len(st.load_table("R12", "W_DEV")) == 0
        rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert rep["kind"] == "V7_METADATA_INGEST_REPORT" and rep["outcome_free"] is True
        assert rep["notices"]["counts"]["classified"] == 1
        assert rep["notices"]["matched_instances"] == [
            {"instance": "XYZUSDT#0", "symbol": "XYZUSDT", "idx": 0,
             "versions": [{"notice_id": f"en:{CODE}", "version": 1, "kind": "delisting", "known_at": ka,
                           "scheduled_ts": to_ns("2024-03-15 09:00"), "scheduled_reason": "mapped"}]}]
        assert rep["instruments"]["counts"] == {"instances": 1, "included": 1, "excluded": 0}
        assert rep["r12"] == {"rows": 0, "valid": True}
        assert res.notices_by_symbol == {"XYZUSDT": res.notices.versions}
        # the same scope is immutable
        with pytest.raises(Exception, match="immutable|written once|refusing"):
            st.put_table("R1", "W_DEV", res.instruments.table.assign(base="OTHER"))


def test_r12_exact_official_rows_round_trip(tmp_path):
    st = MetaStore(tmp_path / "m")
    row = {"venue": "binance", "contract_instance_id": "XYZUSDT#0",
           "settlement_ts": to_ns("2024-03-15 09:00"), "settlement_price": 1.25,
           "source": "official_historical",
           "source_timestamp": to_ns("2024-03-15 09:00"), "known_at": to_ns("2024-03-15 09:00"),
           "retrieved_at": RECV, "sha256": "0" * 64}
    st.put_table("R12", "W_DEV", pd.DataFrame([row]))
    got = st.load_table("R12", "W_DEV")
    assert got.to_dict("records") == [row]


def test_n4_and_capture_identity_sources(tmp_path):
    t = FakeTransport()
    body = xinfo(XYZUSDT=FULL)
    t.routes[metaingest.FAPI_EXCHANGE_INFO] = Response(200, {}, body, metaingest.FAPI_EXCHANGE_INFO)
    out = tmp_path / "run"
    src_n4, cur = metaingest.fetch_exchange_info(Fetcher(t, out, clock=lambda: RECV))
    assert (src_n4.kind, src_n4.ts, src_n4.recv_ts) == ("N4", RECV, RECV)
    assert src_n4.sha256 == hashlib.sha256(body).hexdigest() and "status" not in src_n4.symbols["XYZUSDT"]
    assert cur["XYZUSDT"].provenance == "current_approximation"
    c = capture("20220601000000", gzip.compress(body, mtime=0),
                "https://fapi.binance.com/fapi/v1/exchangeInfo")
    src_c, pit = metaingest.identity_from_capture(c)
    assert (src_c.kind, src_c.ts) == ("capture", to_ns("2022-06-01"))
    assert dict(src_c.symbols["XYZUSDT"]) == dict(src_n4.symbols["XYZUSDT"])
    assert pit["XYZUSDT"].known_at == to_ns("2022-06-01") and pit["XYZUSDT"].provenance == "pit_historical"


def test_instances_from_the_raw_store(tmp_path):
    from luna.v7.rawstore import RawStore

    t0 = to_ns("2024-01-01")
    ts = np.arange(t0, t0 + 2 * D, 15 * MIN, dtype=np.int64)
    n = len(ts)
    df = pd.DataFrame({"open_ts": ts, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "vol_base": 1.0,
                       "vol_quote": 1.0, "n_trades": np.ones(n, np.int64), "taker_buy_quote": 0.5,
                       "known_at": ts + 15 * MIN})
    rs = RawStore(tmp_path / "raw")
    rs.put_chunk("R3", "XYZUSDT", "2024-01", df, sources=[])
    got = metaingest.instances_from_store(rs, ["XYZUSDT"], t0, to_ns("2024-02-01"))
    assert got == {"XYZUSDT": build_instances(df[["open_ts", "vol_base"]], to_ns("2024-02-01"))}
    assert got["XYZUSDT"][0].last_trade_ts == int(ts[-1]) + 15 * MIN
