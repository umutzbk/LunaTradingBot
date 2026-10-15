"""Immutable Wayback captures of Binance announcement pages -> the notice's own title / body text,
``releaseDate``, notice identity and the closed-list notice kind (spec S-2.5, DEC-26 / DEC-36 / DEC-40, S-1.7
N5 / N6, R11; DK-05; Task 26c, AMB-39).

Everything here is pure (no I/O, no clock) and deterministic.

* **The notice's article object** (``article``): the JSON objects of the page's ``<script>`` elements (the
  whole script, else its outermost ``{...}``) whose ``code`` equals the 32-hex article code of the original
  URL. They must agree on ``title`` / ``body`` / ``releaseDate``. A URL without a code, no such object, an
  object without a string title and body, or disagreeing objects: the capture is *unusable* (reported with
  the reason, never guessed). A related article (another code) is never read.
* **Text** (``extract``, S-2.5 / DEC-40 "in the archived title or body"): ``CaptureText.text`` = the article
  title + newline + the article body as text. HTML -> text is the Stage-0 extraction
  (``docs/results/v7-stage0/scripts/m31_notices.py`` ``analyse``) ported to the stdlib ``html.parser``: the
  JSON escapes Stage 0 undid (``\\u003c``, ``\\u003e``, ``\\u0026``, ``\\n``, ``\\"``) are undone in that
  order, every tag is stripped (``<script>`` / ``<style>`` contents parsed as markup, as Stage 0's tag regex
  did), character references are unescaped, data chunks joined by spaces, whitespace collapsed. The whole
  page text (gunzipped when the bytes carry the gzip magic, UTF-8 with errors replaced) and the page
  ``<title>`` (cut at the first `` | ``) are kept as diagnostics only (``page_text``, ``page_title``); they
  are never matched.
* **releaseDate**: the article object's integer ``releaseDate`` (ms) only; never from the CMS, never from a
  related article. Missing / not a plausible ms epoch: unusable.
* **Notice identity** (``notice_id``): ``<locale>:<32-hex article code>`` from the original URL (the slug and
  host spelling do not matter); a URL without a code is identified by ``url:<host+path>``.
* **Kind** (``classify_kind``): CLOSED phrase lists on the NFKC-casefolded, whitespace-collapsed article title
  only; no semantic inference (DK-05 approach; AMB-39 (e), to be ratified with the DK-05 validation report).
  An explicit cancellation / postponement phrase decides the kind unless both occur, or a delisting phrase is
  still present once those phrases are removed (a mixed title): then unclassified. Otherwise a delisting
  phrase gives delisting only when no MODIFIER stem (``MODIFIER_STEMS``: cancel, postpon, delay, extend,
  revok, ...) occurs in the title; with one, the title is unclassified (``ambiguous_kind:modifier:<stem>``).
  No phrase -> unclassified. Unclassified versions never enter R11 or U6.
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

__all__ = ["CANCELLATION_PHRASES", "DELISTING_PHRASES", "MODIFIER_STEMS", "POSTPONEMENT_PHRASES", "Capture",
           "CaptureText", "article", "capture_ts_ns", "classify_kind", "decode", "extract", "html_text",
           "notice_id", "release_date"]

# AMB-39 (e): the closed kind phrase lists (compared on the NFKC-casefolded, whitespace-collapsed title).
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
# Fix round 1 (I1): a delisting title carrying any of these stems is not a delisting (unclassified).
# Matched on the title padded with spaces (" not " needs its spaces).
MODIFIER_STEMS: tuple[str, ...] = ("cancel", "postpon", "delay", "extend", "extens", "revok", "revoc",
                                   "revers", "resum", " not ", "n't", "updat", "withdraw", "suspend")

_JSON_ESCAPES = (("\\u003c", "<"), ("\\u003e", ">"), ("\\u0026", "&"), ("\\n", " "), ('\\"', '"'))
_WS = re.compile(r"\s+")
_TS14 = re.compile(r"\d{14}", re.ASCII)
_CODE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.ASCII)
_LOCALE = re.compile(r"[a-z]{2}(?:-[a-z]{2,4})?", re.ASCII)
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
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
    """``text`` / ``title`` / ``release_ts``: the notice's own article (None when ``article_reason`` is not
    ``ok``: the capture is unusable). ``text_sha256``: of ``text``, or of the page text for an unusable
    capture (version chaining only). ``page_title`` / ``page_text`` / ``date_keys``: diagnostics only."""

    title: str | None
    text: str | None
    text_sha256: str
    release_ts: int | None
    article_reason: str
    page_title: str | None
    page_text: str
    date_keys: tuple[str, ...]

    @property
    def release_reason(self) -> str:
        return "article_release_date" if self.release_ts is not None else self.article_reason


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


def _parse(html: str) -> _Text:
    p = _Text()
    p.feed(_unescape_json(html))
    p.close()
    return p


def html_text(html: str) -> str:
    """Stage-0 HTML -> text (module docstring)."""
    return _collapse(" ".join(_parse(html).parts))


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


def _release_ms(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and _MS_LO <= v < _MS_HI else None


def article(raw: str, original: str) -> tuple[tuple[str, str, int] | None, str]:
    """((title, body, releaseDate ms) of the notice's own article object, "ok") or (None, reason): reasons
    ``no_article_code``, ``no_article_object``, ``article_conflict``, ``article_incomplete``,
    ``no_release_date`` (module docstring)."""
    code = _code(original)
    if code is None:
        return None, "no_article_code"
    found: set[tuple[object, object, object]] = set()
    for obj in _json_objects(raw):
        for d in _walk(obj):
            if str(d.get("code", "")).lower() == code:
                found.add((d.get("title"), d.get("body"), d.get("releaseDate")))
    if not found:
        return None, "no_article_object"
    if len(found) > 1:
        return None, "article_conflict"
    title, body, rel = found.pop()
    if not isinstance(title, str) or not isinstance(body, str) or not (title.strip() or body.strip()):
        return None, "article_incomplete"
    ms = _release_ms(rel)
    if ms is None:
        return None, "no_release_date"
    return (title, body, ms), "ok"


def release_date(raw: str, original: str) -> tuple[int | None, str]:
    """(``releaseDate`` in ns, reason) of the notice's own article object only (module docstring)."""
    a, why = article(raw, original)
    return (None, why) if a is None else (a[2] * 10**6, "article_release_date")


def extract(body: bytes, original: str) -> CaptureText:
    """Deterministic notice title / text / ``releaseDate`` of one capture plus page diagnostics."""
    raw = decode(body)
    page = _parse(raw)
    page_title = None if page.title is None else (_collapse(page.title).split(" | ")[0].strip() or None)
    page_text = _collapse(" ".join(page.parts))
    keys = tuple(sorted({m.group(1) for m in _DATE_KEY.finditer(_unescape_json(raw))}))
    a, why = article(raw, original)
    if a is None:
        sha = hashlib.sha256(("unusable\n" + page_text).encode("utf-8")).hexdigest()
        return CaptureText(None, None, sha, None, why, page_title, page_text, keys)
    title = _collapse(html_text(a[0])) or None
    text = f"{title or ''}\n{html_text(a[1])}"
    return CaptureText(title, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), a[2] * 10**6, "ok",
                       page_title, page_text, keys)


def _norm(title: str) -> str:
    return _collapse(unicodedata.normalize("NFKC", title).casefold())


def classify_kind(title: str | None) -> tuple[Kind | None, str]:
    """(kind, reason) of a notice from its title by the closed phrase lists and the modifier veto (module
    docstring); None = unclassified (ignored: never a delisting / postponement / cancellation)."""
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
    if not hits:
        return None, "no_kind_phrase"
    padded = f" {t} "
    mods = [s for s in MODIFIER_STEMS if s in padded]
    if mods:
        return None, "ambiguous_kind:modifier:" + "|".join(m.strip() for m in mods)
    return "delisting", "delisting:" + "|".join(hits)
