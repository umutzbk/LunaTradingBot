import pandas as pd
import pytest

from luna.research.kernel import STOP_PRICE
from luna.research.tradelists import _exit_rule
from luna.strategy.naive import NaiveMomentum
from luna.v2.strategies import make_tsmom
from luna.v2.timeframe import TF
from tests.v2.equivalence import assert_equivalent

V1_POINT = {"fast": 96, "slow": 480, "atr_n": 96, "stop_atr": 3.0, "max_hold": 2880}


def test_tsmom_15m_v1_point_equals_naive_momentum(v2_panel):
    s = make_tsmom("15m")(V1_POINT)
    a = s.signals(v2_panel, ["BTC"])["BTC"]
    b = NaiveMomentum().signals(v2_panel, ["BTC"])["BTC"]
    for f in ("entry", "exit_long", "exit_short", "stop_distance", "ref"):
        pd.testing.assert_series_equal(getattr(a, f), getattr(b, f), check_names=False)
    assert a.exit_spec == b.exit_spec


def test_tsmom_class_identity_and_params_id():
    T = make_tsmom("1h")
    assert T is make_tsmom("1h") and T.name == "tsmom_1h" and T.TF == "1h"
    assert T(None).params_id == "F24_S120_k2"
    with pytest.raises(ValueError):
        T({"bogus": 1})


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_tsmom_htf_signals_only_on_decision_stamps(v2_panel, tf):
    sf = make_tsmom(tf)(None).signals(v2_panel, ["BTC"])["BTC"]
    close_time = sf.entry.index + pd.Timedelta("15min")
    assert (close_time == close_time.floor(TF[tf])).all()
    assert sf.entry.index.isin(v2_panel["BTC"].index).all()
    assert (sf.entry != 0).sum() > 0


def test_tsmom_signals_causal(v2_panel):
    s = make_tsmom("4h")(None)
    df = v2_panel["AAA"]
    full = s.signals({"AAA": df}, ["AAA"])["AAA"]
    for k in (3000, 9000, len(df) - 5):
        part = s.signals({"AAA": df.iloc[:k]}, ["AAA"])["AAA"]
        for f in ("entry", "exit_long", "stop_distance"):
            pd.testing.assert_series_equal(getattr(part, f), getattr(full, f).loc[part.entry.index])


def test_tsmom_exit_rule_is_stop_price(v2_panel):
    s = make_tsmom("1h")(None)
    assert _exit_rule(s, s.signals(v2_panel, ["BTC"])["BTC"])[0] == STOP_PRICE


@pytest.mark.parametrize(
    ("tf", "kind", "n"), [("1h", "perp", 5), ("4h", "perp", 5), ("1h", "spot", 5), ("1d", "spot", 3)]
)
def test_tsmom_tradelist_matches_backtest(v2_panel, settings, tf, kind, n):
    T = make_tsmom(tf)
    strat = T(T.GRID[0])
    res, tl = assert_equivalent(strat, v2_panel, "BTC", kind, settings, min_trades=n)
    assert {"signal"} <= set(tl["exit_reason"])


def test_tradelist_cache_hash_covers_v2_helper_modules():
    from luna.research.tradelists import _source_files

    files = {p.as_posix() for p in _source_files({"tsmom_1h": make_tsmom("1h")})}
    assert any(f.endswith("luna/v2/timeframe.py") for f in files)
    assert any(f.endswith("luna/v2/params.py") for f in files)
    assert any(f.endswith("luna/v2/strategies.py") for f in files)
