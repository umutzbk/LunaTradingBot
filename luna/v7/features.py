"""Feature engine F1-F6, F8-F14 and linear-model preprocessing (spec §9, S-2.2, S-2.6, S-4.3-S-4.5, S-5.2,
S-10.4, L1, L5, L7).

* Point in time: every input row is filtered to ``known_at <= t - 1 min`` (a missing ``known_at`` column
  raises); ``compute_row`` reports the max ``known_at`` used and asserts it (L1, DT1 ``max_known_at``).
* Instance boundary (S-2.2): every look-back uses only rows of the symbol's current instance
  (``open_ts`` / ``funding_ts`` >= ``first_bar_ts``; ``universe.pit_funding`` for funding). No current
  instance -> every symbol feature is missing (NaN). A look-back reaching before the instance start finds
  no price there and is missing, never bridged.
* Funding: r8 = rate * 8 / nominal interval (S-2.6, ``intervals.annotate``); invalid records are missing.
* Side adjustment: the ``CARRY_DIRECTION`` features are multiplied by -s; ``side`` = s (+1 long, -1 short).
* F11 / F12 use the eligible universe at t: ``XSInput`` rows built by ``xs_input`` at the same t; a row is
  in the cross-section iff ``universe.eligible(row.gate)``.
* F7 and F15 are dropped (Stage 0 coverage rule). Missing inputs give NaN (fail closed, never a leak).
* ``Preprocessor`` (linear models): fitted on T_fit rows only (L5, refuses calibration / test rows):
  winsorize at the fit 1 / 99 % quantiles, standardize (constant column -> 0), median-impute plus one
  missing indicator per feature. LightGBM gets ``raw_matrix`` (raw values, NaN native).
Every interpretation the §9 table leaves open is logged as AMB-08 in docs/results/v7-impl/AMBIGUITIES.md.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.v7.beta import beta_hat, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, D, H, ns_array
from luna.v7.universe import (
    GateRow,
    LeakageError,
    SymbolData,
    daily_sums,
    eligible,
    gates,
    median_daily_quote_volume,
    pit_funding,
    pit_rows,
)
from luna.v7.windows import Fold

BAR_15M = 15 * MIN
NAN = float("nan")
F = FROZEN.features
HOURS_PER_YEAR = 24 * 365  # F9 annualisation (AMB-08), consistent with F13's 3 * 365 settlements per year
F6_STEP = 8 * H  # F6 7 d median sampled on the 8 h decision clock: t - k * 8 h, k = 0..20 (AMB-08)
F6_SAMPLES = 21

XS_KEYS = ("fl1", "fl7", "fl30", "premium_mean_8h", "premium_mean_24h", "premium_last")
F14_COLS = tuple(f"f14_carry_over_cost_{h}h" for h in FROZEN.horizons_h)

FEATURES: tuple[str, ...] = (
    "side",
    "f1_fl1", "f1_fl3", "f1_fl7", "f1_fl30",
    "f2_same_sign_share_30d", "f2_autocorr1_30d",
    "f3_slope_7d", "f3_fl1_minus_fl7", "f3_accel",
    "f4_sign_reversals_30d",
    "f5_premium_mean_8h", "f5_premium_mean_24h", "f5_premium_last",
    "f6_half_spread", "f6_half_spread_median_7d",
    "f8_log_median_quote_vol_30d", "f8_amihud_7d", "f8_log_trades_per_day_7d",
    "f9_rvol_24h", "f9_rvol_7d",
    "f10_ret_24h", "f10_ret_7d", "f10_ret_30d", "f10_resid_ret_7d",
    "f11_btc_ret_30d", "f11_btc_rvol_7d", "f11_xs_median_fl1", "f11_xs_share_fl1_pos",
    *(f"f12_rank_{k}" for k in XS_KEYS),
    "f13_rvol_over_carry", "f13_r8_std_over_fl30",
    "f14_cost_bps", *F14_COLS,
)

# Carry-direction features (x -s): every feature whose value is a signed funding / premium level or change
# of the candidate, the market-wide funding direction, or a rank of such a value (AMB-08).
CARRY_DIRECTION: tuple[str, ...] = tuple(
    c for c in FEATURES
    if c.startswith(("f1_", "f3_", "f5_", "f12_")) or c in ("f11_xs_median_fl1", "f11_xs_share_fl1_pos")
)


def features_for_horizon(h: int) -> tuple[str, ...]:
    """The model columns for horizon h: FEATURES with only the F14 carry/cost column of that h."""
    if h not in FROZEN.horizons_h:
        raise ValueError(f"unknown horizon {h}")
    keep = f"f14_carry_over_cost_{h}h"
    return tuple(c for c in FEATURES if c not in F14_COLS or c == keep)


@dataclass(frozen=True)
class R4Bars:
    """R4 15-minute premium-index, mark-price and index-price bars of one symbol (``open_ts``, ``c``,
    ``known_at``). Frames may be empty but must carry ``known_at``."""

    premium: pd.DataFrame
    mark: pd.DataFrame
    index: pd.DataFrame


@dataclass(frozen=True)
class XSInput:
    """One symbol's cross-sectional inputs at decision t (raw, not side-adjusted) and its gates."""

    symbol: str
    t_ns: int
    gate: GateRow
    values: Mapping[str, float]
    max_known_at: int | None


