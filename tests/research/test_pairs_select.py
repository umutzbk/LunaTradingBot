from __future__ import annotations

import numpy as np
import pandas as pd

from luna.config import PairsConfig
from luna.research.pairs_select import select_pairs
from tests.helpers import _bars_from_close, synthetic_pair

CFG = PairsConfig(
    corr_min=0.7,
    fdr_q=0.05,
    stability_p=0.10,
    half_life_min=8,
    half_life_max=192,
    beta_change_max=0.25,
    max_pairs=5,
    always_watch=["ETH/BTC"],
)
_COLS = ["pair", "corr", "p_raw", "p_bh", "hl_1", "hl_2", "beta_1", "beta_2", "reasons"]


def _window(df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    return df.index[0], df.index[-1] + pd.Timedelta(minutes=15)


def _liq(df: pd.DataFrame, k: float) -> pd.DataFrame:
    return df.assign(volume=df["volume"] * k)


def test_cointegrated_pair_selected():
    a, b = synthetic_pair(8000, "cointegrated", seed=1)
    s, e = _window(a)
    sel = select_pairs({"ETH": a, "BTC": _liq(b, 1000)}, ["ETH", "BTC"], s, e, CFG)
    assert sel.selected == ["ETH/BTC"], sel.table.to_string()
    assert sel.tested == 1 and sel.prefiltered == 1
    row = sel.table.iloc[0]
    assert row["reasons"] == "" and row["p_bh"] <= 0.05
    assert abs(row["beta_1"] - 0.8) < 0.2


def test_canonical_order_by_adv():
    a, b = synthetic_pair(8000, "cointegrated", seed=1)
    s, e = _window(a)
    sel = select_pairs({"BTC": a, "ETH": _liq(b, 1000)}, ["ETH", "BTC"], s, e, CFG)
    assert list(sel.table["pair"]) == ["BTC/ETH"]


def test_unstable_beta_rejected():
    n = 8000
    a, b = synthetic_pair(n, "cointegrated", seed=2)
    lb = np.log(b["close"].to_numpy())
    rng = np.random.default_rng(5)
    eps = rng.normal(0, 0.001, n)
    ou = np.zeros(n)
    for i in range(1, n):
        ou[i] = ou[i - 1] * 0.95 + eps[i]
    beta = np.where(np.arange(n) < n // 2, 0.5, 1.3)
    a2 = _bars_from_close(np.exp(beta * lb + ou), np.random.default_rng(9), 0.001)
    s, e = _window(a)
    sel = select_pairs({"ETH": a2, "BTC": _liq(b, 1000)}, ["ETH", "BTC"], s, e, CFG)
    assert sel.selected == []
    assert sel.table.iloc[0]["reasons"] != ""


def test_uses_only_train_window():
    a, b = synthetic_pair(8000, "cointegrated", seed=1)
    a_i, b_i = synthetic_pair(8000, "independent", seed=3)
    shift = pd.Timedelta(minutes=15 * 8000)
    junk_a = a_i.set_axis(a.index + shift)
    junk_b = b_i.set_axis(b.index + shift)
    s, e = _window(a)
    base = select_pairs({"ETH": a, "BTC": _liq(b, 1000)}, ["ETH", "BTC"], s, e, CFG)
    both = select_pairs(
        {"ETH": pd.concat([a, junk_a]), "BTC": _liq(pd.concat([b, junk_b]), 1000)},
        ["ETH", "BTC"],
        s,
        e,
        CFG,
    )
    pd.testing.assert_frame_equal(base.table, both.table)
    assert base.selected == both.selected == ["ETH/BTC"]


def test_counts_reported():
    a, b = synthetic_pair(8000, "cointegrated", seed=1)
    ind, _ = synthetic_pair(8000, "independent", seed=7)
    panel = {"ETH": a, "BTC": _liq(b, 1000), "XRP": _liq(ind, 10)}
    s, e = _window(a)
    sel = select_pairs(panel, ["ETH", "BTC", "XRP"], s, e, CFG)
    assert sel.tested >= sel.prefiltered >= 1
    assert "ETH/BTC" in set(sel.table["pair"])
    assert set(sel.selected) <= set(sel.stable) <= set(sel.fdr_pass)
    assert list(sel.table.columns) == _COLS


def test_always_watch_below_corr_is_reported_not_selected():
    a, _ = synthetic_pair(2000, "independent", seed=11)
    _, b = synthetic_pair(2000, "independent", seed=12)
    s, e = _window(a)
    sel = select_pairs({"ETH": a, "BTC": _liq(b, 1000)}, ["ETH", "BTC"], s, e, CFG)
    assert sel.tested == 1 and sel.prefiltered == 0 and sel.selected == []
    assert sel.table.iloc[0]["reasons"] == "corr"


def test_independent_random_walks_mostly_rejected():
    n_assets, hours = 12, 2000
    cfg = CFG.model_copy(update={"corr_min": -1.0})  # no prefilter: BH must carry the load
    nonempty = 0
    for seed in range(20):
        rng = np.random.default_rng(1000 + seed)
        idx = pd.date_range("2024-01-01", periods=hours * 4, freq="15min", tz="UTC")
        panel = {}
        for i in range(n_assets):
            close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, len(idx))))
            panel[f"A{i}"] = _bars_from_close(close, rng, 0.0005).set_axis(idx)
        sel = select_pairs(panel, list(panel), idx[0], idx[-1] + pd.Timedelta(minutes=15), cfg)
        assert sel.tested == 66
        nonempty += bool(sel.selected)
    assert nonempty <= 1
