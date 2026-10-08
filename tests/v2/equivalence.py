"""Trade list <-> portfolio engine equivalence for one V2 strategy instance (V1 test pattern,
tests/research/test_tradelists.py::test_tradelist_matches_backtest_single_strategy)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.engine.backtest import default_instruments, run_backtest
from luna.execution.costs import CostModel
from luna.ranking.ranker import AcceptAllRanker
from luna.research.tradelists import TRADE_COLUMNS, build_tradelist

START = pd.Timestamp("2000-01-01", tz="UTC")
END = pd.Timestamp("2100-01-01", tz="UTC")


def engine_settings(settings, kind):
    lim = settings.accounts[kind].model_copy(
        update={
            "capital_usd": 1e9,
            "risk_per_trade": 1e-5,
            "max_risk_per_trade": 1.0,
            "max_open_risk": 1e6,
            "max_positions": 10_000,
            "max_gross_leverage": 1e6,
            "max_leg_notional_frac": 1e6,
            "daily_loss_limit": 1e9,
            "max_drawdown": 1e9,
            "max_orders_per_day": 10**9,
            "emergency_stop_frac": 1e9,
            "liq_distance_mult": 0.0,
        }
    )
    return settings.model_copy(update={"accounts": {**settings.accounts, kind: lim}})


def funding_for(panel, key):
    """Hourly funding known for the second half of the key's history, with gaps."""
    idx = panel[key].index
    hours = pd.date_range(idx[len(idx) // 2].ceil("h"), idx[-1], freq="h")
    rng = np.random.default_rng(7)
    hours = hours[rng.random(len(hours)) > 0.05]
    return {key: pd.DataFrame({"rate": rng.normal(2e-5, 1e-4, len(hours))}, index=hours)}


def assert_equivalent(strat, panel, key, kind, settings, *, min_trades, **order_kw):
    """Engine trades == trade list trades (timing, direction, reason, per-unit net P&L)."""
    s = engine_settings(settings, kind)
    costs = CostModel(s.costs[kind])
    funding = funding_for(panel, key) if kind == "perp" else {}
    sub = {key: panel[key]}
    res = run_backtest(
        sub,
        funding,
        [strat],
        {strat.name: [key]},
        AcceptAllRanker(),
        kind,
        s,
        START,
        END,
        instruments=default_instruments([key], kind, s),
        **order_kw,
    )
    sf = strat.signals(sub, [key])[key]
    tl = build_tradelist(
        sf, sub, funding, costs, kind, strategy=strat, band=s.accounts[kind].max_price_band, **order_kw
    )
    eng = res.trades.sort_values("entry_ts").reset_index(drop=True)
    tl = tl.sort_values("entry_ts").reset_index(drop=True)
    assert len(eng) >= min_trades
    assert list(tl.columns) == TRADE_COLUMNS
    assert len(tl) == len(eng)
    assert (tl["entry_ts"].to_numpy() == eng["entry_ts"].to_numpy()).all()
    assert (tl["exit_ts"].to_numpy() == eng["exit_ts"].to_numpy()).all()
    assert (tl["direction"].to_numpy() == eng["direction"].astype(int).to_numpy()).all()
    assert list(tl["exit_reason"]) == list(eng["exit_reason"])
    model = order_kw.get("order_model") or ("taker" if kind == "perp" else "maker")
    rt = costs.round_trip_frac(key, model)
    g = eng["risk_amount"].astype(float) / (tl["stop_distance"] + rt)
    np.testing.assert_allclose(tl["ret_net"], eng["pnl_net"].astype(float) / g, rtol=0, atol=1e-9)
    np.testing.assert_allclose(tl["funding"], eng["funding"].astype(float) / g, rtol=0, atol=1e-12)
    return res, tl
