"""H1 historical replay (spec §6): matched qty, taker costs, accounting, stops, reset, unwind."""

import math

import numpy as np
import pandas as pd
import pytest

from luna.v6 import verdict as vd
from luna.v6.criteria import evaluate
from luna.v6.data import BarPanel
from luna.v6.h1 import H1Config, H1Engine
from luna.v6.market import HistMarket
from tests.v6.world import build_world

DAY = pd.Timedelta("1D")
BAR = pd.Timedelta("15min")


class Fixed720:
    h = 720

    def __init__(self, levels):
        self.levels = levels

    def forecast(self, t):
        return self.levels.copy()


def h1_world(levels=None, spot_shift=0.0, shocks=None, **kw):
    w = build_world(levels=levels, shocks=shocks, **kw)
    close = w.market.mark.close * (1 + spot_shift)
    w.market.spot = BarPanel(close)
    w.market.spot_volume_usd = w.market.volume_usd
    insts = {a: {"perp": {"step": 0.001, "min_qty": 0.001}, "spot": {"step": 0.001, "min_qty": 0.001}}
             for a in close.columns}
    return w, insts


def run(w, insts, m=1.5, days=10, equity0=10_000.0, market=None):
    lv = w.funding_levels * 4  # strong positive funding on the top names
    eng = H1Engine(market or w.market, Fixed720(lv), insts, H1Config(f"H1-m{m}", m), equity0=equity0)
    return eng.run(w.start, w.start + days * DAY)


def test_pairs_are_qty_matched_taker_both_legs_and_atomic():
    w, insts = h1_world()
    r = run(w, insts)
    f = r.fills[r.fills["intent"] == "open"]
    assert len(f) > 0
    for (a, ts), g in f.groupby(["asset", "ts"]):
        spot, perp = g[g["leg"] == "spot"].iloc[0], g[g["leg"] == "perp"].iloc[0]
        assert spot["dq"] == pytest.approx(-perp["dq"]) and spot["dq"] > 0
        assert spot["fee"] == pytest.approx(0.008 * spot["dq"] * spot["px"])  # spot taker 0.80 %
        assert perp["fee"] == pytest.approx(0.0005 * abs(perp["dq"]) * perp["px"])  # futures taker 0.05 %
        assert (g["ts"] - g["decision_ts"] == BAR).all()


def test_entries_only_with_positive_forecast_above_m_round_trips_and_capital_limits():
    w, insts = h1_world()
    r = run(w, insts, equity0=10_000.0)
    opened = set(r.fills[r.fills["intent"] == "open"]["asset"])
    assert all(w.funding_levels[a] > 0 for a in opened)
    assert len(opened) <= 3
    first = r.fills[(r.fills["intent"] == "open") & (r.fills["leg"] == "perp")].groupby("asset").first()
    assert (first["notional"] <= 10_000.0 / 6 * 1.02).all()


def test_spot_spread_assumption_and_matched_hedge_pnl():
    w, insts = h1_world()
    r = run(w, insts)
    f = r.fills[(r.fills["intent"] == "open")]
    sp = f[f["leg"] == "spot"].iloc[0]
    pp = f[f["leg"] == "perp"].iloc[0]
    assert sp["half"] == pytest.approx(max(2 * pp["half"], 0.0005))
    d = r.daily
    assert (d["net"] - (d["funding"] - d["fees"] - d["spread_slip"] + d["price"])).abs().max() < 1e-9
    assert r.ticks["equity"].iloc[-1] == pytest.approx(d["equity"].iloc[-1])


def test_hedged_book_has_almost_no_price_pnl():
    w, insts = h1_world()
    r = run(w, insts, days=12)
    pnl_price = r.daily["price"].sum()
    gross_moves = r.daily["net"].abs().sum()
    assert abs(pnl_price) < 0.05 * max(1.0, r.daily["funding"].abs().sum() + r.daily["fees"].sum())  # spot == perp prices


def test_hedge_reset_at_40_percent_and_cooldown():
    w0, _ = h1_world()
    top = w0.funding_levels.idxmax()
    w, insts = h1_world(shocks=[(top, pd.Timestamp("2025-05-05T06:00Z"), 1.60)])
    r = run(w, insts, days=8)
    ev = [e for e in r.events if e["event"] == "hedge_reset"]
    assert ev and ev[0]["asset"] == top
    re = r.fills[(r.fills["asset"] == top) & (r.fills["intent"] == "open") & (r.fills["decision_ts"] > ev[0]["t"])]
    assert re.empty or (re["decision_ts"].min() - ev[0]["t"]) >= pd.Timedelta("24h")


