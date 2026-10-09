"""Kraken PF_ (linear multi-collateral) funding semantics, locked by tests.

Verified against Kraken's "Linear Multi-Collateral Derivatives Contract Specifications" and the
historical-funding-rates API reference (see docs/results/v4-funding-semantics.md):
- hourly funding period; the API `timestamp` is the START of the period the rate applies to;
- positive rate: longs pay shorts; negative rate: shorts pay longs;
- `fundingRate` (absolute) = USD received by a 1-unit short held for the whole 1-hour period;
- `relativeFundingRate` = absolute / reference price (decimal fraction per hour, not percent);
- funding accrues continuously and settles at the end of the period or when the net position
  changes, pro rata to the time the position was held within the period.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from luna.v4.funding import (
    HOURS_PER_YEAR,
    PERIOD,
    accrue_funding,
    annualize,
    funding_cashflow,
    known_rates,
    parse_funding,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "v4_historicalfundingrates_PF_XBTUSD.json"
T0 = pd.Timestamp("2026-03-02T09:00:00Z")


def _rates(abs_rates, rel_rates=None, start=T0):
    idx = pd.date_range(start, periods=len(abs_rates), freq="1h")
    rel = rel_rates if rel_rates is not None else [a / 100.0 for a in abs_rates]
    return pd.DataFrame({"funding_abs": abs_rates, "funding_rel": rel}, index=idx)


# --- parsing the real API shape -------------------------------------------------------------


def test_parse_fixture_indexes_by_period_start_utc_hourly():
    df = parse_funding(json.loads(FIXTURE.read_text()))
    assert df.index[0] == T0
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert (df.index.to_series().diff().dropna() == PERIOD).all()
    assert list(df.columns) == ["funding_abs", "funding_rel"]
    assert (df.index + PERIOD)[0] == T0 + PERIOD


def test_fixture_relative_rate_is_absolute_over_price_decimal_per_hour():
    df = parse_funding(json.loads(FIXTURE.read_text()))
    implied_price = df["funding_abs"] / df["funding_rel"]
    # BTC traded ~60k-70k USD in early March 2026; a percent-scaled relative rate would imply ~600-700
    assert implied_price.between(30_000, 150_000).all()
    assert (df["funding_abs"] * df["funding_rel"] >= 0).all()  # same sign


def test_parse_drops_duplicates_and_sorts():
    payload = {"rates": [
        {"timestamp": "2026-03-02T10:00:00Z", "fundingRate": 2.0, "relativeFundingRate": 2e-5},
        {"timestamp": "2026-03-02T09:00:00Z", "fundingRate": 1.0, "relativeFundingRate": 1e-5},
        {"timestamp": "2026-03-02T10:00:00Z", "fundingRate": 3.0, "relativeFundingRate": 3e-5},
    ]}
    df = parse_funding(payload)
    assert list(df.index) == [T0, T0 + PERIOD]
    assert df.loc[T0 + PERIOD, "funding_abs"] == 3.0  # last occurrence wins


# --- sign and magnitude ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qty", "rate", "expected"),
    [
        (+1.0, +2.0, -2.0),  # positive funding: long pays
        (-1.0, +2.0, +2.0),  # positive funding: short receives
        (+1.0, -2.0, +2.0),  # negative funding: long receives
        (-1.0, -2.0, -2.0),  # negative funding: short pays
        (-0.5, +2.0, +1.0),  # magnitude scales with units
        (0.0, +2.0, 0.0),
    ],
)
def test_cashflow_sign_and_magnitude(qty, rate, expected):
    assert funding_cashflow(qty, rate) == pytest.approx(expected)


def test_cashflow_is_prorated_by_fraction_of_period_held():
    assert funding_cashflow(-2.0, 1.5, held_fraction=0.25) == pytest.approx(0.75)
    with pytest.raises(ValueError):
        funding_cashflow(1.0, 1.0, held_fraction=1.5)


def test_hand_calc_short_one_unit_24_hours_relative_1e4_at_101():
    # spec §5.1 T3: r = +0.0001 per hour, price 101, short 1 unit for 24 full periods -> +0.2424
    abs_rate = 0.0001 * 101
    rates = _rates([abs_rate] * 24, [0.0001] * 24)
    total = accrue_funding([(T0, -1.0)], rates, T0 + 24 * PERIOD)["cashflow"].sum()
    assert total == pytest.approx(0.2424)
    rates_neg = _rates([-abs_rate] * 24, [-0.0001] * 24)
    neg = accrue_funding([(T0, -1.0)], rates_neg, T0 + 24 * PERIOD)["cashflow"].sum()
    assert neg == pytest.approx(-0.2424)


# --- timing: which period, position changes, settlement --------------------------------------


def test_position_opened_at_period_start_gets_that_periods_rate():
    rates = _rates([1.0, 10.0])
    out = accrue_funding([(T0, -1.0)], rates, T0 + PERIOD)
    assert out["cashflow"].sum() == pytest.approx(1.0)  # only the period starting at T0
    assert list(out["settle_ts"]) == [T0 + PERIOD]


def test_position_opened_mid_period_accrues_pro_rata_and_settles_at_period_end():
    rates = _rates([4.0, 8.0])
    out = accrue_funding([(T0 + pd.Timedelta("45min"), -1.0)], rates, T0 + 2 * PERIOD)
    assert out["cashflow"].tolist() == pytest.approx([1.0, 8.0])  # 15 min of 4.0, then full 8.0
    assert list(out["settle_ts"]) == [T0 + PERIOD, T0 + 2 * PERIOD]


def test_position_change_settles_accrued_funding_at_the_change():
    rates = _rates([4.0])
    # short 1 from T0, increase to short 3 at T0+30min, close at T0+45min
    path = [(T0, -1.0), (T0 + pd.Timedelta("30min"), -3.0), (T0 + pd.Timedelta("45min"), 0.0)]
    out = accrue_funding(path, rates, T0 + PERIOD)
    assert out["cashflow"].tolist() == pytest.approx([2.0, 3.0])  # 0.5h*1*4 ; 0.25h*3*4
    assert list(out["settle_ts"]) == [T0 + pd.Timedelta("30min"), T0 + pd.Timedelta("45min")]


def test_position_closed_before_period_ends_gets_no_later_funding():
    rates = _rates([4.0, 100.0])
    out = accrue_funding([(T0, -1.0), (T0 + PERIOD, 0.0)], rates, T0 + 2 * PERIOD)
    assert out["cashflow"].sum() == pytest.approx(4.0)


def test_missing_period_is_zero_cashflow_and_counted_unknown():
    rates = _rates([4.0, 4.0, 4.0]).drop(T0 + PERIOD)  # API gap: one hour missing
    out = accrue_funding([(T0, -1.0)], rates, T0 + 3 * PERIOD)
    assert out["cashflow"].sum() == pytest.approx(8.0)
    assert out.attrs["unknown_hours"] == pytest.approx(1.0)
    assert out.attrs["held_hours"] == pytest.approx(3.0)


# --- look-ahead: which rates are known at decision time t ------------------------------------


def test_known_rates_only_include_periods_that_have_ended():
    rates = _rates([1.0, 2.0, 3.0])
    t = T0 + 2 * PERIOD  # periods starting T0 and T0+1h have ended; T0+2h has just started
    assert list(known_rates(rates, t).index) == [T0, T0 + PERIOD]
    assert list(known_rates(rates, t - pd.Timedelta("1ns")).index) == [T0]


def test_annualize_hourly_relative_rate():
    assert HOURS_PER_YEAR == 8760
    assert annualize(1e-5) == pytest.approx(0.0876)
