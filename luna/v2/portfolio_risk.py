"""Portfolio-level risk for the cross-sectional book (spec §4.2/§4.3, SC-1).

``project_weights`` maps raw target weights (fractions of equity, signed) onto the pre-committed
controls. Perp, in this fixed order:

1. asset cap |w_i| <= 30%;
2. correlation concentration: same-side assets linked by 30-day correlation > 0.8 (connected
   components; NaN = uncorrelated) hold at most 50% together, scaled down proportionally;
3. net cap |sum w| <= 0.10: the heavier side is scaled down to (lighter + 0.10);
4. volatility targeting: scale = min(1, (vol_target / sqrt(365)) / daily portfolio sd) from the
   causal EWMA covariance (span 30 days) of daily log returns;
5. VaR budget: 1.645 x daily sd <= 1.5% of equity, scaled down if above.

Steps 4-5 are uniform down-scalings, so they keep 1-3 satisfied; nothing ever scales up. Spot
(§4.3): long-only, normalised to gross (1.0), asset cap 40%; no vol targeting or VaR budget.

Fails closed: a non-finite raw weight, an asset missing from the covariance or a non-finite
covariance entry among the traded assets gives all-zero weights and reason ``cov_unavailable``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from luna.config import RiskLimits

NET_CAP = 0.10
ASSET_CAP = {"perp": 0.30, "spot": 0.40}
CORR_THRESHOLD = 0.8
CORR_GROUP_CAP = 0.50
VAR_BUDGET = 0.015
VAR_Z95 = 1.6448536269514722
CAT_STOP = 0.015
COV_SPAN_DAYS = 30
CORR_DAYS = 30
DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class Projection:
    weights: pd.Series
    vol_scale: float
    var_95: float  # ex-ante 1-day 95% VaR of the projected weights, fraction of equity (NaN on spot)
    reason: str  # "" or "cov_unavailable"


def ewma_cov(daily_logret: pd.DataFrame) -> pd.DataFrame:
    """EWMA covariance (span 30 days, min 30 observations) at the last row of ``daily_logret``."""
    cols = list(daily_logret.columns)
    if len(daily_logret) == 0:
        return pd.DataFrame(np.nan, index=cols, columns=cols)
    cov = daily_logret.ewm(span=COV_SPAN_DAYS, min_periods=COV_SPAN_DAYS).cov()
    last = cov.loc[daily_logret.index[-1]]
    return last.reindex(index=cols, columns=cols)


def trailing_corr(daily_logret: pd.DataFrame) -> pd.DataFrame:
    """Correlation over the last 30 daily returns."""
    return daily_logret.iloc[-CORR_DAYS:].corr()


def _components(w: pd.Series, corr: pd.DataFrame, side: int) -> list[list[str]]:
    names = [a for a in w.index if w[a] * side > 0]

    def linked(x: str, y: str) -> bool:
        try:
            c = float(corr.loc[x, y])
        except KeyError:
            return False
        return math.isfinite(c) and c > CORR_THRESHOLD

    seen: set[str] = set()
    out = []
    for a in names:
        if a in seen:
            continue
        comp: list[str] = []
        stack = [a]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            stack.extend(y for y in names if y not in seen and linked(x, y))
        out.append(comp)
    return out


def _closed(raw: pd.Series) -> Projection:
    return Projection(pd.Series(0.0, index=raw.index), 0.0, math.nan, "cov_unavailable")


def project_weights(
    raw: pd.Series,
    cov: pd.DataFrame,
    corr: pd.DataFrame,
    *,
    controls: Literal["perp", "spot"],
    gross: float,
    vol_target: float | None,
) -> Projection:
    raw = raw.astype(float)
    if not np.isfinite(raw.to_numpy()).all():
        return _closed(raw)
    if controls == "spot":
        w = raw.clip(lower=0.0)
        if w.sum() > 0:
            w = w * (gross / w.sum())
        w = w.clip(upper=ASSET_CAP["spot"])
        return Projection(w, 1.0, math.nan, "")

    traded = [a for a in raw.index if raw[a] != 0]
    if any(a not in cov.index or a not in cov.columns for a in traded):
        return _closed(raw)
    sub = cov.loc[traded, traded].to_numpy(dtype=float)
    if not np.isfinite(sub).all():
        return _closed(raw)

    cap = ASSET_CAP["perp"]
    w = raw.clip(-cap, cap)
    for side in (1, -1):
        for grp in _components(w, corr, side):
            s = float(w[grp].abs().sum())
            if s > CORR_GROUP_CAP:
                w[grp] = w[grp] * (CORR_GROUP_CAP / s)
    net = float(w.sum())
    if abs(net) > NET_CAP:
        heavy = w * np.sign(net) > 0
        h = float(w[heavy].abs().sum())
        light = h - abs(net)
        w[heavy] = w[heavy] * ((light + NET_CAP) / h)

    def sd_of(x: pd.Series) -> float:
        v = x[traded].to_numpy(dtype=float)
        return math.sqrt(max(float(v @ sub @ v), 0.0))

    sd = sd_of(w)
    scale = 1.0
    if vol_target is not None and sd > 0:
        scale = min(1.0, (vol_target / math.sqrt(DAYS_PER_YEAR)) / sd)
    w = w * scale
    var = VAR_Z95 * sd_of(w)
    if var > VAR_BUDGET:
        w = w * (VAR_BUDGET / var)
        var = VAR_Z95 * sd_of(w)
    return Projection(w, scale, var, "")


def catastrophic_stop_hit(leg_pnl: float, equity: float) -> bool:
    """§4.2: a leg's loss since its period start reached 1.5% of equity (never on equity <= 0)."""
    return equity > 0 and leg_pnl <= -CAT_STOP * equity


def xsec_limits(limits: RiskLimits) -> RiskLimits:
    """SC-1 order-level profile for cross-sectional orders: R5/R6/R7/R9 non-binding (replaced by
    the projection's controls); every other limit keeps its V1 value."""
    return limits.model_copy(update={
        "max_risk_per_trade": 1.0,
        "max_open_risk": 1e9,
        "max_positions": 10_000,
        "max_leg_notional_frac": 1.0,
    })
