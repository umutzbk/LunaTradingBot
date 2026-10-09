import numpy as np
import pandas as pd
from typer.testing import CliRunner

from luna.cli import app
from luna.data.integrity import check_spot_integrity
from luna.data.store import BarStore

SINCE = pd.Timestamp("2026-06-30", tz="UTC")
MIN_LAST = pd.Timestamp("2026-07-02", tz="UTC")


def bars(start="2026-06-29", end="2026-07-02 12:00", freq="15min"):
    idx = pd.date_range(start, end, freq=freq, tz="UTC")
    n = len(idx)
    return pd.DataFrame(
        {
            "open": np.full(n, 10.0),
            "high": np.full(n, 11.0),
            "low": np.full(n, 9.0),
            "close": np.full(n, 10.5),
            "volume": np.ones(n),
            "trades": np.ones(n, dtype="int64"),
        },
        index=idx,
    )


def seed(tmp_path, spot: dict[str, pd.DataFrame | None], perp_assets=None):
    store = BarStore(tmp_path)
    for a in perp_assets or spot:
        store.write("kraken_futures", a, bars())
    for a, df in spot.items():
        if df is not None:
            store.write("kraken_spot", a, df)
    return store


def check(store, **kw):
    return {r.asset: r for r in check_spot_integrity(store, SINCE, MIN_LAST, **kw)}


def test_clean_assets_pass(tmp_path):
    res = check(seed(tmp_path, {"BTC": bars(), "ETH": bars()}))
    assert set(res) == {"BTC", "ETH"}
    assert all(r.ok for r in res.values()), res


def test_universe_is_perp_store_and_missing_spot_fails(tmp_path):
    res = check(seed(tmp_path, {"BTC": bars(), "SOL": None}))
    assert res["BTC"].ok
    assert not res["SOL"].ok and "no kraken_spot bars" in res["SOL"].reason


def test_starts_after_since_fails(tmp_path):
    r = check(seed(tmp_path, {"BTC": bars(start="2026-06-30 00:15")}))["BTC"]
    assert not r.ok and "first bar" in r.reason


def test_last_before_min_last_fails(tmp_path):
    r = check(seed(tmp_path, {"BTC": bars(end="2026-07-01 23:45")}))["BTC"]
    assert not r.ok and "last bar" in r.reason


def test_nan_ohlc_fails(tmp_path):
    df = bars()
    df.iloc[200, df.columns.get_loc("close")] = np.nan
    r = check(seed(tmp_path, {"BTC": df}))["BTC"]
    assert not r.ok and "NaN" in r.reason


def test_high_below_low_fails(tmp_path):
    df = bars()
    df.iloc[150, df.columns.get_loc("high")] = 8.0
    r = check(seed(tmp_path, {"BTC": df}))["BTC"]
    assert not r.ok and "high<low" in r.reason


def test_gap_threshold(tmp_path):
    df = bars()
    df = df[(df.index < "2026-07-01") | (df.index >= "2026-07-01 13:00")]  # 13h gap
    store = seed(tmp_path, {"BTC": df})
    assert check(store)["BTC"].ok  # 24h default is loose (MUBARAK is sparse)
    r = check(store, max_gap_h=12)["BTC"]
    assert not r.ok and "gap" in r.reason


def test_old_gap_before_since_ignored(tmp_path):
    df = pd.concat([bars(start="2026-01-01", end="2026-01-02"), bars()])
    assert check(seed(tmp_path, {"BTC": df}))["BTC"].ok


def test_filled_rows_fail(tmp_path):
    df = bars()
    df["filled"] = False
    df.iloc[300, df.columns.get_loc("filled")] = True
    r = check(seed(tmp_path, {"BTC": df}))["BTC"]
    assert not r.ok and "filled" in r.reason


def test_unsorted_or_duplicate_index_fails(tmp_path):
    store = seed(tmp_path, {"BTC": bars(), "ETH": bars()})
    path = tmp_path / "bars" / "kraken_spot"
    df = pd.read_parquet(path / "BTC.parquet")
    pd.concat([df, df.iloc[[5]]]).to_parquet(path / "BTC.parquet")  # duplicate, written raw
    df = pd.read_parquet(path / "ETH.parquet")
    df.iloc[::-1].to_parquet(path / "ETH.parquet")
    res = check(store)
    assert not res["BTC"].ok and "unique" in res["BTC"].reason
    assert not res["ETH"].ok and "monotonic" in res["ETH"].reason


