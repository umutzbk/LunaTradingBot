import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.data.store import BarStore
from luna.engine.backtest import default_instruments
from luna.v2.data import (
    V2_DATA_END,
    HoldoutLeak,
    assert_before_cutoff,
    load_v2_inputs,
    v2_settings,
    v2_windows,
)


def _bars(start, end):
    idx = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    n = len(idx)
    px = 100.0 + np.arange(n) * 0.01
    return pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px,
                         "volume": 1.0, "trades": np.ones(n, dtype="int64"),
                         "filled": np.zeros(n, dtype=bool)}, index=idx)


def test_v2_windows_end_at_cutoff(settings):
    w = v2_windows(settings)
    assert w[0].test_start == pd.Timestamp("2023-07-01", tz="UTC")
    assert w[-1].test_end == V2_DATA_END and len(w) == 18


def test_v2_settings_zero_holdout(settings):
    assert v2_settings(settings).walkforward.holdout_months == 0


def test_assert_before_cutoff_raises_on_late_bar_or_funding():
    ok = _bars("2026-06-30 23:45", "2026-07-01")
    assert len(ok) == 1
    assert_before_cutoff({"A": ok}, {})
    late = _bars("2026-07-01", "2026-07-01 00:15")
    with pytest.raises(HoldoutLeak):
        assert_before_cutoff({"A": late}, {})
    fund = pd.DataFrame({"rate": [0.0]}, index=pd.DatetimeIndex([V2_DATA_END]))
    with pytest.raises(HoldoutLeak):
        assert_before_cutoff({}, {"A": fund})


def test_loader_never_returns_bars_after_cutoff(tmp_path, settings):
    store = BarStore(tmp_path)
    store.write("kraken_futures", "BTC", _bars("2026-06-29", "2026-07-03"))
    fidx = pd.date_range("2026-06-29", "2026-07-02", freq="1h", tz="UTC")
    store.write_funding("BTC", pd.DataFrame({"rate": 0.0001}, index=fidx))
    inst = default_instruments(["BTC"], "perp", settings)
    inp = load_v2_inputs(settings, store, "perp", inst)
    assert inp.panel["BTC"].index.max() == pd.Timestamp("2026-06-30 23:45", tz="UTC")
    assert inp.funding["BTC"].index.max() < V2_DATA_END


def test_v2_package_never_references_holdout():
    src = "\n".join(p.read_text(encoding="utf-8") for p in Path("luna/v2").rglob("*.py"))
    assert not re.search(r"run_holdout|holdout_report|luna holdout|holdout_registry", src)
