import inspect
import json

import numpy as np
import pandas as pd
import pytest

import luna.engine.walkforward as wf
from luna.config import WalkForwardConfig, load_settings
from luna.engine.walkforward import (
    HoldoutAlreadyRun,
    HoldoutRegistryCorrupt,
    Window,
    WindowStrategy,
    fit_window,
    make_windows,
    run_holdout,
    run_walkforward,
)
from luna.portfolio.portfolio import Position
from luna.ranking.expectancy import ANY, ExpectancyRanker
from luna.ranking.ranker import RandomRanker
from luna.research.tradelists import TRADE_COLUMNS
from luna.strategy.momentum import Momentum
from luna.types import ExitSpec, Leg, StopSpec
from tests.helpers import synthetic_bars

BAR = pd.Timedelta("15min")
T = pd.Timestamp
GOOD = "F96_S480_N96_k2"  # momentum params ids (GRID points)
BAD = "F48_S192_N96_k2"
OTHER = "F48_S192_N288_k3"


def _settings(min_history_days=1, **wf_cfg):
    s = load_settings()
    return s.model_copy(update={
        "universe": s.universe.model_copy(update={"min_adv_usd": 0.0, "min_history_days": min_history_days}),
        "ranking": s.ranking.model_copy(update={"bootstrap_samples": 200}),
        "walkforward": s.walkforward.model_copy(update=wf_cfg) if wf_cfg else s.walkforward,
    })


def _panel(n=182 * 96, assets=("BTC", "ETH", "SOL")):
    return {a: synthetic_bars(n, "trend", seed=i, sigma=0.003, drift=0.00003)
            for i, a in enumerate(assets)}


def _trades(strategy, pid, key, entries, r_net, *, direction=1, unk=0, stop=0.02, hold=BAR * 8):
    entries = pd.DatetimeIndex(entries)
    r = np.broadcast_to(np.asarray(r_net, dtype=float), (len(entries),))
    exit_ts = entries + hold
    return pd.DataFrame({
        "strategy": strategy, "params_id": pid, "key": key, "regime": "TREND_UP",
        "direction": direction, "signal_ts": entries - BAR, "entry_ts": entries, "exit_ts": exit_ts,
        "exit_reason": "signal", "ret_gross": r * stop, "cost": 0.0, "funding": 0.0,
        "funding_unknown_bars": unk, "ret_net": r * stop, "stop_distance": stop, "r_net": r,
        "day": exit_ts.date,
    }, columns=TRADE_COLUMNS)


def _daily(start, end, offset_h=1):
    return pd.date_range(T(start, tz="UTC") + pd.Timedelta(hours=offset_h), T(end, tz="UTC"),
                         freq="1D", inclusive="left")


# --- windows --------------------------------------------------------------------------------------


def test_windows_layout():
    cfg = WalkForwardConfig(train_months=6, test_months=2, step_months=2, holdout_months=3)
    windows, (h0, h1) = make_windows(T("2023-01-01", tz="UTC"), T("2026-10-06", tz="UTC"), cfg)
    assert (h0, h1) == (T("2026-07-06", tz="UTC"), T("2026-10-06", tz="UTC"))
    assert windows[0] == Window(T("2023-01-01", tz="UTC"), T("2023-07-01", tz="UTC"),
                                T("2023-07-01", tz="UTC"), T("2023-09-01", tz="UTC"))
    assert len(windows) == 18
    assert windows[-1].test_end == T("2026-07-01", tz="UTC")
    for w in windows:
        assert w.train_end - pd.DateOffset(months=6) == w.train_start
        assert w.test_start == w.train_end
        assert w.test_end == w.test_start + pd.DateOffset(months=2)
        assert w.test_end <= h0  # no test window overlaps the holdout
    for a, b in zip(windows, windows[1:], strict=False):
        assert b.test_start == a.test_end  # contiguous OOS
        assert b.train_start == a.train_start + pd.DateOffset(months=2)


