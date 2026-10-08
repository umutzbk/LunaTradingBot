import inspect

import numpy as np
import pandas as pd
import pytest

from luna.engine.backtest import BacktestResult
from luna.v2 import robustness as rb

T0 = pd.Timestamp("2024-01-01", tz="UTC")


def _t(pnls, keys=None, days=None):
    n = len(pnls)
    keys = keys or ["BTC"] * n
    days = days if days is not None else list(range(n))
    exit_ts = [T0 + pd.Timedelta(days=d) for d in days]
    return pd.DataFrame({"key": keys, "pnl_net": np.asarray(pnls, float), "risk_amount": 1.0,
                         "r_multiple": np.asarray(pnls, float), "entry_ts": exit_ts, "exit_ts": exit_ts})


def test_ex_top_removes_largest_winners():
    t = _t([5, -1, 10, 2, 1] * 20)
    assert rb.ex_top(t, k=1)["pnl_net"].max() == 10  # one of the twenty 10s removed
    assert len(rb.ex_top(t, k=3)) == 97
    assert len(rb.ex_top(t, frac=0.01)) == 99 and len(rb.ex_top(t, frac=0.05)) == 95
    with pytest.raises(ValueError):
        rb.ex_top(t)


def test_trade_metrics():
    m = rb.trade_metrics(_t([1.0, -1.0, 3.0]))
    assert m["n"] == 3 and m["mean_r"] == pytest.approx(1.0) and m["net_usd"] == pytest.approx(3.0)
    assert m["pf"] == pytest.approx(4.0) and m["ci_lo"] <= 1.0 <= m["ci_hi"]
    assert rb.trade_metrics(_t([]))["n"] == 0


def test_trimmed_and_median():
    r = np.r_[np.arange(1, 21, dtype=float)]  # 1..20, cut 5% = 1 each side
    assert rb.trimmed_mean_r(r) == pytest.approx(np.mean(np.arange(2, 20)))
    assert rb.robustness(_t(list(r)), pd.DataFrame(), run_without=None, assets=[])["median_r"] == 10.5


def test_pnl_concentration_hhi():
    t = _t([10, 5, 5, -5, 5] + [0] * 95, keys=["A", "B", "B", "B", "A"] + ["A"] * 95)
    c = rb.pnl_concentration(t)
    assert c["top1pct"] == pytest.approx(10 / 20)
    assert c["top5pct"] == pytest.approx(25 / 20)
    # |pnl| by asset: A 15, B 15 -> HHI 0.5
    assert c["asset_hhi"] == pytest.approx(0.5)


def test_time_loo_drops_one_window_each():
    t = _t([1, 2, 3, 4], days=[0, 1, 40, 41])
    pw = pd.DataFrame({"test_start": [T0, T0 + pd.Timedelta(days=30)],
                       "test_end": [T0 + pd.Timedelta(days=30), T0 + pd.Timedelta(days=60)],
                       "status": ["ok", "ok"]})
    out = rb.time_loo(t, pw)
    assert out["n"].tolist() == [2, 2] and out["net_usd"].tolist() == [7.0, 3.0]


def test_best_month_and_quarter_removal():
    t = _t([1, 9, -2, 3], days=[0, 40, 100, 200])  # Jan, Feb, Apr, Jul 2024
    out = rb.best_period_removal(t)
    assert out["ex_best_month"]["net_usd"] == pytest.approx(2.0)   # Feb removed
    assert out["ex_best_quarter"]["net_usd"] == pytest.approx(1.0)  # Q1 (1 + 9) removed


def _res(t):
    return BacktestResult(pd.Series(dtype=float), t, pd.DataFrame(), pd.DataFrame(), {})


def test_asset_loo_calls_runner_per_asset():
    calls = []

    def run_without(a):
        calls.append(a)
        return _res(_t([1.0, -3.0] if a == "BTC" else [2.0]))

    out = rb.asset_loo(run_without, ["BTC", "ETH"])
    assert calls == ["BTC", "ETH"]
    assert out["asset"].tolist() == ["BTC", "ETH"] and out["mean_r"].tolist() == [-1.0, 2.0]


def test_fragile_label_rules():
    good = rb.robustness(_t([1.0] * 40), pd.DataFrame(), run_without=None, assets=[])
    assert good["fragile"] is False
    one = rb.robustness(_t([100.0] + [-1.0] * 39), pd.DataFrame(), run_without=None, assets=[])
    assert one["full"]["mean_r"] > 0 and one["ex_top1"]["mean_r"] < 0 and one["fragile"] is True
    neg_loo = rb.robustness(_t([1.0] * 40), pd.DataFrame(),
                            run_without=lambda a: _res(_t([-1.0])), assets=["BTC"])
    assert neg_loo["fragile"] is True
    neg_full = rb.robustness(_t([-1.0] * 40), pd.DataFrame(), run_without=None, assets=[])
    assert neg_full["fragile"] is False  # the label only flags positive results


def test_robustness_has_no_verdict_inputs():
    out = rb.robustness(_t([1.0] * 5), pd.DataFrame(), run_without=None, assets=[])
    assert not {"verdict", "passed", "go"} & set(out)
    assert "gonogo" not in inspect.getsource(rb)
    assert set(out) == {"full", "ex_top1", "ex_top3", "ex_top1pct", "trimmed_mean_r", "median_r",
                        "concentration", "time_loo", "asset_loo", "ex_best_month", "ex_best_quarter",
                        "fragile"}
