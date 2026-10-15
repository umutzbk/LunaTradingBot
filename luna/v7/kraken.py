"""Kraken exploratory control: frozen-copy adapter, the L11 Kraken outcome guard and S-13.3 comparison #5
(spec S-1.3, S-1.6, S-2.4, §3 last line, S-4.3 "Kraken", S-13.3 #5, L11, DEC-17; SC-04 user override 1,
DK-83; AMB-41).

Exploratory only: every Kraken object carries ``EXPLORATORY`` ("exploratory (previously observed window)",
S-1.6). Nothing here reaches the primary test, P1-P11, ``Verdict.cls`` or the deployment gates: comparison #5
is one member of the S-13.3 Holm family of six (DK-83 keeps m = 6), entering ``verdict.secondary_family``
only through ``runner_select.holdout_inputs(kraken=...)``.

* **Sources (§3 last line, S-2.4, S-4.3).** Kraken raw data come only from the frozen V5 copy (canonically
  ``var/history_v5/frozen``; the root is always an injected argument, never defaulted), verified against its
  pinned manifest sha256 ``V5_MANIFEST_SHA256`` (the V5 freeze record) before any read: the manifest bytes,
  every listed file's sha256, no unlisted file, the cut at the W_HOLD end. The frozen 20-symbol universe and
  the Kraken futures fees come from the V6 frozen constants at ``v6-code-frozen`` (hash-checked through
  ``spec``; ``V6Kraken``). Kraken ``BTC`` is the hedge instrument.
* **Formats (V5 ``luna.carry.frozen`` / ``history`` / ``funding``, V6 ``luna.v6.devdata``):**
  ``bars/kraken_futures/<A>.parquet`` (15-minute trade bars, UTC open-time index; ``open, high, low, close,
  volume`` (base), ``trades``, ``filled`` = a bar synthesised by the V4 gap fill), ``history_v4/mark|index``
  (15-minute bars, ``close``), ``history_v4/funding`` (hourly periods, index = period START, ``funding_abs``
  USD per unit and ``funding_rel`` fraction per hour; continuous pro-rata accrual), ``history_v5/spreads``
  (real 5-minute snapshots, ``spread_full``, with a ``.provenance.json``), ``config/v4_instruments.json``.
* **known_at (exact):** a 15-minute bar at its close (open + 15 min, as R3 / R4); a funding period at its end
  (start + 1 h, V5 ``FundingPIT``); a spread snapshot labelled T at T + 5 min (V5 Stage 0 rule). Synthesised
  (``filled``) bars are dropped, never used; nothing is interpolated.
* **L11 Kraken outcome guard (``authorize_kraken``).** Refused, raised as ``KrakenBlocked`` and K9-logged,
  unless ALL hold: the hardened W_HOLD authorization verifies (``guard.verified_opening``: forged, foreign,
  no opening or a produced verdict are refused); the window does not overlap M6 before the canonical R_V6
  (checked first, ``guard`` outcome-window semantics on the trusted clock); the window lies inside the DEC-17
  window [2025-10-06, 2026-07-01); the V6 constants verify; the frozen V5 copy verifies against the pinned
  manifest sha256. Only then is a ``KrakenAuthorization`` issued (module-private issuer); every loader
  re-checks it and the opening, and passes its frame through ``localdata.check_frame``.
* **STOPPED (AMB-41, normative amendment required).** The S-2.4 gate evaluation on Kraken data and the Kraken
  ML* book / labels / IC are not literally implementable from the permitted Kraken inputs (``GAPS``): no
  1-minute bars (S-5.2 P(t_e) at hh:20, the S-5.4 halt and gap rules), no Kraken funding convention (S-4.6 is
  Binance's discrete settlement; Kraken accrues pro rata per hourly period), no quote volume and no trade
  counts for F8, and the V5-value scope of "where they differ". (U2 via the V5 24 h ``volume x close``, U6
  inactive / R12 empty and the common-days pairing are implementation clarifications, AMB-41 (c) (d) (f).)
  ``comparison`` therefore runs the guard at the opening
  and returns comparison #5 as UNAVAILABLE with that reason; it computes no Kraken label, book, P&L or IC.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from luna.v7 import guard, k9, localdata, release, runrecord, spec
from luna.v7 import verdict as verdict_mod
from luna.v7.book.engine import BookRun
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.timeutil import MIN, H, ns_array, to_ns

EXPLORATORY = verdict_mod.EXPLORATORY
COMPARISON = verdict_mod.KRAKEN  # the S-13.3 family member #5
WINDOW = (to_ns("2025-10-06T00:00:00Z"), to_ns("2026-07-01T00:00:00Z"))  # DEC-17: 2025-10-06 -> 2026-06-30
V5_MANIFEST_SHA256 = "eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14"  # V5 freeze record
CANONICAL_ROOT = "var/history_v5/frozen"  # §3: documentation only; the root is always passed in
HEDGE = "BTC"  # S-2.4: Kraken BTC is the hedge instrument
MANIFEST = "manifest.json"
BAR_NS = 15 * MIN
FUNDING_PERIOD_NS = H  # a Kraken funding period; its rate is known at its end
SPREAD_KNOWN_AFTER_NS = 5 * MIN  # V5 Stage 0: a snapshot labelled T is known from T + 5 min
SNAPSHOT_NS = 5 * MIN
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")

# Spec input -> Kraken availability (AMB-41). "no" items block the sub-items listed in STOPPED.
GAPS: tuple[tuple[str, str, str], ...] = (
    ("R5 1-minute last-price bars (S-5.2 P(tau) at t_e = hh:20, S-5.4 HALTED / data-gap rules, S-6.2)",
     "no", "only 15-minute trade bars; the S-5.2 15-minute fallback exists only at bar starts, never hh:20"),
    ("R2 funding convention (S-4.6 discrete settlement at mark(settle); S-2.6 nominal intervals)",
     "no", "hourly periods keyed by their START, continuous pro-rata accrual (V4 / V5 semantics); "
     "S-4.6 is the Binance convention"),
    ("R3 quote volume (U2, S-4.4 liquidity cap)", "clarification",
     "S-2.4 names the V5 24 h quote volume: sum of volume x close over 96 bars (AMB-41 (c))"),
    ("trade counts (F8 log trades per day)", "no", "the V4 downloader writes trades = 0"),
    ("R4 premium index (F5)", "fallback", "mark / index 15-minute bars: the F5 mark-index fallback"),
    ("R11 notices / U6, R12 terminal events (S-2.5, S-5.4)", "clarification",
     "no Kraken source may be read (§3): U6 inactive (S-2.5, DEC-12), R12 zero rows (EC-7); AMB-41 (d)"),
    ("R1 identity (S-2.1)", "n/a", "replaced by the frozen 20-symbol V6 universe (S-2.4)"),
    ("S-4.5 spread proxy on R3 OHLC", "yes", "Kraken 15-minute trade bars (synthesised bars dropped)"),
    ("Kraken spread snapshots (S-1.6)", "yes", "real 5-minute snapshots from 2025-10-06, known at T + 5 min; "
     "never a cost input (S-4.5 proxy only, L8)"),
    ("instances (S-2.2 dormancy on volume > 0 15-minute bars)", "yes", "from the trade bars"),
    ("Kraken fees (S-4.3)", "yes", "V6 frozen fee snapshot: futures maker / taker"),
    ("BTC hedge and beta inputs (S-2.4, S-5.2)", "yes", "Kraken BTC 15-minute bars"),
)
STOPPED: Mapping[str, str] = MappingProxyType({
    "S-2.4 gates": "the same U1-U8 definitions on Kraken data need a Kraken funding convention (U3 / U7) "
                   "and the scope of the V5 values 'where they differ' (AMB-41 (b), (e))",
    "Kraken ML* book / labels / IC": "no 1-minute bars (P(t_e) at hh:20, HALTED, data gap), no Kraken "
                                     "funding payment convention, no trade counts for F8 "
                                     "(AMB-41 (a), (b), (c) F8)",
})
UNAVAILABLE_WHY = ("STOPPED, normative amendment required (AMB-41): the Kraken ML* book cannot be computed "
                   "from the permitted Kraken inputs without a new economic rule; " + "; ".join(
                       f"{k}: {v}" for k, v in STOPPED.items()))


class KrakenBlocked(RuntimeError):
    """The L11 Kraken outcome guard refused (always K9-logged)."""


class FrozenCopyMismatch(RuntimeError):
    """The frozen V5 copy does not verify against its pinned manifest."""


# --- V6 frozen constants (universe, fees) -------------------------------------------------------------------


@dataclass(frozen=True)
class V6Kraken:
    """The frozen 20-symbol V6 universe and the V6 Kraken fee snapshot (S-2.4, S-4.3), hash-checked."""

    universe: tuple[str, ...]
    futures_maker: float
    futures_taker: float
    fee_snapshot_sha256: str
    constants_sha256: str
    record_sha256: str


def v6_kraken() -> V6Kraken:
    """From the V6 constants at ``v6-code-frozen`` of the canonical code repository (``guard._roots``, the one
    location seam; sha256 checked by ``spec``; SpecMismatch otherwise)."""
    repo = Path(guard._roots()[0])
    c = spec._v6_json(repo, spec.V6_CONSTANTS_REL, spec.V6_CONSTANTS_SHA256)
    spec._v6_json(repo, spec.V6_RECORD_REL, spec.V6_RECORD_SHA256)  # the record verifies too (S-1.1)
    uni = tuple(str(a) for a in c["common"]["universe"])
    fees = c["fees"]
    if len(uni) != 20 or len(set(uni)) != 20 or HEDGE not in uni:
        raise spec.SpecMismatch(f"V6 universe is not the frozen 20-symbol list with {HEDGE}: {uni}")
    maker, taker = float(fees["futures_maker"]), float(fees["futures_taker"])
    fsha = str(fees["snapshot_sha256"])
    if not (0 <= maker <= taker < 0.01) or not _SHA.match(fsha):
        raise spec.SpecMismatch(f"V6 Kraken fee snapshot malformed: {fees}")
    return V6Kraken(uni, maker, taker, fsha, spec.V6_CONSTANTS_SHA256, spec.V6_RECORD_SHA256)


# --- frozen V5 copy -----------------------------------------------------------------------------------------


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_copy(root: str | Path) -> dict:
    """The frozen V5 copy at ``root`` verified against ``V5_MANIFEST_SHA256`` (manifest bytes first, then
    every listed file's sha256, no unlisted file, the cut at the W_HOLD end); its manifest. FrozenCopyMismatch
    otherwise. Read-only."""
    root = Path(root)
    man = root / MANIFEST
    if not man.is_file():
        raise FrozenCopyMismatch(f"{man}: manifest missing")
    raw = man.read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != V5_MANIFEST_SHA256:
        raise FrozenCopyMismatch(f"manifest sha256 {got} != pinned {V5_MANIFEST_SHA256}")
    m = json.loads(raw.decode("utf-8"))
    files = m.get("files")
    if not isinstance(files, dict) or not files:
        raise FrozenCopyMismatch("manifest lists no files")
    if to_ns(pd.Timestamp(m["cutoff"])) > WINDOW[1]:
        raise FrozenCopyMismatch(f"manifest cutoff {m['cutoff']} is after the W_HOLD end")
    listed = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    extra = sorted(listed - set(files) - {MANIFEST})  # only the root manifest is exempt (relative paths)
    if extra:
        raise FrozenCopyMismatch(f"files not in the manifest: {extra}")
    for rel, e in sorted(files.items()):
        p = root / rel
        if not p.is_file():
            raise FrozenCopyMismatch(f"{rel}: listed but missing")
        if _sha_file(p) != e.get("sha256"):
            raise FrozenCopyMismatch(f"{rel}: sha256 differs from the manifest")
    return m


# --- L11: the Kraken outcome guard --------------------------------------------------------------------------

_ISSUER = object()  # module-private: only authorize_kraken / comparison issue the objects below
# the exact objects issued (by id, weakly held): a dataclasses.replace copy (a widened window) never verifies
_ISSUED: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


@dataclass(frozen=True)
class KrakenAuthorization:
    """Issued only by ``authorize_kraken``; worth nothing by itself (every use re-verifies the opening)."""

    auth: object = field(repr=False)
    opening_id: str
    window: tuple[int, int]
    root: Path
    manifest_sha256: str
    files: Mapping[str, Mapping]
    v6: V6Kraken
    label: str = EXPLORATORY
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise TypeError("a KrakenAuthorization is issued only by kraken.authorize_kraken")


def k9_rule(exc: BaseException) -> str:
    """The K9 rule of a Kraken refusal (the underlying seal rule when a guard refused)."""
    inner = exc.__cause__ if isinstance(exc, KrakenBlocked) and exc.__cause__ is not None else exc
    if isinstance(inner, guard.HoldoutBlocked | guard.EmbargoBlocked | SealedBeforeL7):
        base = guard.k9_rule(inner)
    elif isinstance(inner, FrozenCopyMismatch):
        base = "frozen V5 copy manifest (§3, S-1.6)"
    elif isinstance(inner, KrakenBlocked):
        base = "Kraken scope (DEC-17 window, S-2.4 universe, issued authorization; S-1.6)"
    else:
        base = f"input verification ({type(inner).__name__})"
    return f"L11 Kraken outcome guard: {base}"


def _refuse(exc: BaseException, request: Mapping) -> KrakenBlocked:
    out = exc if isinstance(exc, KrakenBlocked) else KrakenBlocked(f"{type(exc).__name__}: {exc}")
    if out is not exc:
        out.__cause__ = exc
    k9.log_refusal(out, request=dict(request), rule=k9_rule(out))
    return out


def authorize_kraken(auth: object, lo_ns: int, hi_ns: int, root: str | Path) -> KrakenAuthorization:
    """L11 (module docstring). Order: the hardened authorization, M6 / forward / W_HOLD seal (canonical,
    trusted clock), the DEC-17 window, the V6 constants, the frozen copy. Any refusal -> KrakenBlocked,
    K9-logged."""
    k9.require()  # fail closed: no Kraken path without a K9 log
    req = {"kind": "kraken", "lo": int(lo_ns), "hi": int(hi_ns), "root": str(root)}
    try:
        lo, hi = int(lo_ns), int(hi_ns)
        if hi <= lo:
            raise KrakenBlocked(f"empty Kraken window [{lo}, {hi})")
        pin = guard.verified_opening(auth)  # forged / foreign / no opening / verdict produced
        guard._outcome_window(lo, hi, auth=auth, now_ns=release.trusted_now_ns(), m6_span=None,
                              fresh=True)  # M6 first, then W_FWD7, then W_HOLD (not logged: _refuse logs L11)
        if lo < WINDOW[0] or hi > WINDOW[1]:
            raise KrakenBlocked(f"Kraken window [{lo}, {hi}) outside the DEC-17 window {WINDOW} (S-1.6)")
        v6 = v6_kraken()
        m = verify_copy(root)
    except Exception as e:  # noqa: BLE001 - fail closed: every failure is an L11 refusal, K9-logged
        raise _refuse(e, req)  # noqa: B904 - _refuse sets the cause
    files = MappingProxyType({str(k): MappingProxyType(dict(v)) for k, v in m["files"].items()})
    ka = KrakenAuthorization(auth, str(pin["opening_id"]), (lo, hi), Path(root), V5_MANIFEST_SHA256, files,
                             v6, _issuer=_ISSUER)
    _ISSUED[id(ka)] = ka
    return ka


def _check(kauth: object, what: str) -> KrakenAuthorization:
    k9.require()
    try:
        if type(kauth) is not KrakenAuthorization or _ISSUED.get(id(kauth)) is not kauth:
            raise KrakenBlocked("not a KrakenAuthorization issued by authorize_kraken")
        pin = guard.verified_opening(kauth.auth)
        if pin["opening_id"] != kauth.opening_id:
            raise KrakenBlocked("the KrakenAuthorization belongs to another opening")
    except Exception as e:  # noqa: BLE001 - fail closed: every failure is an L11 refusal, K9-logged
        raise _refuse(e, {"kind": "kraken", "what": what})  # noqa: B904 - _refuse sets the cause
    return kauth


# --- read-only loaders --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KrakenSymbol:
    """One Kraken symbol mapped to V7 arrays (int64 ns event times, exact ``known_at``)."""

    symbol: str
    bars15: pd.DataFrame  # open_ts, o, h, l, c, vol_base, known_at (= open_ts + 15 min); no vol_quote
    mark15: pd.DataFrame  # open_ts, c, known_at
    index15: pd.DataFrame  # open_ts, c, known_at
    funding: pd.DataFrame  # period_start_ts, rate_rel, rate_abs, known_at (= period start + 1 h)
    spreads5m: pd.DataFrame  # label_ts, spread_full, known_at (= label + 5 min); diagnostic only (S-4.5, L8)
    filled_dropped: int
    label: str = EXPLORATORY


def _hashed_bytes(kauth: KrakenAuthorization, rel: str) -> bytes:
    """The bytes of a manifest-listed file, read once and hashed; the caller parses exactly these bytes."""
    e = kauth.files.get(rel)
    if e is None:
        raise KrakenBlocked(f"{rel}: not in the frozen V5 manifest (fail closed, never empty)")
    raw = (kauth.root / rel).read_bytes()
    if hashlib.sha256(raw).hexdigest() != e.get("sha256"):
        raise FrozenCopyMismatch(f"{rel}: sha256 changed since the manifest was verified")
    return raw


def _frame(kauth: KrakenAuthorization, rel: str, cols: tuple[str, ...]) -> tuple[pd.DataFrame, np.ndarray]:
    """A manifest-listed parquet file of the copy, parsed from its hashed bytes; (frame, ns index)."""
    raw = _hashed_bytes(kauth, rel)
    e = kauth.files[rel]
    if "rows" in e and pq.ParquetFile(io.BytesIO(raw)).metadata.num_rows != int(e["rows"]):
        raise FrozenCopyMismatch(f"{rel}: row count differs from the manifest")
    df = pd.read_parquet(io.BytesIO(raw))
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError(f"{rel}: a tz-aware UTC DatetimeIndex is required")
    if not idx.is_monotonic_increasing or idx.has_duplicates:
        raise ValueError(f"{rel}: index must be strictly increasing")
    miss = [c for c in cols if c not in df.columns]
    if miss:
        raise ValueError(f"{rel}: required columns missing {miss}")
    ts = (ns_array(idx.tz_convert("UTC").as_unit("ns").asi8, f"{rel}.index") if len(idx)
          else np.empty(0, np.int64))
    return df, ts


def _num(df: pd.DataFrame, c: str) -> np.ndarray:
    return pd.to_numeric(df[c], errors="raise").to_numpy(np.float64)


def _bars(kauth: KrakenAuthorization, rel: str, lo: int, hi: int, ohlcv: bool) -> tuple[pd.DataFrame, int]:
    cols = ("open", "high", "low", "close", "volume", "filled") if ohlcv else ("close",)
    df, ts = _frame(kauth, rel, cols)
    keep = (ts >= lo) & (ts < hi)
    dropped = 0
    if ohlcv:
        filled = df["filled"].to_numpy()
        if filled.dtype != np.bool_:
            raise ValueError(f"{rel}: 'filled' must be bool")
        dropped = int((filled & keep).sum())
        keep &= ~filled  # a bar synthesised by the V4 gap fill is never a bar (no invented price)
    d = df[keep]
    t = ts[keep]
    out = pd.DataFrame({"open_ts": t})
    if ohlcv:
        for src, dst in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "vol_base")):
            out[dst] = _num(d, src)
    else:
        out["c"] = _num(d, "close")
    out["known_at"] = t + BAR_NS
    return out, dropped


def _funding(kauth: KrakenAuthorization, rel: str, lo: int, hi: int) -> pd.DataFrame:
    df, ts = _frame(kauth, rel, ("funding_rel", "funding_abs"))
    keep = (ts >= lo) & (ts < hi)
    d = df[keep]
    return pd.DataFrame({"period_start_ts": ts[keep], "rate_rel": _num(d, "funding_rel"),
                         "rate_abs": _num(d, "funding_abs"), "known_at": ts[keep] + FUNDING_PERIOD_NS})


def _spreads(kauth: KrakenAuthorization, sym: str, lo: int, hi: int) -> pd.DataFrame:
    rel = f"history_v5/spreads/{sym}.parquet"
    prel = f"history_v5/spreads/{sym}.provenance.json"
    df, ts = _frame(kauth, rel, ("spread_full",))
    prov = json.loads(_hashed_bytes(kauth, prel).decode("utf-8"))
    if (prov.get("requested_interval_s") != 300 or prov.get("observed_cadence_s") != 300
            or prov.get("real_resolution_matches_interval") is not True
            or prov.get("sha256") != kauth.files[rel].get("sha256")):
        raise FrozenCopyMismatch(f"{rel}: not real 5-minute snapshots matching its provenance")
    if (ts % SNAPSHOT_NS).any():
        raise ValueError(f"{rel}: snapshot labels off the 5-minute grid")
    keep = (ts >= lo) & (ts < hi)
    return pd.DataFrame({"label_ts": ts[keep], "spread_full": _num(df[keep], "spread_full"),
                         "known_at": ts[keep] + SPREAD_KNOWN_AFTER_NS})


def load_symbol(kauth: KrakenAuthorization, symbol: str, lo_ns: int, hi_ns: int) -> KrakenSymbol:
    """One universe symbol's frozen-copy inputs with event times in [lo_ns, hi_ns) (``hi_ns`` <= the
    authorized window end; earlier rows are point-in-time look-back, incl. the frozen V4 funding before
    2025-10-06). Every frame passes ``localdata.check_frame`` with the opening's authorization. Refusals are
    K9-logged."""
    kauth = _check(kauth, f"load {symbol}")
    req = {"kind": "kraken", "what": f"load {symbol}", "lo": int(lo_ns), "hi": int(hi_ns)}
    try:
        lo, hi = int(lo_ns), int(hi_ns)
        if hi <= lo or hi > kauth.window[1]:
            raise KrakenBlocked(f"[{lo}, {hi}) is empty or ends after the authorized window {kauth.window}")
        if symbol not in kauth.v6.universe:
            raise KrakenBlocked(f"{symbol} is not in the frozen 20-symbol V6 universe (S-2.4)")
        bars, dropped = _bars(kauth, f"bars/kraken_futures/{symbol}.parquet", lo, hi, True)
        mark, _ = _bars(kauth, f"history_v4/mark/{symbol}.parquet", lo, hi, False)
        index, _ = _bars(kauth, f"history_v4/index/{symbol}.parquet", lo, hi, False)
        fund = _funding(kauth, f"history_v4/funding/{symbol}.parquet", lo, hi)
        spr = _spreads(kauth, symbol, lo, hi)
        frames = (("bars15", bars, "open_ts"), ("mark15", mark, "open_ts"), ("index15", index, "open_ts"),
                  ("funding", fund, "period_start_ts"), ("spreads5m", spr, "label_ts"))
        for name, df, col in frames:  # the data-span guard (its own K9 entry) and then the L11 entry
            localdata.check_frame(df, [col], auth=kauth.auth,  # type: ignore[arg-type]
                                  what=f"kraken {symbol}.{name}")
    except Exception as e:  # noqa: BLE001 - fail closed: every failure is an L11 refusal, K9-logged
        raise _refuse(e, req)  # noqa: B904 - _refuse sets the cause
    return KrakenSymbol(symbol, bars, mark, index, fund, spr, dropped)


def load_hedge(kauth: KrakenAuthorization, lo_ns: int, hi_ns: int) -> KrakenSymbol:
    """Kraken BTC, the hedge instrument (S-2.4)."""
    return load_symbol(kauth, HEDGE, lo_ns, hi_ns)


# --- S-13.3 comparison #5 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class KrakenComparison:
    """Comparison #5 as issued at the opening: ``pair`` is what ``verdict.secondary_family`` receives (here
    always ``verdict.Unavailable``: STOPPED, AMB-41). ``provenance`` = the DT4 manifest hashes."""

    available: bool
    why: str | None
    opening_id: str
    window: tuple[int, int]
    ml_run_sha256: str
    provenance: Mapping[str, str]
    stopped: tuple[str, ...]
    name: str = COMPARISON
    label: str = EXPLORATORY
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise TypeError("a KrakenComparison is issued only by kraken.comparison")

    @property
    def pair(self) -> verdict_mod.Unavailable:
        return verdict_mod.Unavailable(f"{self.label}: {self.why}")


def provenance(kauth: KrakenAuthorization) -> dict[str, str]:
    """DT4 data-manifest entries: the frozen V5 copy manifest and the V6 artefact hashes."""
    return {"kraken_v5_frozen_manifest": kauth.manifest_sha256,
            "v6_frozen_constants": kauth.v6.constants_sha256, "v6_code_freeze_record": kauth.v6.record_sha256,
            "v6_kraken_fee_snapshot": kauth.v6.fee_snapshot_sha256}


def comparison(kauth: KrakenAuthorization, ml_binance: BookRun) -> KrakenComparison:
    """Comparison #5 at the opening: the L11 guard is re-checked, the Binance side must be ML*'s E_T research
    run; the Kraken side is STOPPED (AMB-41), so the result is UNAVAILABLE and nothing Kraken-derived is
    computed. It never touches P1-P11, ``Verdict.cls`` or the deployment gates."""
    kauth = _check(kauth, "comparison #5")
    if not isinstance(ml_binance, BookRun) or ml_binance.arm not in ("A2", "A3") \
            or ml_binance.execution != "E_T" or not ml_binance.research:
        raise ValueError("comparison #5 needs the Binance ML* E_T research run (runner.run_book)")
    c = KrakenComparison(False, UNAVAILABLE_WHY, kauth.opening_id, kauth.window,
                         runrecord.run_output_sha256(ml_binance), MappingProxyType(provenance(kauth)),
                         tuple(STOPPED), _issuer=_ISSUER)
    _ISSUED[id(c)] = c
    return c


def verify_comparison(c: object) -> bool:
    return type(c) is KrakenComparison and _ISSUED.get(id(c)) is c and c.label == EXPLORATORY


def dt4_extra(c: KrakenComparison) -> dict:
    """The DT4 ``extra`` block of comparison #5 (label, availability, reason, window, opening)."""
    if not verify_comparison(c):
        raise ValueError("not a KrakenComparison issued by kraken.comparison")
    return {"name": c.name, "label": c.label, "available": c.available, "why": c.why,
            "window": [int(c.window[0]), int(c.window[1])], "opening_id": c.opening_id,
            "ml_run_sha256": c.ml_run_sha256, "stopped": list(c.stopped)}


__all__ = ["CANONICAL_ROOT", "COMPARISON", "EXPLORATORY", "FrozenCopyMismatch", "GAPS", "HEDGE",
           "KrakenAuthorization", "KrakenBlocked", "KrakenComparison", "KrakenSymbol", "STOPPED",
           "UNAVAILABLE_WHY", "V5_MANIFEST_SHA256", "V6Kraken", "WINDOW", "authorize_kraken", "comparison",
           "dt4_extra", "load_hedge", "load_symbol", "provenance", "v6_kraken", "verify_comparison",
           "verify_copy"]
