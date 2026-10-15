"""R1 instruments: activity-based instances with E8 / DEC-31 verified-invariant identity (spec S-2.1, S-2.2,
S-1.7 E8, §3 R1, L1, DEC-30 / DEC-31 / DEC-44, EC-1; DK-15; Task 26c, AMB-40).

* **Instances** come from the bar data only (``data.instances.build_instances``: activity-based, 7-day
  dormancy, ``instance_end_known_at`` = last volume > 0 bar close + 7 d, EC-1). R1 stores one row per instance
  (S-2.2 "instance boundaries are derived from R3 alone and stored in R1").
* **Identity** (base, contract type, underlying type, quote asset; the multiplier derived from the base) only
  from an ``IdentitySource``: an ``exchangeInfo`` body from N4 (current, ``ts`` = its ``recv_ts``) or from a
  timestamped Wayback capture (``ts`` = the capture time). Only the E8 whitelist ``guard.IDENTITY_FIELDS`` is
  read (``guard.historical_field``; ``quoteAsset`` added by the user decision on AMB-40 (a)(ii)); status,
  listing / onboard dates, filters and every other field are never read as identity. Verified invariant
  (DEC-31 / R1): only the sources dated WITHIN the instance count (``first_bar_ts <= ts <= last_trade_ts``;
  an instance with no observed end is open-ended, so a current N4 snapshot counts only for an instance that
  has not ended; literal reading kept by the user, AMB-40 (d)); "no source conflicts" is evaluated on those
  sources (a source outside the instance describes another period / instance). They must state every field
  (``missing:<field>``) and agree (``conflict:<field>``); no such source: ``no_source_within_instance``.
* **Multiplier** (user decision on AMB-40 (a)(i)): derived mechanically from the invariant ``baseAsset`` by
  the CLOSED prefix list ``MULTIPLIER_PREFIXES`` (``1000000`` -> 1e6, ``1000`` -> 1000, ``1M`` -> 1e6; the
  remainder must start with a letter). Ruling R49: only a leading digit run that is a power of ten >= 10, or
  ``1M`` + a letter, is multiplier-like; another multiplier-like run (``10``, ``100``, ``10000``, ...) ->
  excluded (``invalid:multiplier_prefix``); any other base (``1INCH``, ``2Z``, no digit) -> 1.
  A source that states a ``multiplier`` field must agree with the derivation (``conflict:multiplier``).
  Nothing is guessed.
* Anything else excludes the instance (fail closed, DK-15): its identity columns are null. Its ``known_at``
  is the instance's ``first_bar_ts`` (L1 whitelist). ``quote_asset`` is stored as one extra R1 column after
  the frozen S-3 columns (AMB-40 (e)).
* **Time-varying filters** (tick, step, min_notional) are never R1 identity and never backdated (E8): R1's
  filter columns stay null (a single row per instance cannot carry their effective / known timestamps,
  AMB-40 (c)); point-in-time filter snapshots come only from timestamped captures (``pit_filters``:
  ``deploy.Filters`` with provenance ``pit_historical`` and ``known_at`` = the capture time). Current N4
  filters serve only the DEC-21 / S-14.1 approximation layer (``current_filters``: provenance
  ``current_approximation``, refused on every point-in-time path by ``deploy.assert_not_backdated``).
* ``instance_identities``: per INSTANCE the ``universe.Identity`` the universe consumes (fix round 1, I3:
  the literal R1 "the instance is excluded"; an unverifiable earlier instance no longer excludes the later
  verified instances of the same symbol).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from luna.v7 import guard
from luna.v7.data.instances import Instance
from luna.v7.data.schema import TABLES
from luna.v7.deploy import Filters
from luna.v7.universe import Identity

__all__ = ["FIELD_COLUMNS", "MULTIPLIER_PREFIXES", "IdentitySource", "InstrumentRow", "Instruments", "VENUE",
           "build_instruments", "current_filters", "identity_source", "instance_identities", "multiplier_of",
           "pit_filters", "row_identity", "verify_identity"]

VENUE = "binance"
# E8 whitelist field (exchangeInfo name) -> R1 column (the multiplier is derived from baseAsset)
FIELD_COLUMNS: dict[str, str] = {"baseAsset": "base", "contractType": "contract_type",
                                 "underlyingType": "underlying_type", "quoteAsset": "quote_asset"}
assert set(FIELD_COLUMNS) | {"multiplier"} == set(guard.IDENTITY_FIELDS)
# AMB-40 (a)(i), user decision: the closed baseAsset prefix list (longest first)
MULTIPLIER_PREFIXES: tuple[tuple[str, float], ...] = (("1000000", 1e6), ("1000", 1e3), ("1M", 1e6))


def _letter(c: str) -> bool:
    return c[:1].isascii() and c[:1].isalpha()


def multiplier_of(base: str) -> float | None:
    """The multiplier of a ``baseAsset`` (ruling R49): ``1M`` + a letter -> 1e6 (``1M`` + anything else ->
    None); a leading digit run that is a power of ten >= 10 is multiplier-like: ``1000`` / ``1000000``
    followed by a letter -> that factor, any other (``10``, ``100``, ``10000``, ... or a closed-list run not
    followed by a letter) -> None (excluded);
    any other base (no leading digit, or a digit run that is not a power of ten >= 10, e.g. ``1INCH``,
    ``2Z``) -> 1, the base kept as is."""
    if base.startswith("1M"):  # the user's closed list (Binance convention 1MBABYDOGE = 1e6 x BABYDOGE)
        return 1e6 if _letter(base[2:]) else None  # "1M", "1M2X": excluded like "1000", "1000_X" (round 2)
    run = base[:len(base) - len(base.lstrip("0123456789"))]
    if not (len(run) >= 2 and run[0] == "1" and set(run[1:]) == {"0"}):
        return 1.0  # not multiplier-like
    rest = base[len(run):]
    for p, f in MULTIPLIER_PREFIXES:
        if run == p and _letter(rest):
            return f
    return None


@dataclass(frozen=True)
class IdentitySource:
    """One ``exchangeInfo`` body reduced to the E8 whitelist per symbol. ``ts``: N4 = its ``recv_ts``;
    capture = the capture time (ns)."""

    kind: Literal["N4", "capture"]
    ts: int
    recv_ts: int
    url: str
    sha256: str
    symbols: Mapping[str, Mapping[str, object]]

    @property
    def ref(self) -> str:
        return f"{self.kind}@{int(self.ts)}:{self.sha256[:16]}"


def _symbols(body: bytes) -> list[dict]:
    try:
        doc = json.loads(body)
    except ValueError:
        raise ValueError("exchangeInfo body is not JSON") from None
    syms = doc.get("symbols") if isinstance(doc, dict) else None
    if not isinstance(syms, list):
        raise ValueError("exchangeInfo body has no symbols list")
    return [s for s in syms if isinstance(s, dict) and isinstance(s.get("symbol"), str)]


def identity_source(body: bytes, *, kind: Literal["N4", "capture"], ts: int, recv_ts: int, url: str,
                    sha256: str) -> IdentitySource:
    """The E8 whitelist of every symbol of an ``exchangeInfo`` body (nothing else is kept)."""
    if kind not in ("N4", "capture"):
        raise ValueError(f"unknown identity source kind {kind!r}")
    out: dict[str, dict[str, object]] = {}
    for s in _symbols(body):
        fields = {}
        for name in sorted(guard.IDENTITY_FIELDS):
            v = s.get(guard.historical_field(name))
            if v is not None and v != "":
                fields[name] = v
        out[s["symbol"]] = fields
    return IdentitySource(kind, int(ts), int(recv_ts), str(url), str(sha256), out)


def _norm(name: str, v: object) -> object:
    if name == "multiplier":
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ("invalid", repr(v))
        return f if math.isfinite(f) and f > 0 else ("invalid", repr(v))
    return str(v)


def _within(inst: Instance, ts: int) -> bool:
    return inst.first_bar_ts <= ts and (inst.last_trade_ts is None or ts <= inst.last_trade_ts)


def verify_identity(symbol: str, inst: Instance, sources: Sequence[IdentitySource]
                    ) -> tuple[dict[str, object] | None, str, tuple[IdentitySource, ...]]:
    """(identity {R1 column: value} or None, reason, the sources dated within the instance) (module
    docstring)."""
    used = tuple(s for s in sorted(sources, key=lambda s: (s.ts, s.kind, s.sha256))
                 if symbol in s.symbols and _within(inst, s.ts))
    if not used:
        return None, "no_source_within_instance", used
    ident: dict[str, object] = {}
    for name, col in FIELD_COLUMNS.items():
        vals = {_norm(name, s.symbols[symbol][name]) for s in used if name in s.symbols[symbol]}
        if not vals:
            return None, f"missing:{name}", used
        if len(vals) > 1:
            return None, f"conflict:{name}", used
        ident[col] = vals.pop()
    mult = multiplier_of(str(ident["base"]))
    if mult is None:
        return None, "invalid:multiplier_prefix", used
    stated = {_norm("multiplier", s.symbols[symbol]["multiplier"]) for s in used
              if "multiplier" in s.symbols[symbol]}
    if stated and stated != {mult}:
        return None, "conflict:multiplier", used
    ident["multiplier"] = mult
    return ident, "verified", used


@dataclass(frozen=True)
class InstrumentRow:
    symbol: str
    instance: Instance
    identity: Mapping[str, object] | None
    reason: str
    sources: tuple[str, ...]  # refs of the sources dated within the instance

    @property
    def included(self) -> bool:
        return self.identity is not None


@dataclass(frozen=True)
class Instruments:
    table: pd.DataFrame  # R1
    rows: tuple[InstrumentRow, ...]


_R1_DTYPES = {"venue": object, "symbol": object, "instance": "int64", "base": object, "contract_type": object,
              "underlying_type": object, "multiplier": "float64", "first_bar_ts": "Int64",
              "last_trade_ts": "Int64", "instance_end_known_at": "Int64", "tick": "float64",
              "step": "float64", "min_notional": "float64", "source": object, "retrieved_at": "Int64",
              "quote_asset": object}  # extra identity column after the frozen S-3 columns (AMB-40 (e))
R1_FROZEN = [*TABLES["R1"].key, *TABLES["R1"].columns]
assert list(_R1_DTYPES) == [*R1_FROZEN, "quote_asset"]


def r1_frame(records: Sequence[Mapping[str, object]]) -> pd.DataFrame:
    """An R1 frame with the schema columns and dtypes (nullable ints, NaN floats, None strings)."""
    cols = {}
    for c, dt in _R1_DTYPES.items():
        vals = [r.get(c) for r in records]
        if dt == "float64":
            cols[c] = pd.Series([np.nan if v is None else float(v) for v in vals], dtype="float64")  # type: ignore[arg-type]
        else:
            cols[c] = pd.Series(vals, dtype=dt)
    return pd.DataFrame(cols)


def build_instruments(insts_by_symbol: Mapping[str, Sequence[Instance]], sources: Sequence[IdentitySource],
                      venue: str = VENUE) -> Instruments:
    """R1 (one row per instance, symbols sorted) with the verified identity or null (module docstring)."""
    rows: list[InstrumentRow] = []
    recs: list[dict[str, object]] = []
    for sym in sorted(insts_by_symbol):
        for inst in sorted(insts_by_symbol[sym], key=lambda i: i.idx):
            ident, why, used = verify_identity(sym, inst, sources)
            refs = tuple(s.ref for s in used)
            rows.append(InstrumentRow(sym, inst, ident, why, refs))
            rec: dict[str, object] = {"venue": venue, "symbol": sym, "instance": int(inst.idx),
                                      "first_bar_ts": int(inst.first_bar_ts),
                                      "last_trade_ts": inst.last_trade_ts,
                                      "instance_end_known_at": inst.end_known_at}
            if ident is not None:
                rec.update(ident)
                rec["source"] = ";".join(refs)
                rec["retrieved_at"] = max(int(s.recv_ts) for s in used)
            recs.append(rec)
    return Instruments(r1_frame(recs), tuple(rows))


def row_identity(r: InstrumentRow) -> Identity:
    """The ``universe.Identity`` of one R1 row (unverified when the instance is excluded)."""
    d = r.identity
    if d is None:
        return Identity(None, None, None, None, None, False)
    return Identity(str(d["base"]), str(d["contract_type"]), str(d["quote_asset"]), str(d["underlying_type"]),
                    float(d["multiplier"]), True)  # type: ignore[arg-type]


def instance_identities(rows: Sequence[InstrumentRow]) -> dict[int, Identity]:
    """{instance idx: Identity} of ONE symbol's R1 rows (``SymbolInputs.identity`` per instance; fix round 1,
    I3), sorted by idx."""
    if len({r.symbol for r in rows}) > 1:
        raise ValueError("instance_identities takes the rows of one symbol")
    return {int(r.instance.idx): row_identity(r) for r in sorted(rows, key=lambda r: r.instance.idx)}


def _filters(s: dict) -> tuple[float, float, float] | None:
    by = {f.get("filterType"): f for f in s.get("filters") or [] if isinstance(f, dict)}
    try:
        lot, notional = by["LOT_SIZE"], by["MIN_NOTIONAL"]
        step, min_qty, mn = float(lot["stepSize"]), float(lot["minQty"]), float(notional["notional"])
    except (KeyError, TypeError, ValueError):
        return None
    return step, min_qty, mn


def pit_filters(body: bytes, *, known_at: int) -> dict[str, Filters]:
    """Point-in-time filters of a TIMESTAMPED capture (``known_at`` = its capture time): LOT_SIZE stepSize /
    minQty and MIN_NOTIONAL notional; a symbol without them (or with invalid values) has none."""
    out: dict[str, Filters] = {}
    for s in _symbols(body):
        f = _filters(s)
        if f is not None:
            try:
                out[s["symbol"]] = Filters(*f, "pit_historical", int(known_at))
            except ValueError:
                continue
    return out


def current_filters(body: bytes) -> dict[str, Filters]:
    """Current (N4) filters for the DEC-21 / S-14.1 approximation layer only (``current_approximation``)."""
    out: dict[str, Filters] = {}
    for s in _symbols(body):
        f = _filters(s)
        if f is not None:
            try:
                out[s["symbol"]] = Filters(*f, "current_approximation")
            except ValueError:
                continue
    return out
