"""Validation C4 (outcome-free): the FROZEN-candidate notice path (``luna.v7.data.captures`` extraction and
closed-list kind, ``luna.v7.data.notices`` ``confident_match`` / ``scheduled_ts``, ``luna.v7.metaingest``
``build_notices``; AMB-05 / DK-05 / DK-06) run offline over every Stage-0 Wayback announcement capture.

    python scripts/v7/validate_notices_stage0.py var/v7/stage0 [--out docs/results/v7-impl] [--before OLD.json]

Inputs (read only): ``<stage0>/m31/captures/*.bin`` (each capture's timestamp is checked < 2025-07-01 from its
file name and through ``guard.check_data_span`` BEFORE its body is read; a refused capture is recorded and its
body never read) and the committed Stage-0 results under ``docs/results/v7-stage0`` (capture -> original URL
from ``m31/notices.json``, else ``wayback_prepub_check.json`` + ``wayback_notice_lag.json``; instance timing from
``m31/events.json`` and ``symbol_trading_history.csv``; terminations from ``m31/classification.json`` and
``delisting_coverage.csv``; catalog titles from ``wayback_notice_lag.json``). No prices, labels, P&L or
outcomes are read; no network; nothing is written under var/ (the captures go into a temporary ``MetaStore``
in a temporary directory, K9 log there too).

Run: ONE run, ``frozen`` = the FROZEN-candidate code path exactly as committed (Task 26d: R50 release date,
R51 node-tree body text, R52 DK-05 rule); no wrapper, no monkeypatch, no luna/v7 code is changed by this
script. ``--before`` (optional): a previous C4 JSON (the pre-Task-26d run); only its count sections are copied
into ``before`` (with its git head and file sha256) so the report can show before -> after.

Deterministic: inputs sorted, no clock / random in the output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from luna.v7 import guard, k9, metaingest  # noqa: E402
from luna.v7.data import captures as cap  # noqa: E402
from luna.v7.data.instances import Instance, started_instance  # noqa: E402
from luna.v7.data.notices import COIN_M_PHRASES  # noqa: E402
from luna.v7.metastore import MetaStore  # noqa: E402
from luna.v7.timeutil import MIN, D, H, to_ns  # noqa: E402

S0 = REPO / "docs" / "results" / "v7-stage0"
CUTOFF_TS14 = "20250701000000"
BAR = 15 * MIN
DORMANCY_D = 7
CLEAR_H = 72
CODE_FILES = ("luna/v7/data/captures.py", "luna/v7/data/notices.py", "luna/v7/metaingest.py",
              "luna/v7/metastore.py", "luna/v7/data/instances.py")
DOC_INPUTS = ("m31/notices.json", "m31/classification.json", "m31/events.json", "delisting_coverage.csv",
              "symbol_trading_history.csv", "wayback_notice_lag.json", "wayback_prepub_check.json")
_WORD = re.compile(r"[A-Za-z0-9_]+")
# Token swap / rebranding / redenomination titles (normative item 2, reported only; never classified here).
_SWAP = re.compile(r"token swap|rebranding|redenomination|\bswap\b", re.IGNORECASE)


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def iso(ns: int | None) -> str | None:
    return None if ns is None else pd.Timestamp(int(ns), unit="ns", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def jload(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- inputs


def url_map() -> dict[tuple[str, str], tuple[str, str]]:
    """(capture ts, full sha256) -> (original URL, source) from the committed Stage-0 results."""
    m: dict[tuple[str, str], tuple[str, str]] = {}
    for e in jload(S0 / "m31" / "notices.json"):
        for x in e["notices"]:
            if x.get("sha256") and x.get("capture_ts"):
                m.setdefault((x["capture_ts"], x["sha256"]), (x["url"], "m31/notices.json"))
    lag = {x["code"]: x for x in jload(S0 / "wayback_notice_lag.json")}
    for x in jload(S0 / "wayback_prepub_check.json"):
        if x.get("sha256") and x.get("code") in lag and lag[x["code"]].get("first_capture_ts") == x["capture_ts"]:
            m.setdefault((x["capture_ts"], x["sha256"]),
                         (lag[x["code"]]["first_capture_url"], "wayback_prepub_check.json+wayback_notice_lag.json"))
    return m


def universe() -> tuple[dict[str, list[Instance]], dict[str, str]]:
    """Instances per symbol from committed listing / instance timing only (no bars read): events.json
    instances where present (exact Stage-0 instances), else one instance from symbol_trading_history.csv."""
    out: dict[str, list[Instance]] = {}
    how: dict[str, str] = {}
    for e in jload(S0 / "m31" / "events.json"):
        out.setdefault(e["symbol"], []).append(Instance(int(e["instance"]), to_ns(e["instance_start"]),
                                                        to_ns(e["last_trade_bar_close"]), to_ns(e["end_known_at"])))
        how[e["symbol"]] = "m31/events.json" + (" (a later re-listed instance is not in events.json: none modelled)"
                                                if e["resumed_as_new_instance"] else "")
    for s in out:
        out[s].sort(key=lambda i: i.idx)
    with open(S0 / "symbol_trading_history.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            s = r["s"]
            if s in out:
                continue
            first = int(r["first_trade"]) * 10**6
            if r["delisted_in_wdev"] == "True":
                last = int(r["last_trade"]) * 10**6 + BAR
                inst = Instance(0, first, last, last + DORMANCY_D * D)
            else:
                inst = Instance(0, first, None, None)
            out[s] = [inst]
            how[s] = "symbol_trading_history.csv (single instance" + (
                f"; {r['dormant_resumptions']} dormant resumption(s) not modelled)" if r["dormant_resumptions"] != "0"
                else ")")
    return dict(sorted(out.items())), how


def terminations(insts: dict[str, list[Instance]]) -> list[dict]:
    rows: dict[tuple[str, int], dict] = {}
    for c in jload(S0 / "m31" / "classification.json"):
        rows[(c["symbol"], int(c["instance"]))] = {
            "symbol": c["symbol"], "instance": int(c["instance"]), "stage0_class": c["class"],
            "stage0_source": "m31/classification.json", "last_trade": c["last_traded_bar_close"],
            "stage0_notice_url": c.get("notice_url"), "stage0_capture_ts": c.get("capture_ts_utc"),
            "stage0_scheduled_utc": c.get("scheduled_settlement_utc"), "stage0_reason": c["class_reason"]}
    with open(S0 / "delisting_coverage.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            s = r["symbol"]
            if any(k[0] == s and v["last_trade"][:10] == r["last_trade"] for k, v in rows.items()):
                for k, v in rows.items():
                    if k[0] == s and v["last_trade"][:10] == r["last_trade"]:
                        v["coverage_announcement"] = r["announcement"] or None
                continue
            idx = 0
            for i in insts.get(s, []):
                if i.last_trade_ts is not None and iso(i.last_trade_ts - BAR)[:10] == r["last_trade"]:
                    idx = i.idx
            rows[(s, idx)] = {"symbol": s, "instance": idx, "stage0_class": None,
                              "stage0_source": "delisting_coverage.csv", "last_trade": r["last_trade"],
                              "coverage_announcement": r["announcement"] or None}
    return [rows[k] for k in sorted(rows)]


# ------------------------------------------------------------------------------------------- runs


def structure(raw: str, original: str) -> dict:
    """Shape of the same-code JSON objects of a capture (key names and types only)."""
    code = cap._code(original)
    if code is None:
        return {"code": None}
    objs = [d for o in cap._json_objects(raw) for d in cap._walk(o) if str(d.get("code", "")).lower() == code]
    body = [d for d in objs if isinstance(d.get("body"), str)]
    nobody = [d for d in objs if not isinstance(d.get("body"), str)]
    pub = sorted({d.get("publishDate") for d in body if isinstance(d.get("publishDate"), int)})
    rel_nb = sorted({d.get("releaseDate") for d in nobody if isinstance(d.get("releaseDate"), int)})
    return {"code": code, "same_code_objects": len(objs), "with_str_body": len(body),
            "body_object_keys": sorted({k for d in body for k in d}),
            "body_format": sorted({"json_node_tree" if d["body"].lstrip().startswith("{") else
                                   "html" if "<" in d["body"] else "text" for d in body}),
            "body_object_has_releaseDate": any("releaseDate" in d for d in body),
            "body_object_has_publishDate": any("publishDate" in d for d in body),
            "bodyless_objects_keys": sorted({k for d in nobody for k in d}),
            "bodyless_releaseDate_ms": rel_nb, "body_publishDate_ms": pub,
            "publishDate_equals_bodyless_releaseDate": (pub == rel_nb) if pub and rel_nb else None}


def load_captures(stage0: Path, store: MetaStore, umap) -> tuple[list[dict], list[dict]]:
    """Guard each capture by its timestamp BEFORE reading its body, then store it in the tmp MetaStore."""
    refused, rows = [], []
    for f in sorted((stage0 / "m31" / "captures").glob("*.bin")):
        ts, _, sh16 = f.stem.partition("_")
        if not re.fullmatch(r"\d{14}", ts) or ts >= CUTOFF_TS14:
            refused.append({"file": f.name, "reason": f"capture ts {ts} not < {CUTOFF_TS14}: body not read"})
            continue
        try:
            t = cap.capture_ts_ns(ts)
            guard.check_data_span(t, t + 1, what=f"C4 stage-0 capture {f.name}")
        except Exception as e:  # refused by the guard: body never read
            refused.append({"file": f.name, "reason": f"guard: {type(e).__name__}: {e}"})
            continue
        body = f.read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        u = umap.get((ts, sha))
        row = {"file": f.name, "ts": ts, "sha256": sha, "bytes": len(body),
               "file_sha_prefix_ok": sha.startswith(sh16)}
        if u is None:
            row["url"] = None
            row["status"] = "no_original_url"
            rows.append(row)
            continue
        row["url"], row["url_source"] = u
        store.put_capture(cap.Capture(ts, u[0], body, sha, None))
        rows.append(row)
        del body
    return rows, refused


def per_capture(store: MetaStore, rows: list[dict], words: dict) -> None:
    """Per capture (one at a time): extraction + kind by the committed code, and the JSON structure."""
    by_key = {(r["ts"], r["sha256"]): r for r in rows if r.get("url")}
    for e in store.captures():
        c = store.get_capture(e["ts"], e["sha256"])
        r = by_key[(e["ts"], e["sha256"])]
        r["capture_ts"] = iso(c.capture_ts)
        r["notice_id"] = cap.notice_id(c.original)
        t = cap.extract(c.body, c.original)
        kind, why = cap.classify_kind(t.title) if t.text is not None else (None, "unusable")
        r["frozen"] = {"article_status": "usable" if t.text is not None else "unusable",
                       "article_reason": t.article_reason, "title": t.title, "kind": kind, "kind_reason": why,
                       "date_keys": list(t.date_keys), "releaseDate": iso(t.release_ts),
                       "release_reason": t.release_reason, "release_src": t.release_src,
                       "text_lines": None if t.text is None else t.text.count("\n") + 1}
        r["page_title"] = t.page_title
        r["structure"] = structure(cap.decode(c.body), c.original)
        words[e["sha256"]] = {"text": set(_WORD.findall(t.text or "")), "page": set(_WORD.findall(t.page_text))}
        del c, t


def run_build(store: MetaStore, insts) -> dict:
    caps = [store.get_capture(e["ts"], e["sha256"]) for e in store.captures()]
    try:
        return metaingest.build_notices(caps, insts).report
    finally:
        del caps


# ------------------------------------------------------------------------------------------- analysis


def slim_versions(rep: dict) -> list[dict]:
    out = []
    for r in rep["versions"]:
        out.append({"notice_id": r["notice_id"], "version": r["version"], "status": r["status"],
                    "kind": r["kind"], "kind_reason": r["kind_reason"], "title": r["title"],
                    "article_reason": r["article_reason"], "releaseDate": iso(r["release_ts"]),
                    "release_src": r.get("release_src"),
                    "capture_ts": iso(r["capture_ts"]), "known_at": iso(r["known_at"]),
                    "capture_sha256": r["capture_sha256"], "n_captures": r["n_captures"],
                    "match_reason": r["match_reason"],
                    "matches": [{"symbol": m["symbol"], "confident_match": m["match"], "reason": m["reason"],
                                 "instance": m["instance"], "instance_reason": m["instance_reason"],
                                 "scheduled_ts": iso(m["scheduled_ts"]), "scheduled_reason": m["scheduled_reason"],
                                 "evidence": m["evidence"]} for m in r["matches"]],
                    "page_symbols_not_in_notice": r["page"]["symbols_not_in_notice"],
                    "page_coin_m_not_in_notice": r["page"]["coin_m_not_in_notice"]})
    return out


def u6_events(rep: dict, insts) -> dict[str, list[dict]]:
    """Per instance: matched classified versions (delisting with sched, postponement / cancellation)."""
    ev: dict[str, list[dict]] = {}
    for r in rep["versions"]:
        if r["status"] != "classified":
            continue
        for m in r["matches"]:
            if m["instance"] is None:
                continue
            ev.setdefault(m["instance"], []).append({"notice_id": r["notice_id"], "version": r["version"],
                                                     "kind": r["kind"], "known_at": r["known_at"],
                                                     "scheduled_ts": m["scheduled_ts"],
                                                     "scheduled_reason": m["scheduled_reason"]})
    return ev


def u6_activation(evs: list[dict]) -> dict:
    """Whether a matched delisting version can activate U6 at its known_at (halted_fn is NOT evaluated: a
    version whose known_at is already >= sched + 72 h is flagged as halt-dependent)."""
    dl = [e for e in evs if e["kind"] == "delisting"]
    act = [e for e in dl if e["scheduled_ts"] is None or e["known_at"] < e["scheduled_ts"] + CLEAR_H * H]
    return {"delisting_versions": len(dl), "activates_at": iso(min(e["known_at"] for e in act)) if act else None,
            "halt_dependent_only": bool(dl) and not act,
            "postponement_or_cancellation": [f"{e['notice_id']} v{e['version']} {e['kind']}" for e in evs
                                             if e["kind"] != "delisting"]}


def evaluate(rep: dict, sym: str, idx: int, words: dict, insts) -> dict:
    inst = f"{sym}#{idx}"
    lt = next((i.last_trade_ts for i in insts.get(sym, []) if i.idx == idx), None)
    dl, other, why = [], [], []
    base = sym[:-4] if sym.endswith("USDT") else None
    for r in rep["versions"]:
        tag = f"{r['notice_id']} v{r['version']}"
        hit = [m for m in r["matches"] if m["symbol"] == sym]
        if r["status"] == "classified" and r["kind"] == "delisting":
            for m in hit:
                if m["instance"] == inst:
                    off = None if m["scheduled_ts"] is None or lt is None else round((m["scheduled_ts"] - lt) / H, 2)
                    dl.append({"version": tag, "scheduled_ts": iso(m["scheduled_ts"]), "sched_minus_last_trade_h": off,
                               "scheduled_reason": m["scheduled_reason"], "known_at": iso(r["known_at"])})
        if r["status"] == "unusable":
            if sym in r["page"]["symbols_not_in_notice"]:
                why.append(f"unusable_article:{r['article_reason']} ({tag}; symbol on the page)")
            continue
        for m in hit:
            if r["status"] == "unclassified":
                why.append(f"unclassified_kind:{r['kind_reason']} ({tag})")
            elif r["kind"] != "delisting":
                other.append(f"{r['kind']} ({tag})")
            elif not m["match"]:
                why.append(f"no_confident_match:{m['reason']} ({tag})")
            elif m["instance"] != inst:
                why.append(f"instance:{m['instance'] or m['instance_reason']} ({tag})")
        if not hit and base:
            w = words.get(r["capture_sha256"], {}).get("text", set())
            if base in w and sym not in w:
                why.append(f"symbol_not_whole_token:base {base} only ({tag}, {r['status']})")
    sched = sorted({d["scheduled_ts"] for d in dl if d["scheduled_ts"]})
    near = [d for d in dl if d["sched_minus_last_trade_h"] is not None and abs(d["sched_minus_last_trade_h"]) <= 24]
    if near:
        status = "matched_scheduled"
    elif sched:
        status = "matched_scheduled_not_the_termination"
        why = sorted({f"scheduled_ts {d['scheduled_ts']} is {d['sched_minus_last_trade_h']} h from the last trade "
                      f"({d['version']})" for d in dl if d["scheduled_ts"]})
    elif dl:
        status = "matched_null"
        why = sorted({f"scheduled_ts null:{d['scheduled_reason']} ({d['version']})" for d in dl})
    else:
        status = "missed"
        if not why:
            why = ["no_capture (no Stage-0 capture names the symbol in its notice text or page)"]
    return {"status": status, "scheduled_ts": sched, "delisting_versions": dl,
            "other_kinds": sorted(set(other)), "reasons": sorted(set(why))}


def dk05(rep: dict, insts) -> dict:
    """Modifier-vetoed / R52-decided titles in the captures and in the W_DEV catalog titles (committed code)."""
    sets: dict[str, dict] = {}
    cap_veto: dict[str, list[dict]] = {}
    cap_dl_inst: set[str] = set()
    for r in rep["versions"]:
        if r["status"] == "classified" and r["kind"] == "delisting":
            cap_dl_inst |= {m["instance"] for m in r["matches"] if m["instance"]}
    for r in rep["versions"]:
        kr = r["kind_reason"] or ""
        if kr.startswith("ambiguous_kind:modifier:"):
            for st in kr.split(":", 2)[2].split(";")[0].split("|"):
                cap_veto.setdefault(st, []).append(
                    {"title": r["title"], "version": f"{r['notice_id']} v{r['version']}",
                     "instances": sorted({m["instance"] for m in r["matches"] if m["instance"]}),
                     "symbols_in_text": sorted({m["symbol"] for m in r["matches"]})})
    for st, lst in cap_veto.items():
        for x in lst:
            x["instance_has_other_delisting_version"] = {i: i in cap_dl_inst for i in x["instances"]}
    r52 = [{"version": f"{r['notice_id']} v{r['version']}", "title": r["title"], "kind": r["kind"],
            "kind_reason": r["kind_reason"],
            "instances": sorted({m["instance"] for m in r["matches"] if m["instance"]})}
           for r in rep["versions"] if "r52" in (r["kind_reason"] or "")]
    sets["captures"] = {"veto_counts": {st: len(v) for st, v in sorted(cap_veto.items())},
                        "veto_titles": {st: v for st, v in sorted(cap_veto.items())},
                        "r52_decided_versions": r52}
    cat = sorted(jload(S0 / "wayback_notice_lag.json"), key=lambda x: (x["releaseDate"], x["code"]))
    rows = []
    for x in cat:
        k, why = cap.classify_kind(x["title"])
        tw = set(_WORD.findall(x["title"]))
        syms = sorted(s for s in insts if s in tw)
        rel = int(x["releaseDate"]) * 10**9
        att = {}
        for s in syms:
            i = started_instance(insts[s], rel)
            att[s] = None if i is None else f"{s}#{i.idx}"
        rows.append({"code": x["code"], "title": x["title"], "releaseDate": iso(rel), "kind": k, "kind_reason": why,
                     "universe_symbols_in_title": syms, "instances": att,
                     "coin_m_in_title": any(p in cap._norm(x["title"]) for p in COIN_M_PHRASES)})
    title_dl_inst = {i for r in rows if r["kind"] == "delisting" for i in r["instances"].values() if i}
    veto: dict[str, list[dict]] = {}
    for r in rows:
        if r["kind_reason"].startswith("ambiguous_kind:modifier:"):
            for st in r["kind_reason"].split(":", 2)[2].split(";")[0].split("|"):
                veto.setdefault(st, []).append(r)
    flags = []
    for st, lst in sorted(veto.items()):
        for r in lst:
            for i in r["instances"].values():
                if i is not None:
                    flags.append({"instance": i, "stem": st, "title": r["title"],
                                  "other_delisting_evidence": (i in title_dl_inst) or (i in cap_dl_inst)})
    r52_rows = [r for r in rows if "r52" in r["kind_reason"]]
    sets["catalog_titles_wayback_notice_lag"] = {
        "n_titles": len(rows),
        "kind_reason_counts": dict(sorted(Counter(r["kind_reason"] for r in rows).items())),
        "veto_counts": {st: len(v) for st, v in sorted(veto.items())},
        "veto_titles": {st: [r["title"] for r in v] for st, v in sorted(veto.items())},
        "usdm_instances_still_vetoed": sorted(flags, key=lambda f: (f["instance"], f["title"])),
        "r52_reclassified": len(r52_rows),
        "r52_usdm_instances_with_a_delisting_title": sorted(
            {i for r in r52_rows if r["kind"] == "delisting" and not r["coin_m_in_title"]
             for i in r["instances"].values() if i}),
        "r52_postponement_titles": [r["title"] for r in r52_rows if r["kind"] == "postponement"],
        "rows": rows}
    return sets


def normative_items(rep: dict, terms: list[dict]) -> dict:
    """The two items left to the user (normative amendment required): DEC-40 base-only notices and token-swap
    notices that stay ``no_kind_phrase``; listed, never changed here."""
    base_only = []
    for t in terms:
        ev = t["frozen"]
        hits = [x for x in ev["reasons"] if x.startswith("symbol_not_whole_token")]
        if hits and ev["status"] == "missed":
            base_only.append({"instance": f"{t['symbol']}#{t['instance']}", "stage0_class": t["stage0_class"],
                              "evidence": hits})
    swaps = []
    for r in rep["versions"]:
        if r["status"] == "unclassified" and r["kind_reason"] == "no_kind_phrase" and _SWAP.search(r["title"] or ""):
            swaps.append({"version": f"{r['notice_id']} v{r['version']}", "title": r["title"],
                          "symbols_in_text": sorted({m["symbol"] for m in r["matches"]})})
    swap_terms = []
    for t in terms:
        ev = t["frozen"]
        if ev["status"] == "missed" and any(s["version"] in x for s in swaps for x in ev["reasons"]):
            swap_terms.append(f"{t['symbol']}#{t['instance']}")
    return {"dec40_whole_token_base_only_missed": base_only, "token_swap_no_kind_phrase_versions": swaps,
            "token_swap_affected_missed_instances": sorted(swap_terms)}


# ------------------------------------------------------------------------------------------- main


def counts(rows: list[dict], key: str) -> dict:
    us = [r for r in rows if r.get(key)]
    by_reason = Counter(r[key]["article_reason"] for r in us if r[key]["article_status"] == "unusable")
    by_year = Counter(r["ts"][:4] for r in us if r[key]["article_status"] == "unusable")
    kinds = Counter((r[key]["kind"] or ("unusable" if r[key]["article_status"] == "unusable" else "unclassified"))
                    for r in us)
    return {"captures": len(us), "usable": sum(r[key]["article_status"] == "usable" for r in us),
            "kinds": dict(sorted(kinds.items())), "unusable_by_reason": dict(sorted(by_reason.items())),
            "unusable_by_year": dict(sorted(by_year.items()))}


def match_counts(rep: dict) -> dict:
    ms = [(r, m) for r in rep["versions"] for m in r["matches"]]
    return {"symbol_tokens_checked": len(ms), "confident": sum(m["match"] for _, m in ms),
            "reasons": dict(sorted(Counter(m["reason"] for _, m in ms).items())),
            "coin_m_veto": sum(m["reason"].startswith("coin_m_veto") for _, m in ms),
            "scheduled_reasons": dict(sorted(Counter(m["scheduled_reason"] for _, m in ms).items())),
            "versions_with_symbols_only_on_page (related-article-only)": sorted(
                f"{r['notice_id']} v{r['version']}: {','.join(r['page']['symbols_not_in_notice'][:8])}"
                for r in rep["versions"] if r["page"]["symbols_not_in_notice"]),
            "related_article_matches_possible": 0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stage0", type=Path, help="the Stage-0 root (var/v7/stage0)")
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "results" / "v7-impl")
    ap.add_argument("--before", type=Path, default=None, help="a previous C4 JSON (counts copied only)")
    a = ap.parse_args(argv)
    if "var" in a.out.resolve().parts:
        raise SystemExit("refusing to write under var/")
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    insts, inst_how = universe()
    umap = url_map()
    with tempfile.TemporaryDirectory(prefix="c4_") as tmp, k9.using(k9.K9Log.under(tmp)):
        store = MetaStore(Path(tmp) / "meta")
        rows, refused = load_captures(a.stage0, store, umap)
        words: dict = {}
        per_capture(store, rows, words)
        frozen = run_build(store, insts)
        k9_refusals = (Path(tmp) / "k9" / k9.REFUSALS).exists()
    terms = terminations(insts)
    for t in terms:
        t["frozen"] = evaluate(frozen, t["symbol"], t["instance"], words, insts)

    def tcount(key, cls=None):
        sel = [t for t in terms if cls is None or t["stage0_class"] == cls]
        return dict(sorted(Counter(t[key]["status"] for t in sel).items()))

    dk = dk05(frozen, insts)
    before = None
    if a.before is not None:
        old = jload(a.before)
        before = {"git_head": old["git_head"], "sha256": sha_file(a.before),
                  **{k: old[k] for k in ("counts", "build_counts", "termination_coverage") if k in old},
                  "scheduled_reasons": {k: v.get("scheduled_reasons") for k, v in old["match_counts"].items()},
                  "catalog_kind_reason_counts": old["dk05"]["catalog_titles_wayback_notice_lag"]["kind_reason_counts"],
                  "termination_reason_heads": {k: dict(sorted(Counter(
                      x.split(" (")[0].split(":")[0] for t in old["terminations"] for x in t[k]["reasons"]
                      if t[k]["status"] == "missed").items())) for k in old["termination_coverage"]}}
    no_code = sorted({r["url"] for r in rows if r.get("url") and r["frozen"]["article_reason"] == "no_article_code"})
    rep = {
        "kind": "V7_C4_NOTICE_VALIDATION", "outcome_free": True, "git_head": head,
        "code_sha256": {p: sha_file(REPO / p) for p in CODE_FILES},
        "input_sha256": {**{f"docs/results/v7-stage0/{p}": sha_file(S0 / p) for p in DOC_INPUTS},
                         **{f"captures/{r['file']}": r["sha256"] for r in rows}},
        "refused_captures": refused, "k9_refusal_logged": k9_refusals,
        "runs": {"frozen": "metaingest.build_notices as committed (the FROZEN-candidate path; no wrapper)"},
        "headline": {
            "frozen_can_process_real_captures": counts(rows, "frozen")["usable"] > 0,
            "release_src_counts": dict(sorted(Counter(str(r["frozen"]["release_src"]) for r in rows
                                                      if r.get("frozen")).items())),
            "real_article_structure": (
                "The notice's own article object (same 32-hex code, string `body`) carries `publishDate` but NO "
                "`releaseDate`; `releaseDate` (ms) occurs only on body-less same-code catalog entries "
                f"(on {sum(1 for c in rows if c.get('structure', {}).get('bodyless_releaseDate_ms'))} of "
                f"{len(rows)} captures; publishDate equals it on "
                f"{sum(1 for c in rows if c.get('structure', {}).get('publishDate_equals_bodyless_releaseDate'))}). "
                "R50: the release time is the same-code catalog `releaseDate`, else the article `publishDate`, "
                "the later of the two when both exist and differ (`release_src`). R51: the node-tree bodies are "
                "converted to text (block elements break lines, table cells joined by ` | `).")},
        "before": before,
        "counts": {"frozen": counts(rows, "frozen")},
        "body_format_counts": dict(sorted(Counter("+".join(r["structure"].get("body_format", [])) or "none"
                                                  for r in rows if r.get("structure")).items())),
        "build_counts": {"frozen": frozen["counts"]},
        "match_counts": {"frozen": match_counts(frozen)},
        "no_article_code_urls": no_code,
        "termination_coverage": {
            "frozen": {"all": tcount("frozen"), "B": tcount("frozen", "B"), "C": tcount("frozen", "C")}},
        "termination_reason_heads": dict(sorted(Counter(
            x.split(" (")[0].split(":")[0] for t in terms for x in t["frozen"]["reasons"]
            if t["frozen"]["status"] == "missed").items())),
        "terminations": terms,
        "dk05": dk,
        "normative_items": normative_items(frozen, terms),
        "instances_source": inst_how,
        "captures": rows,
        "versions": {"frozen": slim_versions(frozen)},
    }
    a.out.mkdir(parents=True, exist_ok=True)
    js = json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False, default=sorted) + "\n"
    (a.out / "c4-notice-validation.json").write_text(js, encoding="utf-8", newline="\n")
    (a.out / "c4-notice-validation.md").write_text(render_md(rep), encoding="utf-8", newline="\n")
    print(json.dumps({"counts": rep["counts"], "coverage": rep["termination_coverage"]}, indent=1))
    return 0


def _ba(before, after) -> str:
    return f"{before} -> **{after}**"


def render_md(r: dict) -> str:
    b = r.get("before") or {}
    L = ["# C4: notice matcher (AMB-05 / DK-05 / DK-06) on the Stage-0 notice set (outcome-free)", "",
         f"Generated by `scripts/v7/validate_notices_stage0.py` at HEAD `{r['git_head']}` (the FROZEN-candidate "
         "code path only: Task 26d R50 / R51 / R52, no diagnostic wrapper). Machine-readable: "
         "`c4-notice-validation.json` (input / code sha256 recorded there). No prices, labels, P&L or outcomes "
         "read; no network; nothing written under var/.", "",
         "## 1. Headline", "",
         f"- The code processes the real captures: **{r['headline']['frozen_can_process_real_captures']}**.",
         f"- {r['headline']['real_article_structure']}",
         f"- `release_src` per capture: {r['headline']['release_src_counts']}.",
         f"- Article `body` format per capture: {r['body_format_counts']}.",
         f"- Refused captures (ts >= 2025-07-01 / guard): {len(r['refused_captures'])}.", ""]
    if b:
        bc, ac = b["counts"]["frozen"], r["counts"]["frozen"]
        d = b["counts"].get("diag_publishDate", {})
        L += ["## 2. Before -> after", "",
              f"Before = the C4 run at HEAD `{b['git_head']}` (file sha256 `{b['sha256'][:16]}...`): `frozen` = the "
              "code then; `diag` = that run's DIAGNOSTIC publishDate wrapper (shown for reference only).", "",
              "| measure | before (frozen) | before (diag) | after |", "|---|---|---|---|",
              f"| usable captures | {bc['usable']} | {d.get('usable')} | **{ac['usable']}** |",
              f"| kinds (captures) | {bc['kinds']} | {d.get('kinds')} | **{ac['kinds']}** |",
              f"| versions (build) | {b['build_counts']['frozen']['by_kind']} / classified "
              f"{b['build_counts']['frozen']['classified']} | {b['build_counts'].get('diag_publishDate', {}).get('by_kind')}"
              f" / classified {b['build_counts'].get('diag_publishDate', {}).get('classified')} | "
              f"**{r['build_counts']['frozen']['by_kind']} / classified {r['build_counts']['frozen']['classified']}** |",
              f"| Stage-0 terminations (all) | {b['termination_coverage']['frozen']['all']} | "
              f"{b['termination_coverage'].get('diag_publishDate', {}).get('all')} | "
              f"**{r['termination_coverage']['frozen']['all']}** |",
              f"| Stage-0 class B | {b['termination_coverage']['frozen']['B']} | "
              f"{b['termination_coverage'].get('diag_publishDate', {}).get('B')} | "
              f"**{r['termination_coverage']['frozen']['B']}** |",
              f"| Stage-0 class C | {b['termination_coverage']['frozen']['C']} | "
              f"{b['termination_coverage'].get('diag_publishDate', {}).get('C')} | "
              f"**{r['termination_coverage']['frozen']['C']}** |",
              f"| miss reasons (heads) | {b['termination_reason_heads'].get('frozen')} | "
              f"{b['termination_reason_heads'].get('diag_publishDate')} | **{r['termination_reason_heads']}** |",
              f"| scheduled_ts reasons | {b['scheduled_reasons'].get('frozen')} | "
              f"{b['scheduled_reasons'].get('diag_publishDate')} | **{r['match_counts']['frozen']['scheduled_reasons']}** |",
              f"| catalog titles (n=47) | {b['catalog_kind_reason_counts']} | (same) | "
              f"**{r['dk05']['catalog_titles_wayback_notice_lag']['kind_reason_counts']}** |", ""]
    L += ["## 3. Capture counts", "", "| run | captures | usable | kinds | unusable by reason | unusable by year |",
          "|---|---|---|---|---|---|"]
    for k, c in r["counts"].items():
        L.append(f"| {k} | {c['captures']} | {c['usable']} | {c['kinds']} | {c['unusable_by_reason']} | "
                 f"{c['unusable_by_year']} |")
    v = r["match_counts"]["frozen"]
    L += ["", "Version-level (`build_notices`): " + "; ".join(f"{k}: {x}" for k, x in r["build_counts"].items()), "",
          f"`no_article_code` URLs: {r['no_article_code_urls'] or 'none'}", "",
          f"Match counts: confident {v['confident']}/{v['symbol_tokens_checked']}, COIN-M veto {v['coin_m_veto']}, "
          f"reasons {v['reasons']}; scheduled_ts reasons {v['scheduled_reasons']}", "",
          "Related-article-only matches: impossible by construction (only the notice's own article object is "
          "read); versions whose page shows universe symbols absent from the notice text are listed in the JSON.",
          "", "## 4. Per capture", "",
          "| capture ts | notice | status / reason | kind (reason) | title | release_src | releaseDate |",
          "|---|---|---|---|---|---|---|"]
    for c in r["captures"]:
        if not c.get("url"):
            L.append(f"| {c['ts']} | (no URL) | {c.get('status')} | | | | |")
            continue
        f = c["frozen"]
        L.append(f"| {c['ts']} | {c['notice_id']} | {f['article_status']} / {f['article_reason']} | {f['kind']} "
                 f"({f['kind_reason']}) | {(f['title'] or '')[:90]} | {f['release_src']} | {f['releaseDate']} |")
    L += ["", "## 5. Stage-0 termination cross-check", "",
          "Coverage: " + "; ".join(f"{k}: {x}" for k, x in r["termination_coverage"]["frozen"].items()), "",
          f"Miss reasons (heads, counted per reason): {r['termination_reason_heads']}", "",
          "| instance | S0 class | status | scheduled_ts | reasons |", "|---|---|---|---|---|"]
    for t in r["terminations"]:
        d = t["frozen"]
        L.append(f"| {t['symbol']}#{t['instance']} | {t['stage0_class'] or '-'} | {d['status']} | "
                 f"{','.join(d['scheduled_ts']) or '-'} | {'; '.join(d['reasons'] + d['other_kinds'])[:300]} |")
    dk = r["dk05"]
    cat = dk["catalog_titles_wayback_notice_lag"]
    L += ["", "## 6. DK-05 (R52) evidence", "",
          f"Captures: modifier veto counts {dk['captures']['veto_counts']}; R52-decided versions:", ""]
    for x in dk["captures"]["r52_decided_versions"]:
        L.append(f"- {x['version']} {x['kind']} ({x['kind_reason']}): \"{x['title']}\" instances {x['instances']}")
    L += ["", f"W_DEV catalog titles (wayback_notice_lag.json, n={cat['n_titles']}): kind_reason counts "
              f"{cat['kind_reason_counts']}; R52 reclassified {cat['r52_reclassified']}; USDⓈ-M instances with an "
              f"R52 delisting title: {cat['r52_usdm_instances_with_a_delisting_title']}; R52 postponement titles: "
              f"{cat['r52_postponement_titles']}", ""]
    for st, ts in cat["veto_titles"].items():
        L.append(f"- still vetoed `modifier:{st}` ({len(ts)}):")
        for x in ts[:6]:
            L.append(f"  - \"{x}\"")
    n = r["normative_items"]
    L += ["", "## 7. Normative items (NOT changed; normative amendment required)", "",
          "1. DEC-40 exact-symbol whole-token rule: notices naming only the base asset stay no-match. Missed "
          "Stage-0 terminations with such a notice:", ""]
    for x in n["dec40_whole_token_base_only_missed"]:
        L.append(f"   - {x['instance']} (S0 class {x['stage0_class'] or '-'}): {'; '.join(x['evidence'])[:240]}")
    L += ["", "2. Token-swap / rebranding notices stay `no_kind_phrase`:", ""]
    for x in n["token_swap_no_kind_phrase_versions"]:
        L.append(f"   - {x['version']}: \"{x['title']}\" symbols in text {x['symbols_in_text']}")
    L += ["", f"   Missed terminations with such a notice: {n['token_swap_affected_missed_instances']}", ""]
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
