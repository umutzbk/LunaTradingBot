"""V4 accounting (spec §5.1, §5.2): hand calculations T1–T5 with exact numbers, then the carry and
H2 daily simulators on a synthetic market (no real data)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from luna.v4 import accounting as acc
from luna.v4.costs import cost_rows
from luna.v4.funding import accrue_funding

T0 = pd.Timestamp("2026-03-02T00:00:00Z")
PRIMARY = cost_rows()["PRIMARY"]


# --- T1–T5 (spec §5.1) --------------------------------------------------------------------------


def _round_trip(book, q_s, q_p, fees=None):
    """Open at spot 100 / perp 101, close at spot 110 / perp 110.55."""
    f = fees or {"spot": 0.0, "perp": 0.0, "spot_half": 0.0, "perp_half": 0.0}
    book.trade("X", "spot", q_s, 100.0, f["spot"], f["spot_half"])
    book.trade("X", "perp", q_p, 101.0, f["perp"], f["perp_half"])
    book.trade("X", "spot", -q_s, 110.0, f["spot"], f["spot_half"])
    book.trade("X", "perp", -q_p, 110.55, f["perp"], f["perp_half"])


def test_t1_unit_hedge_price_pnl_is_0_45():
    book = acc.Book(spot_cash=1000.0, perp_cash=1000.0)
    _round_trip(book, 1.0, -1.0)
    assert book.spot_cash - 1000.0 == pytest.approx(10.0)
    assert book.perp_cash - 1000.0 == pytest.approx(-9.55)
    assert book.equity({}) - 2000.0 == pytest.approx(0.45)


def test_t2_usd_hedge_price_pnl_is_0_5446():
    q_s, q_p = acc.hedge_qty(100.0, 100.0, 101.0)
    assert q_s == pytest.approx(1.0) and q_p == pytest.approx(-100 / 101)
    book = acc.Book(spot_cash=1000.0, perp_cash=1000.0)
    _round_trip(book, q_s, q_p)
    assert book.perp_cash - 1000.0 == pytest.approx(-9.4554, abs=5e-5)
    assert book.equity({}) - 2000.0 == pytest.approx(0.5446, abs=5e-5)
    assert book.equity({}) - 2000.0 == pytest.approx(100 * (110 / 100 - 110.55 / 101))


def test_t2_price_pnl_splits_into_basis_and_residual_delta():
    basis, residual = acc.price_split(1.0, -100 / 101, 110.0 - 100.0, 110.55 - 101.0)
    assert basis == pytest.approx(0.44554, abs=5e-6)
    assert residual == pytest.approx(0.09901, abs=5e-6)
    assert basis + residual == pytest.approx(0.54455, abs=5e-6)
    # more perp than spot: the residual is driven by the perp price
    b2, r2 = acc.price_split(1.0, -1.25, 10.0, 9.55)
    assert b2 == pytest.approx(-1.0 * (9.55 - 10.0)) and r2 == pytest.approx((1.0 - 1.25) * 9.55)


def _t3_rates(sign=1.0):
    idx = pd.date_range(T0, periods=24, freq="1h")
    return pd.DataFrame({"funding_abs": sign * 0.0001 * 101, "funding_rel": sign * 0.0001}, index=idx)


def test_t3_funding_short_receives_0_2424():
    book = acc.Book(spot_cash=0.0, perp_cash=0.0)
    book.add_funding(accrue_funding([(T0, -1.0)], _t3_rates(), T0 + pd.Timedelta("24h"))["cashflow"].sum())
    assert book.perp_cash == pytest.approx(0.2424)
    neg = accrue_funding([(T0, -1.0)], _t3_rates(-1.0), T0 + pd.Timedelta("24h"))["cashflow"].sum()
    assert neg == pytest.approx(-0.2424)


T4_FEES = {"spot": 0.008, "perp": 0.0005, "spot_half": 0.0010, "perp_half": 0.0005}


def test_t4_costs_total_2_10155():
    assert PRIMARY.fee("spot") == 0.008 and PRIMARY.fee("perp") == 0.0005
    assert PRIMARY.half_spread("spot", None) == pytest.approx(0.0010)
    assert PRIMARY.half_spread("perp", 0.0010 / 2) == pytest.approx(0.0005)  # perp 10 bps full
    book = acc.Book(spot_cash=1000.0, perp_cash=1000.0)
    _round_trip(book, 1.0, -1.0, T4_FEES)
    c = book.costs
    assert c["fees"]["spot"] == pytest.approx(0.80 + 0.88)
    assert c["fees"]["perp"] == pytest.approx(0.0505 + 0.055275)
    assert c["spread"]["spot"] == pytest.approx(0.10 + 0.11)
    assert c["spread"]["perp"] == pytest.approx(0.0505 + 0.055275)
    assert sum(c["fees"].values()) + sum(c["spread"].values()) == pytest.approx(2.10155)


def test_t5_net_is_minus_1_40915():
    book = acc.Book(spot_cash=1000.0, perp_cash=1000.0)
    f = T4_FEES
    book.trade("X", "spot", 1.0, 100.0, f["spot"], f["spot_half"])
    book.trade("X", "perp", -1.0, 101.0, f["perp"], f["perp_half"])
    book.add_funding(accrue_funding([(T0, -1.0)], _t3_rates(), T0 + pd.Timedelta("24h"))["cashflow"].sum())
    book.trade("X", "spot", -1.0, 110.0, f["spot"], f["spot_half"])
    book.trade("X", "perp", 1.0, 110.55, f["perp"], f["perp_half"])
    assert book.equity({}) - 2000.0 == pytest.approx(-1.40915)


# --- order rounding ------------------------------------------------------------------------------


def test_round_qty_floors_toward_zero_on_step_and_checks_minimums():
    meta = acc.LegMeta(step=0.01, min_qty=0.05, min_cost=5.0)
    assert acc.round_qty(1.239, meta) == pytest.approx(1.23)
    assert acc.round_qty(-1.239, meta) == pytest.approx(-1.23)
    assert acc.order_ok(0.04, 1000.0, meta) is False  # below min qty
    assert acc.order_ok(0.05, 50.0, meta) is False  # 2.5 USD below min cost
    assert acc.order_ok(0.10, 50.0, meta) is True


# --- a synthetic market --------------------------------------------------------------------------


@dataclass
class FakeMarket:
    """Constant or scripted prices; quotes at t + 15m; funding per asset."""

    spot: dict = field(default_factory=dict)  # asset -> price or {day: price}
    perp: dict = field(default_factory=dict)
    funding_rates: dict = field(default_factory=dict)
    missing: set = field(default_factory=set)  # (asset, leg, day)
    perp_high: dict = field(default_factory=dict)  # (asset, day) -> high
    perp_low: dict = field(default_factory=dict)
    outages: set = field(default_factory=set)
    marks: dict = field(default_factory=dict)  # asset -> {day: mark}; default = perp price
    forward: bool = False

    def _px(self, table, asset, day):
        v = table.get(asset)
        if isinstance(v, dict):
            keys = [k for k in sorted(v) if k <= day]
            return v[keys[-1]] if keys else None
        return v

    def quote(self, asset, leg, t, qty=0.0):
        if (asset, leg, t) in self.missing:
            return None
        px = self._px(self.spot if leg == "spot" else self.perp, asset, t)
        if px is None:
            return None
        obs = None if leg == "spot" else 0.0005
        return acc.Quote(mid=px, half_spread_obs=obs, ts=t + pd.Timedelta("15min"))

    def value(self, asset, leg, day):
        return self._px(self.spot if leg == "spot" else self.perp, asset, day + pd.Timedelta("1D"))

    def extremes(self, asset, day):
        p = self._px(self.perp, asset, day)
        s = self._px(self.spot, asset, day)
        return {
            "perp_high": self.perp_high.get((asset, day), p),
            "perp_low": self.perp_low.get((asset, day), p),
            "spot_low": s,
            "spot_high": s,
        }

    def funding(self, asset):
        return self.funding_rates.get(
            asset, pd.DataFrame({"funding_abs": [], "funding_rel": []}, index=pd.DatetimeIndex([], tz="UTC"))
        )

    def mark(self, asset, t):
        if asset in self.marks:
            return self._px(self.marks, asset, t)
        return self._px(self.perp, asset, t)

    def suspended(self, asset, t):
        return False

    def outage(self, day):
        return day in self.outages


def _days(n):
    return list(pd.date_range(T0, periods=n, freq="D"))


def _rates(asset_rel=1e-5, price=100.0, n_days=40):
    idx = pd.date_range(T0 - pd.Timedelta("1D"), periods=(n_days + 1) * 24, freq="1h")
    return pd.DataFrame({"funding_abs": asset_rel * price, "funding_rel": asset_rel}, index=idx)


class Scripted:
    """Carry decider: enter given assets on given days, exit on given days."""

    def __init__(self, entries=None, exits=None):
        self.entries, self.exits = entries or {}, exits or {}

    def decide(self, t, state):
        return acc.CarryDecision(exits=self.exits.get(t, []), entries=self.entries.get(t, []))


def _carry(market, decider, days, row=PRIMARY, meta=None):
    return acc.run_carry(market, decider, days, row, meta or {})


# --- carry simulator -------------------------------------------------------------------------------


def test_carry_entry_sizes_slot_at_30pct_of_spot_sleeve_usd_hedged():
    m = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 101.0})
    res = _carry(m, Scripted(entries={T0: ["AAA"]}), _days(2))
    pos = res.trades[0]
    assert pos["asset"] == "AAA"
    assert pos["q_spot"] == pytest.approx(0.30 * 265.0 / 100.0)
    assert pos["q_perp"] == pytest.approx(-0.30 * 265.0 / 101.0)
    d0 = res.daily.iloc[0]
    assert d0["gross"] == pytest.approx(2 * 79.5, rel=1e-9)
    assert d0["net"] == pytest.approx(0.0, abs=1e-9)
    assert res.daily["active"].tolist() == [True, True]


def test_carry_daily_decomposition_adds_up_to_equity_change():
    m = FakeMarket(
        spot={"AAA": {T0: 100.0, T0 + pd.Timedelta("2D"): 120.0}},
        perp={"AAA": {T0: 101.0, T0 + pd.Timedelta("2D"): 120.5}},
        funding_rates={"AAA": _rates(2e-5)},
    )
    res = _carry(m, Scripted(entries={T0: ["AAA"]}, exits={T0 + pd.Timedelta("3D"): ["AAA"]}), _days(5))
    d = res.daily
    comp = (
        d["spot_price_pnl"]
        + d["perp_price_pnl"]
        + d["funding_cf"]
        - d["fees"]
        - d["spread_slippage"]
        - d["liquidation_penalty"]
    )
    prev = d["equity"].shift(1).fillna(530.0)
    np.testing.assert_allclose(comp, d["equity"] - prev, atol=1e-9)
    np.testing.assert_allclose(d["ret"], d["equity"] / prev - 1, atol=1e-12)
    np.testing.assert_allclose(d["basis_contribution"] + d["residual_delta_pnl"],
                               d["spot_price_pnl"] + d["perp_price_pnl"], atol=1e-9)
    assert d["basis_contribution"].abs().sum() > 0
    assert d["funding_cf"].iloc[1] > 0 and d["funding_cf"].iloc[4] == 0
    assert res.trades[0]["exit_t"] == T0 + pd.Timedelta("3D")


def test_carry_funding_unknown_hours_are_counted():
    rates = _rates(2e-5).drop(pd.date_range(T0 + pd.Timedelta("1D"), periods=3, freq="1h"))
    m = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 100.0}, funding_rates={"AAA": rates})
    res = _carry(m, Scripted(entries={T0: ["AAA"]}), _days(3))
    assert res.counters["funding_unknown_hours"] == pytest.approx(3.0)
    assert res.counters["funding_held_hours"] == pytest.approx(3 * 24 - 0.25)


def test_carry_entry_is_all_or_nothing_when_a_leg_has_no_price():
    m = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 101.0}, missing={("AAA", "perp", T0)})
    res = _carry(m, Scripted(entries={T0: ["AAA"]}), _days(2))
    assert res.trades == []
    assert res.counters["skipped_actions"] == 1
    assert res.daily["equity"].tolist() == [530.0, 530.0]


def test_carry_entry_below_minimum_order_is_skipped_and_counted():
    m = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 101.0})
    meta = {"AAA": {"spot": acc.LegMeta(step=1.0, min_qty=1.0), "perp": acc.LegMeta(step=0.001)}}
    res = _carry(m, Scripted(entries={T0: ["AAA"]}), _days(1), meta=meta)
    assert res.trades == [] and res.counters["min_order_skips"] == 1


def test_carry_at_most_three_slots():
    assets = ["AAA", "BBB", "CCC", "DDD"]
    m = FakeMarket(spot={a: 100.0 for a in assets}, perp={a: 100.0 for a in assets})
    res = _carry(m, Scripted(entries={T0: assets}), _days(1))
    assert [t["asset"] for t in res.trades] == ["AAA", "BBB", "CCC"]


def test_carry_hedge_drift_over_5pct_rebalances_the_perp_leg():
    m = FakeMarket(
        spot={"AAA": {T0: 100.0, T0 + pd.Timedelta("1D"): 110.0}},
        perp={"AAA": {T0: 100.0, T0 + pd.Timedelta("1D"): 100.0}},
    )
    res = _carry(m, Scripted(entries={T0: ["AAA"]}), _days(3))
    assert res.counters["hedge_rebalances"] == 1
    d2 = res.daily.iloc[1]
    assert abs(d2["net"]) / d2["gross"] < 1e-9  # rebalanced on day 2 at 00:00


def test_carry_margin_buffer_reduces_both_legs_to_ratio_one():
    # perp leg rallies: perp_equity / perp_notional falls below 0.50
    m = FakeMarket(
        spot={"AAA": {T0: 100.0, T0 + pd.Timedelta("1D"): 180.0}},
        perp={"AAA": {T0: 100.0, T0 + pd.Timedelta("1D"): 180.0}},
    )
    p = acc.CarryParams(e0=200.0, spot_share=0.5, slot_frac=0.9, max_spot_util=0.9)  # perp lev ~0.9
    res = acc.run_carry(m, Scripted(entries={T0: ["AAA"]}), _days(3), PRIMARY, {}, params=p)
    assert res.counters["margin_reductions"] == 1
    assert res.margin_ratio_after[T0 + pd.Timedelta("1D")] == pytest.approx(1.0, rel=1e-6)


def test_carry_margin_ratio_uses_the_mark_not_the_execution_price():
    d1 = T0 + pd.Timedelta("1D")
    p = acc.CarryParams(e0=200.0, spot_share=0.5, slot_frac=0.9, max_spot_util=0.9)
    calm = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 100.0})
    marked = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 100.0}, marks={"AAA": {T0: 100.0, d1: 180.0}})
    assert (
        acc.run_carry(calm, Scripted(entries={T0: ["AAA"]}), _days(3), PRIMARY, {}, params=p).counters[
            "margin_reductions"
        ]
        == 0
    )
    res = acc.run_carry(marked, Scripted(entries={T0: ["AAA"]}), _days(3), PRIMARY, {}, params=p)
    assert res.counters["margin_reductions"] == 1


def test_carry_liquidation_at_5pct_closes_both_legs_with_penalty():
    m = FakeMarket(spot={"AAA": 100.0}, perp={"AAA": 100.0}, perp_high={("AAA", T0): 400.0})
    p = acc.CarryParams(e0=200.0, spot_share=0.5, slot_frac=0.9, max_spot_util=0.9)
    res = acc.run_carry(m, Scripted(entries={T0: ["AAA"]}), _days(2), PRIMARY, {}, params=p)
    assert res.counters["liquidations"] == 1
    d0 = res.daily.iloc[0]
    q = 0.9 * 100.0 / 100.0
    assert d0["liquidation_penalty"] == pytest.approx(0.005 * q * (400.0 + 100.0))
    assert res.daily["active"].tolist() == [True, False]


def test_outage_day_has_zero_return_and_the_change_lands_next_day():
    m = FakeMarket(
        spot={"AAA": 100.0},
        perp={"AAA": 100.0},
        funding_rates={"AAA": _rates(5e-5)},
        outages={T0 + pd.Timedelta("1D")},
    )
    res = _carry(m, Scripted(entries={T0: ["AAA"]}), _days(3))
    d = res.daily
    assert d["ret"].iloc[1] == 0.0 and d["equity"].iloc[1] == d["equity"].iloc[0]
    assert d["funding_cf"].iloc[2] == pytest.approx(2 * d["funding_cf"].iloc[0] * 24 / 23.75, rel=1e-6)
    assert res.counters["outage_days"] == 1


# --- H2 simulator ------------------------------------------------------------------------------------


class H2Scripted:
    def __init__(self, plan):
        self.plan = plan

    def decide(self, t):
        return self.plan.get(t)


MONDAY = pd.Timestamp("2026-03-02T00:00:00Z")  # a Monday


def test_h2_dollar_neutral_half_over_k_of_equity_per_leg():
    assert MONDAY.weekday() == 0
    m = FakeMarket(perp={"AAA": 10.0, "BBB": 20.0, "CCC": 40.0, "DDD": 50.0})
    plan = {MONDAY: acc.H2Decision.rebalance({"AAA": 1, "BBB": 1, "CCC": -1, "DDD": -1})}
    res = acc.run_h2(m, H2Scripted(plan), _days(2), PRIMARY, {})
    q = {t["asset"]: t["qty"] for t in res.trades}
    assert q["AAA"] == pytest.approx(0.25 * 265 / 10) and q["DDD"] == pytest.approx(-0.25 * 265 / 50)
    d0 = res.daily.iloc[0]
    assert d0["gross"] == pytest.approx(265.0, rel=1e-9) and abs(d0["net"]) < 1e-9


def test_h2_flat_week_closes_everything_and_counts():
    m = FakeMarket(perp={"AAA": 10.0, "BBB": 20.0})
    nxt = MONDAY + pd.Timedelta("7D")
    plan = {MONDAY: acc.H2Decision.rebalance({"AAA": 1, "BBB": -1}), nxt: acc.H2Decision.flat()}
    res = acc.run_h2(m, H2Scripted(plan), _days(9), PRIMARY, {})
    assert res.counters["flat_weeks"] == 1 and res.counters["decisions"] == 2
    assert not res.daily["active"].iloc[7] and res.daily["active"].iloc[6]


def test_h2_carry_net_is_funding_minus_costs_over_previous_equity():
    rates = {"AAA": _rates(-3e-5, 10.0), "BBB": _rates(3e-5, 20.0)}
    m = FakeMarket(perp={"AAA": 10.0, "BBB": 20.0}, funding_rates=rates)
    plan = {MONDAY: acc.H2Decision.rebalance({"AAA": 1, "BBB": -1})}
    res = acc.run_h2(m, H2Scripted(plan), _days(3), PRIMARY, {})
    d = res.daily
    prev = d["equity"].shift(1).fillna(265.0)
    np.testing.assert_allclose(d["carry_net"], (d["funding_cf"] - d["fees"] - d["spread_slippage"]) / prev)
    assert d["funding_cf"].iloc[1] > 0  # long the negative-funding asset, short the positive one
    assert (d["long_pnl"] + d["short_pnl"]).iloc[1] == pytest.approx(d["funding_cf"].iloc[1])


def test_h2_rebalance_is_all_or_nothing_on_a_missing_price():
    m = FakeMarket(perp={"AAA": 10.0, "BBB": 20.0}, missing={("BBB", "perp", MONDAY)})
    plan = {MONDAY: acc.H2Decision.rebalance({"AAA": 1, "BBB": -1})}
    res = acc.run_h2(m, H2Scripted(plan), _days(2), PRIMARY, {})
    assert res.trades == [] and res.counters["skipped_actions"] == 1
