"""Immutable Wayback captures of Binance announcement pages -> the notice's own title / body text,
``releaseDate``, notice identity and the closed-list notice kind (spec S-2.5, DEC-26 / DEC-36 / DEC-40, S-1.7
N5 / N6, R11; DK-05; Task 26c, AMB-39; Task 26d, controller rulings R50 / R51 / R52).

Everything here is pure (no I/O, no clock) and deterministic.

* **The notice's article object** (``article`` / ``article_release``): the JSON objects of the page's
  ``<script>`` elements (the whole script, else its outermost ``{...}``) whose ``code`` equals the 32-hex
  article code of the original URL AND that carry a string ``body`` (a catalog / "latest" list / SEO entry of
  the same code without a body is not a candidate; fix round 2, N1). The candidates must agree on ``title`` /
  ``body`` / ``releaseDate`` / ``publishDate``. A URL without a code, no same-code object, none carrying a
  body, or disagreeing candidates: the capture is *unusable* (reported with the reason, never guessed). A
  related article (another code) is never read.
* **Text** (``extract``, S-2.5 / DEC-40 "in the archived title or body"): ``CaptureText.text`` = the article
  title + newline + the article body as text (``body_text``). A body that is a JSON object with a ``node``
  key is a Binance node tree (R51; every real Stage-0 capture): ``node_tree_text`` walks it in document order
  and keeps only the ``text`` of ``{"node": "text"}`` leaves (character references unescaped, inner
  whitespace collapsed); a block element (``p``, ``h1``-``h6``, ``li``, ``tr``, ``br``, ``div``, ``ul`` /
  ``ol``, ``table`` / ``tbody`` / ``thead``, ``figure``, ...) breaks the line, table cells (``td`` / ``th``)
  of one row are joined by `` | ``, any other element (inline ``span`` / ``strong`` / ``a``, unknown tags) is
  walked and contributes only its text; ``attr`` values are never text; empty lines are dropped. The node
  shapes are CLOSED (``root`` {node, child}, ``element`` {node, tag, attr?, child?}, ``text`` {node, text}, as
  in all 54 real captures): any other node type, key (e.g. ``children``) or child is refused and the capture
  is unusable (``unknown_node_shape``; Task 26d round 1, fail closed, never a partial text). The lines
  are kept, so ``notices._SENT_RE`` splits sentences and ``notices._TS_RE`` reads timestamps on real text.
  Any other body is HTML -> text by the Stage-0 extraction
  (``docs/results/v7-stage0/scripts/m31_notices.py`` ``analyse``) ported to the stdlib ``html.parser``: the
  JSON escapes Stage 0 undid (``\\u003c``, ``\\u003e``, ``\\u0026``, ``\\n``, ``\\"``) are undone in that
  order, every tag is stripped (``<script>`` / ``<style>`` contents parsed as markup, as Stage 0's tag regex
  did), character references are unescaped, data chunks joined by spaces, whitespace collapsed. The whole
  page text (gunzipped when the bytes carry the gzip magic, UTF-8 with errors replaced) and the page
  ``<title>`` (cut at the first `` | ``) are kept as diagnostics only (``page_text``, ``page_title``); they
  are never matched.
* **releaseDate** (R50; Binance's article release time): the ``releaseDate`` (ms) of the same-code objects
  (on the real pages only the body-less catalog / list entries ``{id, code, title, type, releaseDate}``
  carry it; an article object carrying it counts the same), else the article object's own ``publishDate``
  (same semantics). Both present and different: the LATER one (``known_at`` = max(releaseDate, capture ts) is
  then never earlier). Same-code ``releaseDate`` values that disagree: unusable (``release_date_conflict``).
  A value that is not a plausible ms epoch is no date. The source is recorded (``release_src``:
  ``releaseDate``, ``publishDate``, ``releaseDate=publishDate``, or ``max:releaseDate`` /
  ``max:publishDate`` = the later of two different values). Never from the CMS, never from another code.
  Neither date: unusable (``no_release_date``).
* **Notice identity** (``notice_id``): ``<locale>:<32-hex article code>`` from the original URL (the slug and
  host spelling do not matter); a URL without a code is identified by ``url:<host+path>``.
* **Kind** (``classify_kind``): CLOSED phrase lists on the NFKC-casefolded, whitespace-collapsed article title
  only; no semantic inference (DK-05 approach; AMB-39 (e), ratified by R52 from the C4 evidence).
  R52 (a): when ``updat`` is the title's ONLY modifier stem and every ``updat`` lies inside an
  ``updat(e|es|ed|ing) [on ][the ]leverage (&|and) margin tiers`` span (``LEVERAGE_TIERS_UPDATE``), those
  spans are dropped first and the remainder is classified normally. R52 (b): a title with no cancellation /
  postponement phrase in the evidence word order ``delisting of ... postponed`` (``DELISTING_OF_POSTPONED``),
  with ``postponed`` and ``delist`` exactly once each, no other delisting phrase and no modifier stem other
  than ``postpon`` is a postponement ("Delisting of ... Postponed", "Delisting of ... Will Be Further
  Postponed"); any other order ("Will Delist X (Previously Postponed)") keeps the veto.
  An explicit cancellation / postponement phrase decides the kind unless both occur, or a delisting phrase is
  still present once those phrases are removed (a mixed title): then unclassified. Otherwise a delisting
  phrase gives delisting only when no MODIFIER stem (``MODIFIER_STEMS``: cancel, postpon, delay, extend,
  revok, ...) occurs in the title; with one, the title is unclassified (``ambiguous_kind:modifier:<stem>``).
  No phrase -> unclassified. Unclassified versions never enter R11 or U6.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from luna.v7.data.notices import Kind
from luna.v7.timeutil import to_ns

__all__ = ["CANCELLATION_PHRASES", "DELISTING_OF_POSTPONED", "DELISTING_PHRASES", "LEVERAGE_TIERS_UPDATE",
           "MODIFIER_STEMS", "POSTPONEMENT_PHRASES", "Capture", "CaptureText", "UnknownNodeShape", "article",
           "article_release", "body_text", "capture_ts_ns", "classify_kind", "decode", "extract", "html_text",
           "node_tree_text", "notice_id", "release_date"]

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
# R52 (a): the only "updat" use that does not veto (28 of 47 W_DEV catalog titles, C4); normalized title.
LEVERAGE_TIERS_UPDATE = re.compile(r"updat(?:e|es|ed|ing) (?:on )?(?:the )?leverage (?:&|and) margin tiers")
# R52 (b): the only postponement word order with evidence ("Delisting of ... (Will Be Further) Postponed").
DELISTING_OF_POSTPONED = re.compile(r"\bdelisting of\b.*\bpostponed\b")

# R51: node-tree element tags that break the line / separate table cells (any other node: its text only).
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre",
    "section", "table", "tbody", "tfoot", "thead", "tr", "ul"})
_CELL_TAGS = frozenset({"td", "th"})
_CELL = "\x1f"  # internal cell boundary (never left in the text)

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
    capture (version chaining only). ``page_title`` / ``page_text`` / ``date_keys``: diagnostics only.
    ``release_src``: where ``release_ts`` came from (R50; None when unusable)."""

    title: str | None
    text: str | None
    text_sha256: str
    release_ts: int | None
    article_reason: str
    page_title: str | None
    page_text: str
    date_keys: tuple[str, ...]
    release_src: str | None = None

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


