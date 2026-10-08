"""Extended research metrics (spec §6, D5). Every function is pure; ``extended_metrics`` gathers them
for one ``BacktestResult`` (signal engine or cross-sectional simulator).

Return conventions: tail-risk values are daily simple returns (negative = loss); R values are
multiples of the trade's planned risk unless stated otherwise.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from luna.config import RiskLimits
from luna.engine.backtest import BacktestResult
from luna.execution.costs import CostModel
from luna.report.metrics import daily_returns
from luna.research.stats import cvar
from luna.types import AccountKind, OrderModel, default_order_model
from luna.v2.fills import maker_fill_rate

BAR = pd.Timedelta("15min")

R_DEFINITIONS: dict[str, str] = {
    "r_net (research)": "trade-list net return / stop distance at the signal bar (per unit of notional)",
    "r_multiple (engine, planned R)": "net P&L / risk_amount, the planned risk at entry after caps "
    "(stop distance x notional + round-trip cost; cross-sectional: "
    "1.5% x equity + round-trip cost)",
    "cap-adjusted R": "net P&L / planned risk BEFORE caps (risk_precap if recorded, else "
    "risk_per_trade x equity at the signal bar); shows R inflated by caps",
    "gross R": "gross P&L (reference prices, before fees, spread, slippage and funding) / risk_amount",
}


def cap_adjusted_r(trades: pd.DataFrame, equity: pd.Series, limits: RiskLimits) -> pd.Series:
    """pnl_net / pre-cap planned risk (module ``R_DEFINITIONS``)."""
    if trades.empty:
        return pd.Series(dtype=float)
    net = trades["pnl_net"].astype(float)
    if "risk_precap" in trades:
        return net / trades["risk_precap"].astype(float)
    sig = pd.DatetimeIndex(trades["entry_ts"]) - BAR
    eq = equity.sort_index()
    pos = eq.index.searchsorted(sig, side="right") - 1
    at = np.where(pos >= 0, eq.to_numpy()[np.clip(pos, 0, None)], np.nan)
    return pd.Series(net.to_numpy() / (limits.risk_per_trade * at), index=trades.index)


def split_spread_slippage(trades: pd.DataFrame, costs: CostModel, order_model: OrderModel) -> pd.DataFrame:
    """Split each trade's spread_slip: taker in proportion half_spread : slippage, maker all spread."""
    ss = trades["spread_slip"].astype(float) if len(trades) else pd.Series(dtype=float)
    if order_model == "maker":
        return pd.DataFrame({"spread": ss, "slippage": 0.0 * ss})

    def spread_share(key: str) -> float:
        hs = costs.half_spread(key.split("/")[0])
        return hs / (hs + costs.slippage) if hs + costs.slippage > 0 else 1.0

    share = np.array([spread_share(str(k)) for k in trades.get("key", [])])
    return pd.DataFrame({"spread": ss * share, "slippage": ss * (1.0 - share)}, index=trades.index)