@dataclass(frozen=True)
class FeatureRow:
    values: dict[str, float]
    max_known_at: int | None


def assert_pit(inputs: Mapping[str, pd.DataFrame], t_ns: int) -> int | None:
    """L1: every row used must have ``known_at <= t - 1 min``; returns the max ``known_at`` (None if empty).

    Raises ``LeakageError`` on a later row and ``ValueError`` on a frame without a ``known_at`` column.
    """
    mx: int | None = None
    for name, df in inputs.items():
        if "known_at" not in df.columns:
            raise ValueError(f"{name}: no known_at column")
        if len(df) == 0:
            continue
        k = int(ns_array(df["known_at"], f"{name}.known_at").max())
        if k > t_ns - MIN:
            raise LeakageError(f"{name}: known_at {k} > t - 1 min ({t_ns - MIN})")
        mx = k if mx is None else max(mx, k)
    return mx


# ----------------------------------------------------------------------------------------- helpers


def _f(x: float | None) -> float:
    return NAN if x is None or not math.isfinite(x) else float(x)


def _mean(x: np.ndarray) -> float:
    return float(np.mean(x)) if len(x) else NAN


def _capped_ratio(num: float, den: float) -> float:
    """F13: num / den capped at 1e4; den = 0 with num > 0 -> cap; 0 / 0 or a missing input -> NaN."""
    if not (math.isfinite(num) and math.isfinite(den)):
        return NAN
    if den == 0.0:
        return F.f13_cap if num > 0 else NAN
    return min(num / den, F.f13_cap)


def _instance_rows(df: pd.DataFrame, cur: Instance, t_ns: int, name: str) -> pd.DataFrame:
    """PIT rows (``known_at <= t - 1 min``) of the current instance (``open_ts >= first_bar_ts``)."""
    df = pit_rows(df, t_ns, name)
    if len(df):
        df = df[ns_array(df["open_ts"], f"{name}.open_ts") >= cur.first_bar_ts]
    return df.sort_values("open_ts")


def _hourly(b15: pd.DataFrame) -> pd.Series:
    """Full-hour closes (15m bar closing at the hour) with a finite, positive price; others are missing."""
    if len(b15) == 0:
        return pd.Series(dtype=np.float64)
    c = hourly_close_from_15m(b15)
    return c[np.isfinite(c.to_numpy()) & (c.to_numpy() > 0)]


