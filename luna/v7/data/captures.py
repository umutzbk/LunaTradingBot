"""Immutable Wayback captures of Binance announcement pages -> deterministic text, ``releaseDate``, notice
identity and the closed-list notice kind (spec S-2.5, DEC-26 / DEC-36 / DEC-40, S-1.7 N5 / N6, R11; DK-05;
Task 26c, AMB-39).

Everything here is pure (no I/O, no clock) and deterministic.

* **Text** (``extract``): the Stage-0 extraction (``docs/results/v7-stage0/scripts/m31_notices.py``
  ``analyse``) ported to the stdlib ``html.parser``: the raw bytes are gunzipped when they carry the gzip
  magic and decoded as UTF-8 (errors replaced); the JSON escapes Stage 0 undid (``\\u003c``, ``\\u003e``,
  ``\\u0026``, ``\\n``, ``\\"``) are undone in the same order; the markup is parsed with ``<script>`` /
  ``<style>`` NOT treated as raw text (Stage 0 stripped every ``<...>`` tag everywhere, so the embedded
  article HTML of the page's JSON is text too); character references are unescaped; the data chunks are
  joined by spaces and every whitespace run collapses to one space. The title is the first ``<title>``
  element's text, whitespace-collapsed, cut at the first `` | `` (Stage 0). ``CaptureText.text`` =
  title + newline + page text (the title is its own sentence for ``notices.scheduled_ts``).
* **releaseDate** (``release_date``): only from inside the capture, never from the CMS. Every JSON object of
  the page's ``<script>`` elements (the whole script, else its outermost ``{...}``) that has an integer
  ``releaseDate`` and a ``code`` equal to the notice code of the URL is a candidate; they must agree; a URL
  with a code and no such object has no release date (a related article's date is never taken). Only for a
  URL without a code is the value taken when the page carries exactly one distinct ``"releaseDate":<ms>``.
  Anything else: no release date (the capture yields no version; reported, never guessed).
* **Notice identity** (``notice_id``): ``<locale>:<32-hex article code>`` from the original URL (the slug and
  host spelling do not matter); a URL without a code is identified by ``url:<host+path>`` (lower-cased, no
  scheme / query / fragment / trailing slash).
* **Kind** (``classify_kind``): a CLOSED phrase list on the NFKC-casefolded, whitespace-collapsed title only;
  no semantic inference (DK-05 approach; the list is AMB-39, to be ratified with the DK-05 validation
  report). A cancellation / postponement phrase decides the kind unless both occur, or a delisting phrase is
  still present once those phrases are removed (a mixed title): then the version is unclassified. Otherwise
  any delisting phrase -> delisting. No phrase -> unclassified. Unclassified versions never enter R11 or U6.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from luna.v7.data.notices import Kind
from luna.v7.timeutil import to_ns

__all__ = ["CANCELLATION_PHRASES", "DELISTING_PHRASES", "POSTPONEMENT_PHRASES", "Capture", "CaptureText",
           "capture_ts_ns", "classify_kind", "decode", "extract", "notice_id", "release_date"]

# AMB-39: the closed kind phrase lists (compared on the NFKC-casefolded, whitespace-collapsed title).
CANCELLATION_PHRASES: tuple[str, ...] = (
    "cancel the delisting", "cancels the delisting", "cancellation of the delisting",
    "delisting cancellation",
    "delisting has been cancelled", "delisting has been canceled", "delisting is cancelled",
    "delisting is canceled", "cancel delisting", "cancel the automatic settlement",
    "cancellation of the automatic settlement")
POSTPONEMENT_PHRASES: tuple[str, ...] = (
    "postpone the delisting", "postpones the delisting", "postponement of the delisting",
    "delisting postponement", "delisting has been postponed", "delisting is postponed", "postpone delisting",
    "postpone the automatic settlement", "postponement of the automatic settlement")
DELISTING_PHRASES: tuple[str, ...] = ("delist", "automatic settlement", "close all positions")

_JSON_ESCAPES = (("\\u003c", "<"), ("\\u003e", ">"), ("\\u0026", "&"), ("\\n", " "), ('\\"', '"'))
_WS = re.compile(r"\s+")
_TS14 = re.compile(r"\d{14}", re.ASCII)
_CODE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.ASCII)
_LOCALE = re.compile(r"[a-z]{2}(?:-[a-z]{2,4})?", re.ASCII)
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_RELEASE = re.compile(r'"releaseDate"\s*:\s*(\d{1,16})(?![\d.])', re.ASCII)
_DATE_KEY = re.compile(r'"([A-Za-z_]*(?:Date|Time))"\s*:\s*\d{13}(?![\d.])', re.ASCII)
_MS_LO, _MS_HI = 1_483_228_800_000, 4_102_444_800_000  # 2017-01-01 .. 2100-01-01 (a plausible ms epoch)


@dataclass(frozen=True)
class Capture:
    """One accepted N6 response: the 14-digit memento timestamp (== ``Memento-Datetime``), the original URL,
    the raw bytes as received, their sha256 and the fetch layer's ``recv_ts``."""

    ts: str
    original: str
    body: bytes
    sha256: str
    recv_ts: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ts, str) or not _TS14.fullmatch(self.ts):
            raise ValueError(f"capture timestamp {self.ts!r} is not 14 digits")
        if hashlib.sha256(self.body).hexdigest() != self.sha256:
            raise ValueError(f"capture {self.ts} {self.original}: body does not match its sha256")

    @property
    def capture_ts(self) -> int:
        return capture_ts_ns(self.ts)

    @property
    def archive_url(self) -> str:
        return f"https://web.archive.org/web/{self.ts}id_/{self.original}"


