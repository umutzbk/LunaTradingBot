"""Counterfactual labeling: outcomes equal the research trade list of the candidate's strategy."""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest
from sqlalchemy import select

from luna.config import load_settings
from luna.data.store import BarStore
from luna.engine.live import candidate_record
from luna.execution.costs import CostModel
from luna.labeling import label_candidates
from luna.research.tradelists import build_tradelist
from luna.store import models as m
from luna.store.db import Db
from luna.strategy.mean_reversion import MeanReversion
from luna.strategy.momentum import Momentum
from tests.helpers import synthetic_bars

SETTINGS = load_settings()
COSTS = CostModel(SETTINGS.costs["perp"])
BAND = SETTINGS.accounts["perp"].max_price_band
BAR = pd.Timedelta("15min")
NO_FUNDING = {"BTC": pd.DataFrame({"rate": pd.Series(dtype=float)},
                                  index=pd.DatetimeIndex([], tz="UTC"))}


@pytest.fixture
def bars():
    return synthetic_bars(3000, "random_walk", seed=0)


@pytest.fixture
def store(tmp_path, bars):
    s = BarStore(tmp_path / "var")
    s.write("kraken_futures", "BTC", bars)
    return s


@pytest.fixture
def db(tmp_path):
    d = Db(f"sqlite:///{(tmp_path / 'luna.db').as_posix()}")
    yield d
    d.close()


def _tradelist(strat, bars):
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    tl = build_tradelist(sf, {"BTC": bars}, NO_FUNDING, COSTS, "perp", strategy=strat, band=BAND)
    return sf, tl


def _record(db, strat, sf, ts, *, selected):
    i = sf.entry.index.get_loc(ts)
    cand = strat.candidate_at(sf, i, {})
    assert cand is not None
    row = candidate_record(cand, "submitted" if selected else "rejected", None if selected else "x")
    return db.record_candidates("run", [row], account="perp")[0]


def _outcomes(db):
    with db.engine.connect() as c:
        return {r["candidate_id"]: r for r in c.execute(select(m.candidate_outcomes)).mappings()}


def test_labels_rejected_candidate_with_stop_hit(db, store, bars):
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    trade = tl[tl.exit_reason == "stop"].iloc[0]
    cid = _record(db, strat, sf, trade.signal_ts, selected=False)
    now = bars.index[-1] + BAR
    assert label_candidates(db, store, COSTS, "perp", now, band=BAND) == 1
    out = _outcomes(db)[cid]
    assert out["exit_reason"] == "stop"
    assert out["outcome_r"] == pytest.approx(trade.r_net)
    assert out["outcome_r"] < 0


def test_labels_mean_reversion_and_other_grid_point(db, store, bars):
    ou = synthetic_bars(3000, "ou", seed=0)
    store.write("kraken_futures", "BTC", ou)
    ids = {}
    expected = {}
    for params in (MeanReversion.GRID[0], MeanReversion.GRID[-1], Momentum.GRID[-1]):
        cls = MeanReversion if "W" in params else Momentum
        strat = cls(dict(params), use_regime=False)
        sf, tl = _tradelist(strat, ou)
        t = tl.iloc[2]
        cid = _record(db, strat, sf, t.signal_ts, selected=True)
        ids[cid], expected[cid] = params, t
    assert label_candidates(db, store, COSTS, "perp", ou.index[-1] + BAR, band=BAND) == 3
    outs = _outcomes(db)
    for cid, t in expected.items():
        assert outs[cid]["outcome_r"] == pytest.approx(t.r_net)
        assert outs[cid]["exit_reason"] == t.exit_reason


def test_skips_candidates_not_yet_matured(db, store, bars):
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    trade = tl.iloc[3]
    _record(db, strat, sf, trade.signal_ts, selected=False)
    assert label_candidates(db, store, COSTS, "perp", trade.signal_ts + 2 * BAR, band=BAND) == 0
    assert label_candidates(db, store, COSTS, "perp", trade.exit_ts, band=BAND) == 0  # exit fill bar open
    assert _outcomes(db) == {}
    # once the exit's fill bar has closed the (early) exit is labeled
    assert label_candidates(db, store, COSTS, "perp", trade.exit_ts + BAR, band=BAND) == 1


def test_idempotent(db, store, bars):
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    for ts in tl.signal_ts.iloc[:3]:
        _record(db, strat, sf, ts, selected=False)
    now = bars.index[-1] + BAR
    assert label_candidates(db, store, COSTS, "perp", now, band=BAND) == 3
    first = _outcomes(db)
    assert label_candidates(db, store, COSTS, "perp", now + BAR, band=BAND) == 0
    assert _outcomes(db) == first


def test_other_account_untouched(db, store, bars):
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    _record(db, strat, sf, tl.signal_ts.iloc[0], selected=False)
    assert label_candidates(db, store, COSTS, "spot", bars.index[-1] + BAR, band=BAND) == 0