def test_windows_from_month_end_have_no_gaps_or_overlaps():
    cfg = WalkForwardConfig(train_months=6, test_months=2, step_months=2, holdout_months=3)
    start = T("2023-08-31", tz="UTC")
    windows, (h0, _) = make_windows(start, T("2026-10-06", tz="UTC"), cfg)
    assert windows[0] == Window(start, T("2024-02-29", tz="UTC"), T("2024-02-29", tz="UTC"),
                                T("2024-04-30", tz="UTC"))
    for k, w in enumerate(windows):
        assert w.train_start == start + pd.DateOffset(months=2 * k)
        assert w.test_end == start + pd.DateOffset(months=2 * k + 8)
        assert w.test_end <= h0
    for a, b in zip(windows, windows[1:], strict=False):
        assert b.test_start == a.test_end
    assert windows[2].train_start == T("2023-12-31", tz="UTC")  # not drifted to the 29th/30th


def test_windows_reject_step_not_equal_test():
    cfg = WalkForwardConfig(train_months=6, test_months=2, step_months=1, holdout_months=3)
    with pytest.raises(ValueError, match="step_months"):
        make_windows(T("2023-01-01", tz="UTC"), T("2026-10-06", tz="UTC"), cfg)


# --- fit_window -----------------------------------------------------------------------------------

W1 = Window(T("2024-01-01", tz="UTC"), T("2024-02-01", tz="UTC"), T("2024-02-01", tz="UTC"),
            T("2024-03-01", tz="UTC"))


def _fit_panel():
    return _panel(n=70 * 96, assets=("BTC", "ETH"))


def test_fit_window_never_sees_future(monkeypatch):
    seen = []
    real = wf.fit_expectancy
    monkeypatch.setattr(wf, "fit_expectancy", lambda tr, *a: seen.append(tr.copy()) or real(tr, *a))
    days = _daily("2024-01-01", "2024-02-01")
    tl = pd.concat([
        _trades("momentum", GOOD, "BTC", days, 0.5),
        _trades("momentum", BAD, "BTC", days, 0.1),
        # planted: straddles train_end, entirely after train_end, and entered before train_start
        _trades("momentum", BAD, "BTC", [W1.train_end - BAR * 2], 1e6, hold=BAR * 10),
        _trades("momentum", BAD, "BTC", _daily("2024-02-01", "2024-02-20"), 1e6),
        _trades("momentum", BAD, "BTC", [W1.train_start - BAR * 4], 1e6),
    ], ignore_index=True)
    m = fit_window(W1, tl, _fit_panel(), _settings(), "perp")
    assert m.params["momentum"] == GOOD
    assert set(m.universe) == {"BTC", "ETH"}
    assert isinstance(m.ranker, ExpectancyRanker)
    (tr,) = seen
    assert (tr["exit_ts"] < W1.train_end).all() and (tr["entry_ts"] >= W1.train_start).all()
    assert set(tr["params_id"]) == {GOOD}
    assert m.table[("momentum", ANY, ANY)].n == len(days)
    assert m.table[("momentum", "BTC", ANY)].accepted


def test_fit_window_restricts_to_universe_keys_and_tiebreaks():
    days = _daily("2024-01-01", "2024-02-01")
    tl = pd.concat([
        _trades("momentum", OTHER, "BTC", days, 0.3),
        _trades("momentum", BAD, "BTC", days, 0.3),  # tie -> smaller params_id wins
        _trades("momentum", GOOD, "DOGE", days, 50.0),  # not in the universe
        _trades("mean_reversion", "W96_zin2.0_zout0.5", "ETH", days, 0.2),
    ], ignore_index=True)
    m = fit_window(W1, tl, _fit_panel(), _settings(), "perp")
    assert m.params == {"momentum": min(BAD, OTHER), "mean_reversion": "W96_zin2.0_zout0.5"}
    assert {s.name for s in m.strategies} == {"momentum", "mean_reversion"}
    assert all(s.use_regime for s in m.strategies)
    assert m.keys["momentum"] == m.universe and m.keys["pairs"] == m.pairs.selected


