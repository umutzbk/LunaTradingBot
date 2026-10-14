"""Pre-opening endpoint allowlist N1-N6 (spec S-1.7, DEC-41 as amended by DEC-46, L10, E6, E8).

Permission is per exact endpoint and parameter set, never per host and never by assuming a time parameter
bounds an endpoint. ``check`` is pure (no I/O): it refuses everything not matched by the explicit table
below before any network access could happen. Every V7 research request must go through it.

    N1  data.binance.vision futures/um zip (+ .CHECKSUM), whole file period < 2025-07-01
    N2  S3 bucket listing (delimiter=/, prefix=data/futures/um/...), names / sizes / checksums only
    N3  fapi/v1/fundingRate with symbol, startTime, endTime < 2025-07-01 (+ limit)
    N4  fapi/v1/exchangeInfo, no parameters (E8-limited use)
    N5  Wayback CDX with ``to`` <= 20250630235959
    N6  Wayback /web/<ts>id_/<original>, only an exact pair returned by an N5 query
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit

from luna.v7 import guard, windows
from luna.v7.guard import GuardFailure
from luna.v7.timeutil import D

__all__ = ["Allowed", "EndpointRefused", "GuardFailure", "Request", "cdx_pair_registry", "check",
           "check_wayback_response", "drop_m6_keys",
           "partition_m6_keys"]


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
_BOUND_TS = "20250701000000"  # first Wayback timestamp that is refused
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


class CdxPairRegistry:
    """The exact (ts, original) pairs returned by bounded N5 queries; N6 may fetch only these."""

    def __init__(self) -> None:
        self._pairs: set[tuple[str, str]] = set()

    def register_cdx_result(self, rows) -> None:
        """``rows``: dicts with ``timestamp`` / ``original`` or (timestamp, original) sequences. A row at or
        beyond the bound means the server-side bound failed: guard failure, nothing from the batch kept."""
        batch: list[tuple[str, str]] = []
        for r in rows:
            ts, orig = (r["timestamp"], r["original"]) if isinstance(r, dict) else (r[0], r[1])
            ts = str(ts)
            if not _TS14.fullmatch(ts) or ts >= _BOUND_TS:
                raise GuardFailure(f"CDX row timestamp {ts!r} is outside the bound < {_BOUND_TS}")
            batch.append((ts, str(orig)))
        self._pairs.update(batch)

    def contains(self, ts: str, original: str) -> bool:
        return (ts, original) in self._pairs

    def clear(self) -> None:
        self._pairs.clear()


cdx_pair_registry = CdxPairRegistry()


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


def check(req: Request) -> Allowed:
    """Pure allowlist decision; raises EndpointRefused for everything not listed (before any I/O)."""
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
        if ts >= _BOUND_TS or not cdx_pair_registry.contains(ts, original):
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
        if per[1] > windows.W_HOLD[0]:
            raise _refuse("N1 file period reaches W_HOLD / M6 (content refused)", req)
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
            if start is None or end is None or end >= _BOUND_MS or start > end:
                raise _refuse(f"N3 needs numeric startTime <= endTime < {_BOUND_MS} ms", req)
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
        if to is None or not _TS14.fullmatch(to) or to > "20250630235959" or not set(p) <= _N5_ALLOWED:
            raise _refuse("N5 requires a 14-digit `to` <= 20250630235959 and only allowed parameters", req)
        return Allowed("N5")

    raise _refuse("endpoint not on the S-1.7 allowlist", req)


def partition_m6_keys(keys: list[str], m6_span: tuple[int, int | None]
                      ) -> tuple[list[str], list[str], list[str]]:
    """N2, fail closed: (kept, dropped_m6, dropped_unparseable). A key is kept only if it is a directory
    prefix (ends with '/') or has a parseable period that does not overlap M6. W_HOLD keys stay (name only,
    nothing else is read from them). The M6 span is supplied by the caller (no I/O here)."""
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


def drop_m6_keys(keys: list[str], m6_span: tuple[int, int | None]) -> list[str]:
    """N2: the keys that survive ``partition_m6_keys`` (M6 and unparseable keys dropped in memory)."""
    return partition_m6_keys(keys, m6_span)[0]