def test_cli_label(tmp_path, monkeypatch, bars):
    from typer.testing import CliRunner

    from luna.cli import app

    var = tmp_path / "var"
    BarStore(var).write("kraken_futures", "BTC", bars)
    path = tmp_path / "l.db"
    d = Db(f"sqlite:///{path.as_posix()}")
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    _record(d, strat, sf, tl.signal_ts.iloc[0], selected=False)
    d.close()
    monkeypatch.setattr("luna.config.load_settings",
                        lambda *a, **k: SETTINGS.model_copy(update={"var_dir": var}))
    res = CliRunner().invoke(app, ["label", "--account", "perp", "--db", str(path)])
    assert res.exit_code == 0, res.output
    assert "1 new outcomes; 1/1 candidates labeled" in res.output


# --- terminal outcomes, stored params, pairs, spot, time exit ------------------------------------

SPOT_COSTS = CostModel(SETTINGS.costs["spot"])
SPOT_BAND = SETTINGS.accounts["spot"].max_price_band


def _spot_store(tmp_path, bars):
    s = BarStore(tmp_path / "var")
    s.write("kraken_spot", "BTC", bars)
    return s


def _spot_tl(strat, bars, entry=None):
    sf = strat.signals({"BTC": bars}, ["BTC"])["BTC"]
    if entry is not None:
        e = pd.Series(0, index=sf.entry.index, dtype="int8")
        e.loc[entry[0]] = entry[1]
        sf = dataclasses.replace(sf, entry=e)
    return sf, build_tradelist(sf, {"BTC": bars}, {}, SPOT_COSTS, "spot", strategy=strat, band=SPOT_BAND)


def _record_acct(db, strat, sf, ts, account, *, params=None, direction=None):
    cand = strat.candidate_at(sf, sf.entry.index.get_loc(ts), {})
    row = candidate_record(cand, "rejected", "x", params)
    if direction is not None:
        row["direction"] = direction
    return db.record_candidates("run", [row], account=account)[0]


def test_spot_long_labeled_post_only_and_short_terminal(db, tmp_path, bars):
    store = _spot_store(tmp_path, bars)
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _spot_tl(strat, bars)
    t = tl[tl.direction > 0].iloc[0]
    cid = _record_acct(db, strat, sf, t.signal_ts, "spot")
    short_id = _record_acct(db, strat, sf, t.signal_ts, "spot", direction=-1)
    assert label_candidates(db, store, SPOT_COSTS, "spot", bars.index[-1] + BAR, band=SPOT_BAND) == 2
    outs = _outcomes(db)
    assert outs[cid]["outcome_r"] == pytest.approx(t.r_net)
    assert outs[cid]["exit_reason"] == t.exit_reason
    assert outs[short_id]["outcome_r"] is None
    assert outs[short_id]["exit_reason"] == "untradeable_short"


def test_spot_unfilled_entry_is_terminal(db, tmp_path, bars):
    store = _spot_store(tmp_path, bars)
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, _ = _spot_tl(strat, bars)
    ts = None
    for cand_ts in sf.entry.index[sf.entry > 0][:80]:
        if _spot_tl(strat, bars, (cand_ts, 1))[1].empty:
            ts = cand_ts
            break
    assert ts is not None, "fixture needs a long entry whose post-only buy never fills"
    cid = _record_acct(db, strat, sf, ts, "spot")
    assert label_candidates(db, store, SPOT_COSTS, "spot", bars.index[-1] + BAR, band=SPOT_BAND) == 1
    assert _outcomes(db)[cid]["exit_reason"] == "entry_unfilled"
    assert _outcomes(db)[cid]["outcome_r"] is None
    assert label_candidates(db, store, SPOT_COSTS, "spot", bars.index[-1] + BAR, band=SPOT_BAND) == 0


def test_unknown_strategy_and_pairs_on_spot_terminal(db, store, bars):
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    a = _record(db, strat, sf, tl.signal_ts.iloc[0], selected=False)
    b = _record(db, strat, sf, tl.signal_ts.iloc[1], selected=False)
    with db.engine.begin() as c:
        c.execute(m.candidates.update().where(m.candidates.c.id == a).values(strategy="naive_momentum"))
        c.execute(m.candidates.update().where(m.candidates.c.id == b).values(account="spot", key="ETH/BTC"))
    assert label_candidates(db, store, COSTS, "perp", bars.index[-1] + BAR, band=BAND) == 1
    assert label_candidates(db, store, SPOT_COSTS, "spot", bars.index[-1] + BAR, band=SPOT_BAND) == 1
    outs = _outcomes(db)
    assert (outs[a]["exit_reason"], outs[a]["outcome_r"]) == ("unknown_strategy", None)
    assert (outs[b]["exit_reason"], outs[b]["outcome_r"]) == ("pairs_on_spot", None)


