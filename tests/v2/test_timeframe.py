import pandas as pd
import pytest

from luna.v2.timeframe import bars_for, resample_causal


def test_bars_for_exact_and_rejects_fractions():
    assert bars_for(pd.Timedelta(days=1), "15m") == 96 and bars_for(pd.Timedelta(days=5), "4h") == 30
    with pytest.raises(ValueError):
        bars_for(pd.Timedelta(hours=1), "4h")


def test_resample_aggregates_ohlcv(v2_panel):
    out = resample_causal(v2_panel["BTC"], "1h")
    src = v2_panel["BTC"].iloc[:4]
    row = out.iloc[0]
    assert out.index[0] == src.index[-1]
    assert row.open == src.open.iloc[0] and row.close == src.close.iloc[-1]
    assert row.high == src.high.max() and row.low == src.low.min()
    assert row.volume == pytest.approx(src.volume.sum()) and row.trades == src.trades.sum()
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trades", "filled"]


def test_resample_drops_incomplete_buckets(v2_panel):
    df = v2_panel["BTC"].iloc[: 4 * 24 + 2]  # exactly one day + 2 bars
    assert len(resample_causal(df, "1h")) == 24 and len(resample_causal(df, "1d")) == 1
    shifted = v2_panel["BTC"].iloc[2 : 4 * 24 + 2]  # 96 bars but misaligned: no complete day
    assert len(resample_causal(shifted, "1h")) == 23 and len(resample_causal(shifted, "1d")) == 0


def test_resample_15m_is_copy(v2_panel):
    df = v2_panel["BTC"].iloc[:10]
    out = resample_causal(df, "15m")
    pd.testing.assert_frame_equal(out, df)
    assert out is not df


def test_resample_filled_only_when_all_filled(v2_panel):
    df = v2_panel["BTC"].iloc[:8].copy()
    df["filled"] = [True, True, True, False, True, True, True, True]
    out = resample_causal(df, "1h")
    assert list(out["filled"]) == [False, True]


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_resample_is_causal(v2_panel, tf):
    df = v2_panel["AAA"]
    full = resample_causal(df, tf)
    for k in (5, 97, 400, 2000, len(df) - 1):
        part = resample_causal(df.iloc[:k], tf)
        pd.testing.assert_frame_equal(part, full[full.index <= df.index[k - 1]])
