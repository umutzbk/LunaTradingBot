import numpy as np
import pandas as pd

from luna.regime.rules import classify_regime
from tests.helpers import assert_causal, synthetic_bars

LABELS = {"TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "UNKNOWN"}
LB = 2000


def _reshape(bars: pd.DataFrame, mult: np.ndarray) -> pd.DataFrame:
    """Scale per-bar log returns by `mult`, rescaling OHLC consistently."""
    ret = np.log(bars["close"]).diff().fillna(0.0) * mult
    close = bars["close"].iloc[0] * np.exp(ret.cumsum())
    ratio = close / bars["close"]
    out = bars.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = bars[c] * ratio
    return out


def test_regime_causal():
    bars = synthetic_bars(1500, "random_walk", seed=1)
    assert_causal(lambda d: classify_regime(d, lookback_bars=400), bars)


def test_warmup_unknown():
    bars = synthetic_bars(3000, "random_walk", seed=2)
    reg = classify_regime(bars, lookback_bars=LB)
    assert set(reg.unique()) <= LABELS
    assert (reg.iloc[:480] == "UNKNOWN").all()  # slow EMA / quantile history not available
    assert (reg.iloc[-500:] != "UNKNOWN").all()
    assert reg.index.equals(bars.index)


def test_flat_noise_labeled_range():
    bars = synthetic_bars(6000, "ou", seed=3, theta=0.5)
    reg = classify_regime(bars, lookback_bars=LB)
    post = reg.iloc[LB:]
    # Percentile rules label ~30% of stationary noise as trend and ~10% as HIGH_VOL by
    # construction, so RANGE is the plurality (~60%), not 70%+.
    shares = post.value_counts(normalize=True)
    assert shares["RANGE"] >= 0.50
    assert shares.idxmax() == "RANGE"


def test_trend_series_labeled_trend():
    base = synthetic_bars(5000, "ou", seed=4, theta=0.5)
    up = synthetic_bars(1000, "trend", seed=5, drift=0.003, sigma=0.0005)
    tail = base["close"].iloc[-1] * up["close"].to_numpy() / 100.0
    close = np.concatenate([base["close"].to_numpy(), tail])
    bars = _reshape_from_close(base, close)
    reg = classify_regime(bars, lookback_bars=LB)
    assert (reg.iloc[-600:] == "TREND_UP").mean() >= 0.8
    down_close = 1e4 / bars["close"]  # mirror image: uptrend becomes downtrend
    reg_dn = classify_regime(_reshape_from_close(bars, down_close.to_numpy()), lookback_bars=LB)
    assert (reg_dn.iloc[-600:] == "TREND_DOWN").mean() >= 0.8


def _reshape_from_close(template: pd.DataFrame, close: np.ndarray) -> pd.DataFrame:
    n = len(close)
    full = synthetic_bars(n, "random_walk", seed=9)
    ratio = close / full["close"].to_numpy()
    for c in ("open", "high", "low", "close"):
        full[c] = full[c].to_numpy() * ratio
    return full


def test_vol_spike_labeled_high_vol():
    bars = synthetic_bars(5000, "random_walk", seed=6)
    mult = np.ones(len(bars))
    mult[4500:4800] = 6.0
    reg = classify_regime(_reshape(bars, mult), lookback_bars=LB)
    assert (reg.iloc[4600:4800] == "HIGH_VOL").mean() >= 0.9
    assert (reg.iloc[LB:4400] == "HIGH_VOL").mean() < 0.2


def test_nan_input_unknown():
    bars = synthetic_bars(3000, "random_walk", seed=7)
    bars.iloc[2500, bars.columns.get_loc("close")] = np.nan
    reg = classify_regime(bars, lookback_bars=LB)
    assert reg.iloc[2500] == "UNKNOWN"