def test_window_not_anchored_by_terminal_rows(db, store, bars, monkeypatch):
    strat = Momentum(dict(Momentum.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    old = _record(db, strat, sf, tl.signal_ts.iloc[0], selected=False)
    with db.engine.begin() as c:
        c.execute(m.candidates.update().where(m.candidates.c.id == old).values(strategy="naive_momentum"))
    late = tl.signal_ts.iloc[-3]
    _record(db, strat, sf, late, selected=False)
    starts = []
    orig = BarStore.read

    def spy(self, venue, asset, start=None, end=None):
        starts.append(start)
        return orig(self, venue, asset, start, end)

    monkeypatch.setattr(BarStore, "read", spy)
    label_candidates(db, store, COSTS, "perp", bars.index[-1] + BAR, band=BAND)
    assert starts
    assert all(s == late - 96 * 120 * BAR for s in starts)


def test_stored_params_preferred_even_off_grid(db, store, bars):
    off = {**Momentum.GRID[0], "k_atr": 2.5}
    strat = Momentum(off, use_regime=False)
    sf, tl = _tradelist(strat, bars)
    t = tl.iloc[1]
    with_params = _record_acct(db, strat, sf, t.signal_ts, "perp", params=strat.params)
    without = _record_acct(db, strat, sf, t.signal_ts, "perp")
    label_candidates(db, store, COSTS, "perp", bars.index[-1] + BAR, band=BAND)
    outs = _outcomes(db)
    assert outs[with_params]["outcome_r"] == pytest.approx(t.r_net)
    assert outs[with_params]["exit_reason"] == t.exit_reason
    assert (outs[without]["exit_reason"], outs[without]["outcome_r"]) == ("no_grid_match", None)


def test_ambiguous_params_when_grid_points_disagree(db, store, bars, monkeypatch):
    import luna.labeling as lb

    class Two(Momentum):
        GRID = [{**Momentum.GRID[0], "atr_n": 96}, {**Momentum.GRID[0], "atr_n": 24}]

    monkeypatch.setattr(lb, "STRATEGIES", {"momentum": Two})
    monkeypatch.setattr(lb, "_DIST_RTOL", 100.0)
    strat = Two(dict(Two.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, bars)
    cid = _record(db, strat, sf, tl.signal_ts.iloc[1], selected=False)
    label_candidates(db, store, COSTS, "perp", bars.index[-1] + BAR, band=BAND)
    out = _outcomes(db)[cid]
    assert (out["exit_reason"], out["outcome_r"]) == ("ambiguous_params", None)


def test_time_exit_at_max_hold(db, tmp_path):
    trend = synthetic_bars(3000, "trend", seed=0)
    s = BarStore(tmp_path / "var")
    s.write("kraken_futures", "BTC", trend)
    strat = MeanReversion(dict(MeanReversion.GRID[0]), use_regime=False)
    sf, tl = _tradelist(strat, trend)
    t = tl[tl.exit_reason == "time"].iloc[0]
    cid = _record(db, strat, sf, t.signal_ts, selected=True)
    assert label_candidates(db, s, COSTS, "perp", t.exit_ts, band=BAND) == 0
    assert label_candidates(db, s, COSTS, "perp", trend.index[-1] + BAR, band=BAND) == 1
    out = _outcomes(db)[cid]
    assert out["exit_reason"] == "time"
    assert out["outcome_r"] == pytest.approx(t.r_net)


def test_pairs_candidate_on_perp(db, tmp_path):
    from luna.strategy.pairs import Pairs

    btc = synthetic_bars(3000, "random_walk", seed=1)
    ou = synthetic_bars(3000, "ou", seed=2)
    factor = (ou["close"] / ou["close"].mean()).to_numpy()
    eth = btc.copy()
    for col in ("open", "high", "low", "close"):
        eth[col] = btc[col] * factor
    s = BarStore(tmp_path / "var")
    s.write("kraken_futures", "BTC", btc)
    s.write("kraken_futures", "ETH", eth)
    panel = {"BTC": btc, "ETH": eth}
    strat = Pairs(dict(Pairs.GRID[0]), use_regime=False)
    sf = strat.signals(panel, ["ETH/BTC"])["ETH/BTC"]
    funding = {a: pd.DataFrame({"rate": pd.Series(dtype=float)},
                               index=pd.DatetimeIndex([], tz="UTC")) for a in panel}
    tl = build_tradelist(sf, panel, funding, COSTS, "perp", strategy=strat, band=BAND)
    assert len(tl) > 0
    t = tl.iloc[0]
    cid = _record_acct(db, strat, sf, t.signal_ts, "perp")
    assert label_candidates(db, s, COSTS, "perp", btc.index[-1] + BAR, band=BAND) == 1
    out = _outcomes(db)[cid]
    assert out["outcome_r"] == pytest.approx(t.r_net)
    assert out["exit_reason"] == t.exit_reason
