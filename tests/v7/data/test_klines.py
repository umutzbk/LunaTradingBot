import pandas as pd
import pytest

from luna.v7.data import klines
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.timeutil import MIN, from_ms

T0_MS = 1_700_000_000_000  # ms
HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,"
          "taker_buy_volume,taker_buy_quote_volume,ignore\n")


def _csv(n, start_ms=T0_MS, step_ms=900_000, header=False, price0=100.0):
    rows = []
    for i in range(n):
        o = start_ms + i * step_ms
        p = price0 + i
        rows.append(f"{o},{p},{p + 1},{p - 1},{p + 0.5},10,{o + step_ms - 1},{p * 10},{5 + i},4,{p * 4},0")
    return (HEADER if header else "") + "\n".join(rows) + "\n"


def _norm(n, **kw):
    return klines.normalise(klines.parse_csv(_csv(n, **kw)), "ms")


def test_normalise_columns_and_known_at():
    df = _norm(2, header=True)
    assert list(df.columns) == list(klines.OUT_COLUMNS)
    assert df["open_ts"].iloc[0] == from_ms(T0_MS)
    assert df["known_at"].iloc[0] == from_ms(T0_MS) + 15 * MIN
    assert df["o"].iloc[1] == 101.0 and df["n_trades"].iloc[0] == 5 and df["taker_buy_quote"].iloc[0] == 400.0
    one = klines.normalise(klines.parse_csv(_csv(1)), "ms", bar_ns=MIN)
    assert one["known_at"].iloc[0] == from_ms(T0_MS) + MIN


def test_ingest_rejects_ambiguous_units():
    raw = klines.parse_csv(_csv(2))
    with pytest.raises(ValueError):
        klines.normalise(raw, "ns")  # ms values read as ns: before 2017
    ns_raw = raw.copy()
    ns_raw["open_time"] = (ns_raw["open_time"].astype("int64") * 10**6).astype(str)
    with pytest.raises(ValueError):
        klines.normalise(ns_raw, "ms")  # ns values read as ms: year > 2100
    with pytest.raises(ValueError):
        klines.normalise(raw, "s")  # type: ignore[arg-type]
    assert klines.normalise(ns_raw, "ns")["open_ts"].iloc[0] == from_ms(T0_MS)


def test_union_fills_truncated_monthly_tail():
    full = _norm(6)
    monthly, daily = full.iloc[:4], full.iloc[2:]  # monthly truncated, overlap on keys 2,3 agrees
    out = klines.union(monthly, daily)
    assert len(out) == 6
    assert out.index.is_monotonic_increasing
    assert out["open_ts"].tolist() == full["open_ts"].tolist()
    assert out.index.tolist() == full["open_ts"].tolist()


def test_union_conflict_raises():
    full = _norm(4)
    daily = full.iloc[2:].copy()
    daily.loc[daily.index[0], "h"] += 0.01
    with pytest.raises(IntegrityBlocker):
        klines.union(full.iloc[:3], daily)


def test_union_per_symbol_keys():
    a = _norm(2).assign(symbol="AAAUSDT")
    b = _norm(2, price0=5.0).assign(symbol="BBBUSDT")
    out = klines.union(pd.concat([a, b], ignore_index=True), a.iloc[:0])
    assert len(out) == 4  # same open_ts across symbols is not a conflict
    d = a.copy()
    d["c"] += 1
    with pytest.raises(IntegrityBlocker):
        klines.union(a, d)