def _node_tree(body: str) -> dict | None:
    """The parsed node tree when ``body`` is a JSON object with a ``node`` key (R51), else None."""
    s = body.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and "node" in obj else None


def _leaf(t: str) -> str:
    return _WS.sub(" ", html.unescape(t).replace(_CELL, " "))


class UnknownNodeShape(ValueError):
    """A node-tree body with a node outside the closed shapes of ``_SHAPES`` (fail closed: the capture is
    unusable, ``unknown_node_shape``)."""


# The closed node shapes of the real bodies (C4: root {node, child}, element {node, tag, attr?, child?}, text
# {node, text}); anything else (another node type, extra keys such as ``children``, a non-object child) is
# refused instead of silently dropped (Task 26d round 1).
_SHAPES: dict[str, frozenset[str]] = {"root": frozenset({"node", "child"}),
                                      "element": frozenset({"node", "tag", "attr", "child"}),
                                      "text": frozenset({"node", "text"})}


def _check_shape(n: object) -> dict:
    if not isinstance(n, dict) or n.get("node") not in _SHAPES or not set(n) <= _SHAPES[n["node"]]:
        raise UnknownNodeShape(f"unknown node shape {sorted(n) if isinstance(n, dict) else type(n).__name__}"
                               f" (node={n.get('node') if isinstance(n, dict) else None!r})")
    kind = n["node"]
    if kind == "text" and not isinstance(n.get("text"), str):
        raise UnknownNodeShape("text node without a string text")
    if kind == "element" and not isinstance(n.get("tag"), str):
        raise UnknownNodeShape("element node without a string tag")
    if "attr" in n and not isinstance(n["attr"], dict):
        raise UnknownNodeShape("element attr is not an object")
    if "child" in n and not isinstance(n["child"], list):
        raise UnknownNodeShape("child is not a list")
    return n


