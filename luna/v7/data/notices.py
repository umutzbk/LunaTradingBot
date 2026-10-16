"""Point-in-time delisting notices and the U6 state machine (spec S-2.5; DEC-26/36/37/40; R11).

* ``known_at``: history ``max(releaseDate, archive capture timestamp)``; forward ``recv_ts``. A version
  with neither a capture nor a recv timestamp (current CMS text) has no ``known_at`` and is never
  backdated: it can never activate U6.
* ``confident_match``: the exact contract symbol as a whole token (non-alphanumeric boundaries, so
  ``1000PEPEUSDT`` does not match ``PEPEUSDT``) AND an unambiguous USDⓈ-M futures / perpetual context
  (``USDM_PHRASES`` / ``USDM_PAIR``, AMB-05). Anything else is no match; no fuzzy or semantic inference.
* A-1 (AMENDMENTS-2026-10-16, normative amendment by the user; in addition to DEC-40, which stays in force):
  **base-asset match** (``match_reason(v, symbol, bases)``, reason
  ``base_asset_match:<BASE>;usdm_context:...``):
  the verified R1 / E8 identity ``baseAsset`` of the symbol's instance live at the version's ``known_at``
  (``instances.started_instance``, the DEC-40 attribution; quote asset USDT; multiplier prefix included, e.g.
  ``1000BTTC``) occurs as a whole token (the DEC-40 boundaries, case-sensitive like the symbol token) in the
  notice's own title or body, the same USDⓈ-M context holds and no COIN-M veto applies. A base that maps to
  more than one live USD-M instance with quote USDT at ``known_at`` (any symbol, e.g. a perpetual and a
  delivery contract) is ambiguous: no match (``base_asset_ambiguous:<BASE>:<n>``). PIT: only instances
  started by ``known_at`` (and not ended) count, for the target and for the ambiguity, with their verified
  invariant identity (DEC-31; R1 ``known_at`` = the instance's first bar); an unverified identity is never a
  base. A version without ``known_at`` is never base-matched. The ingest (``metaingest.build_notices``,
  which holds the whole instance / identity set) decides it and carries the base-matched symbols on the
  version (``NoticeVersion.base_matched``; rebuilt from R11 ``matched_instances`` by
  ``metastore.load_notice_versions``); ``U6State`` / ``terminal`` read the carried decision (the context is
  re-checked). ``scheduled_ts`` (DEC-37), versioning and U6 are unchanged: candidates still come only from
  sentences with the exact symbol token, so a base-only notice has ``scheduled_ts`` None.
* A-5 (AMENDMENTS-2026-10-16, user decision on AMB-46; refines A-1 rule 1 only): the base token and a USDⓈ-M
  context phrase must occur in the SAME sentence (sentences split by ``_SENT_RE``, as DEC-37, on the R51
  text; the ``perpetual contract`` + ``futures`` pair counts with both in that sentence). The COIN-M veto
  stays notice-wide (as A-1), the ambiguity rule is unchanged; no such sentence ->
  ``base_asset:<BASE>;no_same_sentence_usdm_context``. The DEC-40 exact-symbol match keeps the per-notice
  context, the token-swap kind is unchanged; a carried match (decided at ingest under A-5) re-checks the
  notice-wide context only.
* ``scheduled_ts``: only an explicitly mapped UTC timestamp, else None (DEC-37).
* ``U6State.active``: activation at ``known_at``; deactivation by a later matched postponement /
  cancellation at its ``known_at``; the 72 h auto-clear only with a unique scheduled time; a late
  ``known_at`` evaluates the 72 h test at ``known_at``; U6 ends with its instance.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, start_known_at, started_instance
from luna.v7.timeutil import MIN, H, to_ns

Kind = Literal["delisting", "postponement", "cancellation"]

# S-2.5 / DEC-40 (AMB-05): context phrases, compared after NFKC + casefold (so "USDⓈ-M" -> "usds-m").
USDM_PHRASES: tuple[str, ...] = ("usds-m", "usdt-margined", "usds-margined")
# "perpetual contract(s)" counts only together with "futures".
USDM_PAIR: tuple[str, str] = ("perpetual contract", "futures")
# A COIN-M mention makes the context ambiguous -> no match.
COIN_M_PHRASES: tuple[str, ...] = ("coin-m", "coin-margined")

CLEAR_AFTER_NS = FROZEN.notices.u6_clear_h * H
HALT_GRID_NS = MIN  # the 72 h "not HALTED at any time" test samples halted_fn on this grid (AMB-05)

_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\s*\(UTC\)", re.IGNORECASE
)
_SYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{2,}(?:USDT|USDC|BUSD)(?![A-Za-z0-9])")
_SENT_RE = re.compile(r"(?<=[.;!?])\s+|\n+")


@dataclass(frozen=True)
class NoticeVersion:
    notice_id: str
    version: int
    kind: Kind
    release_ts: int
    capture_ts: int | None
    text: str
    sha256: str
    recv_ts: int | None = None
    # A-1: the symbols whose match the ingest established by the base-asset rule (PIT, with the identity set)
    base_matched: tuple[str, ...] = ()


def known_at(v: NoticeVersion) -> int | None:
    """Forward ``recv_ts``; history ``max(release_ts, capture_ts)``; no snapshot -> None (never backdated)."""
    if v.recv_ts is not None:
        return int(v.recv_ts)
    if v.capture_ts is None:
        return None
    return max(int(v.release_ts), int(v.capture_ts))


def _token_re(symbol: str) -> re.Pattern[str]:
    # '_' and '-'/'.' followed by an alphanumeric continue the token (XXXUSDT_240628): no match
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])(?![-.][A-Za-z0-9])")


def _context(text: str) -> tuple[bool, str]:
    """(unambiguous USDⓈ-M context, reason): the vetoing COIN-M phrase, the context phrase found, or none."""
    t = unicodedata.normalize("NFKC", text).casefold()
    veto = [p for p in COIN_M_PHRASES if p in t]
    if veto:
        return False, "coin_m_veto:" + "|".join(veto)
    found = [p for p in USDM_PHRASES if p in t]
    if found:
        return True, "usdm_context:" + "|".join(found)
    if all(p in t for p in USDM_PAIR):
        return True, "usdm_context:" + "+".join(USDM_PAIR)
    return False, "no_usdm_context"


def _usdm_context(text: str) -> bool:
    return _context(text)[0]


class _Ident(Protocol):  # ``universe.Identity`` (R1 / E8; universe imports this module)
    base: str | None
    quote_asset: str | None
    invariant_verified: bool


BASE_QUOTE = "USDT"  # A-1: "a live USD-M instance with quote USDT"


class InstanceBases:
    """A-1: the instances and their verified R1 identities (per symbol, per instance idx), resolving a
    base-asset token at a version's ``known_at`` (module docstring)."""

    def __init__(self, insts_by_symbol: Mapping[str, Sequence[Instance]],
                 identities: Mapping[str, Mapping[int, _Ident] | _Ident | None]) -> None:
        self._insts = {s: list(v) for s, v in insts_by_symbol.items()}
        self._ids = dict(identities)
        self._by_base: dict[str, list[str]] = {}
        for s in sorted(self._insts):
            for i in self._insts[s]:
                b = self._usdt_base(s, i.idx)
                if b is not None and s not in self._by_base.setdefault(b, []):
                    self._by_base[b].append(s)

    def _usdt_base(self, symbol: str, idx: int) -> str | None:
        d = self._ids.get(symbol)
        ident = d.get(idx) if isinstance(d, Mapping) else d
        if ident is None or ident.invariant_verified is not True or not ident.base or \
                ident.quote_asset != BASE_QUOTE:
            return None
        return str(ident.base)

    def symbols_with_base(self, base: str) -> tuple[str, ...]:
        """Every symbol with some verified USDT instance of this base (candidates only; not PIT)."""
        return tuple(self._by_base.get(base, ()))

    def bases(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_base))

    def live_base(self, symbol: str, ka: int) -> str | None:
        """The verified USDT base of ``symbol``'s instance live at ``ka`` (``started_instance``), or
        None."""
        inst = started_instance(self._insts.get(symbol, []), int(ka))
        return None if inst is None else self._usdt_base(symbol, inst.idx)

    def live_count(self, base: str, ka: int) -> int:
        """Live USD-M instances with quote USDT and this verified base at ``ka`` (one per symbol at most)."""
        return sum(1 for s in self._by_base.get(base, ()) if self.live_base(s, ka) == base)


