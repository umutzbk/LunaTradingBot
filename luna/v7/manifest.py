"""The DEC-19 pinned W_HOLD listing manifest (spec DEC-19 "pinned manifest", §17 "Holdout opening (DEC-19):
... pinned manifest, read-only loaders", S-1.7 "file listings (name, size, published checksum) of W_HOLD ...
for the Stage 0 completeness check", N1 / N2, DK-01, K9; red-team r2 MJ4, controller ruling R56, AMB-44).

* **Definition.** The canonical JSON of the W_HOLD file LISTINGS: per N1 key its size and its published
  checksum (the S3 ``ETag`` of the N2 listing), nothing else (S-1.7 allows exactly these before the opening).
* **Scope** (``SCOPE``): R3 15m monthly + daily, R5 1m monthly + daily, R4 mark / index / premium 15m monthly
  (ruling R47), ``fundingRate`` monthly; every USD-M symbol with W_HOLD files in those listings, plus the
  hedge ``BTCUSDT`` (which must have W_HOLD files); and (controller ruling R56b) the W_HOLD DAILY
  ``aggTrades`` files (R10, fetched per (symbol, UTC day) with an E_M order) of every in-scope symbol, from
  one single-page listing per (symbol, W_HOLD month). A key is kept when its whole file period lies in W_HOLD
  (``.zip`` and ``.zip.CHECKSUM`` keys alike; both are N1 files fetched at the opening).
* **Builder** (``build_document`` / ``build_and_write``): single-page N2 listings through the ONE fetch layer
  (a truncated listing fails closed, DK-01; M6 keys are dropped there; only name / size / checksum kept). The
  symbols come from the dataset root listings (``CommonPrefixes``); monthly files from one listing per
  (table, symbol); daily files from one listing per (table, symbol, W_HOLD month) (the monthly-prefix form
  ``ingest.daily_days`` uses, so each page stays small). ``write_document`` writes the canonical bytes to the
  canonical location ``manifest_path()`` (next to the home opening marker; refused once any opening file
  exists) and returns their sha256: the value the operator pins.
* **Opening check** (``verify_pinned``, called by ``guard.preflight``): ``manifest_sha256`` must be the sha256
  of the file at the canonical location, whose bytes are the canonical serialization of a document that
  validates (``validate``). A free 64-hex string no longer passes.
* **Post-opening consumer** (``luna.v7.fetch``): a W_HOLD N1 file fetched under the authorization must be a
  key of the pinned manifest with the same size and published checksum (``match_entry``), else it is refused
  and K9-logged (``fetch.ManifestMismatch``, an ingest gap under R55). The ETag of a single-part upload is the
  md5 of the bytes and is compared; a multipart ETag (``<md5>-<parts>``) cannot be recomputed from the bytes
  alone, so a ``.zip`` with a multipart ETag is bound through its size and its ``.CHECKSUM`` file (whose own
  ETag and size are checked, and whose sha256 the fetch layer verifies the zip against; AMB-44 (c)). Every
  manifest key that never reaches the store is an unresolved gap of the ingest-completeness gate
  (``rawstore.RawStore.check_ingest_complete`` with the authorization; ruling R55).
* **Live listings == manifest** (round 1, minor 6): every N2 listing fetched under the authorization (the
  opening ingest plans its months / days from live listings) must hold exactly the pinned in-scope W_HOLD keys
  directly under its prefix, with the same sizes and checksums (``check_listing``; ``fetch`` refuses and
  K9-logs any difference), so a hand-trimmed manifest cannot reduce what the opening ingests and checks.

Pure except ``build_*`` (fetch layer) and the file helpers; no wall clock in the document (its sha256 is a
function of the listings only).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pandas as pd

from luna.v7 import guard, windows
from luna.v7.guard import HoldoutBlocked
from luna.v7.netguard import Request

__all__ = ["FILE", "FORMAT", "HEDGE", "KIND", "S3", "SCOPE", "Entry", "Manifest", "ManifestError",
           "build_and_write", "build_document", "canonical_bytes", "check_listing", "counts", "document",
           "load_pinned", "manifest_path", "match_entry", "match_source", "parse_key", "validate",
           "verify_pinned", "w_hold_months", "write_document"]

FILE = "W_HOLD_MANIFEST.json"
KIND = "V7_W_HOLD_LISTING_MANIFEST"
FORMAT = 1
HEDGE = "BTCUSDT"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ROOT = "data/futures/um"
# (table, freq, dataset, interval): the ruling R56 scope (R3 / R5 monthly + daily, R4 monthly (R47), funding)
SCOPE: tuple[tuple[str, str, str, str | None], ...] = (
    ("R3", "monthly", "klines", "15m"), ("R3", "daily", "klines", "15m"),
    ("R5", "monthly", "klines", "1m"), ("R5", "daily", "klines", "1m"),
    ("R4_mark", "monthly", "markPriceKlines", "15m"), ("R4_index", "monthly", "indexPriceKlines", "15m"),
    ("R4_premium", "monthly", "premiumIndexKlines", "15m"), ("R2_archive", "monthly", "fundingRate", None),
    ("R10", "daily", "aggTrades", None))  # R56b: W_HOLD daily aggTrades of the in-scope symbols (E_M, R10)
_KEY = re.compile(r"data/futures/um/(?P<freq>monthly|daily)/(?P<dataset>[A-Za-z]+)/"
                  r"(?P<symbol>[A-Z0-9_]{1,40})/(?:(?P<interval>\d+[mhdwM])/)?"
                  r"(?P<name>[A-Za-z0-9_\-]+\.zip(?:\.CHECKSUM)?)\Z", re.ASCII)
_MD5 = re.compile(r"\A[0-9a-f]{32}\Z")
_MULTIPART = re.compile(r"\A[0-9a-f]{32}-[1-9][0-9]{0,4}\Z")
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")


class ManifestError(HoldoutBlocked):
    """The manifest cannot be built or does not validate (fails closed)."""


@dataclass(frozen=True)
class Entry:
    key: str
    size: int
    checksum: str


@dataclass(frozen=True)
class Manifest:
    """A verified pinned manifest: its sha256, symbols and {key: (size, checksum)}."""

    sha256: str
    symbols: tuple[str, ...]
    entries: Mapping[str, tuple[int, str]]

    @cached_property
    def _zips(self) -> tuple[tuple[str, str, str, str], ...]:
        out = []
        for key in sorted(self.entries):
            p = parse_key(key)
            if p is not None and not key.endswith(".CHECKSUM"):
                out.append((p[0], p[2], p[3], key))
        return tuple(out)

    def zips(self) -> tuple[tuple[str, str, str, str], ...]:
        """(table, symbol, period, key) of every ``.zip`` key, sorted by key (period: YYYY-MM or YYYY-MM-DD);
        parsed once per manifest object."""
        return self._zips


def w_hold_months() -> list[str]:
    lo, hi = windows.W_HOLD
    cur = pd.Timestamp(int(lo), unit="ns", tz="UTC")
    out = []
    while int(cur.value) < hi:
        out.append(cur.strftime("%Y-%m"))
        cur = cur + pd.DateOffset(months=1)
    return out


def _scope_of(freq: str, dataset: str, interval: str | None) -> str | None:
    for table, f, d, i in SCOPE:
        if (f, d, i) == (freq, dataset, interval):
            return table
    return None


def parse_key(key: str) -> tuple[str, str, str, str] | None:
    """(table, freq, symbol, period) of an in-scope W_HOLD key, else None (wrong path, out of scope, a file
    name not of its directory symbol, or a period not inside W_HOLD)."""
    m = _KEY.fullmatch(key) if isinstance(key, str) else None
    if m is None:
        return None
    table = _scope_of(m.group("freq"), m.group("dataset"), m.group("interval"))
    sym, name = m.group("symbol"), m.group("name")
    stem = f"{sym}-{m.group('interval')}-" if m.group("interval") else f"{sym}-{m.group('dataset')}-"
    per = guard.file_period(name)
    if table is None or per is None or not name.startswith(stem):
        return None
    daily = per[1] - per[0] == 86_400 * 10**9
    if daily != (m.group("freq") == "daily"):
        return None
    lo, hi = windows.W_HOLD
    if not (lo <= per[0] and per[1] <= hi):
        return None
    period = name.removesuffix(".CHECKSUM").removesuffix(".zip")[len(stem):]
    return table, m.group("freq"), sym, period


# ------------------------------------------------------------------------------------------- document


def document(entries: Iterable[Entry | tuple[str, int, str]]) -> dict:
    """The canonical manifest document of the given listing entries (in-scope W_HOLD keys only; anything
    else is refused, so the document cannot hold a key the builder would not keep)."""
    rows: dict[str, tuple[int, str]] = {}
    for e in entries:
        key, size, ck = (e.key, e.size, e.checksum) if isinstance(e, Entry) else e
        if parse_key(key) is None:
            raise ManifestError(f"W_HOLD manifest: {key!r} is not an in-scope W_HOLD N1 key")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not isinstance(ck, str):
            raise ManifestError(f"W_HOLD manifest: {key!r} needs an integer size >= 0 and a checksum string")
        if key in rows and rows[key] != (size, ck):
            raise ManifestError(f"W_HOLD manifest: {key!r} listed twice with different size / checksum")
        rows[key] = (size, ck)
    symbols = sorted({parse_key(k)[2] for k in rows})  # type: ignore[index]
    if HEDGE not in symbols:
        raise ManifestError(f"W_HOLD manifest: the hedge {HEDGE} has no W_HOLD file in the listings")
    lo, hi = windows.W_HOLD
    return {"kind": KIND, "format": FORMAT, "run_id": guard.RUN_ID, "w_hold": [int(lo), int(hi)],
            "hedge": HEDGE, "scope": [list(s) for s in SCOPE], "symbols": symbols,
            "entries": [[k, rows[k][0], rows[k][1]] for k in sorted(rows)]}


def canonical_bytes(doc: Mapping) -> bytes:
    return (json.dumps(dict(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def validate(doc: object) -> Manifest:
    """A document that ``document`` would produce from its own entries, else ManifestError."""
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise ManifestError("W_HOLD manifest: not a manifest document")
    try:
        entries = [(str(k), s, c) for k, s, c in doc["entries"]]
    except (TypeError, ValueError):
        raise ManifestError("W_HOLD manifest: entries must be [key, size, checksum] triples") from None
    want = document(entries)
    if doc != want:
        diff = sorted(k for k in set(want) | set(doc) if doc.get(k) != want.get(k))
        raise ManifestError(f"W_HOLD manifest: fields differ from the canonical document ({diff})")
    rows = {k: (int(s), c) for k, s, c in want["entries"]}
    return Manifest(hashlib.sha256(canonical_bytes(want)).hexdigest(), tuple(want["symbols"]), rows)


# ------------------------------------------------------------------------------------- location / pin


def manifest_path() -> Path:
    """The canonical location: next to the home opening marker (``guard._roots``, ``RUN_ID``)."""
    return Path(guard._roots()[1]) / ".luna" / "v7_holdout" / guard.RUN_ID / FILE


def write_document(doc: Mapping) -> tuple[Path, str]:
    """Validate ``doc`` and write its canonical bytes to ``manifest_path()`` (atomic replace); refused once
    any file of an opening (marker, pin, verdict) exists in a canonical directory. Returns (path, sha256)."""
    validate(dict(doc))
    done = [str(d / n) for d in guard._log_dirs() for n in (guard.MARKER, guard.PIN, guard.VERDICT)
            if (d / n).exists()]
    if done:
        raise HoldoutBlocked(f"W_HOLD manifest not written: an opening exists ({done}); the pinned manifest "
                             "is immutable")
    data = canonical_bytes(doc)
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    return p, hashlib.sha256(data).hexdigest()


_CACHE: dict[tuple, Manifest] = {}


def load_pinned(sha256: str) -> Manifest:
    """The manifest at the canonical location whose bytes have this sha256 and are the canonical
    serialization of a valid document; HoldoutBlocked (ManifestError) otherwise. Cached per (path, sha256,
    file size, mtime)."""
    if not isinstance(sha256, str) or not _SHA.match(sha256):
        raise ManifestError("the W_HOLD file manifest sha256 to pin is missing or malformed (DEC-19 pinned "
                            "manifest)")
    p = manifest_path()
    try:
        st = p.stat()
    except OSError:
        raise ManifestError(f"the pinned W_HOLD manifest is missing at its canonical location {p} (build it "
                            "with luna.v7.manifest.build_and_write)") from None
    ck = (str(p), sha256, st.st_size, st.st_mtime_ns)
    if ck in _CACHE:
        return _CACHE[ck]
    data = p.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise ManifestError(f"the W_HOLD manifest at {p} has sha256 {got}, not the pinned {sha256}")
    try:
        doc = json.loads(data)
    except ValueError:
        raise ManifestError(f"the W_HOLD manifest at {p} is not JSON") from None
    if canonical_bytes(doc) != data:
        raise ManifestError(f"the W_HOLD manifest at {p} is not in canonical form")
    m = validate(doc)
    _CACHE.clear()
    _CACHE[ck] = m
    return m


def verify_pinned(sha256: str | None) -> Manifest:
    """``guard.preflight``'s DEC-19 check (module docstring)."""
    return load_pinned(sha256)  # type: ignore[arg-type]


