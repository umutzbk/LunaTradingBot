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
                            to_ns("2024-03-01")),), gz: bool = False, nav: str = "") -> bytes:
    art = {"code": code, "title": title, "body": body_html}
    if release is not None:
        art["releaseDate"] = release // 10**6
    app = {"appState": {"loader": {"dataByRouteId": {"article": {
        "articleDetail": art,
        "relatedArticles": [{"code": c, "title": t, "releaseDate": r // 10**6} for c, t, r in related]}}}}}
    js = json.dumps(app, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace(
        "&", "\\u0026")
    html = (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title} | Binance Support</title>'
            f'<meta name="description" content="{title}"></head><body><header><a href="/en">Home</a>{nav}'
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
    assert t.title == DELIST_TITLE and t.article_reason == "ok"  # the article object's own title
    assert t.page_title == DELIST_TITLE  # diagnostic: the page <title> cut at " | " (Stage 0)
    assert t.text.startswith(DELIST_TITLE + "\n")
    assert "Home" not in t.text and "Home" in t.page_text  # the notice's title + body only (S-2.5)
    assert "at 2024-03-15 09:00 (UTC). After that" in t.text  # tags stripped, whitespace collapsed
    assert "Thanks & regards," in t.text and "&amp;" not in t.text  # character references unescaped
    assert "<p>" not in t.text and "\\u003c" not in t.text  # the JSON-embedded article HTML is stripped too
    assert "  " not in t.text
    assert t.release_ts == RELEASE and t.release_reason == "article_release_date"
    assert t.date_keys == ("releaseDate",)
    again = cap.extract(raw, URL)
    assert again == t and t.text_sha256 == hashlib.sha256(t.text.encode()).hexdigest()
    assert cap.extract(page(DELIST_TITLE, DELIST_BODY, gz=True), URL) == t  # gzip transparent


def test_release_date_only_from_inside_the_capture_and_fail_closed():
    # the article object's own date; the related article's releaseDate never wins
    raw = cap.decode(page(DELIST_TITLE, DELIST_BODY))
    assert cap.release_date(raw, URL) == (RELEASE, "article_release_date")
    no_code = "https://www.binance.com/en/support/announcement/some-slug"
    assert cap.release_date(raw, no_code) == (None, "no_article_code")
    other = f"https://www.binance.com/en/support/announcement/{'f' * 32}"
    assert cap.release_date(raw, other) == (None, "no_article_object")
    # the article's own date missing: the related article's date is never taken; the capture is unusable
    t = cap.extract(page(DELIST_TITLE, DELIST_BODY, release=None), URL)
    assert (t.release_ts, t.release_reason, t.text, t.title) == (None, "no_release_date", None, None)
    assert t.page_title == DELIST_TITLE  # diagnostics stay
    a, why = cap.article(cap.decode(page(DELIST_TITLE, DELIST_BODY).replace(b'"body"', b'"bodx"', 1)), URL)
    assert (a, why) == (None, "article_incomplete")


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


@pytest.mark.parametrize(("title", "stem"), [
    ("Binance Futures Postponement of Delisting of USDⓈ-M OMGUSDT", "postpon"),
    ("Binance Futures Cancellation of Delisting of USDⓈ-M OMGUSDT", "cancel"),
    ("Binance Futures Will Delay the Delisting of USDⓈ-M OMGUSDT", "delay"),
    ("Binance Futures Will Not Delist USDⓈ-M OMGUSDT", "not"),
    ("Delisting of USDⓈ-M OMGUSDT Postponed", "postpon"),
    ("Binance Futures Will Extend the Delisting Time of USDⓈ-M OMGUSDT", "extend"),
    ("Binance Futures Will Revoke the Delisting of USDⓈ-M OMGUSDT", "revok"),
    ("Update: Binance Futures Will Delist USDⓈ-M OMGUSDT", "updat"),
])
def test_modifier_veto_never_classifies_a_modified_delisting_title(title, stem):
    kind, why = cap.classify_kind(title)
    assert kind is None and why.startswith("ambiguous_kind:modifier:")
    assert stem in why.split(":")[-1].split("|")


def test_explicit_lists_keep_their_kind_despite_the_modifier_stems():
    pre = "Binance Futures Will "
    assert cap.classify_kind(pre + "Postpone the Delisting of USDⓈ-M OMGUSDT")[0] == "postponement"
    assert cap.classify_kind(pre + "Cancel the Delisting of USDⓈ-M OMGUSDT")[0] == "cancellation"
    assert cap.classify_kind(DELIST_TITLE) == ("delisting", "delisting:delist")


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


def test_only_the_notice_title_and_body_are_matched_the_page_is_a_diagnostic():
    """S-2.5 / DEC-40 "in the archived title or body" (fix round 1, I2): a universe symbol named only by a
    related article is NOT a match and a COIN-M navigation item does NOT veto; both show up as page
    diagnostics in the report."""
    rel = ((OTHER, "Binance Futures Will Launch USDⓈ-M ABCUSDT Perpetual Contract", to_ns("2024-03-01")),)
    raw = page(DELIST_TITLE, DELIST_BODY, related=rel, nav='<a href="/en/futures">COIN-M Futures</a>')
    rec = metaingest.build_notices([capture("20240308095620", raw)], INSTS).report["versions"][0]
    assert [(m["symbol"], m["match"], m["reason"]) for m in rec["matches"]] == [
        ("XYZUSDT", True, "usdm_context:usds-m")]
    assert rec["page"]["symbols_not_in_notice"] == ["ABCUSDT"]
    assert rec["page"]["coin_m_not_in_notice"] == ["coin-m"]
    assert rec["page"]["title"] == DELIST_TITLE


def _with_extra(objs: list) -> bytes:
    """The standard page plus another __APP_DATA-like script holding ``objs`` (catalog / list entries)."""
    js = json.dumps({"catalog": objs}, ensure_ascii=False)
    return page(DELIST_TITLE, DELIST_BODY).replace(
        b"</body>", f'<script type="application/json">{js}</script></body>'.encode(), 1)


def test_catalog_entries_of_the_same_code_without_a_body_are_not_candidates():
    """Round 2, N1: a catalog / "latest" / SEO entry {code, title} of the same article is ignored; only
    same-code objects carrying a body are candidates; two full candidates that disagree stay a conflict."""
    raw = cap.decode(_with_extra([{"code": CODE, "title": DELIST_TITLE},
                                  {"code": CODE, "title": "Short", "releaseDate": 1}]))
    a, why = cap.article(raw, URL)
    assert why == "ok" and a == (DELIST_TITLE, DELIST_BODY, RELEASE // 10**6)
    clash = cap.decode(_with_extra([{"code": CODE, "title": DELIST_TITLE, "body": "<p>other</p>",
                                     "releaseDate": RELEASE // 10**6}]))
    assert cap.article(clash, URL) == (None, "article_conflict")
    only_list = page("x", "y", code=OTHER, related=()).replace(
        b"</body>", f'<script type="application/json">{json.dumps({"l": [{"code": CODE, "title": "T"}]})}'
                    f'</script></body>'.encode(), 1)
    assert cap.article(cap.decode(only_list), URL) == (None, "article_incomplete")


def test_a_capture_without_the_article_object_is_unusable():
    no_obj = page(DELIST_TITLE, DELIST_BODY, code=OTHER, related=())  # the URL names CODE
    rec = metaingest.build_notices([capture("20240308095620", no_obj)], INSTS).report
    assert rec["counts"]["unusable"] == 1 and rec["counts"]["classified"] == 0
    v = rec["versions"][0]
    assert (v["status"], v["article_reason"], v["match_reason"], v["matches"]) == (
        "unusable", "no_article_object", "unusable", [])
    assert v["page"]["symbols_not_in_notice"] == ["XYZUSDT"]


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
        # m4: the store checks the capture time only; the in-capture releaseDate is checked when the notices
        # are built, which refuses it (nothing of it reaches a version / R11)
        st = MetaStore(tmp_path / "m")
        st.put_capture(pre)
        with pytest.raises(guard.HoldoutBlocked):
            metaingest.build_notices([st.get_capture(pre.ts, pre.sha256)], INSTS)


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
        # timestamp substitution: a seal-class refusal of the fetch layer (K9-logged, discarded) aborts
        t2 = FakeTransport()
        serve(t2, [("20240309000000", URL, body)], lie="20240309000001")
        with pytest.raises(GuardFailure, match="Memento"):
            metaingest.ingest_captures(Fetcher(t2, out, clock=lambda: RECV), st, [q])
        assert len(st.captures()) == 1
        assert [r["exception"] for r in k9.K9Log(out / "k9").records()] == ["GuardFailure"]
        # an ordinary fetch error (CDX HTTP 503): non-fatal, recorded and ledgered
        t4 = FakeTransport()
        t4.routes[CDX] = Response(503, {}, b"busy", CDX)
        res4 = metaingest.ingest_captures(Fetcher(t4, out, clock=lambda: RECV), st, [q, q])
        assert [r.status for r in res4] == ["cdx_failed", "cdx_failed"]
        led = [json.loads(x) for x in (out / "INGEST_LEDGER.jsonl").read_text().splitlines()]
        assert [x["status"] for x in led] == ["cdx_failed", "cdx_failed"]
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


FULL = {"baseAsset": "XYZ", "contractType": "PERPETUAL", "underlyingType": "COIN"}  # quoteAsset USDT (xinfo)


def src(kind: str, ts: int, body: bytes) -> ins.IdentitySource:
    return ins.identity_source(body, kind=kind, ts=ts, recv_ts=RECV, url="u",
                               sha256=hashlib.sha256(body).hexdigest())


LIVE = Instance(0, to_ns("2023-06-01"), None, None)


def one(fields: dict, symbol: str = "XYZUSDT") -> ins.InstrumentRow:
    return ins.build_instruments({symbol: [LIVE]}, [src("N4", RECV, xinfo(**{symbol: fields}))]).rows[0]


def test_identity_source_reads_only_the_e8_whitelist():
    s = src("N4", RECV, xinfo(XYZUSDT={**FULL, "multiplier": 1}))
    assert dict(s.symbols["XYZUSDT"]) == {"baseAsset": "XYZ", "contractType": "PERPETUAL",
                                          "underlyingType": "COIN", "quoteAsset": "USDT", "multiplier": 1}
    assert set(s.symbols["XYZUSDT"]) <= guard.IDENTITY_FIELDS
    assert "quoteAsset" in guard.IDENTITY_FIELDS and guard.historical_field("quoteAsset") == "quoteAsset"


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
    assert r.table["multiplier"].tolist() == [1.0, 1.0] and r.table["quote_asset"].tolist() == ["USDT"] * 2
    assert r.table["tick"].isna().all() and r.table["step"].isna().all()  # never backdated (E8)
    ids = ins.instance_identities([x for x in r.rows if x.symbol == "XYZUSDT"])
    assert list(ids) == [0, 1] and all(i.invariant_verified for i in ids.values())
    assert (ids[1].base, ids[1].quote_asset, ids[1].multiplier) == ("XYZ", "USDT", 1.0)
    assert instrument_exclusion("XYZUSDT", ids[1]) is None  # in the instrument set (S-2.1)
    # a conflicting source within the instance -> that instance excluded (DK-15, DEC-31); the other stays
    other = src("capture", to_ns("2022-09-01"), xinfo(XYZUSDT={**FULL, "underlyingType": "INDEX"}))
    r2 = ins.build_instruments(insts, [in_ended, other, n4])
    assert [(x.included, x.reason) for x in r2.rows] == [(False, "conflict:underlyingType"),
                                                         (True, "verified")]
    assert r2.table["base"].isna().tolist() == [True, False]
    ids2 = ins.instance_identities(list(r2.rows))
    assert not ids2[0].invariant_verified and ids2[1].invariant_verified
    # a conflicting source OUTSIDE the instance is about another instance: not a conflict of this one (m1)
    r_out = ins.build_instruments({"XYZUSDT": [live]}, [other, n4])
    assert r_out.rows[0].reason == "verified"
    # no source dated within the ended instance (N4 is dated after its end) -> excluded (literal, AMB-40 (d))
    r3 = ins.build_instruments(insts, [n4])
    assert [(x.included, x.reason) for x in r3.rows] == [(False, "no_source_within_instance"),
                                                         (True, "verified")]


@pytest.mark.parametrize(("base", "mult"), [  # ruling R49
    ("PEPE", 1.0), ("1INCH", 1.0), ("2Z", 1.0), ("1K", 1.0), ("1M", None), ("12X", 1.0),
    ("1000PEPE", 1000.0), ("1000000MOG", 1e6), ("1MBABYDOGE", 1e6), ("1000SATS", 1000.0),
    ("100X", None), ("10000SATS", None), ("10000LADYS", None), ("10X", None), ("1000", None),
    ("1000000", None), ("1000_X", None), ("1M2X", None), ("1M_X", None)])
def test_multiplier_from_the_closed_base_prefix_list(base, mult):
    assert ins.multiplier_of(base) == mult
    row = one({**FULL, "baseAsset": base})
    if mult is None:
        assert (row.included, row.reason) == (False, "invalid:multiplier_prefix")
    else:
        assert row.included and row.identity["multiplier"] == mult


def test_multiplier_and_quote_invariance_and_exclusions():
    assert one({**FULL, "multiplier": 1}).reason == "verified"  # a stated multiplier agreeing
    assert one({**FULL, "baseAsset": "1000XYZ", "multiplier": 1}).reason == "conflict:multiplier"
    assert one({k: v for k, v in FULL.items() if k != "baseAsset"}).reason == "missing:baseAsset"
    # quote asset: missing, conflicting within the instance, not USDT
    assert one({**FULL, "quoteAsset": None}).reason == "missing:quoteAsset"
    a = src("capture", to_ns("2024-01-01"), xinfo(XYZUSDT=FULL))
    b = src("capture", to_ns("2024-02-01"), xinfo(XYZUSDT={**FULL, "quoteAsset": "USDC"}))
    assert ins.build_instruments({"XYZUSDT": [LIVE]}, [a, b]).rows[0].reason == "conflict:quoteAsset"
    usdc = one({**FULL, "quoteAsset": "USDC"}, "XYZUSDC")
    assert usdc.reason == "verified"
    assert instrument_exclusion("XYZUSDC", ins.row_identity(usdc)) == "quote_not_usdt"
    # a base whose multiplier prefix is off the list: excluded, the universe sees it unverified
    bad = one({**FULL, "baseAsset": "10000XYZ"})
    assert instrument_exclusion("XYZUSDT", ins.row_identity(bad)) == "identity_unverified"


def test_per_instance_identity_in_the_universe():
    """I3: the current instance's identity decides; an unverifiable earlier instance does not exclude the
    later verified one (and vice versa)."""
    from luna.v7 import runner, universe
    from luna.v7.universe import Identity
    from tests.v7 import perf_fixtures as pf

    full = pf.build_inputs()
    rel = full["RELUSDT"]
    assert [i.idx for i in rel.insts] == [0, 1]
    ok = Identity("REL", "PERPETUAL", "USDT", "COIN", 1.0, True)
    bad = Identity(None, None, None, None, None, False)
    t0, t1 = pf.START + 65 * D + 8 * H + 5 * MIN, pf.START + 100 * D + 8 * H + 5 * MIN
    for ident, ex0, ex1 in (({0: bad, 1: ok}, True, False), ({0: ok, 1: bad}, False, True),
                            ({0: ok}, False, True)):
        from dataclasses import replace

        u = runner.ResearchUniverse({"BTCUSDT": full["BTCUSDT"], "RELUSDT": replace(rel, identity=ident)})
        r0, r1 = (universe.gates(u.sd["RELUSDT"], t) for t in (t0, t1))
        assert (r0.reasons == ("excluded:identity_unverified",)) is ex0
        assert (r1.reasons == ("excluded:identity_unverified",)) is ex1
    both = replace(rel, identity={0: ok, 1: ok})
    u = runner.ResearchUniverse({"BTCUSDT": full["BTCUSDT"], "RELUSDT": both})
    plain = runner.ResearchUniverse({"BTCUSDT": full["BTCUSDT"], "RELUSDT": replace(rel, identity=ok)})
    for t in (t0, t1):  # an all-verified mapping gives exactly the per-symbol result
        assert universe.gates(u.sd["RELUSDT"], t) == universe.gates(plain.sd["RELUSDT"], t)


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
    got = metaingest.instances_from_store(rs, ["XYZUSDT"], to_ns("2024-02-01"))  # lo: first chunk
    assert got == metaingest.instances_from_store(rs, ["XYZUSDT"], to_ns("2024-02-01"), lo=t0)
    assert got == {"XYZUSDT": build_instances(df[["open_ts", "vol_base"]], to_ns("2024-02-01"))}
    assert got["XYZUSDT"][0].last_trade_ts == int(ts[-1]) + 15 * MIN


def test_identity_sources_from_stored_exchange_info_captures(tmp_path):
    st = MetaStore(tmp_path / "m")
    xi = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    st.put_capture(capture("20220601000000", xinfo(XYZUSDT=FULL), xi))
    st.put_capture(capture("20230101000000", xinfo(XYZUSDT=FULL), xi))
    st.put_capture(capture("20240308095620", page(DELIST_TITLE, DELIST_BODY)))  # a notice: not a source
    srcs, filt = metaingest.identity_sources(st)
    assert [(s.kind, s.ts) for s in srcs] == [("capture", to_ns("2022-06-01")),
                                              ("capture", to_ns("2023-01-01"))]
    assert [f.known_at for f in filt["XYZUSDT"]] == [to_ns("2022-06-01"), to_ns("2023-01-01")]
    r = ins.build_instruments({"XYZUSDT": [Instance(0, to_ns("2022-01-01"), to_ns("2022-12-01"),
                                                    to_ns("2022-12-08"))]}, srcs)
    assert r.rows[0].reason == "verified" and r.rows[0].sources == (srcs[0].ref,)


# ------------------------------------------------------------------------------- E6 (after the opening)


@pytest.fixture(scope="module")
def _opening(tmp_path_factory):
    from tests.v7 import auth_fixtures as af

    return af.opening_once(tmp_path_factory.mktemp("opening"))


def test_e6_authorized_capture_path(_opening, monkeypatch, tmp_path):
    """m7: with the verified authorization a W_HOLD capture (< 2026-07-01) is fetched under the raised E6
    bound, stored, read and built into a version; without it every step is refused."""
    from tests.v7 import auth_fixtures as af

    o = af.use_opening(monkeypatch, _opening)
    ts = "20260115000000"
    body = page(DELIST_TITLE, DELIST_BODY.replace("2024-03-15 09:00", "2026-01-20 09:00"),
                release=to_ns("2026-01-14 09:00"))
    t = FakeTransport()
    serve(t, [(ts, URL, body)])
    out = tmp_path / "run"
    q = metaingest.CdxQuery(url=URL.replace("https://", ""), to="20260630235959")
    with pytest.raises(ValueError, match="to"):
        q.request(None)  # the raised bound needs the authorization
    with k9.using(k9.K9Log(out / "k9")):
        st = MetaStore(tmp_path / "m")
        res = metaingest.ingest_captures(Fetcher(t, out, clock=lambda: RECV), st, [q], auth=o.auth)
        assert [r.status for r in res] == ["stored"]
        with pytest.raises(guard.HoldoutBlocked):
            st.get_capture(ts, res[0].sha256)
        c = st.get_capture(ts, res[0].sha256, auth=o.auth)
        insts = {"XYZUSDT": [Instance(0, to_ns("2025-01-01"), None, None)]}
        with pytest.raises(guard.HoldoutBlocked):
            metaingest.build_notices([c], insts)
        nb = metaingest.build_notices([c], insts, auth=o.auth)
        v = nb.versions[0]
        assert known_at(v) == to_ns("2026-01-15") and scheduled_ts(v, "XYZUSDT") == to_ns("2026-01-20 09:00")
        st.put_table("R11", "W_HOLD", nb.table, auth=o.auth)
        with pytest.raises(guard.HoldoutBlocked):
            st.load_table("R11", "W_HOLD")
        assert st.load_notice_versions("W_HOLD", auth=o.auth) == nb.versions
