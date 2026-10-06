import math

import numpy as np
import pandas as pd
import pytest

from luna.engine.backtest import TRADE_COLUMNS, BacktestResult
from luna.report.gonogo import CHECKS, evaluate, funding_known_mask

D0 = pd.Timestamp("2024-07-01", tz="UTC")
CAPITAL = 265.0


def _res(r, *, pnl_scale=1.0, keys=None, start=D0, equity=None):
    """One trade per day (entry 01:00, exit 05:00), risk 2 USD, pnl = r * risk * pnl_scale; equity
    = capital + cumulative closed P&L at daily resolution unless given."""
    r = np.asarray(r, dtype=float)
    n = len(r)
    entry = pd.DatetimeIndex([start + pd.Timedelta(days=i, hours=1) for i in range(n)])
    pnl = r * 2.0 * pnl_scale
    tr = pd.DataFrame({
        "position_id": [f"p{i}" for i in range(n)], "strategy": "momentum",
        "key": keys if keys is not None else ["BTC"] * n, "direction": 1, "entry_ts": entry,
        "exit_ts": entry + pd.Timedelta(hours=4), "exit_reason": "signal", "gross_notional": 100.0,
        "risk_amount": 2.0, "pnl_gross": pnl + 0.1, "fees": 0.05, "spread_slip": 0.05,
        "funding": 0.0, "funding_unknown_bars": 0, "pnl_net": pnl, "r_multiple": r * pnl_scale,
        "regime": "TREND_UP",
    }, columns=TRADE_COLUMNS)
    if equity is None:
        idx = pd.date_range(start, periods=max(n, 1) * 96, freq="15min", tz="UTC")
        daily = pd.Series(np.cumsum(pnl), index=entry.floor("D"))
        equity = CAPITAL + daily.reindex(idx.floor("D")).ffill().fillna(0.0).to_numpy()
        equity = pd.Series(equity, index=idx)
    return BacktestResult(equity=equity, trades=tr, candidates=pd.DataFrame(),
                          risk_events=pd.DataFrame(), funding_known_from={}, initial_equity=CAPITAL)


def _good_r(n=60):
    return np.resize([0.8, -0.4, 0.8, 0.8, -0.4], n)  # E[r] = +0.32, PF = 3


def _windows(nets):
    return pd.DataFrame({"status": "ok", "net_pnl": nets, "n_trades": 10})


def _evaluate(**over):
    good = _res(_good_r())
    kw = dict(main_edge=good, main_realistic=good, cost_x2_edge=_res(_good_r(), pnl_scale=0.5),
              per_window_edge=_windows([1.0, 2.0, -1.0, 3.0, 1.0]), best_naive_expectancy_r=-0.05,
              funding_known_from=None)
    kw.update(over)
    return evaluate(**kw)


def test_gonogo_go_when_all_pass():
    g = _evaluate()
    assert g.verdict == "GO", g.checks
    assert list(g.checks) == list(CHECKS)
    assert all(ok for ok, _ in g.checks.values())
    assert "complexity_adds_value" in g.checks


def test_gonogo_insufficient():
    few = _res(_good_r(29))
    g = _evaluate(main_edge=few, main_realistic=few)
    assert g.verdict == "INSUFFICIENT DATA"
    ok, detail = g.checks["min_trades"]
    assert not ok and "29" in detail
    assert _evaluate(main_edge=_res(_good_r(30)), main_realistic=_res(_good_r(30))).verdict == "GO"


def test_gonogo_fails_on_cost_x2():
    g = _evaluate(cost_x2_edge=_res(-np.abs(_good_r())))
    assert g.verdict == "NO-GO"
    assert not g.checks["cost_x2"][0]
    assert [k for k, (ok, _) in g.checks.items() if not ok] == ["cost_x2"]
    # exactly zero net expectancy under 2x costs still passes (>= 0)
    flat = _res(np.tile([0.5, -0.5], 30))
    assert _evaluate(cost_x2_edge=flat).checks["cost_x2"][0]


def test_gonogo_fails_when_naive_better():
    edge_r = float(_res(_good_r()).trades["r_multiple"].mean())
    g = _evaluate(best_naive_expectancy_r=edge_r + 0.01)
    assert g.verdict == "NO-GO"
    assert not g.checks["complexity_adds_value"][0]
    assert "complexity does not add value" in g.headline
    assert not _evaluate(best_naive_expectancy_r=edge_r).checks["complexity_adds_value"][0]  # tie fails


def test_gonogo_no_naive_trades_compares_with_cash():
    g = _evaluate(best_naive_expectancy_r=math.nan)
    assert g.checks["complexity_adds_value"][0] and "cash" in g.checks["complexity_adds_value"][1]


def test_gonogo_bootstrap_and_profit_factor():
    weak = _res(np.tile([1.0, -0.9], 30))  # mean 0.05 > 0 but CI straddles 0, PF ~1.1
    g = _evaluate(main_edge=weak)
    assert g.verdict == "NO-GO"
    assert not g.checks["expectancy_ci_lo"][0] and not g.checks["profit_factor"][0]


