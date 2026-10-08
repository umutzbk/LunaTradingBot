import math

import numpy as np
import pandas as pd
import pytest

from luna.engine.backtest import BAR_STAT_COLUMNS, TRADE_COLUMNS, BacktestResult
from luna.execution.costs import CostModel
from luna.v2.metrics import (
    R_DEFINITIONS,
    annual_turnover,
    cap_adjusted_r,
    ex_ante_var_exceedance,
    extended_metrics,
    gini,
    holding_hours,
    mfe_mae_r,
    split_spread_slippage,
    tail_risk,
    winner_concentration,
)

T0 = pd.Timestamp("2024-01-01", tz="UTC")


def _trades(rows):
    base = {c: 0.0 for c in TRADE_COLUMNS}
    base.update(
        {
            "position_id": "p",
            "strategy": "s",
            "key": "BTC",
            "direction": 1,
            "regime": "NONE",
            "exit_reason": "signal",
            "funding_unknown_bars": 0,
        }
    )
    return pd.DataFrame(
        [{**base, **r} for r in rows],
        columns=list(dict.fromkeys(TRADE_COLUMNS + [k for r in rows for k in r])),
    )


def test_cap_adjusted_r_uses_precap_column(settings):
    t = _trades([{"pnl_net": 3.0, "risk_precap": 2.0, "entry_ts": T0}])
    assert cap_adjusted_r(t, pd.Series(dtype=float), settings.accounts["perp"]).tolist() == [1.5]


def test_cap_adjusted_r_from_equity_when_absent(settings):
    eq = pd.Series([200.0, 250.0], index=[T0 - pd.Timedelta("15min"), T0])
    t = _trades([{"pnl_net": 3.0, "entry_ts": T0 + pd.Timedelta("15min")}])
    lim = settings.accounts["perp"]
    assert cap_adjusted_r(t, eq, lim).iloc[0] == pytest.approx(3.0 / (lim.risk_per_trade * 250.0))


def test_split_spread_slippage_taker_and_maker(settings):
    costs = CostModel(settings.costs["perp"])  # BTC half spread 1 bps, slippage 2 bps
    t = _trades([{"spread_slip": 0.9}])
    taker = split_spread_slippage(t, costs, "taker")
    assert taker["spread"].iloc[0] == pytest.approx(0.3) and taker["slippage"].iloc[0] == pytest.approx(0.6)
    maker = split_spread_slippage(_trades([{"spread_slip": -0.2}]), costs, "maker")
    assert maker["spread"].iloc[0] == -0.2 and maker["slippage"].iloc[0] == 0.0


def test_mfe_mae_long_and_short():
    idx = pd.date_range(T0, periods=4, freq="15min")
    bars = pd.DataFrame(
        {
            "open": [100, 100, 104, 98],
            "high": [101, 105, 106, 99],
            "low": [99, 97, 101, 95],
            "close": [100, 104, 99, 97],
        },
        index=idx,
    )
    t = _trades(
        [
            {
                "direction": 1,
                "entry_ts": idx[1],
                "exit_ts": idx[3],
                "gross_notional": 100.0,
                "risk_amount": 2.0,
            },
            {
                "direction": -1,
                "entry_ts": idx[1],
                "exit_ts": idx[3],
                "gross_notional": 100.0,
                "risk_amount": 2.0,
            },
        ]
    )
    out = mfe_mae_r(t, {"BTC": bars})
    # entry open 100; bars idx1..idx2: high max 106, low min 97
    assert out["mfe_r"].tolist() == pytest.approx([3.0, 1.5])
    assert out["mae_r"].tolist() == pytest.approx([-1.5, -3.0])


def test_winner_concentration_and_gini():
    w = winner_concentration(np.array([10.0, 5.0, -3.0, 2.0, 1.0]))
    assert w["top1"] == pytest.approx(10 / 15) and w["top3"] == pytest.approx(17 / 15)
    assert winner_concentration(np.array([-1.0, 0.5]))["top1"] is None
    assert gini(np.array([1.0, 1.0, 1.0])) == pytest.approx(0.0)
    assert gini(np.array([0.0, 0.0, 3.0])) == pytest.approx(2 / 3)


def test_tail_risk_matches_hand_values():
    r = pd.Series(
        np.r_[np.full(95, 0.001), [-0.01, -0.02, -0.03, -0.04, -0.05]],
        index=pd.date_range(T0, periods=100, freq="D"),
    )
    t = tail_risk(r)
    assert t["worst_day"] == -0.05
    assert t["es95"] == pytest.approx(-0.03)  # mean of the worst 5
    assert t["es99"] == pytest.approx(-0.05)
    assert t["var95"] == pytest.approx(np.quantile(r, 0.05))
    assert t["var99"] == pytest.approx(np.quantile(r, 0.01))
    assert t["worst_week"] == pytest.approx(np.prod(1 + r.iloc[-7:]) - 1)


def test_holding_and_turnover():
    t = _trades(
        [
            {"entry_ts": T0, "exit_ts": T0 + pd.Timedelta(hours=h), "gross_notional": 100.0}
            for h in (1, 2, 3, 4, 10)
        ]
    )
    h = holding_hours(t)
    assert h["median"] == 3 and h["p10"] == pytest.approx(1.4) and h["p90"] == pytest.approx(7.6)
    eq = pd.Series(200.0, index=pd.date_range(T0, periods=366, freq="D"))
    assert annual_turnover(t, eq) == pytest.approx(2 * 500 / 1.0 / 200)


