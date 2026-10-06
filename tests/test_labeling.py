"""Counterfactual labeling: outcomes equal the research trade list of the candidate's strategy."""

from __future__ import annotations

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
