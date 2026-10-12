"""Forward glue: the H3 engine runs on a forward market built from snapshot streams; decision vs
settlement funding; risk-model prices from history then fresh ticker marks."""

import numpy as np
import pandas as pd
import pytest

from luna.v6 import forward_exec as fx
from luna.v6.data import FundingPanel
from luna.v6.engine import H3Config, H3Engine
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")


def streams_from_world(w, start, end):
    s = fx.Streams()
    for a in w.market.assets:
        ticks, books = [], []
        t = start
        while t <= end:
            mid = w.market.mid(a, t)
            if np.isfinite(mid):
                ticks.append(fx.Tick(t - pd.Timedelta("5s"), mid * 0.9999, mid * 1.0001, mid, 5e7))
                books.append(fx.Book(t + pd.Timedelta("20s"), ((mid * 0.9999, 1e6),), ((mid * 1.0001, 1e6),)))
            t += pd.Timedelta("15min")
        s.fut_ticks[a], s.fut_book[a] = fx.Stream(ticks), fx.Stream(books)
    return s


def test_h3_engine_runs_on_the_forward_market_with_book_fills():
    w = build_world()
    start, end = w.start, w.start + 4 * DAY
    s = streams_from_world(w, start - DAY, end)
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    r = H3Engine(m, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(start, end)
    f = r.fills[r.fills["intent"] == "open"]
    assert len(f) > 0
    assert ((f["ts"] - f["decision_ts"]) == pd.Timedelta("20s")).all()  # the first book after the decision
    d = r.daily
    assert (d["net"] - (d["funding"] - d["fees"] - d["spread_slip"] + d["price"])).abs().max() < 1e-9


def test_decisions_see_collector_funding_but_settlement_uses_the_api_gap_fill():
    idx = pd.date_range("2026-12-01T00:00Z", periods=4, freq="h")
    rel = pd.DataFrame({"ETH": [1e-5, np.nan, 1e-5, 1e-5]}, index=idx)
    dec = FundingPanel(rel, abs_=rel * 100)
    api = pd.DataFrame([{"asset": "ETH", "period_start": idx[1], "rel": 2e-5, "abs": 0.002}])
    sp = fx.settlement_panel(dec, api, ["ETH"])
    t = idx[3]
    assert np.isnan(dec.abs_rate("ETH", idx[1], t)) and sp.abs_rate("ETH", idx[1], t) == pytest.approx(0.002)


def test_merge_funding_uses_receive_times_from_t_f_on():
    tf = pd.Timestamp("2026-12-01T02:00Z")
    hidx = pd.date_range("2026-11-29T00:00Z", "2026-12-01T05:00Z", freq="h")
    hist = FundingPanel(pd.DataFrame({"ETH": 1e-5}, index=hidx), abs_=pd.DataFrame({"ETH": 1e-3}, index=hidx))
    fidx = pd.date_range("2026-12-01T00:00Z", periods=6, freq="h")
    known = pd.DataFrame({"ETH": fidx + pd.Timedelta("3h")}, index=fidx)
    fwd = FundingPanel(pd.DataFrame({"ETH": 5e-5}, index=fidx), abs_=pd.DataFrame({"ETH": 5e-3}, index=fidx),
                       known_at=known)
    m = fx.merge_funding(hist, fwd, tf)
    assert m.rel.loc[pd.Timestamp("2026-12-01T01:00Z"), "ETH"] == pytest.approx(1e-5)  # history before T_F
    assert m.rel.loc[tf, "ETH"] == pytest.approx(5e-5)
    assert np.isnan(m.abs_rate("ETH", tf, tf + pd.Timedelta("2h")))  # received at T_F + 3h
    assert m.abs_rate("ETH", tf, tf + pd.Timedelta("3h")) == pytest.approx(5e-3)


def test_hourly_marks_use_history_then_fresh_ticker_marks():
    hist = pd.DataFrame({"ETH": [100.0, 101.0]}, index=pd.date_range("2026-12-01T00:00Z", periods=2, freq="h"))
    s = fx.Streams()
    s.fut_ticks["ETH"] = fx.Stream([fx.Tick(pd.Timestamp("2026-12-01T01:59:30Z"), 1, 1, 102.0),
                                    fx.Tick(pd.Timestamp("2026-12-01T02:50:00Z"), 1, 1, 103.0)])
    p = fx.hourly_marks(hist, s, pd.Timestamp("2026-12-01T02:00Z"), pd.Timestamp("2026-12-01T03:00Z"))
    assert p.loc[pd.Timestamp("2026-12-01T02:00Z"), "ETH"] == pytest.approx(102.0)
    assert np.isnan(p.loc[pd.Timestamp("2026-12-01T03:00Z"), "ETH"])  # 10 minutes old: not fresh