@dataclass(frozen=True)
class CaptureText:
    title: str | None
    text: str  # title + "\n" + page text (module docstring)
    text_sha256: str
    release_ts: int | None  # ns
    release_reason: str
    date_keys: tuple[str, ...]  # JSON keys ending in Date / Time with a 13-digit value (diagnostic only)


def capture_ts_ns(ts: str) -> int:
    """The ns UTC time of a 14-digit Wayback timestamp."""
    if not isinstance(ts, str) or not _TS14.fullmatch(ts):
        raise ValueError(f"capture timestamp {ts!r} is not 14 digits")
    return to_ns(f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}")


def decode(body: bytes) -> str:
    """Raw capture bytes -> text: gunzip when the gzip magic is present (and it decompresses), UTF-8 with
    errors replaced (Stage 0 ``decode``)."""
    b = bytes(body)
    if b[:2] == b"\x1f\x8b":
        try:
            b = gzip.decompress(b)
        except (OSError, EOFError):
            pass
    return b.decode("utf-8", "replace")


class _Text(HTMLParser):
    CDATA_CONTENT_ELEMENTS = ()  # script / style contents are markup too (Stage 0 stripped every tag)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str | None = None
        self._in_title = False
        self._title: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title" and self.title is None:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = "".join(self._title)

    def handle_data(self, data):
        self.parts.append(data)
        if self._in_title:
            self._title.append(data)


def _collapse(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _unescape_json(t: str) -> str:
    for a, b in _JSON_ESCAPES:
        t = t.replace(a, b)
    return t


def _json_objects(raw: str):
    for m in _SCRIPT.finditer(raw):
        body = m.group(1).strip()
        if not body:
            continue
        for cand in (body, body[body.find("{"): body.rfind("}") + 1] if "{" in body else ""):
            if not cand:
                continue
            try:
                obj = json.loads(cand)
            except ValueError:
                continue
            yield obj
            break


def _walk(obj):
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            yield x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def _code(original: str) -> str | None:
    try:
        path = urlsplit(original).path
    except ValueError:
        return None
    seg = path.rstrip("/").rsplit("/", 1)[-1].lower()
    codes = _CODE.findall(seg)
    return codes[-1] if len(set(codes)) == 1 else None


def notice_id(original: str) -> str:
    """``<locale>:<code>`` of an announcement URL, else ``url:<host+path>`` (module docstring)."""
    code = _code(original)
    try:
        u = urlsplit(original if "://" in original else "https://" + original)
        host, path = (u.hostname or "").lower(), u.path
    except ValueError:
        host, path = "", original
    if code is not None:
        first = path.strip("/").split("/", 1)[0].lower()
        return f"{first if _LOCALE.fullmatch(first) else ''}:{code}"
    host = host[4:] if host.startswith("www.") else host
    return "url:" + (host + path.lower()).rstrip("/")


def release_date(raw: str, original: str) -> tuple[int | None, str]:
    """(``releaseDate`` in ns, reason) from inside the decoded capture ``raw`` (module docstring)."""
    code = _code(original)
    bound: set[int] = set()
    if code is not None:
        for obj in _json_objects(raw):
            for d in _walk(obj):
                v = d.get("releaseDate")
                if isinstance(v, int) and not isinstance(v, bool) and str(d.get("code", "")).lower() == code:
                    bound.add(v)
    if len(bound) > 1:
        return None, "release_date_conflict"
    if bound:
        ms, why = bound.pop(), "code_bound_json"
    elif code is not None:  # the page's own article is identifiable: only its object may give the date
        return None, "no_code_bound_release_date"
    else:
        vals = {int(m.group(1)) for m in _RELEASE.finditer(_unescape_json(raw))}
        if not vals:
            return None, "no_release_date"
        if len(vals) > 1:
            return None, "release_date_ambiguous"
        ms, why = vals.pop(), "unique_in_page"
    if not _MS_LO <= ms < _MS_HI:
        return None, "release_date_not_ms_epoch"
    return ms * 10**6, why


def extract(body: bytes, original: str) -> CaptureText:
    """Deterministic title / text / ``releaseDate`` of one capture (module docstring)."""
    raw = decode(body)
    p = _Text()
    p.feed(_unescape_json(raw))
    p.close()
    title = None if p.title is None else (_collapse(p.title).split(" | ")[0].strip() or None)
    page = _collapse(" ".join(p.parts))
    text = f"{title or ''}\n{page}"
    rel, why = release_date(raw, original)
    keys = tuple(sorted({m.group(1) for m in _DATE_KEY.finditer(_unescape_json(raw))}))
    return CaptureText(title, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), rel, why, keys)


def _norm(title: str) -> str:
    return _collapse(unicodedata.normalize("NFKC", title).casefold())


def classify_kind(title: str | None) -> tuple[Kind | None, str]:
    """(kind, reason) of a notice from its title by the closed phrase lists (module docstring); None =
    unclassified (ignored: never a delisting / postponement / cancellation)."""
    if not title:
        return None, "no_title"
    t = _norm(title)
    cancel = [p for p in CANCELLATION_PHRASES if p in t]
    post = [p for p in POSTPONEMENT_PHRASES if p in t]
    if cancel and post:
        return None, "ambiguous_kind:cancellation+postponement"
    if cancel or post:
        kind: Kind = "cancellation" if cancel else "postponement"
        rest = t
        for p in sorted(cancel or post, key=len, reverse=True):
            rest = rest.replace(p, " ")
        if any(p in rest for p in DELISTING_PHRASES):
            return None, f"ambiguous_kind:{kind}+delisting"
        return kind, f"{kind}:" + "|".join(cancel or post)
    hits = [p for p in DELISTING_PHRASES if p in t]
    if hits:
        return "delisting", "delisting:" + "|".join(hits)
    return None, "no_kind_phrase"
