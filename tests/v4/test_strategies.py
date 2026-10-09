"""The closed V4 config set (spec R6, §3, §4) and its null models."""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd
import pytest

from luna.v4 import strategies as st
from luna.v4.accounting import CarryState
from luna.v4.signals import COLUMNS

T = pd.Timestamp("2026-03-02T00:00:00Z")  # a Monday


def _table(rows: dict[tuple[pd.Timestamp, str], dict]) -> pd.DataFrame:
    base = {c: np.nan for c in COLUMNS}
    base.update(
        {
            "elig_h1": True,
            "elig_h2_7": True,
            "elig_h2_30": True,
            "F7_n": 168,
            "listed_perp": True,
            "listed_spot": True,
        }
    )
    df = pd.DataFrame.from_dict({k: {**base, **v} for k, v in rows.items()}, orient="index")
    df.index = pd.MultiIndex.from_tuples(df.index, names=["day", "asset"])
    return df[COLUMNS].sort_index()


def _state(pos=None, last_exit=None):
    return CarryState(dict(pos or {}), dict(last_exit or {}), 3)


def test_registry_is_exactly_the_seven_configs():
    assert list(st.CONFIGS) == [
        "H1-Y10",
        "H1-Y20",
        "H2-L7-k1",
        "H2-L7-k2",
        "H2-L30-k1",
        "H2-L30-k2",
        "H5-HFWB",
    ]
    assert st.CONFIGS["H1-Y20"].y == 0.20 and not st.CONFIGS["H1-Y20"].basis_filter
    assert st.CONFIGS["H5-HFWB"].y == 0.10 and st.CONFIGS["H5-HFWB"].basis_filter
    assert (st.CONFIGS["H2-L30-k2"].lookback, st.CONFIGS["H2-L30-k2"].k) == (30, 2)
    assert st.NULL_SEEDS == (1, 2, 3, 4, 5)
    assert not any(st.is_null(c) for c in st.CONFIGS)
    assert st.is_null("N-SHUFFLE:3:H2-L7-k1") and st.is_null("N-ALWAYS:H1-Y10")


def test_h1_entries_by_f7_descending_ties_alphabetical_above_threshold():
    tab = _table(
        {
            (T, "AAA"): {"F7": 0.15},
            (T, "BBB"): {"F7": 0.30},
            (T, "CCC"): {"F7": 0.15},
            (T, "DDD"): {"F7": 0.05},
            (T, "EEE"): {"F7": 0.50, "elig_h1": False},
        }
    )
    dec = st.CarryRule(st.CONFIGS["H1-Y10"], tab).decide(T, _state())
    assert dec.entries == ["BBB", "AAA", "CCC"] and dec.exits == []
    dec20 = st.CarryRule(st.CONFIGS["H1-Y20"], tab).decide(T, _state())
    assert dec20.entries == ["BBB"]


def test_h1_exits_on_nonpositive_f7_or_90_days_and_cooldown_7_days():
    tab = _table(
        {
            (T, "AAA"): {"F7": 0.0},
            (T, "BBB"): {"F7": 0.4},
            (T, "CCC"): {"F7": 0.4},
            (T, "DDD"): {"F7": 0.4},
            (T, "EEE"): {"F7": np.nan},
        }
    )
    state = _state(
        pos={"AAA": T - pd.Timedelta("1D"), "BBB": T - pd.Timedelta("90D"), "EEE": T - pd.Timedelta("3D")},
        last_exit={"CCC": T - pd.Timedelta("6D"), "DDD": T - pd.Timedelta("7D")},
    )
    dec = st.CarryRule(st.CONFIGS["H1-Y10"], tab).decide(T, state)
    assert dec.exits == ["AAA", "BBB"]  # F7 unknown (EEE) does not trigger an exit
    assert dec.entries == ["DDD"]  # CCC still cooling down, BBB exited today


def test_h5_requires_b24_at_or_above_b30med():
    tab = _table(
        {
            (T, "AAA"): {"F7": 0.2, "B24": 0.002, "B30med": 0.001},
            (T, "BBB"): {"F7": 0.3, "B24": 0.0005, "B30med": 0.001},
            (T, "CCC"): {"F7": 0.4, "B24": np.nan, "B30med": 0.001},
            (T, "DDD"): {"F7": 0.1, "B24": 0.001, "B30med": 0.001},
        }
    )
    dec = st.CarryRule(st.CONFIGS["H5-HFWB"], tab).decide(T, _state())
    assert dec.entries == ["AAA", "DDD"]


