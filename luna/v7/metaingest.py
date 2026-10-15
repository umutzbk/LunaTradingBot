"""Metadata ingestion: R11 notices from bounded Wayback captures, R1 instruments with E8 identity, R12 empty
by design, and the outcome-free ingest report (spec S-1.7 N4-N6 / E6 / E8, S-2.1, S-2.2, S-2.5, §3 R1 / R11
/ R12, EC-7, DEC-31 / DEC-36 / DEC-37 / DEC-40 / DEC-43 / DEC-44; DK-05 / DK-06 / DK-07 / DK-15; SC-03;
Task 26c, AMB-39 / AMB-40).

* ``ingest_captures(fetcher, store, queries)``: every ``CdxQuery`` is an N5 request with ``to`` <= the
  mode's bound (20250630235959 before the opening; the E6 bound only with the verified authorization), sent
  through the ONE fetch layer, which registers the returned (ts, original) pairs (a row beyond the bound is a
  guard failure: raised, nothing of the batch kept). Each pair is then fetched once as N6 (exact pair, no
  redirect, HTTP 200, ``Memento-Datetime`` == ts) and stored immutably in the ``MetaStore`` (sha256, capture
  ts, original URL, ``recv_ts``). A seal-class refusal (redirect, memento substitution, a bound, W_HOLD / M6:
  ``GuardFailure`` / ``HoldoutBlocked`` / ``EmbargoBlocked``) is K9-logged by the fetch layer and ABORTS the
  run (raised; fix round 1, m5); an ordinary fetch error (``FetchError``, no content) is non-fatal, recorded
  as ``cdx_failed`` / ``fetch_failed`` and ledgered in ``<out_dir>/INGEST_LEDGER.jsonl``. A pair already
  stored is not fetched again. Runs under the fetcher's K9 log. A stored capture's own ``releaseDate`` is
  checked when the notices are built (``build_notices`` refuses one in W_HOLD / M6; fix round 1, m4).
* ``build_notices(captures, insts_by_symbol)``: every capture time and every ``releaseDate`` passes
  ``guard.check_data_span`` (W_HOLD refused before the opening, M6 before R_V6: raised, K9-logged). Captures
  are grouped by notice identity (``captures.notice_id``) and ordered by (capture time, sha256); a capture
  whose extracted text differs from the previous capture's starts a new version (numbered 1, 2, ... per
  notice; a later archived revision is a new version, S-2.5 / DEC-36; AMB-39 (d)) with the first such
  capture's time as ``capture_ts`` and its sha256. A version is *unusable* without the notice's own article
  object (title, body, ``releaseDate``; ``captures.article``), *unclassified* when the closed kind list does
  not classify its title; only *classified* versions become ``NoticeVersion`` / R11 rows. Matching
  (``notices.match_reason``) is against the universe symbols that occur as tokens of the notice's own title +
  body (S-2.5 / DEC-40; fix round 1, I2: the whole page is a diagnostic only); a match is attributed to the
  instance live at the version's ``known_at`` (``instances.started_instance``, as ``U6State``);
  ``scheduled_delist_ts`` per matched instance comes from ``notices.scheduled_reason`` (delisting versions
  only).
* ``ingest_metadata(store, insts_by_symbol, sources, scope=..., report_path=...)``: the stored announcement
  captures (original URL under ``/support/announcement/``; the store also holds ``exchangeInfo`` captures) ->
  R11, the instances + identity sources -> R1 (``data.instruments``), R12 = the empty table (no exact
  official settlement source exists, DEC-43 / DK-07; never a placeholder), the tables written once per scope
  and the ingest report written (exclusive create) as canonical JSON.
* The report (``ingest_report``) is outcome-free: no price, return or label is read. Per version: status,
  kind and the phrase that decided it, title, release / capture / known times and the release-date source
  (``release_src``, R50), the JSON date keys seen, every universe symbol occurring as a token with match /
  no match, its reason, a short evidence snippet, the attributed instance and ``scheduled_ts`` or null with
  its reason; per matched instance its versions; per instance R1 included / excluded with the reason and the
  sources used.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from luna.v7 import guard, k9, netguard
from luna.v7.data import captures as cap
from luna.v7.data import instruments as ins
from luna.v7.data.instances import Instance, build_instances, started_instance
from luna.v7.data.notices import (
    _SYM_RE,
    COIN_M_PHRASES,
    USDM_PAIR,
    USDM_PHRASES,
    NoticeVersion,
    _token_re,
    known_at,
    match_reason,
    scheduled_reason,
)
from luna.v7.deploy import Filters
from luna.v7.fetch import Fetcher, FetchError
from luna.v7.ingest import _ledger
from luna.v7.metastore import MetaStore, empty_table
from luna.v7.rawstore import RawStore, TableLoader, _canonical, _create_once
from luna.v7.universe import instrument_exclusion

__all__ = ["CaptureOutcome", "CdxQuery", "MetaResult", "NoticeBuild", "REPORT_FORMAT", "build_notices",
           "fetch_exchange_info", "identity_from_capture", "identity_sources", "ingest_captures",
           "ingest_metadata", "ingest_report", "instances_from_store", "notices_by_symbol", "r12_table"]

REPORT_FORMAT = 1
CDX_FIELDS = "timestamp,original"
FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
EVIDENCE_CHARS = 80
MAX_TOKENS = 50
_WORD = re.compile(r"[A-Za-z0-9_]+")


# ------------------------------------------------------------------------------------------ N5 / N6


@dataclass(frozen=True)
class CdxQuery:
    """One bounded N5 query (``url`` without scheme, ``match_type`` exact / prefix)."""

    url: str
    match_type: str = "exact"
    from_ts: str | None = None
    to: str = "20250630235959"
    filter: str | None = "statuscode:200"
    limit: int | None = None

    def request(self, auth: guard.HoldoutAuthorization | None) -> netguard.Request:
        bound = netguard.bounds(auth).cdx_to_max
        if not re.fullmatch(r"\d{14}", self.to or "") or self.to > bound:
            raise ValueError(f"CDX query `to` {self.to!r} must be 14 digits <= {bound}")
        if self.match_type not in ("exact", "prefix"):
            raise ValueError(f"matchType {self.match_type!r}")
        p = {"url": self.url, "matchType": self.match_type, "to": self.to, "fl": CDX_FIELDS}
        if self.from_ts is not None:
            p["from"] = self.from_ts
        if self.filter is not None:
            p["filter"] = self.filter
        if self.limit is not None:
            p["limit"] = str(int(self.limit))
        return netguard.Request("https://web.archive.org/cdx/search/cdx", p)


@dataclass(frozen=True)
class CaptureOutcome:
    query: str
    ts: str | None
    original: str | None
    status: str  # stored | already_stored | cdx_failed | fetch_failed
    reason: str
    sha256: str | None = None

    def as_dict(self) -> dict:
        return {"query": self.query, "ts": self.ts, "original": self.original, "status": self.status,
                "reason": self.reason, "sha256": self.sha256}


def _ledgered(fetcher: Fetcher, o: CaptureOutcome) -> CaptureOutcome:
    _ledger(fetcher, {"kind": "wayback_capture", **o.as_dict()})
    return o


def ingest_captures(fetcher: Fetcher, store: MetaStore, queries: Sequence[CdxQuery], *,
                    auth: guard.HoldoutAuthorization | None = None) -> list[CaptureOutcome]:
    """N5 -> N6 -> immutable store (module docstring)."""
    out: list[CaptureOutcome] = []
    with k9.using(fetcher.k9):
        stored = {(e["ts"], e["original"]) for e in store.captures()}
        for q in queries:
            try:
                rows = fetcher.fetch(q.request(auth), auth=auth).cdx_pairs
            except FetchError as e:  # an ordinary fetch error: non-fatal, ledgered
                out.append(_ledgered(fetcher, CaptureOutcome(q.url, None, None, "cdx_failed", str(e))))
                continue
            for ts, original in sorted(set(rows)):
                if (ts, original) in stored:
                    out.append(CaptureOutcome(q.url, ts, original, "already_stored", "stored earlier"))
                    continue
                url = f"https://web.archive.org/web/{ts}id_/{original}"
                # a seal-class GuardFailure (redirect, memento substitution, bound) is raised: run aborted
                try:
                    f = fetcher.fetch(netguard.Request(url), auth=auth)
                except FetchError as e:  # an ordinary fetch error (no content): non-fatal, ledgered
                    o = CaptureOutcome(q.url, ts, original, "fetch_failed", str(e))
                    out.append(_ledgered(fetcher, o))
                    continue
                c = cap.Capture(ts, original, f.body or b"", f.sha256, f.recv_ts)
                store.put_capture(c, auth=auth)
                stored.add((ts, original))
                out.append(CaptureOutcome(q.url, ts, original, "stored", "ok", c.sha256))
    return out


def fetch_exchange_info(fetcher: Fetcher, *, auth: guard.HoldoutAuthorization | None = None
                        ) -> tuple[ins.IdentitySource, dict[str, Filters]]:
    """N4: the current ``exchangeInfo`` reduced to the E8 identity whitelist (``ts`` = its ``recv_ts``) and
    its current filters for the DEC-21 approximation layer only. The raw body is not kept (its sha256 and
    ``recv_ts`` are in the fetch log)."""
    with k9.using(fetcher.k9):
        f = fetcher.fetch(netguard.Request(FAPI_EXCHANGE_INFO), auth=auth)
    body = f.body or b""
    src = ins.identity_source(body, kind="N4", ts=f.recv_ts, recv_ts=f.recv_ts, url=f.url, sha256=f.sha256)
    return src, ins.current_filters(body)


def identity_from_capture(c: cap.Capture) -> tuple[ins.IdentitySource, dict[str, Filters]]:
    """A timestamped ``exchangeInfo`` capture: identity (``ts`` = the capture time) and point-in-time filters
    (``known_at`` = the capture time)."""
    src = ins.identity_source(cap.decode(c.body).encode("utf-8"), kind="capture", ts=c.capture_ts,
                              recv_ts=c.recv_ts if c.recv_ts is not None else c.capture_ts,
                              url=c.archive_url, sha256=c.sha256)
    return src, ins.pit_filters(cap.decode(c.body).encode("utf-8"), known_at=c.capture_ts)


def identity_sources(store: MetaStore, *, auth: guard.HoldoutAuthorization | None = None
                     ) -> tuple[list[ins.IdentitySource], dict[str, tuple[Filters, ...]]]:
    """The identity sources and point-in-time filters of every stored ``exchangeInfo`` capture (original URL
    ending in ``/fapi/v1/exchangeInfo``), in capture order; filters per symbol sorted by ``known_at``."""
    srcs: list[ins.IdentitySource] = []
    filt: dict[str, list[Filters]] = {}
    for e in store.captures():
        if not e["original"].split("?", 1)[0].endswith("/fapi/v1/exchangeInfo"):
            continue
        src, pit = identity_from_capture(store.get_capture(e["ts"], e["sha256"], auth=auth))
        srcs.append(src)
        for s, f in pit.items():
            filt.setdefault(s, []).append(f)
    return srcs, {s: tuple(sorted(v, key=lambda f: int(f.known_at or 0))) for s, v in sorted(filt.items())}


# --------------------------------------------------------------------------------------- notices


@dataclass(frozen=True)
class NoticeBuild:
    versions: tuple[NoticeVersion, ...]  # classified versions, sorted by (notice_id, version)
    table: pd.DataFrame  # R11
    report: dict


def _check_time(ns: int, what: str, auth) -> None:
    guard.check_data_span(int(ns), int(ns) + 1, auth=auth, what=what)


def _evidence(text: str, symbol: str) -> str:
    m = _token_re(symbol).search(text)
    if m is None:
        return ""
    return text[max(0, m.start() - EVIDENCE_CHARS): m.end() + EVIDENCE_CHARS].replace("\n", " ")


def _js(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_notices(captures: Sequence[cap.Capture], insts_by_symbol: Mapping[str, Sequence[Instance]], *,
                  auth: guard.HoldoutAuthorization | None = None, venue: str = ins.VENUE) -> NoticeBuild:
    """Versions, R11 and the notice part of the report (module docstring)."""
    k9.require()
    for c in captures:
        _check_time(c.capture_ts, f"wayback capture {c.ts} {c.original}", auth)
    groups: dict[str, list[cap.Capture]] = {}
    for c in captures:
        groups.setdefault(cap.notice_id(c.original), []).append(c)
    universe = {s: list(v) for s, v in insts_by_symbol.items()}
    versions: list[NoticeVersion] = []
    recs: list[dict] = []
    rows: list[dict] = []
    for nid in sorted(groups):
        chain: list[tuple[cap.Capture, cap.CaptureText, int]] = []
        for c in sorted(groups[nid], key=lambda c: (c.ts, c.sha256)):
            t = cap.extract(c.body, c.original)
            if chain and chain[-1][1].text_sha256 == t.text_sha256:
                chain[-1] = (chain[-1][0], chain[-1][1], chain[-1][2] + 1)
                continue
            chain.append((c, t, 1))
        for n, (c, t, n_caps) in enumerate(chain, start=1):
            if t.release_ts is not None:
                _check_time(t.release_ts, f"releaseDate of {nid} v{n}", auth)
            kind, kind_reason = cap.classify_kind(t.title) if t.text is not None else (None, "unusable")
            status = "unusable" if t.text is None else ("classified" if kind else "unclassified")
            text = t.text or ""
            # internal (matching / known_at only): only a classified version with a release date is emitted
            v = NoticeVersion(nid, n, kind or "delisting", t.release_ts or 0, c.capture_ts, text, c.sha256)
            ka = None if t.release_ts is None else known_at(v)
            words = set(_WORD.findall(text))
            cands = sorted(s for s in universe if s in words)
            page_words = set(_WORD.findall(t.page_text))
            page_lc = unicodedata.normalize("NFKC", t.page_text).casefold()
            text_lc = unicodedata.normalize("NFKC", text).casefold()
            matches = []
            for s in cands:
                ok, why = match_reason(v, s)
                inst = started_instance(universe[s], ka) if ok and ka is not None else None
                if not ok:
                    sched, sr = None, "no_confident_match"
                elif status != "classified" or kind != "delisting":
                    sched, sr = None, "not_a_delisting"
                else:
                    sched, sr = scheduled_reason(v, s)
                matches.append({"symbol": s, "match": ok, "reason": why,
                                "instance": None if inst is None else f"{s}#{inst.idx}",
                                "instance_reason": ("attributed" if inst is not None else
                                                    "no_match" if not ok else
                                                    "no_known_at" if ka is None else
                                                    "no_instance_live_at_known_at"),
                                "scheduled_ts": sched, "scheduled_reason": sr,
                                "evidence": _evidence(text, s)})
            rec = {"notice_id": nid, "version": n, "status": status, "kind": kind, "kind_reason": kind_reason,
                   "title": t.title, "article_reason": t.article_reason, "release_ts": t.release_ts,
                   "release_reason": t.release_reason, "release_src": t.release_src,
                   "date_keys": list(t.date_keys), "capture_ts": c.capture_ts, "known_at": ka,
                   "archive_url": c.archive_url, "capture_sha256": c.sha256, "text_sha256": t.text_sha256,
                   "n_captures": n_caps, "matches": matches,
                   "match_reason": ("unusable" if t.text is None else
                                    "matched" if any(m["match"] for m in matches) else
                                    "no_match" if matches else "no_universe_symbol_token"),
                   # diagnostics only (never matched): what the whole page adds to the notice's title + body
                   "page": {"title": t.page_title,
                            "symbols_not_in_notice": sorted(s for s in universe
                                                            if s in page_words and s not in words),
                            "coin_m_not_in_notice": [p for p in COIN_M_PHRASES
                                                     if p in page_lc and p not in text_lc]}}
            if not matches:
                rec["symbol_tokens"] = sorted({m.group(0) for m in _SYM_RE.finditer(text)})[:MAX_TOKENS]
            recs.append(rec)
            if status != "classified":
                continue
            versions.append(v)
            hit = {m["instance"]: m["scheduled_ts"] for m in matches if m["instance"] is not None}
            rows.append({"venue": venue, "notice_id": nid, "version": n, "kind": kind,
                         "matched_instances": _js(sorted(hit)), "releaseDate": t.release_ts,
                         "archive_url": c.archive_url, "capture_ts": c.capture_ts, "content_sha256": c.sha256,
                         "scheduled_delist_ts": _js(hit)})
    table = pd.DataFrame(rows, columns=list(empty_table("R11").columns))
    table = table.astype({"version": "int64", "releaseDate": "int64", "capture_ts": "int64"}) if rows else \
        empty_table("R11")
    by_inst: dict[str, dict] = {}
    for r in recs:
        if r["status"] != "classified":
            continue
        for m in r["matches"]:
            if m["instance"] is None:
                continue
            e = by_inst.setdefault(m["instance"], {
                "instance": m["instance"], "symbol": m["symbol"], "idx": int(m["instance"].rsplit("#", 1)[1]),
                "versions": []})
            e["versions"].append({"notice_id": r["notice_id"], "version": r["version"], "kind": r["kind"],
                                  "known_at": r["known_at"], "scheduled_ts": m["scheduled_ts"],
                                  "scheduled_reason": m["scheduled_reason"]})
    matched = []
    for k in sorted(by_inst, key=lambda k: (by_inst[k]["symbol"], by_inst[k]["idx"])):
        e = by_inst[k]
        e["versions"].sort(key=lambda x: (x["known_at"], x["notice_id"], x["version"]))
        matched.append(e)
    kinds = {k: sum(1 for r in recs if r["status"] == "classified" and r["kind"] == k)
             for k in ("cancellation", "delisting", "postponement")}
    counts = {"captures": len(captures), "versions": len(recs),
              "classified": sum(r["status"] == "classified" for r in recs),
              "unclassified": sum(r["status"] == "unclassified" for r in recs),
              "unusable": sum(r["status"] == "unusable" for r in recs), "by_kind": kinds}
    return NoticeBuild(tuple(versions), table,
                       {"counts": counts, "versions": recs, "matched_instances": matched})


def notices_by_symbol(nb: NoticeBuild) -> dict[str, tuple[NoticeVersion, ...]]:
    """Per symbol, the classified versions matched to one of its instances (for ``SymbolMeta.notices``)."""
    keyed = {(v.notice_id, v.version): v for v in nb.versions}
    out: dict[str, list[NoticeVersion]] = {}
    for r in nb.report["versions"]:
        if r["status"] != "classified":
            continue
        v = keyed[(r["notice_id"], r["version"])]
        for m in r["matches"]:
            if m["instance"] is not None and v not in out.get(m["symbol"], []):
                out.setdefault(m["symbol"], []).append(v)
    return {s: tuple(v) for s, v in sorted(out.items())}


# -------------------------------------------------------------------------------------------- R1 / R12


def instances_from_store(store: RawStore, symbols: Sequence[str], hi: int, *, lo: int | None = None,
                         auth: guard.HoldoutAuthorization | None = None) -> dict[str, list[Instance]]:
    """Instances of each symbol from its R3 bars of [lo, hi) (``data_end`` = hi; ``build_instances``), read
    through the guarded loader (open_ts and vol_base only). ``lo`` must be the history start (S-2.2: a
    symbol's first instance starts at its first archived bar; a later ``lo`` would cut an instance and move
    its ``first_bar_ts``); by default it is derived per symbol: the start of its first stored R3 chunk (fix
    round 1, m6)."""
    ld = TableLoader(store, "R3", auth=auth)
    out = {}
    for s in sorted(symbols):
        first = ld.first_period(s)
        if first is None:
            continue
        start = first if lo is None else int(lo)
        if start >= int(hi):
            continue
        a = ld.arrays(s, start, hi, ["vol_base"])
        insts = build_instances(pd.DataFrame({"open_ts": a["open_ts"], "vol_base": a["vol_base"]}), int(hi))
        if insts:
            out[s] = insts
    return out


def r12_table() -> pd.DataFrame:
    """R12 terminal settlements: empty and schema-correct (EC-7; no exact official source exists, DEC-43)."""
    return empty_table("R12")


# ------------------------------------------------------------------------------------------- the report


def ingest_report(notices: NoticeBuild, instruments: ins.Instruments, r12: pd.DataFrame, *, scope: str,
                  capture_outcomes: Sequence[CaptureOutcome] = (), tables: Mapping | None = None) -> dict:
    """The outcome-free ingest report (module docstring). Per instance: the R1 identity result and the S-2.1
    universe exclusion of that instance (``universe.instrument_exclusion`` on its own identity; null =
    in the instrument set), with counts per reason (fix round 1, A3 / m2: for the C6 dry run)."""
    rows = [{"symbol": r.symbol, "instance": r.instance.idx, "first_bar_ts": r.instance.first_bar_ts,
             "last_trade_ts": r.instance.last_trade_ts, "instance_end_known_at": r.instance.end_known_at,
             "included": r.included, "reason": r.reason, "sources": list(r.sources),
             "universe_exclusion": instrument_exclusion(r.symbol, ins.row_identity(r))}
            for r in instruments.rows]
    excl: dict[str, int] = {}
    uni: dict[str, int] = {}
    for r in rows:
        if not r["included"]:
            excl[r["reason"]] = excl.get(r["reason"], 0) + 1
        k = r["universe_exclusion"] or "in_instrument_set"
        uni[k] = uni.get(k, 0) + 1
    return {
        "kind": "V7_METADATA_INGEST_REPORT", "format": REPORT_FORMAT, "outcome_free": True, "scope": scope,
        "rules": {"kind_phrases": {"cancellation": list(cap.CANCELLATION_PHRASES),
                                   "postponement": list(cap.POSTPONEMENT_PHRASES),
                                   "delisting": list(cap.DELISTING_PHRASES)},
                  "context_phrases": {"usdm": list(USDM_PHRASES), "usdm_pair": list(USDM_PAIR),
                                      "coin_m_veto": list(COIN_M_PHRASES)},
                  "identity_fields": sorted(guard.IDENTITY_FIELDS)},
        "captures": [o.as_dict() for o in capture_outcomes],
        "notices": notices.report,
        "instruments": {"counts": {"instances": len(rows), "included": sum(r["included"] for r in rows),
                                   "excluded": sum(not r["included"] for r in rows)},
                        "excluded_by_reason": dict(sorted(excl.items())),
                        "universe_by_reason": dict(sorted(uni.items())), "rows": rows},
        "r12": {"rows": int(len(r12)), "valid": True},
        "tables": dict(tables or {}),
    }


@dataclass(frozen=True)
class MetaResult:
    notices: NoticeBuild
    instruments: ins.Instruments
    report: dict
    notices_by_symbol: Mapping[str, tuple[NoticeVersion, ...]]


def ingest_metadata(store: MetaStore, insts_by_symbol: Mapping[str, Sequence[Instance]],
                    sources: Sequence[ins.IdentitySource], *, scope: str, report_path: str | Path,
                    auth: guard.HoldoutAuthorization | None = None,
                    capture_outcomes: Sequence[CaptureOutcome] = ()) -> MetaResult:
    """The stored notice captures -> R11, instances + identity sources -> R1, R12 empty; tables written once
    per ``scope``; the report written (exclusive create) to ``report_path`` (module docstring)."""
    k9.require()
    caps = [store.get_capture(e["ts"], e["sha256"], auth=auth) for e in store.captures()]
    caps = [c for c in caps if "/support/announcement/" in c.original]
    nb = build_notices(caps, insts_by_symbol, auth=auth)
    instr = ins.build_instruments(insts_by_symbol, sources)
    tables = {"R11": store.put_table("R11", scope, nb.table, auth=auth),
              "R1": store.put_table("R1", scope, instr.table, auth=auth)}
    r12 = store.load_table("R12", scope, auth=auth)  # validated; empty unless exact official rows were stored
    rep = ingest_report(nb, instr, r12, scope=scope, capture_outcomes=capture_outcomes, tables=tables)
    p = Path(report_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _create_once(p, _canonical(rep))
    return MetaResult(nb, instr, rep, notices_by_symbol(nb))