def test_funding_worst_case_rule():
    days = _daily("2024-01-01", "2024-02-01")

    def tl(good_r):
        return pd.concat([
            _trades("momentum", BAD, "BTC", days, 0.5, unk=40),  # 40 unknown funding hours each
            _trades("momentum", GOOD, "BTC", days, good_r),
        ], ignore_index=True)

    hours = pd.date_range("2024-01-05", "2024-01-20", freq="1h", tz="UTC")
    known = {"BTC": pd.DataFrame({"rate": -1e-4}, index=hours)}
    # known rate before train_end: cost 2 * |-1e-4| * 40h = 0.008 per unit = 0.4 R -> BAD 0.1 < 0.4
    m = fit_window(W1, tl(0.4), _fit_panel(), _settings(), "perp", funding=known)
    assert m.params["momentum"] == GOOD
    assert "worst_case" in m.funding_rule and "momentum" in m.funding_rule
    # rates stamped after train_end are ignored, only the floor applies:
    # 2 * 1.25e-5 * 40h = 0.001 = 0.05 R -> BAD 0.45 > 0.4 (the future rate would give BAD 0.1)
    future = {"BTC": pd.DataFrame({"rate": 1e-4}, index=hours + pd.Timedelta(days=60))}
    fut = fit_window(W1, tl(0.4), _fit_panel(), _settings(), "perp", funding=future)
    assert fut.params["momentum"] == BAD
    assert fit_window(W1, tl(0.4), _fit_panel(), _settings(), "perp").params["momentum"] == BAD
    # the floor bites without any known rate: BAD 0.45 < GOOD 0.47 (0x alone would pick BAD)
    for f in (None, future):
        m = fit_window(W1, tl(0.47), _fit_panel(), _settings(), "perp", funding=f)
        assert m.params["momentum"] == GOOD
    # spot: no funding at all
    assert fit_window(W1, tl(0.47), _fit_panel(), _settings(), "spot").params["momentum"] == BAD


def test_funding_cost_is_direction_free_with_floor_and_pair_legs():
    tr = pd.concat([
        _trades("momentum", BAD, "BTC", [T("2024-01-03", tz="UTC")], 0.0, direction=1, unk=10),
        _trades("momentum", BAD, "BTC", [T("2024-01-04", tz="UTC")], 0.0, direction=-1, unk=10),
        _trades("pairs", "W96_zin1.5_zout0.0", "ETH/BTC", [T("2024-01-05", tz="UTC")], 0.0,
                direction=1, unk=20),
        _trades("momentum", BAD, "SOL", [T("2024-01-06", tz="UTC")], 0.0, direction=-1, unk=10),
        _trades("momentum", BAD, "BTC", [T("2024-01-07", tz="UTC")], 0.0, unk=0),
    ], ignore_index=True)
    cost = wf._funding_cost_r(tr, {"BTC": -1e-4, "ETH": 3e-4, "SOL": 1e-6})
    # a cost for long and short alike: 2 * |rate| * hours per unit / 2% stop
    assert cost.iloc[0] == pytest.approx(2 * 1e-4 * 10 / 0.02)
    assert cost.iloc[1] == pytest.approx(2 * 1e-4 * 10 / 0.02)
    # pair, beta ~ 1: half the notional and half the unknown hours per leg, both legs pay
    assert cost.iloc[2] == pytest.approx((0.5 * 2 * 3e-4 * 10 + 0.5 * 2 * 1e-4 * 10) / 0.02)
    # |rate| below the floor -> floor
    assert cost.iloc[3] == pytest.approx(2 * 1.25e-5 * 10 / 0.02)
    assert cost.iloc[4] == 0.0
    # no known rate at all -> floor
    assert wf._funding_cost_r(tr.iloc[:1], {}).iloc[0] == pytest.approx(2 * 1.25e-5 * 10 / 0.02)


@pytest.mark.parametrize("entry, hold", [
    (W1.train_start - BAR, BAR),  # entered before train_start
    (W1.train_end - BAR, BAR * 2),  # exits at/after train_end
    (W1.train_end + BAR, BAR),  # entirely after
])
def test_check_train_raises_on_out_of_window_trades(entry, hold):
    ok = _trades("momentum", GOOD, "BTC", _daily("2024-01-01", "2024-02-01"), 0.5)
    wf._check_train(ok, W1, "x")
    bad = pd.concat([ok, _trades("momentum", GOOD, "BTC", [entry], 0.5, hold=hold)], ignore_index=True)
    with pytest.raises(ValueError, match="outside the train window"):
        wf._check_train(bad, W1, "x")


