"""The one S-1.7 fetch layer: every V7 research network request (spec S-1.7 N1-N6, E6, DEC-41 / DEC-46, L10,
§16 K9; SC-03, DK-01, red-team M3, AMB-35).

``Fetcher.fetch(req, *, auth=None)`` is the only network entry point of V7 research code:

1. ``netguard.check(req, auth=auth)`` runs BEFORE the transport is touched. A refusal (and a forged / stale
   authorization) is K9-logged (``luna.v7.k9``: UTC time, request, rule, reason, plus an incident stub) and
   raised; the transport is never called.
2. The transport (``Transport`` protocol; default ``HttpxTransport``) never follows redirects and has a
   timeout. Any redirect (3xx, or a transport reporting one) is a guard failure everywhere (the allowlist
   permits none): the content is discarded unparsed (only its sha256 goes into the K9 record) and raised.
3. Every accepted response gets ``recv_ts`` (the fetcher's clock), the URL and the sha256 of its bytes, in
   the returned ``Fetched`` and in the run's append-only ``FETCHES.jsonl``.
4. Per class:
   - N1: a ``.zip`` is verified against its published ``.CHECKSUM`` (fetched through the same path; sha256 and
     file name must match) else ``IntegrityError``.
   - N2 (DK-01: no amendment): the S3 XML listing must say ``IsTruncated`` = ``false`` (anything else, or a
     missing element, fails closed: ``ListingTruncated``, K9-logged; never a partial listing); its prefix must
     be the requested one. M6 (and unparseable) keys are dropped in memory with the canonical span
     (``netguard.partition_m6_keys``, no caller span) before anything is returned or logged; every kept key
     keeps only name, size and checksum (S3 ETag). The raw listing bytes are not returned.
   - N5: the rows (``fl`` field order, else the default CDX order) are registered in
     ``netguard.cdx_pair_registry`` under the mode's bound; a row beyond it is a K9-logged guard failure and
     nothing of the batch is kept.
   - N6: HTTP 200, no redirect and ``Memento-Datetime`` == ts (``netguard.check_wayback_response``), else a
     K9-logged guard failure with the content discarded. A capture at or after the pre-opening bound (an E6
     capture) is pinned by sha256 next to the opening markers (``guard.pin_e6_capture``): a re-fetch must be
     byte-identical.
   - Any other non-200 status: ``FetchError`` (no content returned).

Post-opening (E6) mode: ``auth`` = the hardened ``guard.HoldoutAuthorization`` (``netguard`` docstring); never
a boolean. Tests inject a fake ``Transport``; nothing here is exercised against the real network in the gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from luna.v7 import guard, k9, netguard
from luna.v7.guard import EmbargoBlocked, GuardFailure, HoldoutBlocked
from luna.v7.netguard import EndpointRefused, Request

__all__ = ["DEFAULT_TIMEOUT_S", "FETCH_LOG", "FetchError", "Fetched", "Fetcher", "HttpxTransport",
           "IntegrityError", "Listing", "ListingEntry", "ListingTruncated", "Response", "Transport", "fetch"]

DEFAULT_TIMEOUT_S = 30.0
FETCH_LOG = "FETCHES.jsonl"
RULE_ALLOWLIST = "S-1.7 endpoint allowlist N1-N6 (refused before network I/O)"
RULE_REDIRECT = "S-1.7 redirects disabled (N6; every endpoint)"
RULE_N6 = "S-1.7 N6 response (HTTP 200, Memento-Datetime == ts)"
RULE_N5_BOUND = "S-1.7 N5 server-side bound (CDX row beyond the bound)"
RULE_TRUNCATED = "S-1.7 N2 / DK-01 listing must fit one page (IsTruncated)"
RULE_E6_CAPTURE = "S-1.7 E6 capture fetched once, pinned and hashed"
_HEX64 = re.compile(r"\A[0-9a-fA-F]{64}\Z")


class FetchError(RuntimeError):
    """A response that cannot be used (status, parse, integrity); no content is returned."""


class IntegrityError(FetchError):
    """An N1 file does not match its published .CHECKSUM."""


class ListingTruncated(GuardFailure):
    """DK-01: an N2 listing that is truncated (or does not say it is complete) fails closed."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    redirected: bool = False


class Transport(Protocol):
    """One GET of exactly ``url`` + ``params``; must never follow redirects; ``timeout`` in seconds."""

    def get(self, url: str, params: Mapping[str, str], *, timeout: float) -> Response: ...


class HttpxTransport:
    """The default transport: ``httpx`` (a project dependency), ``follow_redirects=False``, a timeout per
    request. ``_mock`` (an ``httpx`` transport) is for tests only (no network)."""

    def __init__(self, *, _mock: httpx.BaseTransport | None = None) -> None:
        self._mock = _mock

    def get(self, url: str, params: Mapping[str, str], *, timeout: float) -> Response:
        with httpx.Client(follow_redirects=False, timeout=timeout, transport=self._mock) as c:
            r = c.get(url, params=dict(params) or None)
            body = b"" if r.is_redirect else r.content  # a redirect body is never read
            return Response(r.status_code, dict(r.headers), body, str(r.url))