def mfe_mae_r(trades: pd.DataFrame, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Max favourable / adverse excursion of 15m highs/lows in [entry_ts, exit_ts) from the entry
    bar's open, in R (fraction x gross_notional / risk_amount); MAE <= 0."""
    mfe, mae = [], []
    for t in trades.itertuples(index=False):
        bars = panel.get(str(t.key))
        if bars is None or not (t.risk_amount > 0):
            mfe.append(math.nan)
            mae.append(math.nan)
            continue
        w = bars[(bars.index >= t.entry_ts) & (bars.index < t.exit_ts)]
        if w.empty:
            mfe.append(math.nan)
            mae.append(math.nan)
            continue
        p0 = float(w["open"].iloc[0])
        hi, lo = float(w["high"].max()), float(w["low"].min())
        fav, adv = ((hi - p0) / p0, (lo - p0) / p0) if t.direction > 0 else ((p0 - lo) / p0, (p0 - hi) / p0)
        scale = float(t.gross_notional) / float(t.risk_amount)
        mfe.append(fav * scale)
        mae.append(adv * scale)
    return pd.DataFrame({"mfe_r": mfe, "mae_r": mae}, index=trades.index)


def gini(x: np.ndarray) -> float:
    v = np.sort(np.asarray(x, dtype=float))
    if v.size == 0 or v.sum() <= 0:
        return math.nan
    n = v.size
    return float((2 * np.arange(1, n + 1) - n - 1) @ v / (n * v.sum()))


def winner_concentration(pnl: np.ndarray) -> dict:
    """Shares of total net P&L in the top 1/3/5 trades (None when net <= 0) and the Gini of winners."""
    v = np.sort(np.asarray(pnl, dtype=float))[::-1]
    total = float(v.sum())
    out: dict = {f"top{k}": (float(v[:k].sum()) / total if total > 0 else None) for k in (1, 3, 5)}
    out["gini"] = gini(v[v > 0])
    return out


def tail_risk(daily: pd.Series) -> dict:
    """Historical VaR / ES at 95% and 99%, worst day, worst 7-day compounded return."""
    r = daily.dropna().astype(float)
    if r.empty:
        return {k: math.nan for k in ("var95", "var99", "es95", "es99", "worst_day", "worst_week")}
    week = (1.0 + r).rolling(7, min_periods=1).apply(np.prod, raw=True) - 1.0
    return {
        "var95": float(np.quantile(r, 0.05)),
        "var99": float(np.quantile(r, 0.01)),
        "es95": cvar(r.to_numpy(), 0.05),
        "es99": cvar(r.to_numpy(), 0.01),
        "worst_day": float(r.min()),
        "worst_week": float(week.min()),
    }


def holding_hours(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"median": math.nan, "p10": math.nan, "p90": math.nan}
    h = (pd.DatetimeIndex(trades["exit_ts"]) - pd.DatetimeIndex(trades["entry_ts"])) / pd.Timedelta(hours=1)
    h = np.asarray(h, dtype=float)
    return {
        "median": float(np.median(h)),
        "p10": float(np.quantile(h, 0.1)),
        "p90": float(np.quantile(h, 0.9)),
    }


def annual_turnover(trades: pd.DataFrame, equity: pd.Series) -> float:
    """Traded notional per year / mean equity. Traded notional = ``traded_notional`` when the engine
    records it (cross-sectional: real fills only, carry-over re-referencing excluded), else
    entry + exit ~ 2 x entry gross."""
    if trades.empty or len(equity) < 2:
        return math.nan
    years = (equity.index[-1] - equity.index[0]) / pd.Timedelta(days=365)
    mean_eq = float(equity.mean())
    if not (years > 0 and mean_eq > 0):
        return math.nan
    if "traded_notional" in trades:
        traded = float(trades["traded_notional"].astype(float).sum())
    else:
        traded = 2.0 * float(trades["gross_notional"].astype(float).sum())
    return traded / years / mean_eq


Z95 = 1.6448536269514722
ES95_PER_SD = 2.0627128075074257  # phi(z95) / 0.05: normal 1-day ES95 in units of the daily sd


def exposure_daily(bar_stats: pd.DataFrame) -> list[dict]:
    """Exposure over time (§6): per UTC day mean gross and net leverage, max single-asset share and
    mean vol-targeting scale (None where not recorded)."""
    if bar_stats is None or bar_stats.empty:
        return []
    cols = {
        "gross": ("gross_leverage", "mean"),
        "net": ("net_leverage", "mean"),
        "max_asset_share": ("max_asset_share", "max"),
        "vol_scale": ("vol_scale", "mean"),
    }
    out = []
    for day, g in bar_stats.groupby(bar_stats.index.floor("D")):
        row: dict = {"date": day.strftime("%Y-%m-%d")}
        for k, (c, how) in cols.items():
            v = getattr(g[c], how)() if c in g else math.nan
            row[k] = float(v) if v is not None and math.isfinite(v) else None
        out.append(row)
    return out


def ex_ante_es_comparison(daily: pd.Series, ex_ante_var: pd.Series) -> dict:
    """Cross-sectional ex-ante 1-day ES95 (normal, from the projection's VaR95) vs the realized
    ES95 of the daily returns on days that had a forecast."""
    prev = ex_ante_var.reindex(daily.index).shift(1)
    ok = prev.notna() & (prev > 0) & daily.notna()
    if not ok.any():
        return {"forecast_es95_mean": math.nan, "realized_es95": math.nan, "n_days": 0}
    return {
        "forecast_es95_mean": float((prev[ok] / Z95 * ES95_PER_SD).mean()),
        "realized_es95": cvar(daily[ok].to_numpy(), 0.05),
        "n_days": int(ok.sum()),
    }


def ex_ante_var_exceedance(daily: pd.Series, ex_ante: pd.Series) -> dict:
    """Fraction of days whose loss exceeds the previous day's ex-ante 1-day 95% VaR (expected ~5%)."""
    prev = ex_ante.reindex(daily.index).shift(1)
    ok = prev.notna() & daily.notna() & (prev > 0)
    n = int(ok.sum())
    return {"rate": float((daily[ok] < -prev[ok]).mean()) if n else math.nan, "n_days": n}


def _exposure(res: BacktestResult) -> dict:
    bs = res.bar_stats
    if bs is None or bs.empty:
        return {}
    out = {"gross_mean": float(bs["gross_leverage"].mean()), "gross_max": float(bs["gross_leverage"].max())}
    if "net_leverage" in bs:
        net = bs["net_leverage"].abs()
        out.update(
            {
                "net_abs_mean": float(net.mean()),
                "net_abs_max": float(net.max()),
                "max_asset_share": float(bs["max_asset_share"].max()),
            }
        )
    if "vol_scale" in bs:
        out["vol_scale_mean"] = float(bs["vol_scale"].mean())
    return out


def extended_metrics(
    res: BacktestResult,
    panel: dict[str, pd.DataFrame],
    costs: CostModel,
    kind: AccountKind,
    limits: RiskLimits,
    order_model: OrderModel | None,
) -> dict:
    """Every §6 metric for one result (module docstring)."""
    model = order_model or default_order_model(kind)
    tr = res.trades
    n = len(tr)
    risk = tr["risk_amount"].astype(float) if n else pd.Series(dtype=float)
    net = tr["pnl_net"].astype(float) if n else pd.Series(dtype=float)
    gross = tr["pnl_gross"].astype(float) if n else pd.Series(dtype=float)
    dr = daily_returns(res.equity, res.initial_equity)
    split = split_spread_slippage(tr, costs, model) if n else pd.DataFrame({"spread": [], "slippage": []})
    fees = float(tr["fees"].sum()) if n else 0.0
    spread_slip = float(tr["spread_slip"].sum()) if n else 0.0
    gross_total = float(gross.sum())
    wins, losses = net[net > 0], net[net < 0]
    init = res.initial_equity if math.isfinite(res.initial_equity) and res.initial_equity > 0 else math.nan
    by_asset = net.groupby(tr["key"]).sum() if n else pd.Series(dtype=float)
    top = tr.assign(_n=net).nlargest(5, "_n") if n else tr
    ex = mfe_mae_r(tr, panel) if n else pd.DataFrame({"mfe_r": [], "mae_r": []})
    ex_ante = ex_ante_es = None
    funding_total = float(tr["funding"].sum()) if n else 0.0
    if res.bar_stats is not None and "ex_ante_var" in res.bar_stats:
        daily_var = res.bar_stats["ex_ante_var"].resample("1D").last()
        ex_ante = ex_ante_var_exceedance(dr, daily_var)
        ex_ante_es = ex_ante_es_comparison(dr, daily_var)
    return {
        "planned_r": float((net / risk).mean()) if n else math.nan,
        "planned_risk_usd": float(risk.mean()) if n else math.nan,
        "cap_adjusted_r": float(cap_adjusted_r(tr, res.equity, limits).mean()) if n else math.nan,
        "gross_r": float((gross / risk).mean()) if n else math.nan,
        "net_r": float((net / risk).mean()) if n else math.nan,
        "payoff_ratio": (
            float(wins.mean()) / abs(float(losses.mean())) if len(wins) and len(losses) else math.nan
        ),
        "costs": {
            "fees": fees,
            "spread": float(split["spread"].sum()),
            "slippage": float(split["slippage"].sum()),
            "funding_known": float(tr["funding"].sum()) if n else 0.0,
            "funding_unknown_hours": int(tr["funding_unknown_bars"].sum()) if n else 0,
        },
        # cost = fees + spread/slippage + funding paid (funding received lowers it)
        "cost_to_gross": (
            (fees + spread_slip - funding_total) / gross_total if gross_total > 0 else "n/a - no gross edge"
        ),
        "equity_contribution": {
            "by_asset": {str(a): {"usd": float(v), "pct": float(v / init)} for a, v in by_asset.items()},
            "top_trades": [
                {
                    "key": str(r.key),
                    "entry_ts": pd.Timestamp(r.entry_ts).isoformat(),
                    "pnl_net": float(r.pnl_net),
                }
                for r in top.itertuples(index=False)
            ],
        },
        "turnover_annual": annual_turnover(tr, res.equity),
        "holding_hours": holding_hours(tr),
        "maker_fill_rate": maker_fill_rate(res.candidates) if res.candidates is not None else math.nan,
        "mfe_mae": {
            "mfe_mean": float(ex["mfe_r"].mean()) if n else math.nan,
            "mfe_median": float(ex["mfe_r"].median()) if n else math.nan,
            "mae_mean": float(ex["mae_r"].mean()) if n else math.nan,
            "mae_median": float(ex["mae_r"].median()) if n else math.nan,
        },
        "winner_concentration": winner_concentration(net.to_numpy()),
        "tail": tail_risk(dr),
        "exposure": _exposure(res),
        "exposure_daily": exposure_daily(res.bar_stats),
        "ex_ante_var": ex_ante,
        "ex_ante_es": ex_ante_es,
        "r_definitions": R_DEFINITIONS,
    }
