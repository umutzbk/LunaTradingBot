"""Robustness diagnostics (spec §5, D3). Diagnostics only: nothing here feeds the ranker or the
GO/NO-GO verdict, and no output is a veto.

All metrics read the closed trades of an OOS result (R = ``r_multiple``, money = ``pnl_net``).
"fragile" (informational): the full mean R is positive but the ex-top-1 mean R, or any time- or
asset-leave-one-out mean R, is negative.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd

from luna.research.stats import block_bootstrap_ci, profit_factor

BOOT_SAMPLES = 2000
CI = 0.90
TRIM = 0.05


def trade_metrics(trades: pd.DataFrame) -> dict:
    """n, mean R, day-block bootstrap 90% CI (blocks = exit day), profit factor, net USD."""
    n = len(trades)
    if n == 0:
        return {
            "n": 0,
            "mean_r": math.nan,
            "ci_lo": math.nan,
            "ci_hi": math.nan,
            "pf": math.nan,
            "net_usd": 0.0,
        }
    r = trades["r_multiple"].to_numpy(dtype=float)
    days = pd.DatetimeIndex(trades["exit_ts"]).floor("D").asi8
    mean, lo, hi = block_bootstrap_ci(r, days, BOOT_SAMPLES, CI)
    net = trades["pnl_net"].to_numpy(dtype=float)
    return {
        "n": n,
        "mean_r": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "pf": profit_factor(net),
        "net_usd": float(net.sum()),
    }


def ex_top(trades: pd.DataFrame, *, k: int | None = None, frac: float | None = None) -> pd.DataFrame:
    """``trades`` without its k largest winners by pnl_net (or the largest ceil(frac x n))."""
    if (k is None) == (frac is None):
        raise ValueError("give exactly one of k or frac")
    m = k if k is not None else math.ceil(frac * len(trades) - 1e-9)
    if m <= 0 or trades.empty:
        return trades
    drop = trades["pnl_net"].astype(float).nlargest(m).index
    return trades.drop(index=drop)


def trimmed_mean_r(r: np.ndarray, cut: float = TRIM) -> float:
    v = np.sort(np.asarray(r, dtype=float))
    k = int(math.floor(cut * v.size))
    v = v[k : v.size - k] if k else v
    return float(v.mean()) if v.size else math.nan


def pnl_concentration(trades: pd.DataFrame) -> dict:
    """Shares of the total net P&L held by the top 1% / 5% / 10% of trades (None if net <= 0) and
    the Herfindahl index of |net P&L| by asset."""
    net = trades["pnl_net"].astype(float) if len(trades) else pd.Series(dtype=float)
    total = float(net.sum())
    v = np.sort(net.to_numpy())[::-1]
    out: dict = {}
    for p in (1, 5, 10):
        m = max(1, math.ceil(p / 100 * v.size - 1e-9)) if v.size else 0
        out[f"top{p}pct"] = float(v[:m].sum()) / total if total > 0 and m else None
    by_asset = net.abs().groupby(trades["key"]).sum() if len(trades) else pd.Series(dtype=float)
    s = float(by_asset.sum())
    out["asset_hhi"] = float(((by_asset / s) ** 2).sum()) if s > 0 else math.nan
    return out


def time_loo(trades: pd.DataFrame, per_window: pd.DataFrame) -> pd.DataFrame:
    """Metrics without each test window in turn (trades assigned by exit_ts)."""
    rows = []
    if per_window is None or per_window.empty or "test_start" not in per_window:
        return pd.DataFrame(columns=["test_start", "n", "mean_r", "net_usd"])
    for w in per_window.itertuples(index=False):
        if str(getattr(w, "status", "ok")).startswith("skipped"):
            continue
        keep = trades[(trades["exit_ts"] < w.test_start) | (trades["exit_ts"] >= w.test_end)]
        m = trade_metrics(keep)
        rows.append({"test_start": w.test_start, "n": m["n"], "mean_r": m["mean_r"], "net_usd": m["net_usd"]})
    return pd.DataFrame(rows, columns=["test_start", "n", "mean_r", "net_usd"])


def best_period_removal(trades: pd.DataFrame) -> dict:
    """Metrics without the best calendar month and without the best quarter (net P&L by exit_ts)."""
    out = {}
    for name, freq in (("ex_best_month", "M"), ("ex_best_quarter", "Q")):
        if trades.empty:
            out[name] = trade_metrics(trades)
            continue
        per = pd.DatetimeIndex(trades["exit_ts"]).tz_localize(None).to_period(freq)
        best = trades["pnl_net"].astype(float).groupby(per).sum().idxmax()
        out[name] = {**trade_metrics(trades[np.asarray(per != best)]), "removed": str(best)}
    return out


def asset_loo(run_without: Callable[[str], pd.DataFrame], assets: list[str]) -> pd.DataFrame:
    """Metrics of the walk-forward re-run without each asset in turn (``run_without(asset)`` returns
    that run's closed trades)."""
    rows = []
    for a in assets:
        m = trade_metrics(run_without(a))
        rows.append({"asset": a, "n": m["n"], "mean_r": m["mean_r"], "net_usd": m["net_usd"]})
    return pd.DataFrame(rows, columns=["asset", "n", "mean_r", "net_usd"])


def _negative(x: float) -> bool:
    return math.isfinite(x) and x < 0


def robustness(
    trades: pd.DataFrame,
    per_window: pd.DataFrame,
    *,
    run_without: Callable[[str], pd.DataFrame] | None,
    assets: list[str],
) -> dict:
    """Every §5 output for one reported configuration (module docstring)."""
    full = trade_metrics(trades)
    top1 = trade_metrics(ex_top(trades, k=1))
    tl = time_loo(trades, per_window)
    al = (
        asset_loo(run_without, assets)
        if run_without is not None
        else pd.DataFrame(columns=["asset", "n", "mean_r", "net_usd"])
    )
    r = trades["r_multiple"].to_numpy(dtype=float) if len(trades) else np.array([])
    loo_negative = any(_negative(float(x)) for x in pd.concat([tl["mean_r"], al["mean_r"]]))
    fragile = bool(
        math.isfinite(full["mean_r"]) and full["mean_r"] > 0 and (_negative(top1["mean_r"]) or loo_negative)
    )
    return {
        "full": full,
        "ex_top1": top1,
        "ex_top3": trade_metrics(ex_top(trades, k=3)),
        "ex_top1pct": trade_metrics(ex_top(trades, frac=0.01)),
        "trimmed_mean_r": trimmed_mean_r(r),
        "median_r": float(np.median(r)) if r.size else math.nan,
        "concentration": pnl_concentration(trades),
        "time_loo": tl.to_dict("records"),
        "asset_loo": al.to_dict("records"),
        **best_period_removal(trades),
        "fragile": fragile,
    }