def match_source(size: int, ck: str, src: Mapping) -> str | None:
    """None when a stored chunk's source record (``ingest._source``: bytes, md5) matches the pinned entry
    (size, and md5 for a single-part ETag; a multipart ETag: size only); else the reason (round 1, m5)."""
    if src.get("bytes") != size:
        return f"{src.get('bytes')} bytes, pinned {size}"
    etag = ck.strip('"').lower()
    if _MD5.match(etag):
        return None if src.get("md5") == etag else f"md5 {src.get('md5')}, pinned {etag}"
    return None if _MULTIPART.match(etag) else f"pinned checksum {ck!r} cannot be verified"


def check_listing(m: Manifest, prefix: str, entries: Iterable) -> str | None:
    """Round 1, minor 6: the in-scope W_HOLD keys directly under ``prefix`` in a live listing (entries with
    ``key`` / ``size`` / ``checksum``) must equal the pinned ones under that prefix; None, else the
    difference."""
    live = {e.key: (int(e.size), str(e.checksum)) for e in entries
            if e.key.startswith(prefix) and "/" not in e.key[len(prefix):] and parse_key(e.key) is not None}
    pinned = {k: v for k, v in m.entries.items() if k.startswith(prefix) and "/" not in k[len(prefix):]}
    if live == pinned:
        return None
    extra = sorted(set(live) - set(pinned))
    gone = sorted(set(pinned) - set(live))
    changed = sorted(k for k in set(live) & set(pinned) if live[k] != pinned[k])
    return (f"live listing {prefix!r} differs from the pinned W_HOLD manifest: {len(extra)} unlisted "
            f"{extra[:3]}, {len(gone)} missing {gone[:3]}, {len(changed)} changed {changed[:3]}")


