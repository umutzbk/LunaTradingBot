import numpy as np
import pandas as pd

from luna.data.store import BarStore, fill_missing, validate_bars

COLS = ["open", "high", "low", "close", "volume", "trades", "filled"]


def make_bars(n=200, start="2024-01-01", seed=0, price0=100.0):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(seed)
    close = price0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    open_ = np.r_[price0, close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.0005,
            "low": np.minimum(open_, close) * 0.9995,
            "close": close,
            "volume": 10.0,
            "trades": 5,
            "filled": False,
        },
        index=idx,
    )


def test_append_dedupes_new_wins(tmp_path):
    store = BarStore(tmp_path)
    a = make_bars(10)
    store.write("kraken_futures", "BTC", a)
    b = make_bars(10, start="2024-01-01 01:30", seed=1)  # overlaps last 4 bars of a
    b.loc[b.index[0], "close"] = 12345.0
    added = store.append("kraken_futures", "BTC", b)
    out = store.read("kraken_futures", "BTC")
    assert added == 6
    assert len(out) == 16
    assert out.index.is_monotonic_increasing and out.index.is_unique
    assert out.loc[b.index[0], "close"] == 12345.0
    assert store.assets("kraken_futures") == ["BTC"]
    assert store.assets("kraken_spot") == []


def test_read_slices_by_time(tmp_path):
    store = BarStore(tmp_path)
    df = make_bars(20)
    store.write("kraken_spot", "ETH", df)
    out = store.read("kraken_spot", "ETH", start=df.index[5], end=df.index[9])
    assert list(out.index) == list(df.index[5:10])
    assert list(out.columns) == COLS


def test_read_missing_returns_empty(tmp_path):
    out = BarStore(tmp_path).read("kraken_spot", "NOPE")
    assert out.empty and list(out.columns) == COLS
    assert str(out.index.tz) == "UTC"


def test_funding_roundtrip(tmp_path):
    store = BarStore(tmp_path)
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    store.write_funding("BTC", pd.DataFrame({"rate": [0.1, 0.2, 0.3]}, index=idx))
    assert (tmp_path / "funding" / "BTC.parquet").exists()
    assert list(store.read_funding("BTC")["rate"]) == [0.1, 0.2, 0.3]


def test_fill_missing_marks_filled_and_carries_close():
    df = make_bars(10).drop(make_bars(10).index[[4, 5]])
    out = fill_missing(df)
    assert len(out) == 10
    prev_close = df.loc[make_bars(10).index[3], "close"]
    filled = out.loc[out["filled"]]
    assert len(filled) == 2
    for c in ["open", "high", "low", "close"]:
        assert (filled[c] == prev_close).all()
    assert (filled["volume"] == 0).all() and (filled["trades"] == 0).all()
    assert not out.loc[~out["filled"].astype(bool)].empty


def test_validate_detects_duplicates_gaps_spikes():
    df = make_bars(300)
    spike_ts = df.index[200]
    df.loc[df.index[200]:, ["open", "high", "low", "close"]] *= 3.0
    df = df.drop(df.index[100])  # one gap
    df = pd.concat([df, df.iloc[[50]]])  # one duplicate, out of order at the end
    rep = validate_bars(df)
    assert rep.duplicates == 1
    assert rep.gaps == 1
    assert rep.non_monotonic == 1
    assert spike_ts in rep.spikes
    assert rep.ok is False


def test_validate_clean_series_ok():
    rep = validate_bars(make_bars(300))
    assert rep.rows == 300
    assert (rep.duplicates, rep.non_monotonic, rep.gaps, rep.filled_bars) == (0, 0, 0, 0)
    assert rep.spikes == []
    assert rep.ok is True


def test_delete_removes_bars_and_is_idempotent(tmp_path):
    import pandas as pd

    from luna.data.store import BarStore

    s = BarStore(tmp_path)
    idx = pd.date_range("2025-01-01", periods=2, freq="15min", tz="UTC")
    df = pd.DataFrame({c: 1.0 for c in ("open", "high", "low", "close", "volume")}, index=idx)
    df["trades"] = 1
    s.write("kraken_spot", "BTC", df)
    s.delete("kraken_spot", "BTC")
    s.delete("kraken_spot", "BTC")
    assert s.assets("kraken_spot") == []


def test_append_is_crash_safe(tmp_path, monkeypatch):
    store = BarStore(tmp_path)
    store.write("kraken_spot", "BTC", make_bars(10))
    path = tmp_path / "bars" / "kraken_spot" / "BTC.parquet"
    real = pd.DataFrame.to_parquet

    def crashing(self, target, *a, **k):
        with open(target, "wb") as fh:  # partial write, then the process "dies"
            fh.write(b"PAR1garbage")
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", crashing)
    try:
        store.append("kraken_spot", "BTC", make_bars(3, start="2024-01-02"))
    except OSError:
        pass
    monkeypatch.setattr(pd.DataFrame, "to_parquet", real)
    assert len(pd.read_parquet(path)) == 10  # original file intact
    assert len(store.read("kraken_spot", "BTC")) == 10