def test_fit_window_checks_every_fitting_frame(monkeypatch):
    checked = []
    real = wf._check_train
    monkeypatch.setattr(wf, "_check_train",
                        lambda f, w, what: checked.append((len(f), what)) or real(f, w, what))
    days = _daily("2024-01-01", "2024-02-01")
    tl = pd.concat([_trades("momentum", GOOD, "BTC", days, 0.5),
                    _trades("momentum", BAD, "BTC", days, 0.1)], ignore_index=True)
    fit_window(W1, tl, _fit_panel(), _settings(), "perp")
    assert checked == [(2 * len(days), "momentum param selection"), (len(days), "expectancy table")]


def test_fit_window_spot_has_no_pairs_and_empty_universe_is_idle():
    days = _daily("2024-01-01", "2024-02-01")
    tl = _trades("momentum", GOOD, "BTC", days, 0.5)
    m = fit_window(W1, tl, _fit_panel(), _settings(), "spot")
    assert m.pairs.selected == [] and m.keys["pairs"] == []
    idle = fit_window(W1, tl, _fit_panel(), _settings(min_history_days=400), "perp")
    assert idle.universe == [] and idle.strategies == [] and idle.params == {}


def test_fit_window_ablations():
    days = _daily("2024-01-01", "2024-02-01")
    tl = _trades("momentum", GOOD, "BTC", days, 0.5)
    m = fit_window(W1, tl, _fit_panel(), _settings(), "perp", ablation="no_regime")
    assert [s.use_regime for s in m.strategies] == [False]
    r = fit_window(W1, tl, _fit_panel(), _settings(), "perp", ablation="random_ranker")
    assert isinstance(r.ranker, RandomRanker)
    assert r.params == m.params


# --- carried positions ----------------------------------------------------------------------------


def test_carried_position_exits_with_owner_strategy():
    panel = _panel(n=2_000, assets=("BTC",))
    owner = Momentum({"fast": 48, "slow": 192, "donchian": 96, "k_atr": 2})
    pos = Position(id="momentum:BTC:x", strategy="momentum", key="BTC", legs=(Leg("BTC", 1.0),),
                   direction=1, qty={"BTC": 1.0}, entry_price={"BTC": 100.0},
                   entry_ts=panel["BTC"].index[1000], entry_ref=100.0,
                   stop=StopSpec("price", 1e9, 0.02), exit=ExitSpec(2880, 2), risk_amount=1.0,
                   regime="TREND_UP")
    # the key left the universe and momentum is not run in the new window: exits still evaluated
    ws = WindowStrategy("momentum", None, {pos.id: (owner, "BTC")})
    sfs = ws.signals(panel, [])
    assert list(sfs) == ["BTC"] and (sfs["BTC"].entry == 0).all()
    assert ws.should_exit(sfs["BTC"], 1500, pos, {"BTC": float(panel["BTC"]["close"].iloc[1500])}) == "stop"
    # new params in the window: entries from the new params, exits of the carried position from the owner
    new = Momentum({"fast": 96, "slow": 480, "donchian": 288, "k_atr": 3})
    ws2 = WindowStrategy("momentum", new, {pos.id: (owner, "BTC")})
    sf2 = ws2.signals(panel, ["BTC"])["BTC"]
    pd.testing.assert_series_equal(sf2.entry, new.signals(panel, ["BTC"])["BTC"].entry)
    calls = []
    owner.should_exit = lambda sf, i, p, c: calls.append(sf) or None
    ws2.should_exit(sf2, 1500, pos, {"BTC": 1.0})
    assert calls and calls[0] is not sf2


# --- run_walkforward ------------------------------------------------------------------------------

D0, D1 = T("2024-01-01", tz="UTC"), T("2024-07-01", tz="UTC")


def _stamped(tl, use_regime=True):
    tl = tl.copy()
    tl.attrs = {"use_regime": use_regime}
    return tl


