import numpy as np
import pandas as pd
import pytest

import luna.v7.universe as uni
from luna.v7.data.instances import build_instances
from luna.v7.data.intervals import annotate
from luna.v7.data.notices import NoticeVersion
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.universe import (
    GateRow,
    Identity,
    LeakageError,
    SymbolData,
    assert_pit,
    d0_month,
    eligible,
    entry_allowed,
    exit_triggered,
    g_d0,
    gates,
    instrument_exclusion,
)

S = to_ns("2024-01-01")
Q = 15 * MIN
T = S + 70 * D + 5 * MIN  # decision time, 00:05
ID_OK = Identity("ABC", "PERPETUAL", "USDT", "COIN", 1.0, True)


def bars15(start=S, end=T, vol_q=30e6, drop=(), zero=(), px=100.0):
    """15m grid from start through end + 2 h (rows past t known later); daily quote volume = vol_q."""
    ts = np.arange(start, end + 2 * H, Q, dtype=np.int64)
    ts = ts[~np.isin(ts, list(drop))]
    v = np.where(np.isin(ts, list(zero)), 0.0, 10.0)
    return pd.DataFrame(
        dict(open_ts=ts, o=px, h=px * 1.0001, l=px * 0.9999, c=px, vol_base=v,
             vol_quote=np.where(v > 0, vol_q / 96.0, 0.0), known_at=ts + Q)
    )


def funding(start=S, end=T, step_h=8, skip=()):
    ts = [x for x in range(start, end + 20 * H, step_h * H) if x not in set(skip)]
    return pd.DataFrame(
        dict(funding_ts=ts, rate=1e-4, interval_h_explicit=pd.array([step_h] * len(ts), dtype="Int64"))
    )


def make(b15=None, fund=None, insts=None, notices=(), ident=ID_OK, symbol="ABCUSDT"):
    b15 = bars15() if b15 is None else b15
    insts = build_instances(b15, T + 3 * H) if insts is None else insts
    fa = annotate(funding() if fund is None else fund, insts)
    fa["known_at"] = fa["funding_ts"] + MIN
    b1 = pd.DataFrame(dict(open_ts=np.arange(T - 2 * H, T, MIN, dtype=np.int64), vol_base=1.0))
    b1["known_at"] = b1["open_ts"] + MIN
    return SymbolData(symbol, b15, b1, fa, insts, list(notices), ident)


def test_all_pass_baseline():
    r = gates(make(), T)
    assert r.reasons == () and eligible(r) and g_d0(r) and entry_allowed(r) and not exit_triggered(r)
    assert r.max_known_at <= T - MIN


def test_u1_uses_current_instance():
    first = bars15(S, S + 30 * D)
    first = first[first["open_ts"] < S + 30 * D]
    new_start = T - 10 * D  # 8+ d dormancy, then a new instance 10 d before t
    b = pd.concat([first, bars15(new_start, T)], ignore_index=True)
    insts = build_instances(b, T + 3 * H)
    assert len(insts) == 2
    assert not gates(make(b15=b, insts=insts, fund=funding(start=new_start)), T).u1
    assert gates(make(), T).u1  # one long instance passes


def test_u3_resets_at_instance():
    first = bars15(S, S + 40 * D)
    first = first[first["open_ts"] < S + 40 * D]
    new_start = T - 20 * D  # new instance only 20 d old although the symbol has more history
    b = pd.concat([first, bars15(new_start, T)], ignore_index=True)
    insts = build_instances(b, T + 3 * H)
    assert len(insts) == 2
    f = funding(start=S, end=T)  # records through both instances and the dormant gap
    assert not gates(make(b15=b, insts=insts, fund=f), T).u3
    assert gates(make(fund=f), T).u3