def _h2_table(t=T, n=5, **over):
    return _table(
        {(t, f"A{i}"): {"FL7": float(i), "FL30": float(-i), **over.get(f"A{i}", {})} for i in range(n)}
    )


def test_h2_long_lowest_short_highest_on_mondays_only():
    rule = st.H2Rule(st.CONFIGS["H2-L7-k2"], _h2_table())
    dec = rule.decide(T)
    assert dec.kind == "rebalance" and dec.sides == {"A0": 1, "A1": 1, "A3": -1, "A4": -1}
    assert rule.decide(T + pd.Timedelta("1D")) is None
    dec30 = st.H2Rule(st.CONFIGS["H2-L30-k1"], _h2_table()).decide(T)
    assert dec30.sides == {"A4": 1, "A0": -1}


def test_h2_ties_alphabetical_and_flat_when_fewer_than_2k_eligible():
    tab = _table({(T, "BBB"): {"FL7": 1.0}, (T, "AAA"): {"FL7": 1.0}, (T, "CCC"): {"FL7": 2.0}})
    assert st.H2Rule(st.CONFIGS["H2-L7-k1"], tab).decide(T).sides == {"AAA": 1, "CCC": -1}
    assert st.H2Rule(st.CONFIGS["H2-L7-k2"], tab).decide(T).kind == "flat"
    inel = _h2_table(A0={"elig_h2_7": False}, A1={"elig_h2_7": False})
    assert st.H2Rule(st.CONFIGS["H2-L7-k2"], inel).decide(T).kind == "flat"


def test_n_shuffle_permutes_with_the_crc32_seed():
    tab = _h2_table(n=8)
    real = st.H2Rule(st.CONFIGS["H2-L7-k2"], tab).decide(T).sides
    names = sorted(f"A{i}" for i in range(8))
    perm = list(np.random.default_rng(zlib.crc32(f"3:H2-L7-k2:{T}".encode())).permutation(names))
    expected = {perm[0]: 1, perm[1]: 1, perm[-2]: -1, perm[-1]: -1}
    assert st.H2Rule(st.CONFIGS["H2-L7-k2"], tab, shuffle_seed=3).decide(T).sides == expected
    seeds = {
        tuple(sorted(st.H2Rule(st.CONFIGS["H2-L7-k2"], tab, shuffle_seed=s).decide(T).sides.items()))
        for s in st.NULL_SEEDS
    }
    assert len(seeds) > 1 and real is not None


def test_n_always_targets_first_three_alphabetical_on_d0_and_month_firsts():
    d0 = pd.Timestamp("2026-02-27T00:00:00Z")
    rows = {}
    for d in pd.date_range(d0, periods=5, freq="D"):
        for a in ("EEE", "DDD", "CCC", "BBB", "AAA"):
            rows[(d, a)] = {
                "F7": -1.0,
                "elig_h1": not (a == "AAA" and d >= pd.Timestamp("2026-03-01T00:00Z")),
            }
    tab = _table(rows)
    rule = st.AlwaysRule(tab, d0)
    assert rule.decide(d0, _state()).entries == ["AAA", "BBB", "CCC"]
    assert rule.decide(d0 + pd.Timedelta("1D"), _state(pos={"AAA": d0})).entries == []
    held = {"AAA": d0, "BBB": d0, "CCC": d0}
    dec = rule.decide(pd.Timestamp("2026-03-01T00:00Z"), _state(pos=held))
    assert dec.exits == ["AAA"] and dec.entries == ["DDD"]  # only the changed asset trades
    old = {"AAA": d0 - pd.Timedelta("200D"), "BBB": d0, "CCC": d0}
    assert rule.decide(d0 + pd.Timedelta("1D"), _state(pos=old)).exits == []  # no 90-day cap, no F7 exit


def _elig_table(days, assets):
    return _table({(d, a): {"F7": 0.0} for d in days for a in assets})