def base_match_reason(v: NoticeVersion, symbol: str, bases: InstanceBases) -> tuple[bool, str]:
    """A-1 rule 1 for a symbol whose exact token is absent (module docstring)."""
    ka = known_at(v)
    if ka is None:
        return False, "no_symbol_token;base_asset:no_known_at"
    base = bases.live_base(symbol, ka)
    if base is None:
        return False, "no_symbol_token;base_asset:no_live_usdt_identity"
    if _token_re(base).search(v.text) is None:
        return False, f"no_symbol_token;base_asset:no_base_token:{base}"
    n = bases.live_count(base, ka)
    if n != 1:
        return False, f"no_symbol_token;base_asset_ambiguous:{base}:{n}"
    ok, why = _context(v.text)  # the notice-wide test: the COIN-M veto (and a context at all), as A-1
    if not ok:
        return False, f"base_asset:{base};{why}"
    tok = _token_re(base)
    for sent in _SENT_RE.split(v.text):  # A-5: the base token and a context phrase in the SAME sentence
        if tok.search(sent) is not None:
            s_ok, s_why = _context(sent)
            if s_ok:
                return True, f"base_asset_match:{base};{s_why}"
    return False, f"base_asset:{base};no_same_sentence_usdm_context"


def match_reason(v: NoticeVersion, symbol: str, bases: InstanceBases | None = None) -> tuple[bool, str]:
    """``confident_match`` with its reason (Task 26c ingest report): ``no_symbol_token``, the context reason
    of ``_context`` (``coin_m_veto:...`` / ``no_usdm_context`` -> no match; ``usdm_context:...`` -> match).
    Without the exact symbol token, A-1: with ``bases`` (the ingest) the base-asset rule decides
    (``base_match_reason``); without, a match the ingest carried on the version (``base_matched``) holds when
    the context still holds (``base_asset_match:carried;...``)."""
    if not symbol:
        return False, "no_symbol_token"
    if _token_re(symbol).search(v.text) is not None:
        return _context(v.text)
    if bases is not None:
        return base_match_reason(v, symbol, bases)
    if symbol in v.base_matched:
        ok, why = _context(v.text)
        return (True, f"base_asset_match:carried;{why}") if ok else (False, f"base_asset:carried;{why}")
    return False, "no_symbol_token"


