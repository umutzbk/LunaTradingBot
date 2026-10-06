"""Backtest metrics (spec §8), primary metrics first. Sharpe/Sortino only from DAILY returns and
informational only (never GO/NO-GO)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.engine.backtest import BacktestResult
from luna.research.stats import cvar, profit_factor

DAYS_PER_YEAR = 365  # crypto trades every day


def daily_returns(equity: pd.Series) -> pd.Series:
    """Simple returns of the UTC daily closing equity. The first day's return is measured against
    the first equity observation of the series."""
    if equity.empty:
        return pd.Series(dtype=float)
    closes = equity.resample("1D").last().ffill()
    prev = closes.shift(1)
    prev.iloc[0] = equity.iloc[0]
    return closes / prev - 1.0


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough decline as a positive fraction of the peak."""
    if equity.empty:
        return math.nan
    peak = equity.cummax()
    return float(((peak - equity) / peak).max())


def _ratio(num: float, den: float) -> float:
    return num / den if den > 0 and math.isfinite(den) else math.nan


def summary(res: BacktestResult) -> dict[str, float]:
    """Flat metric dict, primary metrics first.

    expectancy_r     mean pnl_net / risk_amount per trade.
    expectancy_bps   mean pnl_net / entry gross notional per trade, in bps.
    max_dd           max drawdown fraction (positive).
    worst_day        lowest daily return; cvar_5_daily mean of the worst 5% daily returns.
    worst_trade_r    lowest R multiple.
    turnover         traded notional per day (entry + exit ~ 2 x entry gross) / mean equity.
    exposure         fraction of bars with at least one open position.
    corr_btc         correlation of daily strategy returns with BTC daily returns (NaN if no BTC).
    """
    tr = res.trades
    eq = res.equity
    n = len(tr)
    net = tr["pnl_net"].to_numpy(dtype=float) if n else np.array([])
    r = tr["r_multiple"].to_numpy(dtype=float) if n else np.array([])
    gross_notional = tr["gross_notional"].to_numpy(dtype=float) if n else np.array([])
    dr = daily_returns(eq)
    n_days = max(len(dr), 1)
    mean_eq = float(eq.mean()) if len(eq) else math.nan

    if res.bar_stats is not None and len(res.bar_stats):
        exposure = float((res.bar_stats["n_positions"] > 0).mean())
        lev = res.bar_stats["gross_leverage"]
        avg_lev, max_lev = float(lev.mean()), float(lev.max())
    else:
        exposure, avg_lev, max_lev = math.nan, math.nan, math.nan

    corr_btc = math.nan
    if res.benchmark is not None and len(dr) >= 3:
        btc = daily_returns(res.benchmark.dropna())
        both = pd.concat([dr, btc], axis=1, join="inner").dropna()
        if len(both) >= 3 and both.iloc[:, 0].std() > 0 and both.iloc[:, 1].std() > 0:
            corr_btc = float(both.iloc[:, 0].corr(both.iloc[:, 1]))

    sd = float(dr.std(ddof=1)) if len(dr) > 1 else math.nan
    downside = dr[dr < 0]
    dsd = float(np.sqrt((downside**2).sum() / (len(dr) - 1))) if len(dr) > 1 else math.nan
    ann = math.sqrt(DAYS_PER_YEAR)

    def tot(col: str) -> float:
        return float(tr[col].sum()) if n else 0.0

    return {
        # primary
        "n_trades": float(n),
        "expectancy_r": float(r.mean()) if n else math.nan,
        "expectancy_bps": float(np.mean(net / gross_notional) * 1e4) if n else math.nan,
        "profit_factor": profit_factor(net),
        "max_dd": max_drawdown(eq),
        "worst_day": float(dr.min()) if len(dr) else math.nan,
        "cvar_5_daily": cvar(dr.to_numpy(), 0.05),
        "worst_trade_r": float(r.min()) if n else math.nan,
        "turnover": _ratio(2.0 * float(gross_notional.sum()) / n_days, mean_eq),
        "exposure": exposure,
        "win_rate": float((net > 0).mean()) if n else math.nan,
        "pnl_gross": tot("pnl_gross"),
        "fees": tot("fees"),
        "spread_slip": tot("spread_slip"),
        "funding": tot("funding"),
        "pnl_net": tot("pnl_net"),
        "funding_unknown_trades": float((tr["funding_unknown_bars"] > 0).sum()) if n else 0.0,
        # secondary / informational
        "sharpe_daily": _ratio(float(dr.mean()), sd) * ann if len(dr) > 1 else math.nan,
        "sortino_daily": _ratio(float(dr.mean()), dsd) * ann if len(dr) > 1 else math.nan,
        "corr_btc": corr_btc,
        "avg_leverage": avg_lev,
        "max_leverage": max_lev,
    }