def test_n_randentry_preserves_durations_and_constraints_reproducibly():
    days = list(pd.date_range(T, periods=120, freq="D"))
    assets = ["AAA", "BBB", "CCC", "DDD"]
    tab = _elig_table(days, assets)
    trades = [{"asset": "AAA", "entry_t": days[i], "exit_t": days[i + 10]} for i in (0, 20, 40, 60)]
    trades.append({"asset": "BBB", "entry_t": days[100], "exit_t": None})
    s1, dropped1 = st.randentry_schedule(trades, tab, days, "H1-Y10", seed=1)
    s1b, _ = st.randentry_schedule(trades, tab, days, "H1-Y10", seed=1)
    s2, _ = st.randentry_schedule(trades, tab, days, "H1-Y10", seed=2)
    assert s1 == s1b and s1 != s2 and dropped1 == 0
    assert sorted(x["exit_t"] - x["entry_t"] for x in s1) == sorted(
        [pd.Timedelta("10D")] * 4 + [days[-1] + pd.Timedelta("1D") - days[100]]
    )
    for x in s1:
        assert x["asset"] in assets and x["entry_t"] in days
        overlap = [y for y in s1 if y["entry_t"] < x["exit_t"] and x["entry_t"] < y["exit_t"]]
        assert len(overlap) <= 3
        for y in s1:
            if y is not x and y["asset"] == x["asset"]:
                assert y["entry_t"] >= x["exit_t"] + pd.Timedelta("7D") or x["entry_t"] >= y[
                    "exit_t"
                ] + pd.Timedelta("7D")


def test_n_randentry_drops_and_counts_after_100_failed_redraws():
    days = list(pd.date_range(T, periods=10, freq="D"))
    tab = _elig_table(days, ["AAA"])
    trades = [{"asset": "AAA", "entry_t": days[0], "exit_t": days[9]}] * 2
    sched, dropped = st.randentry_schedule(trades, tab, days, "H1-Y10", seed=1)
    assert len(sched) == 1 and dropped == 1


def test_n_randentry_rng_is_seeded_with_seed_and_crc32_of_config():
    days = list(pd.date_range(T, periods=30, freq="D"))
    tab = _elig_table(days, ["AAA", "BBB", "CCC"])
    trades = [{"asset": "AAA", "entry_t": days[0], "exit_t": days[5]}]
    sched, _ = st.randentry_schedule(trades, tab, days, "H5-HFWB", seed=4)
    rng = np.random.default_rng([4, zlib.crc32(b"H5-HFWB")])
    day = days[int(rng.integers(len(days)))]
    asset = ["AAA", "BBB", "CCC"][int(rng.integers(3))]
    assert (sched[0]["entry_t"], sched[0]["asset"]) == (day, asset)


def test_schedule_rule_enters_and_exits_on_schedule():
    sched = [{"asset": "AAA", "entry_t": T, "exit_t": T + pd.Timedelta("3D")}]
    rule = st.ScheduleRule(sched)
    assert rule.decide(T, _state()).entries == ["AAA"]
    assert rule.decide(T + pd.Timedelta("3D"), _state(pos={"AAA": T})).exits == ["AAA"]
    assert rule.decide(T + pd.Timedelta("1D"), _state(pos={"AAA": T})).exits == []
    # the exit day was skipped (no price): the exit fires on the next decision while still held
    assert rule.decide(T + pd.Timedelta("4D"), _state(pos={"AAA": T})).exits == ["AAA"]
    assert rule.decide(T + pd.Timedelta("4D"), _state()).exits == []


def test_schedule_rule_exit_belongs_to_the_entry_that_is_held():
    sched = [{"asset": "AAA", "entry_t": T, "exit_t": T + pd.Timedelta("3D")},
             {"asset": "AAA", "entry_t": T + pd.Timedelta("10D"), "exit_t": T + pd.Timedelta("20D")}]
    rule = st.ScheduleRule(sched)
    later = T + pd.Timedelta("10D")
    assert rule.decide(T + pd.Timedelta("12D"), _state(pos={"AAA": later})).exits == []
    assert rule.decide(T + pd.Timedelta("20D"), _state(pos={"AAA": later})).exits == ["AAA"]


def test_n_shuffle_key_is_str_of_t_exactly():
    assert str(T) == "2026-03-02 00:00:00+00:00"


@pytest.mark.parametrize("cid", ["H1-Y10", "H2-L7-k1"])
def test_null_ids(cid):
    assert st.null_ids(cid)[0].startswith("N-")
    assert all(st.is_null(n) for n in st.null_ids(cid))