def _cli(tmp_path, monkeypatch, *extra):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    return CliRunner().invoke(
        app, ["spot-integrity", "--since", "2026-06-30", "--min-last", "2026-07-02T00:00:00", *extra]
    )


def test_cli_all_pass_exit_0(tmp_path, monkeypatch):
    seed(tmp_path, {"BTC": bars(), "ETH": bars()})
    res = _cli(tmp_path, monkeypatch)
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert [ln.split()[0] for ln in lines[:2]] == ["BTC", "ETH"]
    assert all("PASS" in ln for ln in lines[:2])


def test_cli_any_fail_exit_1(tmp_path, monkeypatch):
    seed(tmp_path, {"BTC": bars(), "ETH": bars(end="2026-07-01")})
    res = _cli(tmp_path, monkeypatch)
    assert res.exit_code == 1
    assert any(ln.startswith("ETH") and "FAIL" in ln for ln in res.output.splitlines())
    res = _cli(tmp_path, monkeypatch, "--min-last", "2026-07-01T00:00:00")
    assert res.exit_code == 0, res.output


def test_cli_empty_universe_fails(tmp_path, monkeypatch):
    res = _cli(tmp_path, monkeypatch)
    assert res.exit_code == 1


# ---- review: gap visibility on PASS (I1), candle consistency, positive prices, 15-min grid (I2)


def test_pass_reports_hour_gaps_and_largest_gap(tmp_path):
    df = bars()
    df = df[(df.index < "2026-07-01") | (df.index >= "2026-07-01 03:00")]  # 3h15m gap
    df = df[(df.index < "2026-07-01 06:00") | (df.index >= "2026-07-01 07:30")]  # 1h45m gap
    r = check(seed(tmp_path, {"BTC": df, "ETH": bars()}))
    assert r["BTC"].ok and r["BTC"].gaps_over_1h == 2 and r["BTC"].max_gap_h == 3.25
    assert r["ETH"].ok and r["ETH"].gaps_over_1h == 0 and r["ETH"].max_gap_h == 0.25


def test_cli_pass_line_shows_gaps(tmp_path, monkeypatch):
    df = bars()
    seed(tmp_path, {"BTC": df[(df.index < "2026-07-01") | (df.index >= "2026-07-01 02:00")]})
    line = _cli(tmp_path, monkeypatch).output.splitlines()[0]
    assert line.startswith("BTC PASS") and "gaps>1h=1" in line and "max_gap=2.25h" in line


def test_high_below_open_or_close_fails(tmp_path):
    df = bars()
    df.iloc[150, df.columns.get_loc("close")] = 11.5  # above high
    r = check(seed(tmp_path, {"BTC": df}))["BTC"]
    assert not r.ok and "high<max(open,close)" in r.reason


def test_low_above_open_or_close_fails(tmp_path):
    df = bars()
    df.iloc[150, df.columns.get_loc("open")] = 8.5  # below low
    r = check(seed(tmp_path, {"BTC": df}))["BTC"]
    assert not r.ok and "low>min(open,close)" in r.reason


def test_non_positive_price_fails(tmp_path):
    df = bars()
    df.iloc[150, df.columns.get_loc("low")] = 0.0
    r = check(seed(tmp_path, {"BTC": df}))["BTC"]
    assert not r.ok and "non-positive" in r.reason


def test_off_grid_timestamp_fails(tmp_path):
    df = bars()
    off = df.iloc[[100]].copy()
    off.index = off.index + pd.Timedelta("7min")
    r = check(seed(tmp_path, {"BTC": pd.concat([df, off]).sort_index()}))["BTC"]
    assert not r.ok and "15-minute grid" in r.reason


def test_off_grid_before_window_ignored(tmp_path):
    old = bars(start="2026-01-01", end="2026-01-01 01:00")
    old.index = old.index + pd.Timedelta("1min")
    assert check(seed(tmp_path, {"BTC": pd.concat([old, bars()])}))["BTC"].ok
