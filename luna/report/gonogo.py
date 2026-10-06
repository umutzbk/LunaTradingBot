"""Automatic GO / NO-GO verdict (spec §8), per account.

Two simulation variants feed the checks (controller ruling):

- **realistic**: the config as-is (drawdown kill switch on). It answers "what would the account have
  lived through", so the drawdown / tail checks (max DD, worst day, daily CVaR 5%) read it.
- **edge**: the same models with the kill switch disabled (``max_drawdown_override=1.0``). A kill
  switch that fires early would hide whether the strategy has an edge at all (few trades, frozen
  account), so every expectancy-based check (trade count, bootstrap CI, profit factor, 2x costs,
  funding-known period, windows, naive comparison) reads it.

Sharpe / Sortino are never used here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from luna.engine.backtest import BacktestResult
from luna.report.metrics import metrics_with_ci

Verdict = Literal["GO", "NO-GO", "INSUFFICIENT DATA"]

MIN_TRADES = 30
MIN_PROFIT_FACTOR = 1.2
MAX_DRAWDOWN = 0.15
MIN_WORST_DAY = -0.03
MIN_DAILY_CVAR5 = -0.02
MIN_WINDOW_FRACTION = 0.60

# check name -> (variant it reads, spec §8 condition)
CHECKS: dict[str, tuple[str, str]] = {
    "min_trades": ("edge", f"OOS trades >= {MIN_TRADES} (else INSUFFICIENT DATA)"),
    "expectancy_ci_lo": ("edge", "bootstrap 90% CI lower bound of expectancy (R) > 0"),
    "profit_factor": ("edge", f"profit factor >= {MIN_PROFIT_FACTOR}"),
    "max_drawdown": ("realistic", f"max drawdown < {MAX_DRAWDOWN:.0%}"),
    "worst_day": ("realistic", f"worst day > {MIN_WORST_DAY:.0%}"),
    "daily_cvar5": ("realistic", f"daily CVaR 5% > {MIN_DAILY_CVAR5:.0%}"),
    "cost_x2": ("edge, costs x2", "net expectancy (R) under 2x costs >= 0"),
    "funding_known_net": ("edge", "net P&L > 0 on trades of the funding-known period"),
    "windows_positive": ("edge", f">= {MIN_WINDOW_FRACTION:.0%} of walk-forward windows net > 0"),
    "complexity_adds_value": ("edge", "OOS expectancy (R) beats the best naive baseline"),
}


@dataclass
class GoNoGo:
    verdict: Verdict
    checks: dict[str, tuple[bool, str]]  # name -> (passed, detail)

    @property
    def failed(self) -> list[str]:
        return [k for k, (ok, _) in self.checks.items() if not ok]

    @property
    def headline(self) -> str:
        if self.verdict == "GO":
            return "GO: every §8 condition holds"
        if self.verdict == "INSUFFICIENT DATA":
            text = f"INSUFFICIENT DATA: {self.checks['min_trades'][1]}"
            others = [k for k in self.failed if k != "min_trades"]
            return text + (f" (also failing: {', '.join(others)})" if others else "")
        text = "NO-GO: failed " + ", ".join(self.failed)
        if "complexity_adds_value" in self.failed:
            text += " (complexity does not add value)"
        return text


def _fin(x: float) -> bool:
    return x is not None and math.isfinite(x)


def funding_known_mask(trades: pd.DataFrame, funding_known_from: dict | None) -> np.ndarray:
    """True for trades entered at or after the first known funding timestamp of every asset they
    hold (pairs "A/B": both legs). ``funding_known_from=None`` = no funding on this account (spot):
    every trade counts as known. An asset missing from the dict or with NaT is never known."""
    n = len(trades)
    if funding_known_from is None:
        return np.ones(n, dtype=bool)
    out = np.zeros(n, dtype=bool)
    for i, (key, entry) in enumerate(zip(trades["key"], trades["entry_ts"], strict=True)):
        starts = [funding_known_from.get(a, pd.NaT) for a in str(key).split("/")]
        if any(pd.isna(s) for s in starts):
            continue
        out[i] = pd.Timestamp(entry) >= max(pd.Timestamp(s) for s in starts)
    return out


def _windows(per_window: pd.DataFrame) -> tuple[int, int]:
    """(windows net > 0, windows simulated); skipped windows (no net_pnl) are not counted."""
    if per_window is None or per_window.empty or "net_pnl" not in per_window:
        return 0, 0
    net = pd.to_numeric(per_window["net_pnl"], errors="coerce").dropna()
    return int((net > 0).sum()), len(net)


def evaluate(
    main_edge: BacktestResult,
    main_realistic: BacktestResult,
    cost_x2_edge: BacktestResult,
    per_window_edge: pd.DataFrame,
    best_naive_expectancy_r: float,
    funding_known_from: dict | None,
    *,
    metrics: dict[str, dict] | None = None,
) -> GoNoGo:
    """Every §8 condition; verdict "INSUFFICIENT DATA" if the edge OOS has < 30 trades, "GO" if all
    checks pass, else "NO-GO". Checks are always all computed (shown in the report).

    ``metrics``: already computed ``metrics_with_ci`` of the three results, keys "edge",
    "realistic", "cost_x2_edge" (computed here when absent)."""
    pre = metrics or {}
    e = pre.get("edge") or metrics_with_ci(main_edge)
    r = pre.get("realistic") or metrics_with_ci(main_realistic)
    c2 = pre.get("cost_x2_edge") or metrics_with_ci(cost_x2_edge)
    lo, hi = e["ci_lo"], e["ci_hi"]
    n = int(e["n_trades"])
    checks: dict[str, tuple[bool, str]] = {}

    checks["min_trades"] = (n >= MIN_TRADES, f"n={n} (need >= {MIN_TRADES})")
    checks["expectancy_ci_lo"] = (_fin(lo) and lo > 0, f"expectancy {e['expectancy_r']:.3f} R, "
                                  f"90% CI [{lo:.3f}, {hi:.3f}]")
    pf = e["profit_factor"]
    checks["profit_factor"] = (not math.isnan(pf) and pf >= MIN_PROFIT_FACTOR,
                               f"PF={pf:.2f} (need >= {MIN_PROFIT_FACTOR})")
    dd, wd, cv = r["max_dd"], r["worst_day"], r["cvar_5_daily"]
    checks["max_drawdown"] = (_fin(dd) and dd < MAX_DRAWDOWN, f"max DD {dd:.2%} (need < {MAX_DRAWDOWN:.0%})")
    checks["worst_day"] = (_fin(wd) and wd > MIN_WORST_DAY,
                           f"worst day {wd:.2%} (need > {MIN_WORST_DAY:.0%})")
    checks["daily_cvar5"] = (_fin(cv) and cv > MIN_DAILY_CVAR5,
                             f"daily CVaR5 {cv:.2%} (need > {MIN_DAILY_CVAR5:.0%})")
    x2 = c2["expectancy_r"]
    checks["cost_x2"] = (_fin(x2) and x2 >= 0, f"expectancy under 2x costs {x2:.3f} R "
                         f"(n={int(c2['n_trades'])}, net {c2['pnl_net']:.2f} USD; need >= 0)")

    tr = main_edge.trades
    known = funding_known_mask(tr, funding_known_from)
    net_known = float(tr.loc[known, "pnl_net"].sum()) if len(tr) else 0.0
    scope = "no funding (spot): all trades" if funding_known_from is None else "funding-known period"
    checks["funding_known_net"] = (int(known.sum()) > 0 and net_known > 0,
                                   f"{scope}: {int(known.sum())} trades, net {net_known:.2f} USD (need > 0)")

    pos, tot = _windows(per_window_edge)
    frac = pos / tot if tot else math.nan
    checks["windows_positive"] = (
        tot > 0 and frac >= MIN_WINDOW_FRACTION - 1e-12,
        f"{pos}/{tot} windows net > 0 ({frac:.0%}; need >= {MIN_WINDOW_FRACTION:.0%})",
    )

    exp_r = e["expectancy_r"]
    if _fin(best_naive_expectancy_r):
        bar, what = best_naive_expectancy_r, f"best naive {best_naive_expectancy_r:.3f} R"
    else:
        bar, what = 0.0, "no naive trades: compared with cash (0 R)"
    checks["complexity_adds_value"] = (_fin(exp_r) and exp_r > bar,
                                       f"Luna {exp_r:.3f} R vs {what} (need strictly greater)")

    if list(checks) != list(CHECKS):
        raise RuntimeError(f"GO/NO-GO checks {list(checks)} out of sync with CHECKS {list(CHECKS)}")
    if n < MIN_TRADES:
        verdict: Verdict = "INSUFFICIENT DATA"
    elif all(ok for ok, _ in checks.values()):
        verdict = "GO"
    else:
        verdict = "NO-GO"
    return GoNoGo(verdict, {k: (bool(ok), d) for k, (ok, d) in checks.items()})