def _last_hour(t_ns: int) -> int:
    return ((t_ns - MIN) // H) * H


def _ret(hc: pd.Series, t_ns: int, window_ns: int) -> float:
    """Simple return C(L) / C(L - W) - 1 between full-hour closes, L = last full hour <= t - 1 min."""
    L = _last_hour(t_ns)  # noqa: N806
    a, b = hc.get(L), hc.get(L - window_ns)
    return NAN if a is None or b is None else float(a / b - 1.0)


def _rvol(hc: pd.Series, t_ns: int, window_ns: int) -> float:
    """Annualised realized vol sqrt(mean(r^2) * 24 * 365) of the hourly log returns ending in (L - W, L]."""
    L = _last_hour(t_ns)  # noqa: N806
    hours = np.arange(L - window_ns + H, L + H, H, dtype=np.int64)
    cur = hc.reindex(hours).to_numpy(np.float64)
    prev = hc.reindex(hours - H).to_numpy(np.float64)
    with np.errstate(all="ignore"):
        r = np.log(cur / prev)
    r = r[np.isfinite(r)]
    return math.sqrt(float(np.mean(r**2)) * HOURS_PER_YEAR) if len(r) else NAN


# -------------------------------------------------------------------------------- symbol features


def _funding_feats(fund: pd.DataFrame, t_ns: int) -> dict[str, float]:
    ts = ns_array(fund["funding_ts"], "funding_ts") if len(fund) else np.empty(0, np.int64)
    r8 = fund["r8"].to_numpy(np.float64) if len(fund) else np.empty(0)
    ok = np.isfinite(r8)  # invalid records (and NaN rates) are missing (S-2.6)
    ts, r8 = ts[ok], r8[ok]

    def win(hours: int) -> np.ndarray:
        return ts > t_ns - hours * H

    fl1, fl3, fl7, fl30 = (_mean(r8[win(w)]) for w in F.funding_windows_h)
    x30 = r8[win(720)]
    out = dict(fl1=fl1, fl3=fl3, fl7=fl7, fl30=fl30)
    out["f2_same_sign_share_30d"] = (
        float(np.mean(np.sign(x30) == np.sign(fl1))) if len(x30) and math.isfinite(fl1) else NAN
    )
    ac = NAN
    if len(x30) >= 2:
        a, b = x30[:-1], x30[1:]
        if np.ptp(a) > 0 and np.ptp(b) > 0:
            ac = float(np.corrcoef(a, b)[0, 1])
    out["f2_autocorr1_30d"] = ac
    w7 = win(168)
    x = (ts[w7] - t_ns) / D
    y = r8[w7]
    slope = NAN
    if len(x) >= 2 and np.ptp(x) > 0:  # OLS slope of r8 on time in days
        slope = float(np.sum((x - x.mean()) * (y - y.mean())) / np.sum((x - x.mean()) ** 2))
    out["f3_slope_7d"] = slope
    out["f3_fl1_minus_fl7"] = fl1 - fl7
    out["f3_accel"] = (fl1 - fl3) - (fl3 - fl7)
    out["f4_sign_reversals_30d"] = float(np.sum(x30[1:] * x30[:-1] < 0) / len(x30)) if len(x30) else NAN
    out["r8_std_30d"] = float(np.std(x30, ddof=1)) if len(x30) >= 2 else NAN
    return out


def _premium(r4: R4Bars, cur: Instance, t_ns: int) -> dict[str, float]:
    """F5 per bar: premium-index close; where it is missing, (mark - index) / index of the same bar."""
    p = _instance_rows(r4.premium, cur, t_ns, "premium")
    m = _instance_rows(r4.mark, cur, t_ns, "mark")
    i = _instance_rows(r4.index, cur, t_ns, "index")
    ps = pd.Series(p["c"].to_numpy(np.float64), index=ns_array(p["open_ts"], "premium.open_ts"))
    ps = ps[np.isfinite(ps.to_numpy())]
    ms = pd.Series(m["c"].to_numpy(np.float64), index=ns_array(m["open_ts"], "mark.open_ts"))
    ix = pd.Series(i["c"].to_numpy(np.float64), index=ns_array(i["open_ts"], "index.open_ts"))
    joined = pd.concat([ms.rename("m"), ix.rename("i")], axis=1, join="inner")
    ok = np.isfinite(joined["m"]) & np.isfinite(joined["i"]) & (joined["i"] > 0)
    fb = ((joined["m"] - joined["i"]) / joined["i"])[ok]
    s = pd.concat([ps, fb[~fb.index.isin(ps.index)]]).sort_index()
    close = s.index.to_numpy(np.int64) + BAR_15M
    v = s.to_numpy(np.float64)
    w8, w24 = close > t_ns - 8 * H, close > t_ns - 24 * H
    return dict(
        premium_mean_8h=_mean(v[w8]),
        premium_mean_24h=_mean(v[w24]),
        premium_last=float(v[w24][-1]) if w24.any() else NAN,
    )


def _symbol(sd: SymbolData, r4: R4Bars, t_ns: int, xs_only: bool) -> tuple[dict[str, float], dict]:
    """Raw (unsigned) symbol features and the PIT frames used. No current instance -> {} (all missing)."""
    used: dict[str, pd.DataFrame] = {}
    cur = current_instance(sd.insts, t_ns)
    if cur is None:
        for name, df in (("bars15", sd.bars15), ("premium", r4.premium), ("mark", r4.mark),
                         ("index", r4.index), ("funding", sd.funding_annot)):
            pit_rows(df, t_ns, f"{sd.symbol}.{name}")  # inputs are still validated (known_at required)
        return {}, used
    fund = pit_funding(sd.funding_annot, cur, t_ns)
    used["funding"] = fund
    out = _funding_feats(fund, t_ns)
    out.update(_premium(r4, cur, t_ns))
    used.update({k: _instance_rows(getattr(r4, k), cur, t_ns, k) for k in ("premium", "mark", "index")})
    b15 = _instance_rows(sd.bars15, cur, t_ns, f"{sd.symbol}.bars15")
    used["bars15"] = b15
    if xs_only:
        return out, used
    h0 = half_spread(b15, t_ns) if len(b15) else None
    recent = b15[ns_array(b15["open_ts"], "open_ts") >= t_ns - 8 * D] if len(b15) else b15
    hs = [half_spread(recent, t_ns - k * F6_STEP) if len(recent) else None for k in range(F6_SAMPLES)]
    hs = [x for x in hs if x is not None]
    out["f6_half_spread"] = _f(h0)
    out["f6_half_spread_median_7d"] = float(np.median(hs)) if hs else NAN
    med, _ = median_daily_quote_volume(b15, t_ns, 30) if len(b15) else (0.0, None)
    out["f8_log_median_quote_vol_30d"] = math.log(med) if med > 0 else NAN
    hc = _hourly(b15)
    d_t = t_ns // D
    if len(b15):
        q, _ = daily_sums(b15, t_ns, 7, "vol_quote")
        am = []
        for j, d in enumerate(range(d_t - 7, d_t)):
            a, b = hc.get((d + 1) * D), hc.get(d * D)
            if a is not None and b is not None and q[j] > 0:
                am.append(abs(a / b - 1.0) / q[j])
        out["f8_amihud_7d"] = _mean(np.array(am))
        if "n_trades" in b15.columns:
            n, _ = daily_sums(b15, t_ns, 7, "n_trades")
            out["f8_log_trades_per_day_7d"] = math.log(n.sum() / 7) if n.sum() > 0 else NAN
    out["f9_rvol_24h"] = _rvol(hc, t_ns, D)
    out["f9_rvol_7d"] = _rvol(hc, t_ns, 7 * D)
    for name, w in (("f10_ret_24h", D), ("f10_ret_7d", 7 * D), ("f10_ret_30d", 30 * D)):
        out[name] = _ret(hc, t_ns, w)
    out["_h0"] = _f(h0)
    return out, used


def xs_input(sd: SymbolData, r4: R4Bars, t_ns: int) -> XSInput:
    """Gates and raw cross-sectional values (FL1, FL7, FL30, F5) of one symbol at t, for F11 / F12."""
    t_ns = int(t_ns)
    g = gates(sd, t_ns)
    vals, used = _symbol(sd, r4, t_ns, xs_only=True)
    ka = assert_pit(used, t_ns)
    ka = max((x for x in (ka, g.max_known_at) if x is not None), default=None)
    return XSInput(sd.symbol, t_ns, g, {k: vals.get(k, NAN) for k in XS_KEYS}, ka)


def _cross_section(rows: Sequence[XSInput], t_ns: int, symbol: str, own: dict[str, float]) -> dict:
    seen: set[str] = set()
    for r in rows:
        if r.t_ns != t_ns:
            raise ValueError(f"universe row {r.symbol} built at {r.t_ns}, not at t = {t_ns}")
        if r.symbol in seen:
            raise ValueError(f"duplicate universe row {r.symbol}")
        seen.add(r.symbol)
        if r.max_known_at is not None and r.max_known_at > t_ns - MIN:
            raise LeakageError(f"universe row {r.symbol}: max_known_at > t - 1 min")
    el = {r.symbol: r.values for r in rows if eligible(r.gate)}
    fl1 = np.array([v["fl1"] for v in el.values()], np.float64)
    fl1 = fl1[np.isfinite(fl1)]
    out = dict(
        f11_xs_median_fl1=float(np.median(fl1)) if len(fl1) else NAN,
        f11_xs_share_fl1_pos=float(np.mean(fl1 > 0)) if len(fl1) else NAN,
    )
    for k in XS_KEYS:
        x = own.get(k, NAN)
        vals = {s: v[k] for s, v in el.items()}
        vals[symbol] = x  # the candidate's own value (identical when it is eligible)
        v = np.array([y for y in vals.values() if math.isfinite(y)], np.float64)
        if not math.isfinite(x):
            out[f"f12_rank_{k}"] = NAN
            continue
        less, eq = float(np.sum(v < x)), float(np.sum(v == x))
        out[f"f12_rank_{k}"] = (less + (eq + 1.0) / 2.0) / len(v)  # average-rank percentile
    return out


def compute_row(
    sd: SymbolData, btc: SymbolData, universe_rows: Sequence[XSInput], t_ns: int, side: int, *, r4: R4Bars
) -> FeatureRow:
    """All FEATURES of candidate (sd.symbol, side) at t, side-adjusted, with the max known_at used."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side!r}")
    t_ns = int(t_ns)
    raw, used = _symbol(sd, r4, t_ns, xs_only=False)
    v: dict[str, float] = {"side": float(side)}
    for k in ("fl1", "fl3", "fl7", "fl30"):
        v[f"f1_{k}"] = raw.get(k, NAN)
    for k in ("premium_mean_8h", "premium_mean_24h", "premium_last"):
        v[f"f5_{k}"] = raw.get(k, NAN)
    for c in FEATURES:
        if c in raw:
            v[c] = raw[c]
    # BTC (hedge instrument): its own current instance, PIT
    bcur = current_instance(btc.insts, t_ns)
    bb = _instance_rows(btc.bars15, bcur, t_ns, "btc.bars15") if bcur is not None else btc.bars15.iloc[0:0]
    if bcur is None:
        pit_rows(btc.bars15, t_ns, "btc.bars15")
    used["btc_bars15"] = bb
    bhc = _hourly(bb)
    v["f11_btc_ret_30d"] = _ret(bhc, t_ns, 30 * D)
    v["f11_btc_rvol_7d"] = _rvol(bhc, t_ns, 7 * D)
    resid = NAN
    ab = used.get("bars15")  # the asset's current-instance PIT bars (absent without a current instance)
    if ab is not None and len(ab) and len(bb):
        beta = beta_hat(_hourly(ab), bhc, t_ns, hourly_known_at_from_15m(ab), hourly_known_at_from_15m(bb))
        ra, rb = raw.get("f10_ret_7d", NAN), _ret(bhc, t_ns, 7 * D)
        if beta is not None:
            resid = ra - beta * rb
    v["f10_resid_ret_7d"] = _f(resid)
    v.update(_cross_section(universe_rows, t_ns, sd.symbol, raw))
    v["f13_rvol_over_carry"] = _capped_ratio(
        raw.get("f9_rvol_7d", NAN), abs(raw.get("fl7", NAN)) * F.f13_settlements_per_year
    )
    v["f13_r8_std_over_fl30"] = _capped_ratio(raw.get("r8_std_30d", NAN), abs(raw.get("fl30", NAN)))
    c = FROZEN.costs
    cost = 2.0 * FROZEN.fees.taker + 2.0 * raw.get("_h0", NAN) + 2.0 * c.slip  # S-4.3 taker, S-4.4/4.5
    v["f14_cost_bps"] = cost * 1e4
    for h, col in zip(FROZEN.horizons_h, F14_COLS, strict=True):
        v[col] = abs(raw.get("fl7", NAN)) * 3.0 * h / 24.0 / cost  # same units (fractions): unitless
    for k in FEATURES:
        v.setdefault(k, NAN)
    out = {k: (-side * v[k] if k in CARRY_DIRECTION else v[k]) for k in FEATURES}
    out = {k: _f(x) for k, x in out.items()}
    ka = assert_pit(used, t_ns)
    xs_ka = [r.max_known_at for r in universe_rows if r.max_known_at is not None]
    ka = max([x for x in (ka, *xs_ka) if x is not None], default=None)
    return FeatureRow(out, ka)


def compute(
    sd: SymbolData, btc: SymbolData, universe_rows: Sequence[XSInput], t_ns: int, side: int, *, r4: R4Bars
) -> dict[str, float]:
    """Side-adjusted FEATURES of one candidate at t (see module docstring); missing inputs give NaN."""
    return compute_row(sd, btc, universe_rows, t_ns, side, r4=r4).values


# ---------------------------------------------------------------------------------- preprocessing


def raw_matrix(X: pd.DataFrame, columns: Sequence[str] = FEATURES) -> pd.DataFrame:  # noqa: N803
    """LightGBM input: the raw feature values (float64); +-inf become NaN (native missing handling)."""
    R = X[list(columns)].astype(np.float64)  # noqa: N806
    return R.where(np.isfinite(R.to_numpy()), np.nan)


@dataclass(frozen=True)
class _ColParams:
    lo: float
    hi: float
    mean: float
    sd: float  # 0 -> the column is constant on the fit rows: transforms to 0
    median_z: float


class Preprocessor:
    """Linear-model preprocessing fitted on T_fit = T_tr u T_sel only (S-10.4, L5).

    ``fit`` refuses any row whose decision time is outside T_fit (T_cal, the test quarter or before D0):
    ``LeakageError``. Per column, on the finite fit values: winsorize at the 1 / 99 % quantiles
    (``numpy.quantile``, linear), standardize with the mean and population std of the winsorized values
    (a constant column maps to 0), impute missing (NaN / +-inf) with the median of the standardized fit
    values (0 for an all-missing column) and add a ``<col>__missing`` indicator (1.0 / 0.0).
    """

    def __init__(self, columns: Sequence[str] = FEATURES) -> None:
        self.columns = tuple(columns)
        self._params: dict[str, _ColParams] | None = None

    def fit(self, X_fit: pd.DataFrame, decision_ts, fold: Fold) -> Preprocessor:  # noqa: N803
        ts = ns_array(decision_ts, "decision_ts")
        if len(ts) != len(X_fit):
            raise ValueError("decision_ts and X_fit lengths differ")
        if len(ts) == 0:
            raise ValueError("empty fit set")
        lo, hi = fold.tr[0], fold.sel[1]
        bad = (ts < lo) | (ts >= hi)
        if bad.any():
            raise LeakageError(f"L5: {int(bad.sum())} rows outside T_fit [{lo}, {hi}) (calibration / test)")
        params: dict[str, _ColParams] = {}
        for c in self.columns:
            x = X_fit[c].to_numpy(np.float64)
            x = x[np.isfinite(x)]
            if len(x) == 0:
                params[c] = _ColParams(NAN, NAN, 0.0, 0.0, 0.0)
                continue
            q1, q99 = (float(q) for q in np.quantile(x, [0.01, 0.99]))
            w = np.clip(x, q1, q99)
            mean = float(w.mean())
            sd = float(w.std()) if np.ptp(w) > 0 else 0.0
            z = (w - mean) / sd if sd > 0 else np.zeros_like(w)
            params[c] = _ColParams(q1, q99, mean, sd, float(np.median(z)))
        self._params = params
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:  # noqa: N803
        if self._params is None:
            raise RuntimeError("Preprocessor is not fitted")
        out: dict[str, np.ndarray] = {}
        miss: dict[str, np.ndarray] = {}
        for c in self.columns:
            p = self._params[c]
            x = X[c].to_numpy(np.float64)
            m = ~np.isfinite(x)
            if p.sd > 0:
                with np.errstate(invalid="ignore"):
                    z = (np.clip(x, p.lo, p.hi) - p.mean) / p.sd
            else:
                z = np.zeros(len(x))
            z = np.where(m, p.median_z, z)
            out[c], miss[f"{c}__missing"] = z, m.astype(np.float64)
        return pd.DataFrame({**out, **miss}, index=X.index)
