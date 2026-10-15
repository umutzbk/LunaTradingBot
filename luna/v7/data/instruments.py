"""R1 instruments: activity-based instances with E8 / DEC-31 verified-invariant identity (spec S-2.1, S-2.2,
S-1.7 E8, §3 R1, L1, DEC-30 / DEC-31 / DEC-44, EC-1; DK-15; Task 26c, AMB-40).

* **Instances** come from the bar data only (``data.instances.build_instances``: activity-based, 7-day
  dormancy, ``instance_end_known_at`` = last volume > 0 bar close + 7 d, EC-1). R1 stores one row per instance
  (S-2.2 "instance boundaries are derived from R3 alone and stored in R1").
* **Identity** (base, contract type, underlying type, multiplier) only from an ``IdentitySource``: an
  ``exchangeInfo`` body from N4 (current, ``ts`` = its ``recv_ts``) or from a timestamped Wayback capture
  (``ts`` = the capture time). Only the E8 whitelist ``guard.IDENTITY_FIELDS`` is read
  (``guard.historical_field``); status, listing / onboard dates, filters and every other field are never
  read as identity. Verified invariant (DEC-31 / R1): the sources dated WITHIN the instance
  (``first_bar_ts <= ts <= last_trade_ts``; an instance with no observed end is open-ended, so a current N4
  snapshot counts only for an instance that has not ended) must state every field (``missing:<field>``),
  agree (``conflict:<field>``) and give a finite multiplier > 0 (``invalid:multiplier``); no such source:
  ``no_source_within_instance``. Anything else excludes the instance (fail closed, DK-15): its identity
  columns are null. Its ``known_at`` is the instance's ``first_bar_ts`` (L1 whitelist).
* **Time-varying filters** (tick, step, min_notional) are never R1 identity and never backdated (E8): R1's
  filter columns stay null (a single row per instance cannot carry their effective / known timestamps,
  AMB-40 (c)); point-in-time filter snapshots come only from timestamped captures (``pit_filters``:
  ``deploy.Filters`` with provenance ``pit_historical`` and ``known_at`` = the capture time). Current N4
  filters serve only the DEC-21 / S-14.1 approximation layer (``current_filters``: provenance
  ``current_approximation``, refused on every point-in-time path by ``deploy.assert_not_backdated``).
* ``symbol_identity``: the per-symbol ``universe.Identity`` the universe consumes. Verified only when every
  instance of the symbol is verified with the same identity (AMB-40 (b)); the quote asset is not on the E8
  list and is left unknown (AMB-40 (a), normative: stopped), so ``universe.instrument_exclusion`` excludes it.
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

__all__ = ["FIELD_COLUMNS", "IdentitySource", "InstrumentRow", "Instruments", "VENUE", "build_instruments",
           "current_filters", "identity_source", "pit_filters", "symbol_identity", "verify_identity"]

VENUE = "binance"
# E8 whitelist field (exchangeInfo name) -> R1 column; order = the R1 column order
FIELD_COLUMNS: dict[str, str] = {"baseAsset": "base", "contractType": "contract_type",
                                 "underlyingType": "underlying_type", "multiplier": "multiplier"}
assert set(FIELD_COLUMNS) == set(guard.IDENTITY_FIELDS)


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
        v = vals.pop()
        if isinstance(v, tuple):
            return None, f"invalid:{name}", used
        ident[col] = v
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
              "step": "float64", "min_notional": "float64", "source": object, "retrieved_at": "Int64"}
assert list(_R1_DTYPES) == [*TABLES["R1"].key, *TABLES["R1"].columns]


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


def symbol_identity(rows: Sequence[InstrumentRow]) -> Identity:
    """The per-symbol ``universe.Identity`` (module docstring): verified only when every instance is verified
    with one identity; ``quote_asset`` unknown (AMB-40 (a))."""
    ids = {tuple(sorted(r.identity.items())) for r in rows if r.identity is not None}
    if not rows or len(ids) != 1 or any(r.identity is None for r in rows):
        return Identity(None, None, None, None, None, False)
    d = dict(ids.pop())
    return Identity(str(d["base"]), str(d["contract_type"]), None, str(d["underlying_type"]),
                    float(d["multiplier"]), True)  # type: ignore[arg-type]


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
