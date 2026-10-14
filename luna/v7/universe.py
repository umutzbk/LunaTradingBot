"""Point-in-time universe gates U1-U8, instrument exclusions and the D0 rule (spec S-1.8, S-2.1-S-2.3, S-2.5).

* Every gate sees only rows with ``known_at <= t - 1 min`` (rows dated later are dropped before use).
  ``GateRow.max_known_at`` is the max ``known_at`` of every row a gate used; ``assert_pit`` raises
  ``LeakageError`` if it exceeds ``t - 1 min`` (DT1 invariant).
* Gate values are True when the gate PASSES (U6 passes when no matched notice is in force).
* U1-U5, U7, U8 use the current instance only (S-2.2); ``instances.current_instance`` is the only
  point-in-time membership. Undetermined / missing inputs fail the gate (fail closed).
* Instrument exclusions (S-2.1, DEC-30/31, E8) are data-driven from an ``Identity`` whose source is
  verified invariant; unknown identity -> excluded.
* U8 false blocks new entries / increases only (``entry_allowed``); it is not an exit trigger
  (``exit_triggered`` follows U6 only, S-2.5 / S-5.4).
Interpretations the spec does not fix are logged as AMB-07 in docs/results/v7-impl/AMBIGUITIES.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.data.intervals import annotate, u3_gap
from luna.v7.data.notices import NoticeVersion, U6State, known_at
from luna.v7.market import halted
from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, D, H, ns_array, to_ns

BAR_15M = 15 * MIN
BARS_PER_DAY = 96
U = FROZEN.universe

# S-2.1: stablecoin bases; BTCUSDT is the hedge instrument only; DEC-30: Stage 0 unknown-type symbols.
STABLE_BASES: frozenset[str] = frozenset({"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE"})
HEDGE_ONLY: frozenset[str] = frozenset({"BTCUSDT"})
UNKNOWN_TYPE_SYMBOLS: frozenset[str] = frozenset(
    {"AERGOUSDT", "BDXNUSDT", "DOTECOUSDT", "LENDUSDT", "MBLUSDT"}
)


class LeakageError(RuntimeError):
    """An input with ``known_at > t - 1 min`` was used at decision time t (S-2.3, L1)."""


@dataclass(frozen=True)
class Identity:
    """Immutable contract identity (R1 / E8). ``invariant_verified`` = its source is verified invariant."""

    base: str | None
    contract_type: str | None
    quote_asset: str | None
    underlying_type: str | None
    multiplier: float | None
    invariant_verified: bool


@dataclass(frozen=True)
class SymbolData:
    symbol: str
    bars15: pd.DataFrame  # open_ts, o,h,l,c, vol_base, vol_quote, known_at
    bars1m: pd.DataFrame  # open_ts, vol_base (+ known_at optional; default open_ts + 1 min)
    funding_annot: pd.DataFrame  # intervals.annotate output + known_at
    insts: list[Instance]
    notices: list[NoticeVersion] = field(default_factory=list)
    identity: Identity | None = None


@dataclass(frozen=True)
class GateRow:
    u1: bool
    u2: bool
    u3: bool
    u4: bool
    u5: bool
    u6: bool
    u7: bool
    u8: bool
    reasons: tuple[str, ...]
    max_known_at: int | None


def instrument_exclusion(symbol: str, identity: Identity | None) -> str | None:
    """S-2.1 exclusion reason, or None if the instrument is in the set. Unknown identity -> excluded."""
    if symbol in HEDGE_ONLY:
        return "hedge_only"
    if symbol in UNKNOWN_TYPE_SYMBOLS:
        return "unknown_type_symbol"
    if identity is None or not identity.invariant_verified:
        return "identity_unverified"
    if not (identity.base and identity.contract_type and identity.quote_asset and identity.underlying_type):
        return "identity_unknown"
    if identity.contract_type != "PERPETUAL":
        return "not_perpetual"
    if identity.quote_asset != "USDT":
        return "quote_not_usdt"
    if identity.underlying_type != "COIN":
        return "underlying_not_coin"
    if identity.base.upper() in STABLE_BASES:
        return "stablecoin_base"
    if identity.multiplier is None or not np.isfinite(identity.multiplier) or identity.multiplier <= 0:
        return "identity_unknown"
    return None


def eligible(row: GateRow) -> bool:
    return all((row.u1, row.u2, row.u3, row.u4, row.u5, row.u6, row.u7, row.u8))


def g_d0(row: GateRow) -> bool:
    """S-1.8 breadth gates G_D0 = U1 and U2 and U3 and U4."""
    return row.u1 and row.u2 and row.u3 and row.u4


def entry_allowed(row: GateRow) -> bool:
    """New entries / increases need every gate (including U8)."""
    return eligible(row)


def exit_triggered(row: GateRow) -> bool:
    """U6 forces an exit at the next decision; U8 does not (S-2.3, S-2.5)."""
    return not row.u6


def assert_pit(row: GateRow, t_ns: int) -> None:
    if row.max_known_at is not None and row.max_known_at > t_ns - MIN:
        raise LeakageError(f"max_known_at {row.max_known_at} > t - 1 min ({t_ns - MIN})")


def pit_rows(df: pd.DataFrame, t_ns: int, name: str) -> pd.DataFrame:
    """Rows with known_at <= t - 1 min (required column, validated)."""
    if "known_at" not in df.columns:
        raise ValueError(f"{name}: no known_at column: point-in-time filtering is impossible")
    if len(df) == 0:
        return df
    return df[ns_array(df["known_at"], f"{name}.known_at") <= t_ns - MIN]


def _mx(*vals: int | None) -> int | None:
    v = [x for x in vals if x is not None]
    return max(v) if v else None


def _max_ka(df: pd.DataFrame, mask: np.ndarray | None = None) -> int | None:
    if len(df) == 0:
        return None
    ka = ns_array(df["known_at"], "known_at")
    if mask is not None:
        ka = ka[mask]
    return int(ka.max()) if len(ka) else None


def daily_sums(bars: pd.DataFrame, t_ns: int, days: int, col: str) -> tuple[np.ndarray, np.ndarray]:
    """Per-UTC-day sums of ``col`` over the ``days`` UTC days fully ended before t (an empty day or a
    non-finite value counts 0; AMB-07) and the row mask used. The caller passes PIT current-instance bars."""
    d_t = t_ns // D
    first = d_t - days
    ot = ns_array(bars["open_ts"], "open_ts")
    m = (ot // D >= first) & (ot // D < d_t)
    q = pd.to_numeric(bars[col], errors="coerce").to_numpy(np.float64)[m]
    q = np.where(np.isfinite(q), q, 0.0)
    daily = np.zeros(days)
    np.add.at(daily, (ot[m] // D - first).astype(int), q)
    return daily, m


def median_daily_quote_volume(bars: pd.DataFrame, t_ns: int, days: int) -> tuple[float, int | None]:
    """Median daily quote volume over the ``days`` UTC days fully ended before t (U2, F8) and max known_at."""
    daily, m = daily_sums(bars, t_ns, days, "vol_quote")
    return float(np.median(daily)), _max_ka(bars, m)


def _u2(bars: pd.DataFrame, t_ns: int) -> tuple[bool, int | None]:
    """Median daily quote volume over the 30 UTC days fully ended before t (empty day = 0; AMB-07)."""
    med, ka = median_daily_quote_volume(bars, t_ns, U.u2_days)
    return med >= U.u2_min_quote_vol, ka


def _u4(bars: pd.DataFrame, t_ns: int) -> tuple[bool, int | None]:
    """>= 95 % of the 96 bars closed in (L - 24 h, L], L = last 15m close <= t - 1 min (AMB-07)."""
    last_close = (t_ns - MIN) // BAR_15M * BAR_15M
    lo_open = last_close - BARS_PER_DAY * BAR_15M
    ot = ns_array(bars["open_ts"], "open_ts")
    c = pd.to_numeric(bars["c"], errors="coerce").to_numpy(np.float64)
    m = (ot >= lo_open) & (ot < last_close) & np.isfinite(c) & (c > 0)
    n = len(np.unique(ot[m]))
    return n / BARS_PER_DAY >= U.u4_min_coverage, _max_ka(bars, m)


def _u8(bars: pd.DataFrame, t_ns: int) -> tuple[bool, int | None]:
    """A volume > 0 bar whose close lies in [t - 60 min, t - 1 min] (both ends inclusive)."""
    ot = ns_array(bars["open_ts"], "open_ts")
    v = pd.to_numeric(bars["vol_base"], errors="coerce").to_numpy(np.float64)
    close = ot + BAR_15M
    m = (close >= t_ns - U.u8_window[0] * MIN) & (close <= t_ns - U.u8_window[1] * MIN) & (v > 0)
    return bool(m.any()), _max_ka(bars, m)


def pit_funding(f: pd.DataFrame, cur: Instance, t_ns: int) -> pd.DataFrame:
    """Funding records of the current instance known by t - 1 min, annotated point-in-time.

    Membership is ``funding_ts >= cur.first_bar_ts`` with no upper bound (the end of the instance is unknown
    before ``end_known_at``, DEC-44); the interval flags (``nominal_h``, ``invalid``, ``r8``, ...) are
    recomputed on that PIT set with an open-ended instance, so they depend only on earlier records
    (``intervals.annotate``). Sorted by ``funding_ts`` with a fresh index; may be empty. Used by U3 / U7
    and by the §9 funding features.
    """
    cols = ["funding_ts", "rate", "interval_h_explicit", "known_at"]
    f = pit_rows(f, t_ns, "funding_annot")
    if len(f):
        f = f[ns_array(f["funding_ts"], "funding_ts") >= cur.first_bar_ts].sort_values("funding_ts")
    if len(f) == 0:
        return annotate(pd.DataFrame({c: pd.Series(dtype="int64") for c in cols}), [])
    raw = f[cols].reset_index(drop=True)
    return annotate(raw, [Instance(cur.idx, cur.first_bar_ts, None, None)])


def _funding(f: pd.DataFrame, cur: Instance, t_ns: int) -> tuple[bool, bool, int | None]:
    """(U3, U7, max known_at) on ``pit_funding``."""
    a = pit_funding(f, cur, t_ns)
    if len(a) == 0:
        return False, False, None
    ts = ns_array(a["funding_ts"], "funding_ts")
    ka = ns_array(a["known_at"], "known_at")
    nom = a["nominal_h"].astype("Float64").to_numpy(dtype=float, na_value=np.nan)
    ok = ~a["invalid"].to_numpy(bool)  # invalid records are missing (S-2.6), for U3 and U7 alike
    tv, nv = ts[ok], nom[ok]
    if len(tv) == 0:
        return False, False, int(ka.max())
    # U7: the last valid settled record; an undetermined nominal fails closed
    u7 = bool(not np.isnan(nv[-1]) and t_ns - tv[-1] <= nv[-1] * H + U.u7_slack_min * MIN)
    u3 = bool(tv[0] <= t_ns - U.u3_days * D)
    if u3:
        lo = max(int(np.searchsorted(tv, t_ns - U.u3_gap_lookback_days * D, "left")), 1)
        for i in range(lo, len(tv)):
            if u3_gap(nv[i - 1], nv[i], int(tv[i] - tv[i - 1])):
                u3 = False
                break
    return u3, u7, int(ka.max())


def gates(sd: SymbolData, t_ns: int) -> GateRow:
    """Evaluate U1-U8 for one symbol at decision time t (S-2.3)."""
    t_ns = int(t_ns)
    why = instrument_exclusion(sd.symbol, sd.identity)
    if why is not None:
        return GateRow(*(False,) * 8, (f"excluded:{why}",), None)
    cur = current_instance(sd.insts, t_ns)
    if cur is None:
        return GateRow(*(False,) * 8, ("no_current_instance",), None)
    b15 = pit_rows(sd.bars15, t_ns, "bars15")
    if len(b15):
        b15 = b15[ns_array(b15["open_ts"], "open_ts") >= cur.first_bar_ts]
    u1 = cur.first_bar_ts <= t_ns - U.u1_days * D
    u2, k2 = _u2(b15, t_ns) if len(b15) else (False, None)
    u4, k4 = _u4(b15, t_ns) if len(b15) else (False, None)
    u8, k8 = _u8(b15, t_ns) if len(b15) else (False, None)
    h = half_spread(b15, t_ns) if len(b15) else None
    u5 = h is not None and h <= U.u5_max_half_spread
    k5 = None
    if len(b15):
        k5 = _max_ka(b15, ns_array(b15["open_ts"], "open_ts") >= t_ns - FROZEN.spread.window_ns)
    u3, u7, kf = _funding(sd.funding_annot, cur, t_ns)
    b1 = sd.bars1m
    b1 = pit_rows(b1, t_ns, "bars1m")
    kn = [n for n in sd.notices if (k := known_at(n)) is not None and k <= t_ns - MIN]
    u6_active = U6State(kn, sd.symbol, sd.insts).active(cur, t_ns, lambda tau: halted(b1, tau))
    k6 = _mx(*(known_at(n) for n in kn))
    flags = (u1, u2, u3, u4, u5, not u6_active, u7, u8)
    reasons = tuple(f"U{i + 1}" for i, ok in enumerate(flags) if not ok)
    row = GateRow(*flags, reasons, _mx(k2, k4, k5, k8, kf, k6))
    assert_pit(row, t_ns)
    return row


def _month_start(ns: int) -> pd.Timestamp:
    return pd.Timestamp(ns, tz="UTC").tz_localize(None).normalize().replace(day=1)


def d0_month(counts_by_decision: Mapping[int, int]) -> str | None:
    """S-1.8: first calendar-month start >= FROZEN.d0 where every decision (00:05, 08:05, 16:05 UTC of every
    day) has >= 30 G_D0 symbols. A month with a missing decision does not qualify (fail closed)."""
    n_min = 30
    d0 = pd.Timestamp(FROZEN.d0)
    by_month: dict[pd.Timestamp, dict[int, int]] = {}
    for ts_, c in counts_by_decision.items():
        by_month.setdefault(_month_start(int(ts_)), {})[int(ts_)] = int(c)
    for m in sorted(by_month):
        if m < d0:
            continue
        end = m + pd.offsets.MonthBegin(1)
        expected = [
            to_ns(day + pd.Timedelta(hours=hh, minutes=5))
            for day in pd.date_range(m, end - pd.Timedelta(days=1), freq="D")
            for hh in (0, 8, 16)
        ]
        got = by_month[m]
        if all(e in got and got[e] >= n_min for e in expected):
            return m.strftime("%Y-%m-%d")
    return None
