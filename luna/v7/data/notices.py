"""Point-in-time delisting notices and the U6 state machine (spec S-2.5; DEC-26/36/37/40; R11).

* ``known_at``: history ``max(releaseDate, archive capture timestamp)``; forward ``recv_ts``. A version
  with neither a capture nor a recv timestamp (current CMS text) has no ``known_at`` and is never
  backdated: it can never activate U6.
* ``confident_match``: the exact contract symbol as a whole token (non-alphanumeric boundaries, so
  ``1000PEPEUSDT`` does not match ``PEPEUSDT``) AND an unambiguous USDⓈ-M futures / perpetual context
  (``USDM_PHRASES`` / ``USDM_PAIR``, AMB-05). Anything else is no match; no fuzzy or semantic inference.
* ``scheduled_ts``: only an explicitly mapped UTC timestamp, else None (DEC-37).
* ``U6State.active``: activation at ``known_at``; deactivation by a later matched postponement /
  cancellation at its ``known_at``; the 72 h auto-clear only with a unique scheduled time; a late
  ``known_at`` evaluates the 72 h test at ``known_at``; U6 ends with its instance.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from luna.v7.data.instances import Instance, current_instance
from luna.v7.timeutil import MIN, H, to_ns

Kind = Literal["delisting", "postponement", "cancellation"]

# S-2.5 / DEC-40 (AMB-05): context phrases, compared after NFKC + casefold (so "USDⓈ-M" -> "usds-m").
USDM_PHRASES: tuple[str, ...] = ("usds-m", "usdt-margined", "usds-margined")
# "perpetual contract(s)" counts only together with "futures".
USDM_PAIR: tuple[str, str] = ("perpetual contract", "futures")
# A COIN-M mention makes the context ambiguous -> no match.
COIN_M_PHRASES: tuple[str, ...] = ("coin-m", "coin-margined")

CLEAR_AFTER_NS = 72 * H
HALT_GRID_NS = MIN  # the 72 h "not HALTED at any time" test samples halted_fn on this grid (AMB-05)

_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?\s*\(UTC\)", re.IGNORECASE
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


def known_at(v: NoticeVersion) -> int | None:
    """Forward ``recv_ts``; history ``max(release_ts, capture_ts)``; no snapshot -> None (never backdated)."""
    if v.recv_ts is not None:
        return int(v.recv_ts)
    if v.capture_ts is None:
        return None
    return max(int(v.release_ts), int(v.capture_ts))


def _token_re(symbol: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(symbol) + r"(?![A-Za-z0-9])")


def _usdm_context(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text).casefold()
    if any(p in t for p in COIN_M_PHRASES):
        return False
    return any(p in t for p in USDM_PHRASES) or all(p in t for p in USDM_PAIR)


def confident_match(v: NoticeVersion, symbol: str) -> bool:
    return bool(symbol) and _token_re(symbol).search(v.text) is not None and _usdm_context(v.text)


def _parse_ts(m: re.Match[str]) -> int | None:
    hour, minute, ampm = int(m.group(2)), int(m.group(3)), (m.group(4) or "").upper()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "PM" else 0)
    if hour > 23 or minute > 59:
        return None
    return to_ns(f"{m.group(1)} {hour:02d}:{minute:02d}:00")


def scheduled_ts(v: NoticeVersion, symbol: str) -> int | None:
    """Unique, explicitly mapped scheduled delisting time of ``symbol`` (DEC-37), else None.

    Candidates come only from sentences that contain the symbol token: a sentence with exactly one
    timestamp maps it to the symbols it names; a sentence with several timestamps maps only through an
    ordered "A and B ... T1 and T2 respectively" list (equal counts, symbol listed once). Exactly one
    distinct candidate must result; any unmappable sentence or any disagreement gives None.
    """
    if not confident_match(v, symbol):
        return None
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
            return None
        if len(ms) == 1:
            cands.add(stamps[0])
            continue
        pos = sent.lower().find("respectively")
        if pos < 0:
            return None
        syms = [m.group(0) for m in _SYM_RE.finditer(sent[: ms[0].start()])]
        if len(syms) != len(stamps) or syms.count(symbol) != 1 or pos < ms[-1].end():
            return None
        cands.add(stamps[syms.index(symbol)])
    return cands.pop() if len(cands) == 1 else None


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
            inst = current_instance(insts, ka)  # the instance live at the version's known_at
            if inst is None:
                continue
            sched = scheduled_ts(v, symbol) if v.kind == "delisting" else None
            ev = _Event(ka, (ka, v.release_ts, v.notice_id, v.version), v.kind, sched)
            self._events.setdefault(inst.idx, []).append(ev)
        for evs in self._events.values():
            evs.sort(key=lambda e: e.order)

    def _no_halt(self, sched: int, halted_fn: Callable[[int], bool]) -> bool:
        """True when the instrument is not HALTED at any time in [sched, sched + 72 h] (cached)."""
        key = (sched, halted_fn)
        if key not in self._clear_cache:
            end = sched + CLEAR_AFTER_NS
            grid = [*range(sched, end, HALT_GRID_NS), end]
            self._clear_cache[key] = not any(halted_fn(x) for x in grid)
        return self._clear_cache[key]

    def _cleared(self, sched: int | None, at: int, halted_fn: Callable[[int], bool]) -> bool:
        return sched is not None and sched + CLEAR_AFTER_NS <= at and self._no_halt(sched, halted_fn)

    def active(self, instance: Instance, t_ns: int, halted_fn: Callable[[int], bool]) -> bool:
        if t_ns < instance.first_bar_ts or (
            instance.end_known_at is not None and t_ns >= instance.end_known_at
        ):
            return False  # U6 ends with its instance
        on = False
        sched: int | None = None
        for e in self._events.get(instance.idx, []):
            if e.ka > t_ns:
                break
            if on and self._cleared(sched, e.ka, halted_fn):
                on = False  # auto-cleared before this event
            if e.kind != "delisting":
                on, sched = False, None
                continue
            if self._cleared(e.sched, e.ka, halted_fn):
                continue  # the 72 h test already passed at known_at: never activates
            on, sched = True, e.sched
        return on and not self._cleared(sched, t_ns, halted_fn)
