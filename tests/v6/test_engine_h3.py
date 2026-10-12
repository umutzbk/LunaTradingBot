"""H3 replay engine (spec §3, §5): timing, accounting identities, funding, risk exits, infeasible
clock, terminal unwind."""

import math

import numpy as np
import pandas as pd
import pytest

from luna.v6.engine import H3Config, H3Engine
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")
BAR = pd.Timedelta("15min")


def run(w, days=10, k=4, model=None, equity0=10_000.0):
    eng = H3Engine(w.market, model or FixedModel(w.funding_levels), w.prices, w.vol, w.insts,
                   H3Config("H3-k4", k), equity0=equity0)
    return eng.run(w.start, w.start + days * DAY)


def test_decisions_at_midnight_fills_at_t_plus_15m_and_accounting_identities():
    w = build_world()
    r = run(w)
    f = r.fills[r.fills["intent"] != "terminal"]
    assert len(f) > 0
    assert ((f["ts"] - f["decision_ts"]) == BAR).all()
    opens = f[f["intent"] == "open"]
    assert (opens["decision_ts"].dt.hour == 0).all() and (opens["decision_ts"].dt.minute == 0).all()
    # fill at the mid of t + 15m
    row = opens.iloc[0]
    assert row["mid"] == pytest.approx(w.market.mid(row["asset"], row["ts"]))
    d = r.daily
    assert d["net"].sum() == pytest.approx(r.ticks["equity"].iloc[-1] - r.equity0)
    assert (d["net"] - (d["funding"] - d["fees"] - d["spread_slip"] + d["price"])).abs().max() < 1e-9
    assert (d["carry"] - (d["funding"] - d["fees"] - d["spread_slip"])).abs().max() < 1e-12


def test_shorts_in_positive_funding_receive_and_longs_in_negative_funding_receive():
    w = build_world()
    r = run(w)
    s = r.settlements
    held = r.fills.groupby("asset")["dq"].first()
    for a, amt in s.groupby("asset")["amount"].sum().items():
        lvl = w.funding_levels[a]
        side = np.sign(held[a])
        assert np.sign(amt) == np.sign(-side * lvl)  # carry = -side * rate


def test_terminal_unwind_closes_everything_with_costs():
    w = build_world()
    r = run(w)
    term = r.fills[r.fills["intent"] == "terminal"]
    assert len(term) > 0 and (term["fee"] > 0).all() and (term["ts"] == r.end).all()
    assert r.ticks["equity"].iloc[-1] == pytest.approx(r.daily["equity"].iloc[-1])
    assert r.fills.groupby("asset")["dq"].sum().abs().max() < 1e-9  # flat at the end


def test_r4_kill_exits_everything_and_never_reenters():
    w = build_world(shocks=[("A00", pd.Timestamp("2025-05-06T12:00Z"), 4.0)])  # A00 is a short leg
    r = run(w, days=12, equity0=1000.0)
    assert r.counters["kill"] == 1
    kt = [e["t"] for e in r.events if e["event"] == "kill"][0]
    later = r.fills[(r.fills["decision_ts"] > kt) & (r.fills["intent"].isin(["open", "resize"]))]
    assert later.empty
    assert (r.fills[r.fills["decision_ts"] == kt]["intent"] == "kill").all()


def test_leg_stop_cooldown_and_intraday_repair():
    w = build_world(shocks=[("A01", pd.Timestamp("2025-05-04T06:00Z"), 1.25)])  # short leg +25 %
    r = run(w, days=8)
    stops = [e for e in r.events if e["event"] == "stop"]
    assert stops and stops[0]["asset"] == "A01"
    st = stops[0]["t"]
    assert r.counters["repairs"] >= 1
    reentry = r.fills[(r.fills["asset"] == "A01") & (r.fills["intent"] == "open") & (r.fills["decision_ts"] > st)]
    assert reentry.empty or (reentry["decision_ts"].min() - st) >= pd.Timedelta("24h")


