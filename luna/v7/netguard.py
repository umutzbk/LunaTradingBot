"""Pre-opening endpoint allowlist N1-N6 (spec S-1.7, DEC-41 as amended by DEC-46, L10, E6, E8).

Permission is per exact endpoint and parameter set, never per host and never by assuming a time parameter
bounds an endpoint. ``check`` refuses everything not matched by the explicit table below before any network
access could happen; it is pure (no I/O) without an authorization. Every V7 research request goes through it,
called by the one fetch layer ``luna.v7.fetch`` (which K9-logs every refusal).

    N1  data.binance.vision futures/um zip (+ .CHECKSUM), whole file period < 2025-07-01
    N2  S3 bucket listing (delimiter=/, prefix=data/futures/um/...), names / sizes / checksums only
    N3  fapi/v1/fundingRate with symbol, startTime, endTime < 2025-07-01 (+ limit); after the opening
        endTime < 2026-07-01 (controller ruling R46)
    N4  fapi/v1/exchangeInfo, no parameters (E8-limited use)
    N5  Wayback CDX with ``to`` <= 20250630235959
    N6  Wayback /web/<ts>id_/<original>, only an exact pair returned by an N5 query of THIS run's fetcher
        (``cdx_pairs``: the fetcher's private ``CdxPairs``; there is no module-level registry)

Post-opening mode (E6; SC-03, controller ruling R46, AMB-35): with ``auth`` = the hardened
``guard.HoldoutAuthorization``, re-verified against the canonical opening by ``guard.verified_opening``
(never a boolean; refused once the verdict marker exists): N1 also allows file content whose whole period is
< the W_HOLD end and does not overlap the canonical M6; N3 ``endTime`` may reach < 2026-07-01 (R46, symmetric
with N1 W_HOLD content); the N5 / N6 bound is raised to 20260630235959 / < 20260701000000 (E6). All from
``post_opening_bounds``. Everything else is unchanged: the research CMS stays blocked, N2 / N4 keep their
rules, M6 stays refused. Without ``auth`` the pre-opening bounds apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit

import pandas as pd

from luna.v7 import guard, windows
from luna.v7.guard import GuardFailure
from luna.v7.timeutil import D

__all__ = ["Allowed", "Bounds", "CdxPairs", "EndpointRefused", "GuardFailure", "Request", "bounds", "check",
           "check_wayback_response", "drop_m6_keys", "params_of", "partition_m6_keys", "post_opening_bounds",
           "wayback_pair"]


class EndpointRefused(RuntimeError):
    """The request is not on the S-1.7 allowlist; raised before any I/O."""


@dataclass(frozen=True)
class Request:
    url: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Allowed:
    kind: str  # "N1".."N6"


_BOUND_MS = windows.W_HOLD[0] // 10**6  # 2025-07-01T00:00:00Z in ms
_E6_BOUND_MS = windows.W_HOLD[1] // 10**6  # R46: 2026-07-01T00:00:00Z in ms (N3 after the opening)


def _ts14(ns: int) -> str:
    return pd.Timestamp(int(ns), unit="ns", tz="UTC").strftime("%Y%m%d%H%M%S")


_BOUND_TS = _ts14(windows.W_HOLD[0])  # first Wayback timestamp that is refused before the opening
_TO_MAX = _ts14(windows.W_HOLD[0] - 10**9)  # 20250630235959
_E6_BOUND_TS = _ts14(windows.W_HOLD[1])  # E6: first refused capture timestamp after the opening
_E6_TO_MAX = _ts14(windows.W_HOLD[1] - 10**9)  # 20260630235959
_TS14 = re.compile(r"\d{14}", re.ASCII)
_DIGITS = re.compile(r"\d{1,16}", re.ASCII)

_N1_PATH = re.compile(
    r"/data/futures/um/(?P<freq>monthly|daily)/(?P<dataset>[A-Za-z]+)/(?P<symbol>[A-Z0-9_]+)/"
    r"(?:(?P<interval>\d+[mhdwM])/)?(?P<file>[A-Za-z0-9_\-]+)\.zip(?:\.CHECKSUM)?", re.ASCII)
_N2_PREFIX = re.compile(r"data/futures/um/[A-Za-z0-9_\-/]*", re.ASCII)
_N6 = re.compile(r"https://web\.archive\.org/web/(\d{14})id_/(.+)", re.ASCII)
_N5_ALLOWED = {"url", "matchType", "from", "filter", "fl", "collapse", "limit", "to"}


def _refuse(why: str, req: Request) -> EndpointRefused:
    return EndpointRefused(f"{why}: {req.url} {dict(req.params)}")


def _has_control(s: str) -> bool:
    return any(ord(c) <= 0x20 or ord(c) == 0x7F or c.isspace() for c in s)


def _int_param(v: object) -> int | None:
    s = str(v)
    return int(s) if _DIGITS.fullmatch(s) else None


@dataclass(frozen=True)
class Bounds:
    cdx_to_max: str  # N5 ``to`` maximum (14 digits)
    n6_first_refused: str  # first refused N6 / CDX row timestamp
    n1_end_ns: int  # an N1 file's period end must be <= this
    n3_end_ms: int  # N3 ``endTime`` must be < this


_PRE = Bounds(_TO_MAX, _BOUND_TS, windows.W_HOLD[0], _BOUND_MS)
_POST = Bounds(_E6_TO_MAX, _E6_BOUND_TS, windows.W_HOLD[1], _E6_BOUND_MS)


def post_opening_bounds() -> Bounds:
    """The post-opening bounds (E6 N5 / N6, W_HOLD N1 content, R46 N3)."""
    return _POST


def bounds(auth: object = None) -> Bounds:
    """The pre-opening bounds without an authorization; with one, the post-opening bounds once
    ``guard.verified_opening(auth)`` verifies it (HoldoutBlocked otherwise: a forged or stale authorization is
    refused, never treated as "no authorization")."""
    if auth is None:
        return _PRE
    guard.verified_opening(auth)
    return _POST


class CdxPairs:
    """The exact (ts, original) pairs returned by the bounded N5 queries of ONE fetcher (one run); N6 may
    fetch only these. Registration is private to the fetch layer (``luna.v7.fetch.Fetcher``, which registers
    the rows of the N5 responses it received); nothing is persisted or shared between fetchers. A pair at or
    beyond the pre-opening bound can only come from an E6 query and is fetchable only in the E6 mode
    (``check`` applies the mode's bound to the N6 ts as well)."""

    def __init__(self) -> None:
        self._pairs: set[tuple[str, str]] = set()

    def _register(self, rows, *, auth: object = None) -> None:
        """Fetch layer only. ``rows``: (timestamp, original) pairs. A row at or beyond the mode's bound means
        the server-side bound failed: guard failure, nothing from the batch kept."""
        bound = bounds(auth).n6_first_refused
        batch: list[tuple[str, str]] = []
        for r in rows:
            ts, orig = str(r[0]), r[1]
            if not _TS14.fullmatch(ts) or ts >= bound:
                raise GuardFailure(f"CDX row timestamp {ts!r} is outside the bound < {bound}")
            batch.append((ts, str(orig)))
        self._pairs.update(batch)

    def contains(self, ts: str, original: str) -> bool:
        return (ts, original) in self._pairs

    def __len__(self) -> int:
        return len(self._pairs)


def check_wayback_response(ts: str, status: int, headers: dict, redirected: bool) -> None:
    """N6: HTTP 200, no redirect, ``Memento-Datetime`` == ts; otherwise discard unparsed (GuardFailure)."""
    if redirected or status != 200:
        raise GuardFailure(f"Wayback response for {ts}: status {status}, redirected={redirected}")
    h = {str(k).lower(): v for k, v in headers.items()}
    raw = h.get("memento-datetime")
    try:
        got = parsedate_to_datetime(raw).astimezone(UTC).strftime("%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        raise GuardFailure(f"Wayback {ts}: missing or invalid Memento-Datetime {raw!r}") from None
    if got != ts:
        raise GuardFailure(f"Wayback timestamp substitution: requested {ts}, Memento-Datetime {got}")


def _merged_params(req: Request, query: str) -> dict:
    pairs = parse_qsl(query, keep_blank_values=True)
    out: dict[str, str] = {}
    for k, v in pairs:
        if k in out:
            raise _refuse(f"duplicate query parameter {k}", req)
        out[k] = v
    for k, v in req.params.items():
        if k in out:
            raise _refuse(f"parameter {k} given both in the URL and in params", req)
        out[k] = str(v)
    return out


def params_of(req: Request) -> dict[str, str]:
    """The request's parameters merged from its URL query and ``params`` (EndpointRefused on duplicates)."""
    try:
        q = urlsplit(req.url).query
    except ValueError:
        raise _refuse("unparsable URL", req) from None
    return _merged_params(req, q)


def wayback_pair(url: str) -> tuple[str, str] | None:
    """(ts, original) of an N6 URL, else None."""
    m = _N6.fullmatch(url) if isinstance(url, str) else None
    return None if m is None else (m.group(1), m.group(2))


def check(req: Request, *, auth: object = None, cdx_pairs: CdxPairs | None = None) -> Allowed:
    """Allowlist decision; raises EndpointRefused for everything not listed (before any I/O). ``auth``: the
    hardened holdout authorization for the post-opening mode (module docstring), verified first.
    ``cdx_pairs``: the calling fetcher's N5 pairs (None: no N6 request is allowed)."""
    b = bounds(auth)
    to_max, n6_bound, n1_hi = b.cdx_to_max, b.n6_first_refused, b.n1_end_ns
    url = req.url
    if not isinstance(url, str) or not url:
        raise _refuse("empty URL", req)
    if _has_control(url) or any(_has_control(str(k)) or _has_control(str(v)) for k, v in req.params.items()):
        raise _refuse("control characters or whitespace in URL / parameters", req)
    m = _N6.fullmatch(url)
    if m:  # the original URL carries its own '?' / '#': match on the raw string
        if req.params:
            raise _refuse("N6 takes no parameters", req)
        ts, original = m.groups()
        if ts >= n6_bound or cdx_pairs is None or not cdx_pairs.contains(ts, original):
            raise _refuse("N6 only for an exact (ts, original) pair returned by a bounded N5 query", req)
        return Allowed("N6")
    try:
        u = urlsplit(url)
        port, user = u.port, u.username
    except ValueError:
        raise _refuse("unparsable URL", req) from None
    if u.scheme != "https" or port is not None or user is not None or u.fragment or not u.hostname:
        raise _refuse("scheme / port / userinfo / fragment not allowed", req)
    host, path = u.hostname.lower(), u.path
    p = _merged_params(req, u.query)

    if host == "data.binance.vision":
        n1 = _N1_PATH.fullmatch(path)
        per = guard.file_period(path) if n1 else None
        if not n1 or per is None or p:
            raise _refuse("N1 path not recognised or parameters given", req)
        if (n1.group("freq") == "daily") != (per[1] - per[0] == D):
            raise _refuse("N1 file period does not match its monthly / daily directory", req)
        if not n1.group("file").startswith(n1.group("symbol")):
            raise _refuse("N1 file name does not belong to the directory symbol", req)
        if per[1] > n1_hi:
            raise _refuse("N1 file period reaches W_HOLD / M6 (content refused)" if auth is None else
                          "N1 file period reaches past W_HOLD (M6 content refused after the opening)", req)
        if auth is not None and windows.overlaps(per[0], per[1], windows.m6_cached()):
            raise _refuse("N1 file period overlaps the canonical M6 (content refused)", req)
        return Allowed("N1")

    if host == "s3-ap-northeast-1.amazonaws.com":
        if path.rstrip("/") != "/data.binance.vision" or set(p) != {"delimiter", "prefix"}:
            raise _refuse("N2 takes exactly delimiter and prefix", req)
        if p["delimiter"] != "/" or not _N2_PREFIX.fullmatch(p["prefix"]) or ".." in p["prefix"]:
            raise _refuse("N2 delimiter must be '/' and prefix under data/futures/um/", req)
        return Allowed("N2")

    if host == "fapi.binance.com":
        if path == "/fapi/v1/fundingRate":
            required = {"symbol", "startTime", "endTime"}
            if not required <= set(p) or not set(p) <= required | {"limit"}:
                raise _refuse("N3 requires symbol, startTime, endTime (+ optional limit) only", req)
            start, end = _int_param(p["startTime"]), _int_param(p["endTime"])
            if start is None or end is None or end >= b.n3_end_ms or start > end:
                raise _refuse(f"N3 needs numeric startTime <= endTime < {b.n3_end_ms} ms", req)
            if "limit" in p and _int_param(p["limit"]) is None:
                raise _refuse("N3 limit must be an integer", req)
            if not p["symbol"].isalnum():
                raise _refuse("N3 symbol must be alphanumeric", req)
            return Allowed("N3")
        if path == "/fapi/v1/exchangeInfo":
            if p:
                raise _refuse("N4 takes no parameters", req)
            return Allowed("N4")
        raise _refuse("current-state fapi endpoints are refused", req)

    if host == "web.archive.org" and path == "/cdx/search/cdx":
        to = p.get("to")
        if to is None or not _TS14.fullmatch(to) or to > to_max or not set(p) <= _N5_ALLOWED:
            raise _refuse(f"N5 requires a 14-digit `to` <= {to_max} and only allowed parameters", req)
        return Allowed("N5")

    raise _refuse("endpoint not on the S-1.7 allowlist", req)


def partition_m6_keys(keys: list[str]) -> tuple[list[str], list[str], list[str]]:
    """N2, fail closed: (kept, dropped_m6, dropped_unparseable). A key is kept only if it is a directory
    prefix (ends with '/') or has a parseable period that does not overlap M6. W_HOLD keys stay (name only,
    nothing else is read from them). M6 is always the canonical span (``windows.m6_cached``: the hash-checked
    V6 artefacts and R_V6 release record, read once per process); no caller span is taken (SC-06, red-team
    M5: a hand-built span kept M6 keys) and no caller repository (red-team r2 MJ5: a forged scratch repository
    emptied M6)."""
    m6_span = windows.m6_cached()
    kept: list[str] = []
    m6_dropped: list[str] = []
    unparseable: list[str] = []
    for k in keys:
        per = guard.file_period(k)
        if per is None:
            (kept if k.endswith("/") else unparseable).append(k)
        elif windows.overlaps(per[0], per[1], m6_span):
            m6_dropped.append(k)
        else:
            kept.append(k)
    return kept, m6_dropped, unparseable


def drop_m6_keys(keys: list[str]) -> list[str]:
    """N2: the keys that survive ``partition_m6_keys`` (M6 and unparseable keys dropped in memory)."""
    return partition_m6_keys(keys)[0]