def test_u3_gap_and_invalid_as_missing():
    assert not gates(make(fund=funding(skip={S + 67 * D})), T).u3  # settlement missing inside the last 7 d
    assert gates(make(fund=funding(skip={S + 10 * D})), T).u3  # an old gap is allowed
    # a 6 h off-schedule record in the last 7 d is invalid -> missing -> the surrounding spacing is a gap
    f = funding()
    k = f.index[f["funding_ts"] >= T - 3 * D][0]
    f.loc[k:, "funding_ts"] -= 2 * H
    sd = make(fund=f)
    assert bool(sd.funding_annot["invalid"].any())
    assert not gates(sd, T).u3


def test_u3_needs_30_days():
    assert not gates(make(fund=funding(start=T - 29 * D)), T).u3
    assert gates(make(fund=funding(start=T - 31 * D)), T).u3


def test_u2_median_threshold_and_empty_days():
    assert gates(make(b15=bars15(vol_q=20e6)), T).u2  # exactly 20M median
    assert not gates(make(b15=bars15(vol_q=19.9e6)), T).u2
    lo = T // D - 30
    b = bars15()
    day = b["open_ts"] // D
    b.loc[(day >= lo) & (day < lo + 16), ["vol_base", "vol_quote"]] = 0.0  # 16 of 30 days empty: median 0
    assert not gates(make(b15=b), T).u2
    b2 = bars15()
    b2.loc[(day >= lo) & (day < lo + 14), ["vol_base", "vol_quote"]] = 0.0
    assert gates(make(b15=b2), T).u2