def confident_match(v: NoticeVersion, symbol: str) -> bool:
    return match_reason(v, symbol)[0]


def _parse_ts(m: re.Match[str]) -> int | None:
    hour, minute, sec = int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)
    ampm = (m.group(5) or "").upper()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "PM" else 0)
    if hour > 23 or minute > 59 or sec > 59:
        return None
    return to_ns(f"{m.group(1)} {hour:02d}:{minute:02d}:{sec:02d}")


def scheduled_ts(v: NoticeVersion, symbol: str) -> int | None:
    """Unique, explicitly mapped scheduled delisting time of ``symbol`` (DEC-37), else None.

    Candidates come only from sentences that contain the symbol token: a sentence with exactly one
    timestamp maps it to the symbols it names; a sentence with several timestamps maps only through an
    ordered "A and B ... T1 and T2 respectively" list (equal counts, symbol listed once). Exactly one
    distinct candidate must result; any unmappable sentence or any disagreement gives None.
    """
    return scheduled_reason(v, symbol)[0]


def scheduled_reason(v: NoticeVersion, symbol: str) -> tuple[int | None, str]:
    """``scheduled_ts`` with the reason of its result (Task 26c ingest report): ``mapped`` (a time) or, for
    None, ``no_confident_match``, ``unparseable_timestamp``, ``several_timestamps_without_respectively``,
    ``respectively_list_mismatch``, ``no_timestamp_in_symbol_sentences`` or ``conflicting_candidates``."""
    if not confident_match(v, symbol):
        return None, "no_confident_match"
    tok = _token_re(symbol)
    cands: set[int] = set()
    for sent in _SENT_RE.split(v.text):
        if not tok.search(sent):
            continue
        ms = list(_TS_RE.finditer(sent))
        if not ms:
            continue
        stamps = [_parse_ts(m) for m in ms]
        if any(s is None for s in stamps):
            return None, "unparseable_timestamp"
        if len(ms) == 1:
            cands.add(stamps[0])
            continue
        pos = sent.lower().find("respectively")
        if pos < 0:
            return None, "several_timestamps_without_respectively"
        syms = [m.group(0) for m in _SYM_RE.finditer(sent[: ms[0].start()])]
        if len(syms) != len(stamps) or syms.count(symbol) != 1 or pos < ms[-1].end():
            return None, "respectively_list_mismatch"
        cands.add(stamps[syms.index(symbol)])
    if not cands:
        return None, "no_timestamp_in_symbol_sentences"
    if len(cands) > 1:
        return None, "conflicting_candidates"
    return cands.pop(), "mapped"