@pytest.fixture(scope="module")
def wf_case(tmp_path_factory):
    panel = _panel()
    days = _daily("2024-01-01", "2024-07-01")
    rng = np.random.default_rng(0)
    tl = pd.concat([
        _trades("momentum", pid, a, days, r + rng.normal(0, 0.05, len(days)))
        for a in panel for pid, r in ((GOOD, 0.5), (OTHER, 0.3))
    ], ignore_index=True)
    # 2 train / 1 test / 1 step / 1 holdout; history >= 70 days -> the first window is skipped
    settings = _settings(min_history_days=70, train_months=2, test_months=1, step_months=1,
                         holdout_months=1)
    settings = settings.model_copy(update={"var_dir": tmp_path_factory.mktemp("var")})
    return panel, _stamped(tl), settings


def _run(wf_case, **kw):
    panel, tl, settings = wf_case
    if kw.get("ablation") == "no_regime":
        tl = _stamped(tl, use_regime=False)
    return run_walkforward(panel, {}, tl, settings, "perp", data_start=D0, data_end=D1, **kw)


class SlowMomentum(Momentum):
    """Momentum whose trailing stop is out of reach: positions last ``max_hold`` (15 days), so
    positions opened late in a test window are carried into the next one."""

    GRID = [{"fast": 96, "slow": 480, "donchian": 96, "k_atr": 50}]
    _defaults = {**Momentum._defaults, "max_hold": 1_440}


def test_portfolio_carries_across_windows(wf_case, monkeypatch):
    panel, tl, settings = wf_case
    slow_id = SlowMomentum(SlowMomentum.GRID[0]).params_id
    monkeypatch.setitem(wf.STRATEGIES, "momentum", SlowMomentum)
    wf_case = (panel, _stamped(tl.assign(params_id=tl["params_id"].replace(GOOD, slow_id))), settings)
    calls = []
    real = wf.run_backtest

    sig = inspect.signature(real)

    def spy(*a, **kw):
        res = real(*a, **kw)
        bound = sig.bind(*a, **kw)
        bound.apply_defaults()
        calls.append((bound.arguments, res))
        return res

    monkeypatch.setattr(wf, "run_backtest", spy)
    res = _run(wf_case, max_drawdown_override=1.0)
    pw = res.per_window
    assert list(pw["status"]) == ["skipped: empty universe", "ok", "ok"]
    assert len(res.windows) == 2 and len(calls) == 2
    (a1, r1), (a2, r2) = calls
    assert a1["initial_portfolio"] is None and a1["initial_state"] is None
    assert a2["initial_portfolio"] is r1.portfolio
    assert a2["initial_state"] is r1.risk_state
    assert a2["initial_exiting"] is r1.pending_exits
    w1, w2 = res.windows[0].window, res.windows[1].window
    assert (a1["start"], a1["end"], a2["start"], a2["end"]) == (
        w1.test_start, w1.test_end, w2.test_start, w2.test_end)
    assert a1["end"] == a2["start"]
    for a in (a1, a2):
        assert a["settings"].accounts["perp"].max_drawdown == 1.0  # "edge": kill switch disabled
    assert wf_case[2].accounts["perp"].max_drawdown == 0.15  # caller's settings untouched
    # stitched OOS
    oos = res.oos
    assert oos.equity.index.is_monotonic_increasing and oos.equity.index.is_unique
    assert oos.equity.index[0] == w1.test_start and oos.equity.index[-1] == w2.test_end - BAR
    assert oos.initial_equity == r1.initial_equity and oos.portfolio is r2.portfolio
    assert r2.initial_equity == pytest.approx(r1.equity.iloc[-1])
    assert len(oos.trades) == len(r1.trades) + len(r2.trades) > 0
    spans = oos.trades[(oos.trades["entry_ts"] < w2.test_start) & (oos.trades["exit_ts"] >= w2.test_start)]
    assert len(spans) > 0  # a position opened in window 1 was carried and closed in window 2
    assert pw.loc[pw["status"] == "ok", "n_trades"].sum() == len(oos.trades)
    assert pw["net_pnl"].sum() == pytest.approx(oos.trades["pnl_net"].sum())
    for col in ("train_start", "train_end", "test_start", "test_end", "universe", "pairs_tested",
                "pairs_selected", "params_momentum", "accepted_cells", "n_trades",
                "expectancy_r", "net_pnl", "funding_rule"):
        assert col in pw.columns
    assert pw["params_momentum"].iloc[1] == slow_id
    assert set(spans["exit_reason"]) <= {"time", "stop"}  # exited by the owner's rules
    # positions still open at the end of the OOS, with their unrealized net:
    # final equity = capital + closed trades' net + open positions' unrealized net
    oae = res.open_at_end
    assert len(oae) > 0 and list(oae["position_id"]) == list(r2.portfolio.positions)
    assert r2.portfolio.equity() == pytest.approx(
        settings.accounts["perp"].capital_usd + oos.trades["pnl_net"].sum() + oae["unrealized_net"].sum())