def test_u2_ignores_today_and_future():
    b = bars15(vol_q=19e6)
    b.loc[b["open_ts"] >= T // D * D, "vol_quote"] = 1e12  # the current (unfinished) UTC day is not counted
    assert not gates(make(b15=b), T).u2


def test_u4_zero_volume_counts_present():
    last_close = (T - MIN) // Q * Q
    window = [last_close - Q * i for i in range(1, 97)]
    assert gates(make(b15=bars15(zero=window[:20])), T).u4  # zero volume is still a present bar
    assert gates(make(b15=bars15(drop=window[20:24])), T).u4  # 4 missing -> 92/96
    assert not gates(make(b15=bars15(drop=window[20:25])), T).u4  # 5 missing -> 91/96


def test_u8_window_inclusive_ends():
    t = S + 70 * D + 10 * MIN
    for close, expect in [(t - 60 * MIN, True), (t - MIN, True), (t - 61 * MIN, False), (t, False)]:
        b = bars15(end=t)
        b["vol_base"] = 0.0
        b = b[b["open_ts"] != close - Q]
        b = pd.concat(
            [b, pd.DataFrame(dict(open_ts=[close - Q], o=100.0, h=100.0, l=100.0, c=100.0, vol_base=5.0,
                                  vol_quote=1.0, known_at=[close]))],
            ignore_index=True,
        )
        ok, _ = uni._u8(b[b["known_at"] <= t - MIN], t)
        assert ok is expect, (close - t) // MIN


def test_u8_false_blocks_entry_not_exit():
    r = GateRow(True, True, True, True, True, True, True, False, ("U8",), None)
    assert not entry_allowed(r) and not eligible(r)
    assert not exit_triggered(r)  # U8 is not an exit trigger
    r6 = GateRow(True, True, True, True, True, False, True, True, ("U6",), None)
    assert exit_triggered(r6) and not entry_allowed(r6)
    b = bars15()
    b.loc[b["open_ts"] >= T - 2 * H, "vol_base"] = 0.0  # no activity in the last hour; bars still present
    row = gates(make(b15=b), T)
    assert not row.u8 and row.u4 and not entry_allowed(row) and not exit_triggered(row)


def test_u5_threshold_7_5bps(monkeypatch):
    sd = make()
    monkeypatch.setattr(uni, "half_spread", lambda b, t: 7.5e-4)
    assert gates(sd, T).u5
    monkeypatch.setattr(uni, "half_spread", lambda b, t: 7.5e-4 + 1e-9)
    assert not gates(sd, T).u5
    monkeypatch.setattr(uni, "half_spread", lambda b, t: None)  # missing estimate fails closed
    assert not gates(sd, T).u5


def test_u7_freshness():
    for age, expect in [(8 * H + 15 * MIN, True), (8 * H + 15 * MIN + 1, False)]:
        last = T - age
        f = funding(start=last - 40 * D, end=last)
        f = f[f["funding_ts"] <= last].reset_index(drop=True)
        f.loc[f.index[-1], "funding_ts"] = last
        assert gates(make(fund=f), T).u7 is expect, age


def test_known_at_assertion():
    with pytest.raises(LeakageError):
        assert_pit(GateRow(*(True,) * 8, (), T - MIN + 1), T)
    assert_pit(GateRow(*(True,) * 8, (), T - MIN), T)  # boundary allowed
    r = gates(make(), T)
    assert r.max_known_at <= T - MIN
    # rows known only after t - 1 min never enter a gate
    b = bars15(vol_q=19e6)
    b.loc[b["open_ts"] >= T - Q, "vol_quote"] = 1e15
    assert gates(make(b15=b), T).u2 == gates(make(b15=bars15(vol_q=19e6)), T).u2
    # a missing known_at column cannot be filtered point-in-time -> refuse
    sd = make()
    b15 = sd.bars15.drop(columns="known_at")
    bad = SymbolData(sd.symbol, b15, sd.bars1m, sd.funding_annot, sd.insts, [], ID_OK)
    with pytest.raises(ValueError):
        gates(bad, T)


def test_known_at_future_funding_ignored():
    sd = make()
    fa = sd.funding_annot.copy()
    hole = (fa["funding_ts"] >= T - 5 * D) & (fa["funding_ts"] < T - 4 * D)
    fa.loc[hole, "known_at"] += 10 * D  # these records are known only 10 d later: a hole at t
    r = gates(SymbolData(sd.symbol, sd.bars15, sd.bars1m, fa, sd.insts, [], ID_OK), T)
    assert gates(sd, T).u3 and not r.u3
    late = fa["funding_ts"] >= T - 12 * H
    fa.loc[late, "known_at"] += 10 * D  # the freshest records unknown: U7 fails
    assert not gates(SymbolData(sd.symbol, sd.bars15, sd.bars1m, fa, sd.insts, [], ID_OK), T).u7


def test_u6_notice_activates_at_known_at():
    text = "Binance Futures will delist ABCUSDT USDⓈ-M perpetual contract"
    nv = NoticeVersion("n1", 1, "delisting", T - 5 * H, T - 4 * H, text, "h")
    r = gates(make(notices=[nv]), T)
    assert not r.u6 and exit_triggered(r) and "U6" in r.reasons
    late = NoticeVersion("n2", 1, "delisting", T - 5 * H, T + H, text, "h")
    assert gates(make(notices=[late]), T).u6  # known after t - 1 min: ignored


@pytest.mark.parametrize(
    ("sym", "ident", "why"),
    [
        ("ABCUSDT", ID_OK, None),
        ("1000PEPEUSDT", Identity("1000PEPE", "PERPETUAL", "USDT", "COIN", 1000.0, True), None),
        ("BTCUSDT", Identity("BTC", "PERPETUAL", "USDT", "COIN", 1.0, True), "hedge_only"),
        ("ABCUSDT", Identity("ABC", "CURRENT_QUARTER", "USDT", "COIN", 1.0, True), "not_perpetual"),
        ("ABCUSDT", Identity("ABC", "PERPETUAL", "USDC", "COIN", 1.0, True), "quote_not_usdt"),
        ("BTCDOMUSDT", Identity("BTCDOM", "PERPETUAL", "USDT", "INDEX", 1.0, True), "underlying_not_coin"),
        ("USDCUSDT", Identity("USDC", "PERPETUAL", "USDT", "COIN", 1.0, True), "stablecoin_base"),
        ("ABCUSDT", Identity("ABC", "PERPETUAL", "USDT", None, 1.0, True), "identity_unknown"),
        ("ABCUSDT", Identity("ABC", "PERPETUAL", "USDT", "COIN", 1.0, False), "identity_unverified"),
        ("ABCUSDT", None, "identity_unverified"),
        ("AERGOUSDT", Identity("AERGO", "PERPETUAL", "USDT", "COIN", 1.0, True), "unknown_type_symbol"),
    ],
)
def test_instrument_exclusions(sym, ident, why):
    assert instrument_exclusion(sym, ident) == why


def test_stablecoin_list_and_excluded_gate_row():
    for b in ("USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE"):
        assert instrument_exclusion(b + "USDT", Identity(b, "PERPETUAL", "USDT", "COIN", 1.0, True))
    r = gates(make(ident=None), T)
    assert not any((r.u1, r.u2, r.u3, r.u4, r.u5, r.u6, r.u7, r.u8))
    assert r.reasons == ("excluded:identity_unverified",)


def _month_counts(year, month, n, override=None):
    out = {}
    day = pd.Timestamp(year=year, month=month, day=1)
    while day.month == month:
        for hh in (0, 8, 16):
            out[to_ns(day + pd.Timedelta(hours=hh, minutes=5))] = n
        day += pd.Timedelta(days=1)
    if override:
        out[to_ns(override[0])] = override[1]
    return out


def test_d0_rule():
    c = {}
    c.update(_month_counts(2020, 12, 99))  # before D0: ignored
    c.update(_month_counts(2021, 1, 29))
    c.update(_month_counts(2021, 2, 40, override=("2021-02-17 08:05", 29)))  # one decision below 30
    c.update(_month_counts(2021, 3, 30))  # exactly 30 everywhere
    assert d0_month(c) == "2021-03-01"
    assert d0_month(_month_counts(2021, 3, 30, override=("2021-03-31 16:05", 29))) is None
    inc = _month_counts(2021, 4, 50)
    del inc[to_ns("2021-04-10 00:05")]  # a missing decision: the month never qualifies
    assert d0_month(inc) is None
    assert d0_month(_month_counts(2020, 12, 99)) is None
    assert g_d0(GateRow(True, True, True, True, False, False, False, False, (), None))
    assert not g_d0(GateRow(True, True, False, True, True, True, True, True, (), None))


def test_u7_invalid_last_record_is_missing():
    f = funding()
    f = f[f["funding_ts"] <= T - 10 * H].reset_index(drop=True)  # last valid record 10 h old: stale
    extra = pd.DataFrame(dict(funding_ts=[f["funding_ts"].iloc[-1] + 6 * H], rate=1e-4,
                              interval_h_explicit=pd.array([None], dtype="Int64")))
    f2 = pd.concat([f, extra], ignore_index=True)  # a fresh 6 h off-schedule record -> invalid
    sd = make(fund=f2)
    assert bool(sd.funding_annot["invalid"].iloc[-1])
    r = gates(sd, T)
    assert not r.u7 and r.u3  # U7 judged on the last valid record; U3 unaffected


def test_bars1m_requires_known_at():
    sd = make()
    b1 = sd.bars1m.drop(columns="known_at")
    bad = SymbolData(sd.symbol, sd.bars15, b1, sd.funding_annot, sd.insts, [], ID_OK)
    with pytest.raises(ValueError):
        gates(bad, T)


def test_funding_after_last_trade_ts_counts_before_end_known_at():
    b = bars15(end=T - 3 * D)
    b = b[b["open_ts"] < T - 3 * D]
    insts = build_instances(b, T + 10 * D)  # last_trade_ts = t - 3 d, end_known_at = t + 4 d > t
    assert insts[0].last_trade_ts < T - 2 * D and insts[0].end_known_at > T
    sd = make(b15=b, insts=insts)
    assert not bool(sd.funding_annot["in_instance"].iloc[-1])  # retrospective membership drops the tail
    r = gates(sd, T)
    assert r.u3 and r.u7  # the tail records are known at t and the end is not yet known


def test_future_dated_rows_change_no_gate():
    base = gates(make(), T)
    b = bars15()
    fut = b["open_ts"] >= T - Q
    b.loc[fut, ["vol_quote", "vol_base"]] = 1e15
    b.loc[fut, "c"] = 1.0
    sd = make(b15=b)
    fa = sd.funding_annot
    far = fa["funding_ts"] > T
    assert far.any()
    fa.loc[far, "rate"] = 9.0
    assert gates(sd, T) == base


def test_boundaries_u1_60d_and_u3_30d():
    b = bars15(start=T - 60 * D)
    insts = build_instances(b, T + 3 * H)
    assert gates(make(b15=b, insts=insts, fund=funding(start=T - 60 * D)), T).u1
    b = bars15(start=T - 60 * D + Q)
    assert not gates(make(b15=b, fund=funding(start=T - 60 * D)), T).u1
    assert gates(make(fund=funding(start=T - 30 * D)), T).u3
    assert not gates(make(fund=funding(start=T - 30 * D + 1)), T).u3


# --- Task 19b: incremental pit_funding (identical output) --------------------------------------------------


def _random_funding(seed: int) -> tuple[pd.DataFrame, uni.Instance]:
    """Records with 8 h then 4 h spacing, missing / unreconciled / implicit intervals, late (non-monotone)
    known_at values and an instance start inside the series (rows before it are not members)."""
    g = np.random.default_rng(seed)
    ts = np.concatenate([np.arange(S, S + 20 * D, 8 * H), np.arange(S + 20 * D, S + 40 * D, 4 * H)])
    ts = ts + g.choice([0, 0, 0, 30 * 10**9, 90 * 10**9], len(ts))  # some off-grid (beyond +-60 s) stamps
    ts = np.sort(np.delete(ts, g.choice(len(ts), 6, replace=False)))  # gaps
    ex = pd.array(np.where(ts < S + 20 * D, 8, 4), dtype="Int64")
    ex[g.random(len(ts)) < 0.2] = pd.NA  # implicit intervals (DEC-29 inference)
    ex[g.random(len(ts)) < 0.05] = 2  # unreconciled explicit value
    ka = ts + MIN
    late = g.random(len(ts)) < (0.1 if seed % 2 else 0.0)  # REST-style late arrivals (odd seeds only)
    ka[late] += g.integers(1, 30, late.sum()) * H
    f = pd.DataFrame(dict(funding_ts=ts, rate=g.normal(1e-4, 3e-4, len(ts)), interval_h_explicit=ex,
                          known_at=ka, extra=g.random(len(ts))))
    f = f.sample(frac=1.0, random_state=seed).reset_index(drop=True)  # input order must not matter
    return f, uni.Instance(seed % 3, int(S + (seed % 4) * D), None, None)


@pytest.mark.parametrize("seed", range(6))
def test_pit_funding_incremental_equivalence(seed):
    f, cur = _random_funding(seed)
    grid = np.arange(S - D, S + 45 * D, 9 * H + 7 * MIN, dtype=np.int64)
    refs = {int(t): uni._pit_funding_reference(f, cur, int(t)) for t in grid}
    assert uni._prefix(f, cur) is not None  # the incremental path is exercised (not only the fallback)
    for _ in range(2):  # second sweep: memo hits
        for t in grid:
            got = uni.pit_funding(f, cur, int(t))
            pd.testing.assert_frame_equal(got, refs[int(t)], check_exact=True)
    # another object with the same content, and a changed copy, are both exact
    g2 = f.copy()
    g2.loc[g2.index[0], "rate"] += 1e-3
    for t in grid[::6]:
        pd.testing.assert_frame_equal(uni.pit_funding(f.copy(), cur, int(t)), refs[int(t)], check_exact=True)
        pd.testing.assert_frame_equal(uni.pit_funding(g2, cur, int(t)),
                                      uni._pit_funding_reference(g2, cur, int(t)), check_exact=True)


def test_pit_funding_duplicate_timestamps_use_reference():
    f, cur = _random_funding(2)
    f = pd.concat([f, f.iloc[[3]]], ignore_index=True)  # a duplicated funding_ts: no fast path
    t = int(S + 30 * D)
    pd.testing.assert_frame_equal(uni.pit_funding(f, cur, t), uni._pit_funding_reference(f, cur, t),
                                  check_exact=True)