def match_entry(m: Manifest, key: str, body: bytes) -> str | None:
    """None when ``body`` matches the pinned entry of ``key`` (size, and the md5 of a single-part ETag; a
    ``.zip`` with a multipart ETag: size only, its ``.CHECKSUM`` binds the content); else the reason."""
    e = m.entries.get(key)
    if e is None:
        return f"{key} is not in the pinned W_HOLD manifest"
    size, ck = e
    if len(body) != size:
        return f"{key}: {len(body)} bytes, the pinned listing says {size}"
    etag = ck.strip('"').lower()
    if _MD5.match(etag):
        got = hashlib.md5(body, usedforsecurity=False).hexdigest()
        return None if got == etag else f"{key}: md5 {got} differs from the pinned published checksum {etag}"
    if _MULTIPART.match(etag) and key.endswith(".zip"):
        return None
    return f"{key}: pinned published checksum {ck!r} cannot be verified (fails closed)"


# ------------------------------------------------------------------------------------------- builder


def _list(fetcher, prefix: str):
    got = fetcher.fetch(Request(S3, {"delimiter": "/", "prefix": prefix}))
    if got.listing is None:
        raise ManifestError(f"N2 listing {prefix!r} returned no listing")
    return got.listing


def _symbols(fetcher, freq: str, dataset: str) -> list[str]:
    root = f"{ROOT}/{freq}/{dataset}/"
    out = []
    for p in _list(fetcher, root).prefixes:
        rest = p[len(root):] if p.startswith(root) else ""
        sym = rest.rstrip("/")
        if rest.endswith("/") and re.fullmatch(r"[A-Z0-9_]{1,40}", sym):
            out.append(sym)
    return sorted(set(out))