def test_gonogo_tail_checks_use_realistic_variant():
    deep = _res(_good_r())
    deep.equity = deep.equity.copy()
    deep.equity.iloc[500:600] = CAPITAL * 0.5  # 50% drawdown, -50% day
    g = _evaluate(main_edge=deep)  # edge variant drawdown is NOT checked
    assert g.checks["max_drawdown"][0] and g.verdict == "GO"
    g = _evaluate(main_realistic=deep)
    assert g.verdict == "NO-GO"
    assert not g.checks["max_drawdown"][0] and not g.checks["worst_day"][0]
    assert not g.checks["daily_cvar5"][0]


def test_gonogo_expectancy_checks_use_edge_variant():
    bad = _res(-np.abs(_good_r()))
    g = _evaluate(main_realistic=bad)  # only tail checks read the realistic run
    assert all(g.checks[k][0] for k in ("min_trades", "expectancy_ci_lo", "profit_factor",
                                        "funding_known_net", "complexity_adds_value"))
    g = _evaluate(main_edge=bad)
    assert not g.checks["expectancy_ci_lo"][0] and not g.checks["complexity_adds_value"][0]


def test_gonogo_windows_positive_threshold():
    assert _evaluate(per_window_edge=_windows([1, 1, 1, -1, -1])).checks["windows_positive"][0]  # 60%
    g = _evaluate(per_window_edge=_windows([1, 1, 0, -1, -1]))  # 40%: zero is not > 0
    assert not g.checks["windows_positive"][0] and "2/5" in g.checks["windows_positive"][1]
    skipped = pd.concat([pd.DataFrame({"status": ["skipped: empty universe"], "net_pnl": [np.nan]}),
                         _windows([1, 1, 1, -1, -1])], ignore_index=True)
    assert _evaluate(per_window_edge=skipped).checks["windows_positive"][0]  # skipped not counted


def test_funding_known_mask_and_check():
    keys = ["BTC"] * 30 + ["ETH/BTC"] * 30
    r = np.concatenate([np.full(30, -0.5), np.full(30, 1.0)])
    res = _res(r, keys=keys)
    # BTC known from day 0, ETH from day 45: pairs before day 45 are "unknown"
    known_from = {"BTC": D0, "ETH": D0 + pd.Timedelta(days=45)}
    mask = funding_known_mask(res.trades, known_from)
    assert mask[:30].all() and not mask[30:45].any() and mask[45:].all()
    assert funding_known_mask(res.trades, None).all()  # spot: funding not applicable
    assert not funding_known_mask(res.trades, {"BTC": pd.NaT}).any()  # no rate ever / missing asset
    # known period: 30 x -0.5 + 15 x 1.0 R -> net = 0 (not > 0) although the whole run is net positive
    g = _evaluate(main_edge=res, funding_known_from=known_from)
    assert res.trades["pnl_net"].sum() > 0
    assert not g.checks["funding_known_net"][0]
    assert g.checks["funding_known_net"][0] is False
    assert _evaluate(funding_known_from={"BTC": D0}).checks["funding_known_net"][0]
    nothing_known = _evaluate(funding_known_from={"BTC": D0 + pd.Timedelta(days=400)})
    assert not nothing_known.checks["funding_known_net"][0]


@pytest.mark.parametrize("verdict_input", ["GO", "NO-GO", "INSUFFICIENT DATA"])
def test_verdict_values(verdict_input):
    cases = {"GO": {}, "NO-GO": {"best_naive_expectancy_r": 5.0},
             "INSUFFICIENT DATA": {"main_edge": _res(_good_r(5))}}
    assert _evaluate(**cases[verdict_input]).verdict == verdict_input


def test_insufficient_headline_lists_other_failures():
    few = _res(_good_r(5))
    g = _evaluate(main_edge=few, best_naive_expectancy_r=5.0)
    assert g.verdict == "INSUFFICIENT DATA"
    assert g.headline.startswith("INSUFFICIENT DATA: n=5")
    assert "(also failing: " in g.headline and "complexity_adds_value" in g.headline
    assert "min_trades" not in g.headline.split("also failing")[1]
    ok = _evaluate(main_edge=_res(_good_r(29)), main_realistic=_res(_good_r(29)))
    assert ok.verdict == "INSUFFICIENT DATA" and "also failing" not in ok.headline


def test_precomputed_metrics_are_used(monkeypatch):
    import luna.report.gonogo as gm
    from luna.report.metrics import metrics_with_ci

    good, x2 = _res(_good_r()), _res(_good_r(), pnl_scale=0.5)
    pre = {"edge": metrics_with_ci(good), "realistic": metrics_with_ci(good),
           "cost_x2_edge": metrics_with_ci(x2)}
    expected = _evaluate()
    monkeypatch.setattr(gm, "metrics_with_ci", lambda *a: (_ for _ in ()).throw(AssertionError("recomputed")))
    assert _evaluate(metrics=pre) == expected
    pre["edge"] = {**pre["edge"], "ci_lo": -1.0}  # the given numbers are what is judged
    assert not _evaluate(metrics=pre).checks["expectancy_ci_lo"][0]