@dataclass(frozen=True)
class ListingEntry:
    """One kept N2 key: name, size and published checksum only (S-1.7 N2)."""

    key: str
    size: int
    checksum: str


@dataclass(frozen=True)
class Listing:
    prefix: str
    entries: tuple[ListingEntry, ...]
    prefixes: tuple[str, ...]
    n_dropped_m6: int
    n_dropped_unparseable: int


@dataclass(frozen=True)
class Fetched:
    """An accepted response: the allowlist class, the request, ``recv_ts`` and the sha256 of the response
    bytes. ``body`` is None for N2 (only the filtered ``listing`` is returned)."""

    kind: str
    url: str
    params: Mapping[str, str]
    status: int
    recv_ts: int
    sha256: str
    body: bytes | None = field(repr=False)
    listing: Listing | None = None
    checksum_sha256: str | None = None
    cdx_pairs: tuple[tuple[str, str], ...] = ()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Fetcher:
    """The fetch layer of one run. ``out_dir``: the run's output directory (K9 log under ``out_dir/k9`` unless
    ``k9_log`` is injected; ``FETCHES.jsonl`` in ``out_dir``). ``clock``: ns UTC for ``recv_ts``."""

    def __init__(self, transport: Transport, out_dir: str | Path, *, k9_log: k9.K9Log | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, clock: Callable[[], int] = time.time_ns) -> None:
        t = float(timeout)
        if not math.isfinite(t) or t <= 0:
            raise ValueError(f"timeout must be a finite number of seconds > 0, got {timeout!r}")
        self.transport, self.timeout, self._clock = transport, t, clock
        self.out_dir = Path(out_dir)
        self.k9 = k9_log if k9_log is not None else k9.K9Log.under(self.out_dir)

    # -- K9 -------------------------------------------------------------------------------------------------

    def _refused(self, exc: BaseException, req: Request, rule: str, body: bytes | None = None) -> None:
        extra = {} if body is None else {"response_sha256": _sha(body)}
        request = {"url": req.url, "params": {str(k): str(v) for k, v in req.params.items()}}
        k9.log_refusal(exc, request=request, rule=rule, log=self.k9, extra=extra)

    # -- the one path ---------------------------------------------------------------------------------------

    def fetch(self, req: Request, *, auth: guard.HoldoutAuthorization | None = None) -> Fetched:
        """Check (before any I/O), fetch, verify per class, record (module docstring)."""
        try:
            kind = netguard.check(req, auth=auth).kind
        except (EndpointRefused, HoldoutBlocked, EmbargoBlocked, GuardFailure) as e:
            self._refused(e, req, RULE_ALLOWLIST if isinstance(e, EndpointRefused) else guard.k9_rule(e))
            raise
        params = {str(k): str(v) for k, v in req.params.items()}
        resp = self.transport.get(req.url, params, timeout=self.timeout)
        recv_ts = int(self._clock())
        body = bytes(resp.body)
        status = int(resp.status)
        if kind == "N6":
            pair = netguard.wayback_pair(req.url)
            assert pair is not None  # check() matched N6
            try:
                netguard.check_wayback_response(pair[0], status, dict(resp.headers),
                                                bool(resp.redirected) or 300 <= status < 400)
            except GuardFailure as e:
                redirect = bool(resp.redirected) or 300 <= status < 400
                self._refused(e, req, RULE_REDIRECT if redirect else RULE_N6, body)
                raise
        elif resp.redirected or 300 <= status < 400:
            e = GuardFailure(f"redirect from {req.url} (status {status}): redirects are disabled; discarded")
            self._refused(e, req, RULE_REDIRECT, body)
            raise e
        elif status != 200:
            raise FetchError(f"{req.url}: HTTP {status} (no content returned)")
        sha = _sha(body)
        out: dict = {}
        if kind == "N1" and not req.url.endswith(".CHECKSUM"):
            out["checksum_sha256"] = self._verify_checksum(req, body, sha, auth)
        elif kind == "N2":
            out["listing"] = self._listing(req, body)
            body = None  # type: ignore[assignment]  (the raw listing may name M6 keys: never returned)
        elif kind == "N5":
            out["cdx_pairs"] = self._register_cdx(req, body, auth)
        elif kind == "N6":
            ts, original = netguard.wayback_pair(req.url)  # type: ignore[misc]
            if ts >= netguard.bounds(None)[1]:  # an E6 capture (only reachable in the E6 mode)
                try:
                    guard.pin_e6_capture(auth, ts=ts, original=original, sha256=sha, recv_ts=recv_ts)
                except (GuardFailure, HoldoutBlocked) as e:
                    self._refused(e, req, RULE_E6_CAPTURE, body or b"")
                    raise
        self._log_fetch(kind, req, params, status, recv_ts, sha)
        return Fetched(kind, req.url, params, status, recv_ts, sha, body, **out)

    # -- per class ----------------------------------------------------------------------------------------

    def _verify_checksum(self, req: Request, body: bytes, sha: str, auth) -> str:
        ck = self.fetch(Request(req.url + ".CHECKSUM"), auth=auth)
        parts = (ck.body or b"").decode("ascii", errors="replace").split()
        name = req.url.rsplit("/", 1)[-1]
        if len(parts) < 2 or not _HEX64.match(parts[0]) or parts[1].lstrip("*") != name:
            raise IntegrityError(f"{req.url}: unparseable published .CHECKSUM")
        if parts[0].lower() != sha:
            raise IntegrityError(f"{req.url}: sha256 {sha} differs from the published .CHECKSUM {parts[0]}")
        return parts[0].lower()

    def _listing(self, req: Request, body: bytes) -> Listing:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise FetchError(f"N2 listing is not XML: {e}") from None
        if _local(root.tag) != "ListBucketResult":
            raise FetchError(f"N2 response is not an S3 ListBucketResult ({_local(root.tag)})")
        kids = {_local(c.tag): c for c in root}
        trunc = kids.get("IsTruncated")
        if trunc is None or (trunc.text or "") != "false":
            e = ListingTruncated(f"N2 listing {req.params.get('prefix')!r}: IsTruncated = "
                                 f"{None if trunc is None else trunc.text!r}; fails closed (DK-01: only "
                                 "listings that fit one page; address daily files as exact N1 URLs)")
            self._refused(e, req, RULE_TRUNCATED)
            raise e
        want = netguard.params_of(req)["prefix"]
        got = kids["Prefix"].text if "Prefix" in kids else None
        if (got or "") != want:
            raise FetchError(f"N2 listing prefix {got!r} differs from the requested {want!r}")
        raw: list[tuple[str, int, str]] = []
        prefixes: list[str] = []
        for c in root:
            tag = _local(c.tag)
            if tag == "Contents":
                f = {_local(x.tag): (x.text or "") for x in c}
                try:
                    raw.append((f["Key"], int(f["Size"]), f.get("ETag", "").strip('"')))
                except (KeyError, ValueError):
                    raise FetchError("N2 listing entry without a key or an integer size") from None
            elif tag == "CommonPrefixes":
                prefixes += [(x.text or "") for x in c if _local(x.tag) == "Prefix"]
        kept, m6, bad = netguard.partition_m6_keys([k for k, _, _ in raw], repo=guard._roots()[0])
        keep = set(kept)
        entries = tuple(ListingEntry(k, s, ck) for k, s, ck in raw if k in keep)
        cps = tuple(p for p in netguard.drop_m6_keys(prefixes, repo=guard._roots()[0]))
        return Listing(want, entries, cps, len(m6), len(bad))

    def _register_cdx(self, req: Request, body: bytes, auth) -> tuple[tuple[str, str], ...]:
        fl = netguard.params_of(req).get("fl")
        fields = fl.split(",") if fl else ["urlkey", "timestamp", "original", "mimetype", "statuscode",
                                           "digest", "length"]
        if "timestamp" not in fields or "original" not in fields:
            raise FetchError("N5 `fl` must include timestamp and original to register N6 pairs")
        i_ts, i_or = fields.index("timestamp"), fields.index("original")
        rows: list[tuple[str, str]] = []
        for line in body.decode("utf-8", errors="strict").splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) != len(fields):
                raise FetchError(f"N5 row with {len(parts)} fields, expected {len(fields)}: nothing "
                                 "registered")
            rows.append((parts[i_ts], parts[i_or]))
        try:
            netguard.cdx_pair_registry.register_cdx_result(rows, auth=auth)
        except GuardFailure as e:
            self._refused(e, req, RULE_N5_BOUND, body)
            raise
        return tuple(rows)

    def _log_fetch(self, kind: str, req: Request, params: Mapping[str, str], status: int, recv_ts: int,
                   sha: str) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        rec = {"recv_ts": recv_ts, "kind": kind, "url": req.url, "params": dict(params), "status": status,
               "sha256": sha}
        with open(self.out_dir / FETCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def fetch(req: Request, *, transport: Transport, out_dir: str | Path,
          auth: guard.HoldoutAuthorization | None = None, k9_log: k9.K9Log | None = None) -> Fetched:
    """One request through a ``Fetcher`` (the only network entry point of V7 research code)."""
    return Fetcher(transport, out_dir, k9_log=k9_log).fetch(req, auth=auth)