def test_table_per_window_uses_only_past_trades(wf_case, monkeypatch):
    seen = []
    real = wf.fit_expectancy
    monkeypatch.setattr(wf, "fit_expectancy", lambda tr, *a: seen.append(tr.copy()) or real(tr, *a))
    res = _run(wf_case)
    assert len(seen) == len(res.windows) == 2
    for tr, m in zip(seen, res.windows, strict=True):
        assert len(tr) > 0
        assert tr["exit_ts"].max() < m.window.train_end
        assert tr["entry_ts"].min() >= m.window.train_start


@pytest.mark.parametrize("ablation", ["none", "no_regime", "random_ranker"])
def test_ablation_modes_run(wf_case, ablation):
    res = _run(wf_case, ablation=ablation)
    assert len(res.windows) == 2
    for m in res.windows:
        assert all(s.use_regime == (ablation != "no_regime") for s in m.strategies)
        assert isinstance(m.ranker, RandomRanker if ablation == "random_ranker" else ExpectancyRanker)
    assert len(res.oos.equity) > 0


VARIANTS_PERP = ["realistic", "edge", "cost_2x", "funding_zero", "funding_estimated_x2"]


def test_holdout_runs_once(wf_case, tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "_git_sha", lambda: "abc123")
    monkeypatch.setattr(wf, "_git_dirty", lambda: True)
    panel, tl, settings = wf_case
    settings = settings.model_copy(update={"var_dir": tmp_path})
    calls = []
    real = wf.run_backtest
    sig = inspect.signature(real)

    def spy(*a, **kw):
        bound = sig.bind(*a, **kw)
        bound.apply_defaults()
        calls.append(bound.arguments)
        return real(*a, **kw)

    monkeypatch.setattr(wf, "run_backtest", spy)
    out = run_holdout(panel, {}, tl, settings, "perp", data_end=D1)
    assert list(out) == VARIANTS_PERP and len(calls) == 5
    hw = Window(T("2024-04-01", tz="UTC"), T("2024-06-01", tz="UTC"), T("2024-06-01", tz="UTC"), D1)
    for res in out.values():
        assert [m.window for m in res.windows] == [hw]
        assert res.oos.equity.index[0] == T("2024-06-01", tz="UTC")
        assert res.oos.initial_equity == settings.accounts["perp"].capital_usd  # fresh account
        assert list(res.open_at_end.columns) == wf.OPEN_COLUMNS
    by = dict(zip(VARIANTS_PERP, calls, strict=True))
    assert by["edge"]["settings"].accounts["perp"].max_drawdown == 1.0
    assert by["realistic"]["settings"].accounts["perp"].max_drawdown == 0.15
    assert by["cost_2x"]["cost_mult"] == 2.0 and by["realistic"]["cost_mult"] == 1.0
    assert by["cost_2x"]["settings"].accounts["perp"].max_drawdown == 0.15
    assert by["funding_zero"]["funding_mode"] == "zero"
    assert by["funding_estimated_x2"]["funding_mode"] == "estimated_x2"
    assert by["realistic"]["funding_mode"] == "known_only"
    reg = tmp_path / "holdout_registry.json"  # default: settings.var_dir
    rec = json.loads(reg.read_text(encoding="utf-8"))
    assert rec["perp"]["git_sha"] == "abc123" and rec["perp"]["git_dirty"] is True
    assert "run_at" in rec["perp"] and rec["perp"]["variants"] == VARIANTS_PERP
    assert rec["perp"]["window"]["test_start"].startswith("2024-06-01")
    with pytest.raises(HoldoutAlreadyRun):
        run_holdout(panel, {}, tl, settings, "perp", data_end=D1)
    # another account is independent; spot has no funding scenarios
    monkeypatch.setattr(wf, "run_backtest", real)
    spot = run_holdout(panel, {}, tl, settings, "spot", data_end=D1)
    assert list(spot) == ["realistic", "edge", "cost_2x"]
    assert set(json.loads(reg.read_text(encoding="utf-8"))) == {"perp", "spot"}
    # a walk-forward whose test windows reach into the recorded holdout is refused
    with pytest.raises(ValueError, match="overlaps the recorded perp holdout"):
        run_walkforward(panel, {}, tl, settings, "perp", data_start=D0,
                        data_end=D1 + pd.DateOffset(months=1))
    run_walkforward(panel, {}, tl, settings, "perp", data_start=D0, data_end=D1)  # disjoint: fine


