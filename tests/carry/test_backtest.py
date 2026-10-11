"""V5 backtest replay adapter (frozen protocol rev 3, §3.2, §4, §6, §9): fabricated data only."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from luna.carry import backtest as bt
from luna.carry.history import SnapshotBuilder
from tests.carry.backtest_fixtures import ASSETS, T, base_cfg, instruments, make_data

M15 = pd.Timedelta("15min")


def run(arm="W", data=None, days=3, row="PRIMARY", **kw):
    data = data if data is not None else make_data(days + 1)
    cfg = bt.arm_config(base_cfg(), arm, nokill=bt.ROWS[row].nokill)
    return bt.run_arm(data, cfg, instruments(), name=arm, row=bt.ROWS[row], start=T, end=T + pd.Timedelta(days=days),
                      **kw)


# --- arms differ only where R6 allows -----------------------------------------------------------------


def test_arms_differ_only_in_decision_mode_and_trigger():
    base = base_cfg().model_dump()
    for arm in "WDCE":
        d = bt.arm_config(base_cfg(), arm).model_dump()
        diff = {k for k in base if base[k] != d[k]}
        assert diff <= {"decision_mode", "evaluation"}
        ev = {k for k in base["evaluation"] if base["evaluation"][k] != d["evaluation"][k]}
        assert ev <= {"trigger", "weekday", "at_hour"}
    assert bt.arm_config(base_cfg(), "W").evaluation.trigger == "weekly"
    assert bt.arm_config(base_cfg(), "E").decision_mode == "cost_aware"


def test_variants_and_nokill_change_only_their_parameters():
    e = bt.arm_config(base_cfg(), "E").model_dump()
    t = bt.arm_config(base_cfg(), "E", variant="P-TIGHT").model_dump()
    assert {k for k in e if e[k] != t[k]} == {"switch_horizon_hours", "min_carry_improvement_bps",
                                              "switch_cost_multiplier", "safety_buffer_bps", "min_hold_hours"}
    n = bt.arm_config(base_cfg(), "E", nokill=True).model_dump()
    assert {k for k in e if e[k] != n[k]} == {"risk"}
    assert {k for k in e["risk"] if e["risk"][k] != n["risk"][k]} == {"max_drawdown"}
    with pytest.raises(ValueError):
        bt.arm_config(base_cfg(), "W", variant="P-TIGHT")


# --- §3.2 execution ------------------------------------------------------------------------------------


def test_w_enters_monday_and_fills_15_minutes_later_at_the_t15_price():
    data = make_data(4)
    for a in ASSETS:  # the bar that closes at t + 15m moves: the fill must use it
        data.mark[a].loc[T, "close"] = 101.0
    r = run("W", data)
    f = r.fills
    assert len(f) == 4 and set(f["ts"]) == {T + M15} and set(f["decision_ts"]) == {T}
    sides = dict(zip(f["asset"], f["side"], strict=True))
    assert sides == {"A": "sell", "B": "sell", "E": "buy", "F": "buy"}
    for _, x in f.iterrows():
        s = 1 if x["side"] == "buy" else -1
        assert x["mid_t"] == 100.0 and x["mid_fill"] == 101.0
        assert x["px"] == pytest.approx(101.0 * (1 + s * 2e-4) * (1 + s * 2e-4))
        assert x["fee"] == pytest.approx(0.0005 * x["qty"] * x["px"])
        assert x["delay_drift"] == pytest.approx(s * x["qty"] * 1.0)


def test_no_ioc_band_a_large_move_still_fills():
    data = make_data(4)
    data.mark["A"].loc[T, "close"] = 110.0  # +10 %, far beyond the 0.25 % band
    f = run("W", data).fills
    assert (f["asset"] == "A").any() and f.loc[f["asset"] == "A", "mid_fill"].iloc[0] == 110.0


def test_no_fill_without_a_t15_price_and_the_scheduled_rebalance_is_retried():
    data = make_data(4)
    for src in (data.mark["F"], data.perp["F"]):
        src.drop(index=T, inplace=True)  # bar open t: no price at t + 15m
    r = run("W", data)
    assert r.counters["unfilled_orders"] >= 1
    first = r.schedule[0]  # F's open did not fill at 00:15: the permission stays open, retried at 00:15
    assert first["outcome"] == "completed" and first["attempts"] == 2 and first["done_at"] == T + M15
    assert not (r.fills["asset"] == "F").any()  # F has no quote at 00:15 either: the core picks the next asset
    assert (r.fills["decision_ts"] == T + M15).any()


def test_decision_at_t_does_not_depend_on_data_after_t():
    base = run("W", make_data(4), days=1)
    data = make_data(4)
    for a in ASSETS:
        for src in (data.mark[a], data.perp[a]):
            src.loc[src.index >= T, "close"] = 55.0  # every bar that closes after t
        data.spreads[a].loc[data.spreads[a].index >= T, "spread_full"] = 0.03
        data.store.merge_rows(a, [{"timestamp": h.isoformat(), "fundingRate": 9.0, "relativeFundingRate": 0.09}
                                  for h in pd.date_range(T - pd.Timedelta("30min"), T + pd.Timedelta("1D"), freq="1h")])
    other = run("W", data, days=1)
    pick = ["asset", "side", "qty", "mid_t"]
    a = base.fills[base.fills["decision_ts"] == T][pick].reset_index(drop=True)
    b = other.fills[other.fills["decision_ts"] == T][pick].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    ra = base.records[base.records["ts"] == T].drop(columns=["ts"]).reset_index(drop=True)
    rb = other.records[other.records["ts"] == T].drop(columns=["ts"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(ra, rb)


# --- §3.2 valuation and the day of a fill --------------------------------------------------------------


def _stop_at_2345(data):
    """Short A's mark jumps +20 % on the bar that closes at 23:45 of day T: E stops it at 23:45, the
    close fills at T + 1D 00:00 (day T + 1)."""
    for src in (data.mark["A"], data.perp["A"]):
        src.loc[src.index >= T + pd.Timedelta("23h30min"), "close"] = 120.0


def test_a_fill_stamped_at_midnight_belongs_to_the_next_day():
    data = make_data(4)
    _stop_at_2345(data)
    r = run("E", data, days=3)
    mid = r.fills[r.fills["ts"] == T + pd.Timedelta("1D")]
    assert len(mid) and (mid["decision_ts"] == T + pd.Timedelta("23h45min")).all()
    d0, d1 = r.daily.loc[T], r.daily.loc[T + pd.Timedelta("1D")]
    assert d1["fees"] == pytest.approx(r.fills.loc[r.fills["ts"].dt.floor("D") == T + pd.Timedelta("1D"), "fee"].sum())
    assert d0["fees"] == pytest.approx(r.fills.loc[r.fills["ts"].dt.floor("D") == T, "fee"].sum())
    assert mid["fee"].sum() > 0 and d0["fees"] < r.fills.loc[r.fills["ts"] <= T + pd.Timedelta("1D"), "fee"].sum()


def test_daily_equity_equals_an_independent_reconstruction_from_the_fill_log():
    data = make_data(4)
    _stop_at_2345(data)
    r = run("E", data, days=3)
    b = SnapshotBuilder(data)
    for d in r.daily.index:
        at = d + pd.Timedelta("1D")
        marks = {a: b.mid(a, at)[0] for a in ASSETS}
        assert r.daily.loc[d, "equity"] == pytest.approx(
            bt.equity_from_log(r.fills, r.settlements, 265.0, at, marks), abs=1e-9)


def test_daily_components_add_up_and_carry_net_excludes_delay_drift():
    data = make_data(4)
    data.mark["A"].loc[T, "close"] = 103.0
    r = run("W", data)
    d = r.daily
    prev = d["equity"].shift(1).fillna(265.0)
    assert ((d["equity"] - prev) - (d["funding"] - d["fees"] - d["spread_slip"] + d["price"])).abs().max() < 1e-9
    assert ((d["funding"] - d["fees"] - d["spread_slip"]) / prev - d["carry_net"]).abs().max() < 1e-15
    assert d.loc[T, "delay_drift"] != 0 and d.loc[T, "funding"] > 0


# --- §3.2 scheduled rebalance permission ---------------------------------------------------------------


def _res(ok=True, opens=True):
    o = SimpleNamespace(client_id="x", qty=1.0)
    fills = [(SimpleNamespace(client_id="x", qty=1.0), "p")] if ok else []
    return SimpleNamespace(forced_blocked=False, records=[], fills=fills, orders=[(o, 1.0)], opens_allowed=opens)


def test_schedule_retries_at_most_4_counted_cycles_and_blocked_cycles_do_not_count():
    ev = bt.arm_config(base_cfg(), "D").evaluation
    s = bt.Schedule(ev)
    t = T
    assert s.allow(t)
    for i in range(10):  # entries blocked: the permission stays open
        s.after(t, _res(ok=False, opens=False), True, True)
        t += M15
        assert s.allow(t)
    for i in range(3):
        s.after(t, _res(ok=False), True, True)
        t += M15
        assert s.allow(t)
    s.after(t, _res(ok=False), True, True)
    assert not s.allow(t + M15) and s.log[-1]["outcome"] == "exhausted"
    assert s.allow(T + pd.Timedelta("1D"))  # next slot


def test_schedule_closes_on_completion_and_event_is_always_allowed():
    s = bt.Schedule(bt.arm_config(base_cfg(), "W").evaluation)
    assert s.allow(T)
    s.after(T, _res(ok=True), True, True)
    assert not s.allow(T + M15) and s.log[-1]["outcome"] == "completed"
    assert not s.allow(T + pd.Timedelta("1D"))  # W: next Monday only
    assert s.allow(T + pd.Timedelta("7D"))
    e = bt.Schedule(bt.arm_config(base_cfg(), "E").evaluation)
    assert all(e.allow(T + i * M15) for i in range(5))


def test_interval_slots_for_p_c8():
    ev = bt.arm_config(base_cfg(), "C", variant="P-C8").evaluation
    assert bt.slot(ev, T + pd.Timedelta("9h")) == T + pd.Timedelta("8h")
    assert bt.slot(ev, T + pd.Timedelta("7h45min")) == T


# --- outcome guard -------------------------------------------------------------------------------------


def test_real_inputs_are_refused_without_the_outcome_authorization():
    data = make_data(4)
    data.real = True
    with pytest.raises(bt.OutcomeBlocked):
        run("W", data)
    with pytest.raises(bt.OutcomeBlocked):
        bt.OutcomeAuthorization(object(), {})


def test_the_core_books_the_fill_at_t15_so_funding_accrues_from_the_fill():
    r = run("W", make_data(4), days=1)
    first = r.settlements[r.settlements["period_start"] == T]
    assert len(first) == 4 and first["held_hours"].tolist() == pytest.approx([0.75] * 4)


def test_no_decision_at_the_last_quarter_hour_and_no_fill_at_or_after_the_end():
    r = run("W", make_data(4), days=2)
    end = T + pd.Timedelta("2D")
    assert r.records["ts"].max() == end - 2 * M15  # 23:30 is the last decision
    assert (r.fills["ts"] < end).all()


# --- §12 mandatory scenarios -----------------------------------------------------------------------


def test_runner_refuses_rows_at_or_after_the_cut_and_an_end_beyond_it():
    from luna.carry.frozen import CUTOFF
    from luna.v2.data import HoldoutLeak

    data = make_data(2)
    with pytest.raises(HoldoutLeak):
        bt.run_arm(data, bt.arm_config(base_cfg(), "W"), instruments(), start=T, end=CUTOFF + pd.Timedelta("1D"))
    for kind in ("mark", "perp"):
        d = make_data(2)
        src = getattr(d, kind)["A"]
        src.loc[CUTOFF] = src.iloc[-1]  # a bar opening at the cut (closes after it)
        with pytest.raises(HoldoutLeak):
            bt.run_arm(d, bt.arm_config(base_cfg(), "W"), instruments(), start=T, end=T + pd.Timedelta("1D"))
    d = make_data(2)
    d.store.merge_rows("A", [{"timestamp": CUTOFF.isoformat(), "fundingRate": 1e-3, "relativeFundingRate": 1e-5}])
    with pytest.raises(HoldoutLeak):
        bt.run_arm(d, bt.arm_config(base_cfg(), "W"), instruments(), start=T, end=T + pd.Timedelta("1D"))


def test_funding_api_gap_makes_the_book_wait_and_the_scheduled_rebalance_stays_open():
    from luna.carry.funding import FundingStore

    data = make_data(3)
    store = FundingStore()
    for a in ASSETS:  # no realized rate for the 6 periods before t: funding_stale everywhere
        rows = [{"timestamp": t.isoformat(), "fundingRate": r[0], "relativeFundingRate": r[1]}
                for t, r in ((pd.Timestamp(k, tz="UTC"), v) for k, v in data.store._rates[a].items())
                if not (T - pd.Timedelta("6h") <= t < T)]
        store.merge_rows(a, rows)
    data.store = store
    r = run("W", data, days=1)
    assert r.fills["decision_ts"].min() == T + pd.Timedelta("1h")  # first period after the gap ends at 01:00
    assert (r.records.loc[r.records["ts"] == T, "reason"] == "global_data_outage").any()
    assert r.schedule[0]["outcome"] == "completed"


def test_w_reproduces_a_hand_computed_week_start():
    r = run("W", make_data(3), days=2)
    q = 0.6625  # 0.25 x 265 / 100 on the 0.0001 step
    px_b, px_s = 100 * 1.0002 ** 2, 100 * 0.9998 ** 2
    fees = 0.0005 * q * (2 * px_b + 2 * px_s)
    slip = 2 * q * (px_b - 100) + 2 * q * (100 - px_s)
    per_hour = q * 100 * (3e-5 + 2e-5 + 2e-5 + 3e-5)
    d0, d1 = r.daily.loc[T], r.daily.loc[T + pd.Timedelta("1D")]
    assert d0["fees"] == pytest.approx(fees, rel=1e-12) and d0["spread_slip"] == pytest.approx(slip, rel=1e-9)
    assert d0["funding"] == pytest.approx(per_hour * 23.75, rel=1e-12)
    assert d0["equity"] == pytest.approx(265 + per_hour * 23.75 - fees - slip, rel=1e-12)
    assert abs(d0["price"]) < 1e-9 and d0["delay_drift"] == 0.0
    assert d1["funding"] == pytest.approx(per_hour * 24, rel=1e-12) and d1["fees"] == 0.0


def test_with_a_fixed_ranking_all_four_arms_hold_the_same_book():
    rels = {a: 3 * v for a, v in __import__("tests.carry.backtest_fixtures", fromlist=["RELS"]).RELS.items()}
    data = make_data(3, rels=rels)
    books = {}
    for arm in "WDCE":
        f = run(arm, data, days=2).fills
        net = f.groupby("asset")["dq"].sum()
        books[arm] = {a: int(v > 0) - int(v < 0) for a, v in net.items() if abs(v) > 1e-12}
    assert books["W"] == {"A": -1, "B": -1, "E": 1, "F": 1}
    assert books["W"] == books["D"] == books["C"] == books["E"]
