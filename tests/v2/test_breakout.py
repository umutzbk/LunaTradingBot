import numpy as np
import pandas as pd
import pytest

from luna.research.kernel import STOP_TRAIL
from luna.research.tradelists import _exit_rule
from luna.v2.strategies import V2_STRATEGY_CLASSES, make_breakout
from tests.v2.equivalence import assert_equivalent


def _hand_bars(close):
    idx = pd.date_range("2024-01-01", periods=len(close), freq="15min", tz="UTC")
    c = np.asarray(close, float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1.0,
                         "trades": np.ones(len(c), dtype="int64"), "filled": False}, index=idx)


def test_breakout_entry_on_close_break_of_previous_n():
    s = make_breakout("15m")({"donchian": 4, "atr_n": 2, "k_atr": 2.0, "max_hold": 100})
    close = [10, 10, 10, 10, 10, 10.5, 10.6, 10.0, 9.0]
    sf = s.signals({"X": _hand_bars(close)}, ["X"])["X"]
    # bar 5 closes 10.5 > max(high of bars 1..4) = 10.1 -> long; bar 6 still above -> no re-trigger
    assert sf.entry.tolist() == [0, 0, 0, 0, 0, 1, 0, 0, -1]


def test_breakout_class_and_params_id():
    B = make_breakout("4h")
    assert B.name == "breakout_4h" and B(None).params_id == "N12_k2"


def test_breakout_exit_rule_is_trailing(v2_panel):
    s = make_breakout("1h")(None)
    assert _exit_rule(s, s.signals(v2_panel, ["BTC"])["BTC"])[0] == STOP_TRAIL


@pytest.mark.parametrize(("tf", "kind"), [("1h", "perp"), ("4h", "perp"), ("4h", "spot")])
def test_breakout_tradelist_matches_backtest(v2_panel, settings, tf, kind):
    B = make_breakout(tf)
    res, tl = assert_equivalent(B(B.GRID[0]), v2_panel, "ETH", kind, settings, min_trades=5)
    assert "trailing" in set(tl["exit_reason"])


def test_v2_strategy_classes_per_account():
    assert set(V2_STRATEGY_CLASSES("spot")) == {f"{f}_{t}" for f in ("tsmom", "breakout")
                                                for t in ("1h", "4h", "1d")}
    assert len(V2_STRATEGY_CLASSES("perp")) == 8
    assert all(cls.name == name for name, cls in V2_STRATEGY_CLASSES("perp").items())