def _tree_text(root: object) -> str:
    """Text of a node tree (module docstring); raises ``UnknownNodeShape`` on any node outside ``_SHAPES``."""
    if not isinstance(root, dict) or root.get("node") != "root":
        raise UnknownNodeShape("the tree does not start with a root node")
    out: list[str] = []
    stack: list[object] = [root]
    while stack:  # iterative pre-order walk; ("sep", s) entries are the block / cell separators
        n = stack.pop()
        if isinstance(n, tuple):
            out.append(n[1])
            continue
        n = _check_shape(n)
        if n["node"] == "text":
            out.append(_leaf(n["text"]))
        else:
            tag = str(n.get("tag") or "").lower()  # an unknown tag is walked like an inline element
            sep = "\n" if tag in _BLOCK_TAGS else _CELL if tag in _CELL_TAGS else ""
            kids = n.get("child", [])
            if sep:
                stack.append(("sep", sep))
            stack.extend(reversed(kids))
            if sep:
                stack.append(("sep", sep))
    lines = []
    for line in "".join(out).split("\n"):
        cells = [c for c in (_collapse(x) for x in line.split(_CELL)) if c]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def node_tree_text(body: str) -> str | None:
    """R51 node tree -> text (module docstring), or None when ``body`` is not a node tree; a node tree with a
    node outside the closed shapes raises ``UnknownNodeShape``."""
    tree = _node_tree(body)
    return None if tree is None else _tree_text(tree)


def body_text(body: str) -> str:
    """The article body as text: a node tree by ``node_tree_text`` (R51), anything else by ``html_text``."""
    t = node_tree_text(body)
    return html_text(body) if t is None else t


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


def article_release(raw: str, original: str) -> tuple[tuple[str, str, int] | None, str, str | None]:
    """((title, body, release ms), "ok", release_src) of the notice's own article object, or (None, reason,
    None): reasons ``no_article_code``, ``no_article_object``, ``article_conflict``, ``article_incomplete``,
    ``release_date_conflict``, ``no_release_date`` (module docstring; R50)."""
    code = _code(original)
    if code is None:
        return None, "no_article_code", None
    same = False
    found: set[tuple[object, object, object, object]] = set()
    rel_values: list[object] = []
    for obj in _json_objects(raw):
        for d in _walk(obj):
            if str(d.get("code", "")).lower() == code:
                same = True
                if "releaseDate" in d:  # a catalog / list entry or the article object, same code only (R50)
                    rel_values.append(d.get("releaseDate"))
                if isinstance(d.get("body"), str):  # a candidate carries the body (round 2, N1)
                    found.add((d.get("title"), d.get("body"), d.get("releaseDate"), d.get("publishDate")))
    if not same:
        return None, "no_article_object", None
    if not found:
        return None, "article_incomplete", None  # only catalog / list / SEO entries of the code (no body)
    if len(found) > 1:
        return None, "article_conflict", None
    title, body, _rel, pub_raw = found.pop()
    if not isinstance(title, str) or not (title.strip() or body.strip()):  # type: ignore[union-attr]
        return None, "article_incomplete", None
    rels = {m for m in (_release_ms(v) for v in rel_values) if m is not None}
    if len(rels) > 1:
        return None, "release_date_conflict", None
    rel = rels.pop() if rels else None
    pub = _release_ms(pub_raw)
    if rel is None and pub is None:
        return None, "no_release_date", None
    if pub is None:
        ms, src = rel, "releaseDate"
    elif rel is None:
        ms, src = pub, "publishDate"
    elif rel == pub:
        ms, src = rel, "releaseDate=publishDate"
    else:  # both, different: the later one (R50)
        ms, src = (rel, "max:releaseDate") if rel > pub else (pub, "max:publishDate")
    return (title, body, ms), "ok", src  # type: ignore[return-value]


