import numpy as np
import pandas as pd

from luna.universe import adv_usd, select_universe

ASOF = pd.Timestamp("2025-01-01", tz="UTC")


def bars(start, end, volume):
    idx = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    return pd.DataFrame({"close": 10.0, "volume": volume}, index=idx)


def test_adv_usd_mean_daily():
    b = bars("2024-11-01", "2025-01-01", 1.0)  # 96 bars/day * 10
    assert np.isclose(adv_usd(b, ASOF), 960.0)


def test_select_universe_uses_only_past_data():
    past = bars("2023-01-01", "2025-01-01", 1000.0)
    late = pd.concat([bars("2023-01-01", "2024-12-01", 0.001), bars("2025-01-01", "2025-02-01", 1e9)])
    out = select_universe({"A": past, "B": late}, ASOF, min_adv_usd=1e5, min_history_days=548)
    assert out == ["A"]


def test_select_universe_requires_history():
    young = bars("2024-09-23", "2025-01-01", 1e6)  # ~100 days
    old = bars("2023-01-01", "2025-01-01", 1e6)
    assert select_universe({"Y": young, "O": old}, ASOF, 1e5, 548) == ["O"]


def test_select_universe_sorted_by_adv_desc():
    p = {"A": bars("2023-01-01", "2025-01-01", 100.0), "B": bars("2023-01-01", "2025-01-01", 900.0)}
    assert select_universe(p, ASOF, 1.0, 548) == ["B", "A"]


def test_adv_usd_divides_by_window_days_not_fixed_days():
    asof = pd.Timestamp("2025-01-01", tz="UTC")
    b = bars("2024-12-30", "2025-01-01", 1.0)  # 2 full days of data, window 2 days
    assert np.isclose(adv_usd(b, asof, days=2), 960.0)