def build_document(fetcher) -> dict:
    """The manifest document from the live N2 listings (module docstring). ``fetcher``: the run's
    ``fetch.Fetcher`` (pre-opening mode; N2 only)."""
    syms: dict[tuple[str, str], list[str]] = {}
    for t, freq, dataset, _i in SCOPE:
        if t != "R10" and (freq, dataset) not in syms:
            syms[(freq, dataset)] = _symbols(fetcher, freq, dataset)
    months = w_hold_months()
    entries: list[Entry] = []
    for table, freq, dataset, interval in SCOPE:
        if table == "R10":  # aggTrades: only the in-scope symbols, below
            continue
        for sym in syms[(freq, dataset)]:
            base = f"{ROOT}/{freq}/{dataset}/{sym}/" + (f"{interval}/" if interval else "")
            stem = f"{sym}-{interval}-" if interval else f"{sym}-{dataset}-"
            prefixes = [base] if freq == "monthly" else [f"{base}{stem}{m}-" for m in months]
            for prefix in prefixes:
                for e in _list(fetcher, prefix).entries:
                    p = parse_key(e.key)
                    if p is not None and p[0] == table and p[1] == freq and p[2] == sym:
                        entries.append(Entry(e.key, int(e.size), e.checksum))
    for sym in sorted({parse_key(e.key)[2] for e in entries} | {HEDGE}):  # type: ignore[index]  (R56b)
        for mo in months:
            prefix = f"{ROOT}/daily/aggTrades/{sym}/{sym}-aggTrades-{mo}-"
            for e in _list(fetcher, prefix).entries:
                p = parse_key(e.key)
                if p is not None and p[0] == "R10" and p[2] == sym:
                    entries.append(Entry(e.key, int(e.size), e.checksum))
    return document(entries)


def counts(doc: Mapping) -> dict[str, int]:
    """Key counts per (table, freq) of a manifest document (for the builder dry run)."""
    out: dict[str, int] = {}
    for k, _s, _c in doc["entries"]:
        p = parse_key(k)
        name = f"{p[0]}/{p[1]}{'/CHECKSUM' if k.endswith('.CHECKSUM') else ''}"  # type: ignore[index]
        out[name] = out.get(name, 0) + 1
    return dict(sorted(out.items()))


def build_and_write(fetcher) -> tuple[Path, str]:
    """Build the manifest from the listings and write it to the canonical location; returns (path, sha256 to
    pin at the opening)."""
    return write_document(build_document(fetcher))