def test_holdout_crash_does_not_record(wf_case, tmp_path, monkeypatch):
    panel, tl, settings = wf_case
    reg = tmp_path / "reg.json"
    real = wf.run_backtest
    n = []

    def flaky(*a, **kw):
        n.append(1)
        if len(n) == 3:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(wf, "run_backtest", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        run_holdout(panel, {}, tl, settings, "perp", registry=reg, data_end=D1)
    assert not reg.exists()
    monkeypatch.setattr(wf, "run_backtest", real)
    assert list(run_holdout(panel, {}, tl, settings, "perp", registry=reg, data_end=D1)) == VARIANTS_PERP


@pytest.mark.parametrize("content", ["", "  \n", "[]", '"x"', "{not json"])
def test_holdout_corrupt_registry_refused_before_running(wf_case, tmp_path, monkeypatch, content):
    panel, tl, settings = wf_case
    reg = tmp_path / "reg.json"
    reg.write_text(content, encoding="utf-8")
    called = []
    monkeypatch.setattr(wf, "fit_window", lambda *a, **kw: called.append(1))
    monkeypatch.setattr(wf, "run_backtest", lambda *a, **kw: called.append(1))
    with pytest.raises(HoldoutRegistryCorrupt):
        run_holdout(panel, {}, tl, settings, "perp", registry=reg, data_end=D1)
    assert called == [] and reg.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(("ablation", "stamp"), [("none", False), ("no_regime", True),
                                                  ("random_ranker", False), ("none", None)])
def test_tradelists_stamp_must_match_ablation(wf_case, tmp_path, ablation, stamp):
    panel, tl, settings = wf_case
    tl = tl.copy()
    tl.attrs = {} if stamp is None else {"use_regime": stamp}
    with pytest.raises(ValueError, match="use_regime"):
        run_walkforward(panel, {}, tl, settings, "perp", ablation, data_start=D0, data_end=D1)
    with pytest.raises(ValueError, match="use_regime"):
        run_holdout(panel, {}, tl, settings, "perp", registry=tmp_path / "r.json", ablation=ablation,
                    data_end=D1)
    assert not (tmp_path / "r.json").exists()


def test_tradelists_built_once_over_all_candidate_keys(wf_case, monkeypatch):
    panel, _, settings = wf_case
    calls = []
    tl = pd.DataFrame({"x": [1]})
    monkeypatch.setattr(wf, "build_all_tradelists", lambda *a, **kw: calls.append((a, kw)) or tl)
    out = wf.build_walkforward_tradelists(panel, {}, settings, "spot", use_regime=False,
                                          data_start=D0, data_end=D1, cache_dir=None)
    assert out is tl and out.attrs == {"use_regime": False} and len(calls) == 1
    (strategies, _, _, _, kind, keys), kw = calls[0]
    assert set(strategies) == {"momentum", "mean_reversion", "pairs"} and kind == "spot"
    assert keys == {"momentum": sorted(panel), "mean_reversion": sorted(panel), "pairs": []}
    assert kw == {"band": settings.accounts["spot"].max_price_band, "use_regime": False, "cache_dir": None}
