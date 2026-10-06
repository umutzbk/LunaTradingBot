import json

import numpy as np
import pandas as pd
import pytest

import luna.engine.walkforward as wf
from luna.config import WalkForwardConfig, load_settings
from luna.engine.walkforward import (
    HoldoutAlreadyRun,
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
    tl = pd.concat([
        _trades("momentum", BAD, "BTC", days, 0.5, unk=100),  # long, 100 unknown funding hours
        _trades("momentum", GOOD, "BTC", days, 0.4),
    ], ignore_index=True)
    hours = pd.date_range("2024-01-05", "2024-01-20", freq="1h", tz="UTC")
    known = {"BTC": pd.DataFrame({"rate": 1e-4}, index=hours)}
    # 2x estimate: -1 * 2 * 1e-4 * 100h = -0.02 per unit = -1 R at a 2% stop -> BAD scores -0.5
    m = fit_window(W1, tl, _fit_panel(), _settings(), "perp", funding=known)
    assert m.params["momentum"] == GOOD
    assert "worst_case" in m.funding_rule and "momentum" in m.funding_rule
    # rates published only after train_end are not used (estimate 0 -> 0x == 2x)
    future = {"BTC": pd.DataFrame({"rate": 1e-4}, index=hours + pd.Timedelta(days=60))}
    assert fit_window(W1, tl, _fit_panel(), _settings(), "perp", funding=future).params["momentum"] == BAD
    # spot: no funding at all
    assert fit_window(W1, tl, _fit_panel(), _settings(), "spot").params["momentum"] == BAD


def test_funding_estimate_signs_and_pairs_legs():
    tr = pd.concat([
        _trades("momentum", BAD, "BTC", [T("2024-01-03", tz="UTC")], 0.0, direction=1, unk=10),
        _trades("momentum", BAD, "BTC", [T("2024-01-04", tz="UTC")], 0.0, direction=-1, unk=10),
        _trades("pairs", "W96_zin1.5_zout0.0", "ETH/BTC", [T("2024-01-05", tz="UTC")], 0.0,
                direction=1, unk=20),
    ], ignore_index=True)
    est = wf._funding_estimate_r(tr, {"BTC": 1e-4, "ETH": 3e-4})
    # long pays a positive rate; 2x; per-unit / 2% stop
    assert est.iloc[0] == pytest.approx(-2 * 1e-4 * 10 / 0.02)
    assert est.iloc[1] == pytest.approx(+2 * 1e-4 * 10 / 0.02)
    # pair long = long ETH, short BTC, beta ~ 1: half the notional and half the hours per leg
    assert est.iloc[2] == pytest.approx(-2 * (0.5 * 3e-4 - 0.5 * 1e-4) * 10 / 0.02)


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


@pytest.fixture(scope="module")
def wf_case():
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
    return panel, tl, settings


def _run(wf_case, **kw):
    panel, tl, settings = wf_case
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
    wf_case = (panel, tl.assign(params_id=tl["params_id"].replace(GOOD, slow_id)), settings)
    calls = []
    real = wf.run_backtest

    def spy(*a, **kw):
        res = real(*a, **kw)
        calls.append((a, kw, res))
        return res

    monkeypatch.setattr(wf, "run_backtest", spy)
    res = _run(wf_case, max_drawdown_override=1.0)
    pw = res.per_window
    assert list(pw["status"]) == ["skipped: empty universe", "ok", "ok"]
    assert len(res.windows) == 2 and len(calls) == 2
    (a1, kw1, r1), (a2, kw2, r2) = calls
    assert kw1["initial_portfolio"] is None and kw1["initial_state"] is None
    assert kw2["initial_portfolio"] is r1.portfolio
    assert kw2["initial_state"] is r1.risk_state
    assert kw2["initial_exiting"] is r1.pending_exits
    w1, w2 = res.windows[0].window, res.windows[1].window
    assert (a1[7], a1[8], a2[7], a2[8]) == (w1.test_start, w1.test_end, w2.test_start, w2.test_end)
    assert a1[8] == a2[7]
    assert a1[6].accounts["perp"].max_drawdown == 1.0  # "edge" variant: kill switch disabled
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


def test_holdout_runs_once(wf_case, tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "_git_sha", lambda: "abc123")
    panel, tl, settings = wf_case
    reg = tmp_path / "holdout_registry.json"
    res = run_holdout(panel, {}, tl, settings, "perp", registry=reg, data_end=D1)
    (m,) = res.windows
    assert m.window == Window(T("2024-04-01", tz="UTC"), T("2024-06-01", tz="UTC"),
                              T("2024-06-01", tz="UTC"), D1)
    assert res.oos.equity.index[0] == T("2024-06-01", tz="UTC")
    rec = json.loads(reg.read_text(encoding="utf-8"))
    assert rec["perp"]["git_sha"] == "abc123" and "run_at" in rec["perp"]
    assert rec["perp"]["window"]["test_start"].startswith("2024-06-01")
    with pytest.raises(HoldoutAlreadyRun):
        run_holdout(panel, {}, tl, settings, "perp", registry=reg, data_end=D1)
    # another account is independent
    run_holdout(panel, {}, tl, settings, "spot", registry=reg, data_end=D1)
    assert set(json.loads(reg.read_text(encoding="utf-8"))) == {"perp", "spot"}


def test_tradelists_built_once_over_all_candidate_keys(wf_case, monkeypatch):
    panel, _, settings = wf_case
    calls = []
    monkeypatch.setattr(wf, "build_all_tradelists", lambda *a, **kw: calls.append((a, kw)) or "TL")
    out = wf.build_walkforward_tradelists(panel, {}, settings, "spot", use_regime=False,
                                          data_start=D0, data_end=D1, cache_dir=None)
    assert out == "TL" and len(calls) == 1
    (strategies, _, _, _, kind, keys), kw = calls[0]
    assert set(strategies) == {"momentum", "mean_reversion", "pairs"} and kind == "spot"
    assert keys == {"momentum": sorted(panel), "mean_reversion": sorted(panel), "pairs": []}
    assert kw == {"band": settings.accounts["spot"].max_price_band, "use_regime": False, "cache_dir": None}