def test_infeasible_book_holds_then_exits_after_24h():
    w = build_world()
    # from day 3 only 5 assets keep forecasts -> k' = 2 < 3 -> infeasible
    drop_at = w.start + 3 * DAY
    keep = ["A00", "A01", "A09", "A10", "BTC"]
    drop = {a: drop_at for a in w.funding_levels.index if a not in keep}
    r = run(w, days=8, model=FixedModel(w.funding_levels, drop))
    assert r.counters["infeasible_decisions"] >= 1
    ev = [e for e in r.events if e["event"] == "infeasible_exit_all"]
    assert ev and ev[0]["t"] == drop_at + DAY + BAR  # the 00:00 one day later is not > 24 h; 00:15 is
    opens_after = r.fills[(r.fills["intent"] == "open") & (r.fills["decision_ts"] >= drop_at)]
    assert opens_after.empty


def test_no_position_opens_when_the_build_is_infeasible():
    vols = {"BTC": 0.6, **{f"A{i:02d}": (2.0 if i >= 7 else 0.5) for i in range(11)}}  # longs >> shorts
    w = build_world(vols=vols, rho=0.0)
    r = run(w, days=3)
    assert r.counters["infeasible_decisions"] >= 1
    assert any(str(d.get("reason", "")).startswith("build") for d in r.decisions)
    assert r.fills[r.fills["intent"] == "open"].empty


def test_missing_forecast_for_more_than_24h_exits_the_leg():
    w = build_world()
    drop_at = w.start + 2 * DAY
    r = run(w, days=6, model=FixedModel(w.funding_levels, {"A00": drop_at}))
    ex = r.fills[(r.fills["asset"] == "A00") & (r.fills["intent"].isin(["missing_fc", "exit"]))]
    assert not ex.empty and ex["decision_ts"].min() <= drop_at + DAY + BAR


def test_r3_blocks_entries_for_the_rest_of_the_day():
    when = pd.Timestamp("2025-05-04T01:00Z")
    w = build_world(shocks=[(a, when, 1.14) for a in ("BTC", "A00", "A01")])  # three shorts +14 %
    r = run(w, days=4, equity0=1000.0)
    assert r.counters["r3_days"] >= 1
    blocked_day = r.fills[(r.fills["decision_ts"] > when) & (r.fills["decision_ts"] < when.ceil("D"))]
    assert not (blocked_day["intent"].isin(["open"])).any()


class WarmupAfter(FixedModel):
    def __init__(self, levels, since):
        super().__init__(levels)
        self.since = since

    def forecast(self, t):
        from luna.v6.forecast import ModelWarmup

        if t >= self.since:
            raise ModelWarmup("no fit")
        return super().forecast(t)


def test_model_warmup_holds_then_exits_held_legs_after_24h():
    w = build_world()
    since = w.start + 2 * DAY
    r = run(w, days=5, model=WarmupAfter(w.funding_levels, since))
    ex = r.fills[r.fills["intent"] == "missing_fc"]
    assert not ex.empty and (ex["decision_ts"] == since + DAY + BAR).all()


def test_zero_volatility_world_has_zero_price_component_every_day():
    names = ["BTC"] + [f"A{i:02d}" for i in range(11)]
    w = build_world(vols={a: 1e-12 for a in names})
    r = run(w, days=5)
    assert r.daily["price"].abs().max() < 1e-6
    assert r.daily["funding"].abs().sum() > 0


def test_entry_rule_that_leaves_fewer_than_three_legs_is_reported_before_any_covariance():
    w = build_world()
    flat = w.funding_levels * 1e-3  # tiny forecast dispersion: no candidate passes c*168 > 2 cost
    r = run(w, days=3, model=FixedModel(flat))
    assert r.fills[r.fills["intent"] == "open"].empty
    assert all(d.get("reason") == "entry rule left fewer than 3 legs on a side"
               for d in r.decisions if d.get("status") == "INFEASIBLE")