def article(raw: str, original: str) -> tuple[tuple[str, str, int] | None, str]:
    """((title, body, release ms) of the notice's own article object, "ok") or (None, reason):
    ``article_release`` without the source."""
    a, why, _src = article_release(raw, original)
    return a, why


def release_date(raw: str, original: str) -> tuple[int | None, str]:
    """(release time in ns, reason) of the notice's own article object only (module docstring; R50)."""
    a, why = article(raw, original)
    return (None, why) if a is None else (a[2] * 10**6, "article_release_date")


def extract(body: bytes, original: str) -> CaptureText:
    """Deterministic notice title / text / ``releaseDate`` of one capture plus page diagnostics."""
    raw = decode(body)
    page = _parse(raw)
    page_title = None if page.title is None else (_collapse(page.title).split(" | ")[0].strip() or None)
    page_text = _collapse(" ".join(page.parts))
    keys = tuple(sorted({m.group(1) for m in _DATE_KEY.finditer(_unescape_json(raw))}))
    a, why, src = article_release(raw, original)
    if a is None:
        sha = hashlib.sha256(("unusable\n" + page_text).encode("utf-8")).hexdigest()
        return CaptureText(None, None, sha, None, why, page_title, page_text, keys)
    try:
        body = body_text(a[1])
    except UnknownNodeShape:  # fail closed (Task 26d round 1): never a partial text
        sha = hashlib.sha256(("unusable\n" + page_text).encode("utf-8")).hexdigest()
        return CaptureText(None, None, sha, None, "unknown_node_shape", page_title, page_text, keys)
    title = _collapse(html_text(a[0])) or None
    text = f"{title or ''}\n{body}"
    return CaptureText(title, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), a[2] * 10**6, "ok",
                       page_title, page_text, keys, src)


def _norm(title: str) -> str:
    return _collapse(unicodedata.normalize("NFKC", title).casefold())


def _mods(t: str) -> list[str]:
    padded = f" {t} "
    return [s for s in MODIFIER_STEMS if s in padded]


def classify_kind(title: str | None) -> tuple[Kind | None, str]:
    """(kind, reason) of a notice from its title by the closed phrase lists, the modifier veto and the R52
    rules (module docstring); None = unclassified (ignored: never a delisting / postponement /
    cancellation)."""
    if not title:
        return None, "no_title"
    t = _norm(title)
    note = ""
    if _mods(t) == ["updat"]:  # R52 (a): drop leverage & margin tier update spans holding every updat
        rest = _collapse(LEVERAGE_TIERS_UPDATE.sub(" ", t))
        if rest != t and "updat" not in rest:
            t, note = rest, ";r52a:leverage_margin_tiers_update_dropped"
    kind, why = _classify(t)
    return kind, why + note


def _classify(t: str) -> tuple[Kind | None, str]:
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
    mods = _mods(t)
    # R52 (b): "Delisting of ... (Will Be Further) Postponed" (no cancellation / postponement phrase here)
    if (mods == ["postpon"] and t.count("postponed") == 1 and t.count("delist") == 1 and hits == ["delist"]
            and DELISTING_OF_POSTPONED.search(t)):
        return "postponement", "postponement:r52b:postponed+delist"
    if mods:
        return None, "ambiguous_kind:modifier:" + "|".join(m.strip() for m in mods)
    return "delisting", "delisting:" + "|".join(hits)