@dataclass(frozen=True)
class _Event:
    ka: int
    order: tuple
    kind: Kind
    sched: int | None


class U6State:
    """U6 for one symbol, built from its notice versions and instances (S-2.5)."""

    def __init__(self, versions: list[NoticeVersion], symbol: str, insts: list[Instance]) -> None:
        self.symbol = symbol
        self._events: dict[int, list[_Event]] = {}
        self._clear_cache: dict[tuple, bool] = {}
        for v in versions:
            ka = known_at(v)
            if ka is None or not confident_match(v, symbol):
                continue
            # the instance live at the version's known_at (attribution; used only once that instance is
            # current, DK-108: a version published in its first 16 min is not dropped)
            inst = started_instance(insts, ka)
            if inst is None:
                continue
            sched = scheduled_ts(v, symbol) if v.kind == "delisting" else None
            ev = _Event(ka, (ka, v.release_ts, v.notice_id, v.version), v.kind, sched)
            self._events.setdefault(inst.idx, []).append(ev)
        for evs in self._events.values():
            evs.sort(key=lambda e: e.order)

    def scheds(self) -> tuple[int, ...]:
        """Every scheduled delisting time a 72 h test can be evaluated for (sorted, unique)."""
        return tuple(sorted({e.sched for evs in self._events.values() for e in evs if e.sched is not None}))

    def has_events(self, idx: int) -> bool:
        """Whether any matched version is attributed to instance ``idx`` (else ``active`` is False without
        reading ``halted_fn``)."""
        return bool(self._events.get(idx))

    def _no_halt(self, sched: int, halted_fn: Callable[[int], bool]) -> bool:
        """True when the instrument is not HALTED at any time in [sched, sched + 72 h] (cached). A
        ``halted_fn`` with a ``no_halt(sched, end, step)`` method (``pitindex.HaltProbe``, Task 26b) answers
        the same grid test itself (indexed, cached per scheduled time and PIT prefix; identical result)."""
        indexed = getattr(halted_fn, "no_halt", None)
        if indexed is not None:
            return bool(indexed(sched, sched + CLEAR_AFTER_NS, HALT_GRID_NS))
        key = (sched, halted_fn)
        if key not in self._clear_cache:
            end = sched + CLEAR_AFTER_NS
            grid = [*range(sched, end, HALT_GRID_NS), end]
            self._clear_cache[key] = not any(halted_fn(x) for x in grid)
        return self._clear_cache[key]

    def _cleared(self, sched: int | None, at: int, halted_fn: Callable[[int], bool]) -> bool:
        return sched is not None and sched + CLEAR_AFTER_NS <= at and self._no_halt(sched, halted_fn)

    def active(self, instance: Instance, t_ns: int, halted_fn: Callable[[int], bool],
               known_by: int | None = None) -> bool:
        """U6 at t. ``known_by``: only versions with ``known_at <= known_by`` exist (Task 26b: one state
        built from every version answers ``U6State(<versions known by t - 1 min>).active(...)`` exactly: the
        events are ordered by ``known_at`` first, so the known versions are a prefix of each instance's
        list)."""
        if t_ns - MIN < start_known_at(instance) or (  # not yet current (DK-108)
            instance.end_known_at is not None and t_ns >= instance.end_known_at
        ):
            return False  # U6 ends with its instance
        lim = t_ns if known_by is None else min(t_ns, int(known_by))
        inforce: list[int | None] = []  # sched of each in-force version
        for e in self._events.get(instance.idx, []):
            if e.ka > lim:
                break
            inforce = [x for x in inforce if not self._cleared(x, e.ka, halted_fn)]
            if e.kind != "delisting":
                inforce = []  # a matched postponement / cancellation clears every earlier in-force version
                continue
            if self._cleared(e.sched, e.ka, halted_fn):
                continue  # the 72 h test already passed at known_at: never activates
            inforce.append(e.sched)
        return any(not self._cleared(x, t_ns, halted_fn) for x in inforce)