def test_ex_ante_var_exceedance():
    idx = pd.date_range(T0, periods=4, freq="D")
    daily = pd.Series([0.0, -0.02, -0.001, -0.03], index=idx)
    ex_ante = pd.Series([0.01, 0.01, 0.01, 0.01], index=idx)
    out = ex_ante_var_exceedance(daily, ex_ante)
    assert out == {"rate": pytest.approx(2 / 3), "n_days": 3}  # day 0 has no prior forecast


def test_cost_to_gross_na_without_edge(settings):
    res = _result(
        _trades(
            [
                {
                    "pnl_gross": -1.0,
                    "fees": 0.5,
                    "pnl_net": -1.5,
                    "risk_amount": 1.0,
                    "gross_notional": 10.0,
                    "entry_ts": T0,
                    "exit_ts": T0 + pd.Timedelta(hours=1),
                }
            ]
        )
    )
    m = extended_metrics(res, {}, CostModel(settings.costs["perp"]), "perp", settings.accounts["perp"], None)
    assert m["cost_to_gross"] == "n/a - no gross edge"


def test_bar_stats_have_net_leverage_and_max_share():
    assert BAR_STAT_COLUMNS[-2:] == ["net_leverage", "max_asset_share"]


def _result(trades):
    idx = pd.date_range(T0, periods=96 * 3, freq="15min")
    bs = pd.DataFrame(0.0, index=idx, columns=BAR_STAT_COLUMNS)
    bs["equity"] = 265.0
    return BacktestResult(
        pd.Series(265.0, index=idx),
        trades,
        pd.DataFrame({"status": []}),
        pd.DataFrame(),
        {},
        bs,
        None,
        None,
        None,
        None,
        265.0,
    )


def test_extended_metrics_keys_complete(settings):
    t = _trades(
        [
            {
                "pnl_gross": 2.0,
                "fees": 0.1,
                "spread_slip": 0.1,
                "pnl_net": 1.8,
                "risk_amount": 1.0,
                "gross_notional": 10.0,
                "entry_ts": T0,
                "exit_ts": T0 + pd.Timedelta(hours=2),
            }
        ]
    )
    idx = pd.date_range(T0, periods=12, freq="15min")
    bars = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=idx)
    m = extended_metrics(
        _result(t), {"BTC": bars}, CostModel(settings.costs["perp"]), "perp", settings.accounts["perp"], None
    )
    for k in (
        "planned_r",
        "planned_risk_usd",
        "cap_adjusted_r",
        "gross_r",
        "net_r",
        "equity_contribution",
        "costs",
        "cost_to_gross",
        "turnover_annual",
        "holding_hours",
        "maker_fill_rate",
        "mfe_mae",
        "payoff_ratio",
        "winner_concentration",
        "tail",
        "exposure",
        "ex_ante_var",
        "r_definitions",
    ):
        assert k in m, k
    assert set(m["costs"]) == {"fees", "spread", "slippage", "funding_known", "funding_unknown_hours"}
    assert m["net_r"] == pytest.approx(1.8) and m["gross_r"] == pytest.approx(2.0)
    assert m["cost_to_gross"] == pytest.approx(0.1)
    assert m["r_definitions"] == R_DEFINITIONS and math.isnan(m["maker_fill_rate"])


def test_turnover_uses_traded_notional_when_recorded():
    t = _trades(
        [
            {
                "entry_ts": T0,
                "exit_ts": T0 + pd.Timedelta(days=7),
                "gross_notional": 100.0,
                "traded_notional": 10.0,
            }
        ]
    )
    eq = pd.Series(200.0, index=pd.date_range(T0, periods=366, freq="D"))
    assert annual_turnover(t, eq) == pytest.approx(10.0 / 1.0 / 200)


def test_cost_to_gross_counts_funding_paid(settings):
    res = _result(
        _trades(
            [
                {
                    "pnl_gross": 2.0,
                    "fees": 0.1,
                    "spread_slip": 0.1,
                    "funding": -0.2,
                    "pnl_net": 1.6,
                    "risk_amount": 1.0,
                    "gross_notional": 10.0,
                    "entry_ts": T0,
                    "exit_ts": T0 + pd.Timedelta(hours=2),
                }
            ]
        )
    )
    m = extended_metrics(res, {}, CostModel(settings.costs["perp"]), "perp", settings.accounts["perp"], None)
    assert m["cost_to_gross"] == pytest.approx(0.2)  # (0.1 + 0.1 + 0.2 funding paid) / 2.0


def test_exposure_over_time_and_ex_ante_es(settings):
    from luna.v2.metrics import exposure_daily

    idx = pd.date_range(T0, periods=96 * 3, freq="15min")
    bs = pd.DataFrame(
        {
            "gross_leverage": 1.0,
            "net_leverage": 0.05,
            "max_asset_share": 0.3,
            "vol_scale": 0.8,
            "ex_ante_var": 0.01,
        },
        index=idx,
    )
    daily = exposure_daily(bs)
    assert len(daily) == 3 and set(daily[0]) == {"date", "gross", "net", "max_asset_share", "vol_scale"}
    res = _result(_trades([]))
    res.bar_stats = bs.assign(equity=265.0, n_positions=1, open_risk=0.0, entry_gross_leverage=np.nan)
    m = extended_metrics(res, {}, CostModel(settings.costs["perp"]), "perp", settings.accounts["perp"], None)
    assert len(m["exposure_daily"]) == 3
    es = m["ex_ante_es"]
    assert es["forecast_es95_mean"] == pytest.approx(0.01 / 1.6448536269514722 * 2.0627128075074257)
    assert "realized_es95" in es