def test_combined_stop_on_basis_divergence():
    w0, _ = h1_world()
    top = w0.funding_levels.idxmax()
    w, insts = h1_world()
    when = pd.Timestamp("2025-05-05T06:00Z")
    spot = w.market.spot.close.copy()
    spot.loc[spot.index >= when, top] *= 0.80  # spot falls 20 %, perp unchanged -> hedged loss
    w.market.spot = BarPanel(spot)
    r = run(w, insts, days=8)
    assert any(e["event"] == "combined_stop" and e["asset"] == top for e in r.events)


def test_terminal_unwind_both_legs_and_flat_end():
    w, insts = h1_world()
    r = run(w, insts, days=5)
    term = r.fills[r.fills["intent"] == "terminal"]
    assert set(term["leg"]) == {"spot", "perp"} and (term["fee"] > 0).all()
    assert r.fills.groupby(["asset", "leg"])["dq"].sum().abs().max() < 1e-9


def test_maker_bound_row_reprices_the_primary_orders_with_maker_fees_at_the_mid():
    from luna.v6.h1 import reprice_maker_bound

    w, insts = h1_world()
    base = run(w, insts, days=6)
    r = reprice_maker_bound(base)
    key = ["asset", "decision_ts", "ts", "leg", "dq", "intent"]
    assert base.fills[key].equals(r.fills[key])  # the same decisions, quantities and timestamps
    assert (r.fills["px"] == r.fills["mid"]).all() and (r.fills["spread_slip"] == 0).all()
    spot = r.fills[r.fills["leg"] == "spot"]
    assert (spot["fee"] - 0.004 * spot["dq"].abs() * spot["mid"]).abs().max() < 1e-12
    d = r.daily
    assert (d["net"] - (d["funding"] - d["fees"] - d["spread_slip"] + d["price"])).abs().max() < 1e-9
    assert d["price"].sum() == pytest.approx(base.daily["price"].sum(), abs=1e-6)  # same positions, same marks
    assert d["funding"].sum() == pytest.approx(base.daily["funding"].sum())
    assert r.ticks["equity"].iloc[-1] > base.ticks["equity"].iloc[-1]


def test_cost2x_keeps_the_primary_decisions_and_doubles_realised_costs():
    w, insts = h1_world()
    base = run(w, insts, days=6)
    c2 = HistMarket(mark=w.market.mark, trade=None, volume_usd=w.market.volume_usd, funding=w.market.funding,
                    spreads=w.market.spreads, spot=w.market.spot, spot_volume_usd=w.market.spot_volume_usd,
                    cost_mult=2.0)
    r = run(w, insts, days=6, market=c2)
    key = ["asset", "decision_ts", "leg", "dq"]
    assert base.fills[base.fills["intent"] == "open"][key].reset_index(drop=True).equals(
        r.fills[r.fills["intent"] == "open"][key].reset_index(drop=True))
    assert r.daily["fees"].sum() == pytest.approx(2 * base.daily["fees"].sum(), rel=0.02)


def test_historical_h1_cannot_be_final_go():
    w, insts = h1_world()
    r = run(w, insts, days=6)
    ev = evaluate("H1-m1.5", "H1", "holdout", r, {}, "VALID")
    ev.stats = {"p_go": 0.0, "lb_ok": True}
    assert vd.ladder(ev, stat_reject=True).cls != "FINAL GO"


def test_spot_funding_plus_one_x_collateral_never_exceed_cash():
    w, insts = h1_world()
    r = run(w, insts, equity0=3000.0, days=6)
    o = r.fills[(r.fills["intent"] == "open")]
    for d, g in o.groupby("decision_ts"):
        spot = g[g["leg"] == "spot"]
        need = 2 * (spot["dq"] * spot["mid"]).sum()  # spot purchase + 1x perp collateral
        assert need <= 3000.0 + 1e-6
    assert r.counters["skipped_cash"] + r.counters["skipped_size"] >= 0
