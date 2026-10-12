"""Regression tests for the independent code review of the V6 implementation (round 1)."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from luna.v6 import forward as fw
from luna.v6 import forward_exec as fx
from luna.v6 import guard
from luna.v6 import verdict as vd
from luna.v6.criteria import evaluate
from luna.v6.data import BarPanel, FundingPanel
from luna.v6.engine import FILL_COLS, H3Config, H3Engine
from luna.v6.h1 import H1Config, H1Engine
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")
BAR = pd.Timedelta("15min")


class Fixed720(FixedModel):
    h = 720


def fwd_world(names=None, drop_ticks=None, gap=None):
    """A synthetic world with forward snapshot streams (tickers every minute, books every 5 minutes)."""
    w = build_world(names=names)
    s = fx.Streams()
    start, end = w.start - DAY, w.start + 6 * DAY
    for a in w.market.assets:
        ticks, books, sdepth = [], [], []
        t = start
        while t <= end:
            mid = w.market.mid(a, t.floor("15min"))
            in_gap = gap is not None and gap[0] <= t < gap[1]
            if np.isfinite(mid) and not in_gap:
                ticks.append(fx.Tick(t, mid * 0.9999, mid * 1.0001, mid, 5e7))
                if t.minute % 5 == 0:
                    books.append(fx.Book(t + pd.Timedelta("2s"), ((mid * 0.9999, 1e6),), ((mid * 1.0001, 1e6),)))
                    sdepth.append(fx.Book(t + pd.Timedelta("1s"), ((mid * 0.9998, 1e6),), ((mid * 1.0002, 1e6),)))
            t += pd.Timedelta("1min")
        s.fut_ticks[a], s.fut_book[a] = fx.Stream(ticks), fx.Stream(books)
        s.spot_ticks[a], s.spot_depth[a] = fx.Stream(ticks), fx.Stream(sdepth)
    return w, s


# --- C1: maker flag reaches the fills table and the feasibility measurement --------------------------

def test_maker_flag_is_recorded_and_feasibility_sees_it():
    assert "maker" in FILL_COLS
    w, s = fwd_world()
    insts = {a: {"perp": {"step": 0.001, "min_qty": 0.001}, "spot": {"step": 0.001, "min_qty": 0.001}} for a in w.market.assets}
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    r = H1Engine(m, Fixed720(w.funding_levels * 4), insts, H1Config("H1-m1.5", 1.5), equity0=10_000.0).run(
        w.start, w.start + 4 * DAY)
    assert "maker" in r.fills.columns and r.fills["maker"].dtype == bool
    lag, unw = fw.lag_and_unwind(r, m, w.start + 4 * DAY)
    meas = fw.feasibility(r, {}, lag)
    spot_open = r.fills[(r.fills["leg"] == "spot") & (r.fills["intent"] == "open")]
    assert len(spot_open) > 0
    exp = float((spot_open["maker"] * spot_open["dq"].abs() * spot_open["mid"]).sum())
    assert meas.maker_share >= 0.0 and (exp == 0 or meas.maker_share > 0)


# --- M1: markers ------------------------------------------------------------------------------------

def test_identical_marker_paths_are_refused_without_writing(tmp_path, monkeypatch):
    p = tmp_path / "same" / guard.MARKER
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: [p, p])
    monkeypatch.setattr(guard, "preflight", lambda repo=None: {"head": "x"})
    with pytest.raises(guard.HoldoutBlocked, match="distinct"):
        guard.authorize_holdout(guard.approval_phrase())
    assert not p.exists()


def test_a_failing_second_marker_rolls_back_the_first(tmp_path, monkeypatch):
    a, b = tmp_path / "a" / guard.MARKER, tmp_path / "b" / guard.MARKER
    b.parent.mkdir(parents=True)
    b.write_text("{}")  # appears between the check and the write
    calls = {"n": 0}

    def paths(repo=None):
        calls["n"] += 1
        return [a, b]
    monkeypatch.setattr(guard, "marker_paths", paths)
    monkeypatch.setattr(guard, "preflight", lambda repo=None: {"head": "x"})
    monkeypatch.setattr(guard.Path, "exists", lambda self: False if self == b else Path_exists(self))
    with pytest.raises(guard.HoldoutBlocked):
        guard.authorize_holdout(guard.approval_phrase())
    monkeypatch.undo()
    assert not a.exists()


def Path_exists(p):
    import os
    return os.path.exists(p)


def test_holdout_opened_checks_marker_content(tmp_path, monkeypatch):
    ms = [tmp_path / "a", tmp_path / "b"]
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: ms)
    for m in ms:
        m.write_text("{}")
    assert not guard.holdout_opened()
    for m in ms:
        m.write_text(json.dumps({"spec_sha256": guard.SPEC_SHA256, "approved": True}))
    assert guard.holdout_opened()


# --- M2: H1 cash rule counts entry costs once --------------------------------------------------------

def test_h1_third_pair_fits_when_2n_plus_costs_fit():
    w = build_world()
    w.market.spot, w.market.spot_volume_usd = BarPanel(w.market.mark.close), w.market.volume_usd
    insts = {a: {"perp": {"step": 1e-6, "min_qty": 1e-6}, "spot": {"step": 1e-6, "min_qty": 1e-6}} for a in w.market.assets}
    r = H1Engine(w.market, Fixed720(w.funding_levels * 4), insts, H1Config("H1-m1.5", 1.5), equity0=265.0).run(
        w.start, w.start + 2 * DAY)
    first = r.fills[(r.fills["intent"] == "open") & (r.fills["decision_ts"] == w.start)]
    assert first["asset"].nunique() == 3


# --- M3: INVALID RUN from unknown held funding --------------------------------------------------------

def test_unknown_funding_for_a_held_period_is_an_invalid_run():
    w = build_world()
    rel = w.market.funding.rel.copy()
    rel.loc[w.start + DAY: w.start + DAY + pd.Timedelta("3h")] = np.nan
    w.market.funding = FundingPanel(rel, abs_=w.market.funding.abs.where(rel.notna()))
    r = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 3 * DAY)
    assert r.counters["funding_unknown"] > 0
    ev = evaluate("H3-k4", "H3", "holdout", r, {}, "VALID")
    ev.stats = {"p_go": 0.0, "lb_ok": True}
    assert vd.ladder(ev, stat_reject=True).cls == "INVALID RUN"


def test_a_maker_fill_keeps_its_flag_in_the_fills_table_and_counts_in_m_a():
    from luna.v6.engine import SETT_COLS, fill_row, fills_frame
    from tests.v6.test_forward_exec import M, S, T, streams

    s = streams(ticks=[(-30 * S, 100.0, 100.2), (10 * M, 99.9, 100.0), (20 * M, 99.8, 99.95)],
                depths=[(-60 * S, [(100.0, 5)], [(100.2, 5)]), (19 * M, [(99.8, 5)], [(99.95, 2.0)])])
    res = fx.maker_first_spot(s, "ETH", 3.0, T)
    assert any(f.maker for f in res.fills)
    F = fills_frame([fill_row(f, "open") for f in res.fills])
    assert F["maker"].any()
    run = SimpleNamespace(fills=F, settlements=pd.DataFrame(columns=SETT_COLS),
                          daily=pd.DataFrame({"net": [0.0], "carry": [0.0]}))
    assert fw.feasibility(run, {}, 0.0).maker_share > 0


# --- M4: collector gaps ------------------------------------------------------------------------------

def test_a_global_snapshot_gap_is_a_no_op_and_the_midnight_decision_is_deferred():
    gap = (pd.Timestamp("2025-05-03T23:30Z"), pd.Timestamp("2025-05-04T02:00Z"))
    w, s = fwd_world(gap=gap)
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    r = H3Engine(m, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 4 * DAY)
    assert r.counters["inelig_exits"] == 0  # the gap did not force exits
    d = [x for x in r.decisions if x["t"].date() == pd.Timestamp("2025-05-04").date()]
    assert d and d[0]["t"] >= gap[1]  # deferred to the first fresh snapshot


def test_a_gap_beyond_six_hours_skips_the_day():
    gap = (pd.Timestamp("2025-05-03T23:30Z"), pd.Timestamp("2025-05-04T07:00Z"))
    w, s = fwd_world(gap=gap)
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    r = H3Engine(m, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 4 * DAY)
    assert not [x for x in r.decisions if x["t"].date() == pd.Timestamp("2025-05-04").date()]
    assert r.counters["skipped_decision_days"] == 1


def test_the_first_fresh_quarter_after_more_than_six_hours_does_not_decide():
    gap = (pd.Timestamp("2025-05-03T23:30Z"), pd.Timestamp("2025-05-04T06:15Z"))  # 06:00 stale, 06:15 fresh
    w, s = fwd_world(gap=gap)
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    r = H3Engine(m, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 4 * DAY)
    assert not [x for x in r.decisions if x["t"].date() == pd.Timestamp("2025-05-04").date()]
    assert r.counters["skipped_decision_days"] == 1


# --- M5: no funding gap between the holdout end and T_F -------------------------------------------------

def test_merge_funding_bridges_the_holdout_end_with_collector_rounds():
    hend = guard.HOLDOUT_END.floor("h")
    hidx = pd.date_range(hend - pd.Timedelta("30D"), hend - pd.Timedelta("1h"), freq="h")
    hist = FundingPanel(pd.DataFrame({"ETH": 1e-5}, index=hidx), abs_=pd.DataFrame({"ETH": 1e-3}, index=hidx))
    fidx = pd.date_range(hend - pd.Timedelta("48h"), hend + pd.Timedelta("10D"), freq="h")
    known = pd.DataFrame({"ETH": fidx + pd.Timedelta("5min")}, index=fidx)
    fwd = FundingPanel(pd.DataFrame({"ETH": 2e-5}, index=fidx), abs_=pd.DataFrame({"ETH": 2e-3}, index=fidx),
                       known_at=known)
    m = fx.merge_funding(hist, fwd, guard.HOLDOUT_END)
    t = hend + pd.Timedelta("5D")
    assert m.fl(t, 720, 648).notna().all()  # no hole between the holdout end and T_F


# --- M6: forward loaders are guarded --------------------------------------------------------------------

def test_forward_loaders_refuse_before_the_code_freeze(tmp_path):
    with pytest.raises(guard.HoldoutBlocked):
        fx.read_streams(tmp_path, pd.Timestamp("2026-12-01T00:00Z"), pd.Timestamp("2026-12-02T00:00Z"), ["ETH"],
                        repo=tmp_path)
    with pytest.raises(guard.HoldoutBlocked):
        fx.funding_from_collector(tmp_path, pd.Timestamp("2026-12-01T00:00Z"), pd.Timestamp("2026-12-02T00:00Z"),
                                  ["ETH"], repo=tmp_path)


def test_evidence_streams_are_clamped_to_t_f(tmp_path, monkeypatch):
    rec = tmp_path / guard.FREEZE_RECORD_REL
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"review_pass_at": "2026-11-30T12:00:00Z", "T_F": "2026-12-02T00:00:00Z"}), encoding="utf-8")
    monkeypatch.setattr(guard, "holdout_opened", lambda repo=None: True)
    raw = tmp_path / "raw"
    d = raw / "fut_tickers" / "date=2026-12-01"
    d.mkdir(parents=True)
    import gzip
    with gzip.open(d / "23.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"recv_ts": "2026-12-01T23:59:00Z", "ok": True, "payload": {"tickers": [
            {"symbol": "PF_ETHUSD", "bid": 1, "ask": 1, "markPrice": 1, "volumeQuote": 1}]}}) + "\n")
    s = fx.read_streams(raw, pd.Timestamp("2026-12-01T23:00Z"), pd.Timestamp("2026-12-02T01:00Z"), ["ETH"],
                        repo=tmp_path, purpose="evidence")
    assert "ETH" not in s.fut_ticks  # received before T_F


# --- M7: Newey-West threshold uses the look's own p ---------------------------------------------------

def test_nw_threshold_ignores_closed_slots():
    fam = vd.ForwardFamily(cand=("H3-k6",), feas=())
    p_go = {"H3-k6": 0.00024, "H3-k4": 1e-9}  # H3-k4 is CLOSED: its p must not rank first
    res = fam.look(90, p_go, lambda c, rej: vd.Verdict(c, "FINAL GO" if rej else "INCONCLUSIVE", "", []),
                   nw_p={"H3-k6": (0.0002, 0.0002)})
    assert res["holm"]["H3-k6"]["alpha"] == pytest.approx(0.001 / 4)
    assert fam.status["H3-k6"] == "FINAL GO"
    fam2 = vd.ForwardFamily(cand=("H3-k6",), feas=())
    fam2.look(90, p_go, lambda c, rej: vd.Verdict(c, "FINAL GO" if rej else "INCONCLUSIVE", "", []),
              nw_p={"H3-k6": (0.0003, 0.0002)})
    assert fam2.status["H3-k6"] == "PENDING"  # NW 0.0003 > 0.00025


# --- M8: forward P7 / P11 are required ----------------------------------------------------------------

def test_forward_without_p7_or_p11_rows_is_an_invalid_run():
    P = {k: {"evaluable": True, "pass": True} for k in ("P1", "P3", "P4", "P5", "P6", "P10", "P12",
                                                        "P8a", "P8b", "P8c", "P8d", "P9")}
    P["P7"] = {"evaluable": False, "pass": None}
    P["P11"] = {"evaluable": True, "pass": True}
    from luna.v6.criteria import Evaluation
    ev = Evaluation("H3-k4", "H3", "forward", 1.0, 1.0, P=P, evidence={"ok": True}, gate="VALID",
                    stats={"p_go": 0.0, "lb_ok": True})
    assert vd.ladder(ev, stat_reject=True).cls == "INVALID RUN"


def test_forward_cost2x_view_subtracts_realised_costs_once_more():
    d = pd.DataFrame({"net": [1.0, 2.0], "fees": [0.1, 0.2], "spread_slip": [0.05, 0.05], "carry": [0.5, 0.5],
                      "ret": [0.001, 0.002], "carry_ret": [0.0, 0.0], "active": [True, True]})
    v = fw.cost2x_view(SimpleNamespace(daily=d))
    assert v.daily["net"].sum() == pytest.approx(3.0 - 0.4)


def test_forward_pit10_market_uses_snapshots_received_five_minutes_earlier():
    s = fx.Streams()
    t = pd.Timestamp("2026-12-01T00:00Z")
    s.fut_ticks["ETH"] = fx.Stream([fx.Tick(t - pd.Timedelta("6min"), 99, 101, 100), fx.Tick(t - pd.Timedelta("1min"), 199, 201, 200)])
    m = fx.FwdMarket(s, None, ["ETH"], pit_lag=pd.Timedelta("5min"))
    assert m.mid("ETH", t) == pytest.approx(100.0)


# --- M9: blind run in invariant mode --------------------------------------------------------------------

def test_invariant_mode_disables_performance_dependent_risk_exits():
    w = build_world(shocks=[("A00", pd.Timestamp("2025-05-06T12:00Z"), 4.0)])
    eng = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                   equity0=1000.0)
    r = eng.run(w.start, w.start + 8 * DAY, invariant_mode=True)
    assert r.counters["kill"] == 0 and r.counters["stops"] == 0
    assert len([d for d in r.decisions]) == 8  # one decision per day, independent of performance


# --- M3b: V5 Stage 0 audit on the holdout download (spec S-GUARD-1) -----------------------------------

def _spreads(lo, hi, drop=None, value=0.0005):
    idx = pd.date_range(lo, hi - pd.Timedelta("5min"), freq="5min", tz="UTC").as_unit("ns")
    s = pd.Series(value, index=idx)
    if drop is not None:
        s = s[~drop(s.index)]
    return s


def test_stage0_audit_passes_a_complete_5_minute_series():
    from luna.v6 import holdout_data as hd

    lo, hi = pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-07-03", tz="UTC")
    a = hd.stage0_audit({"ETH": _spreads(lo, hi)}, lo, hi)
    assert a["pass"] and a["failures"] == []


def test_stage0_audit_fails_an_hour_of_day_cell_below_ninety_percent():
    from luna.v6 import holdout_data as hd

    lo, hi = pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-07-11", tz="UTC")
    s = _spreads(lo, hi, drop=lambda i: (i.hour == 3))  # one hour of the day missing every day
    a = hd.stage0_audit({"ETH": s}, lo, hi)
    assert not a["pass"] and any("hour" in f for f in a["failures"])


def test_stage0_audit_fails_off_grid_labels_and_rows_outside_the_window():
    from luna.v6 import holdout_data as hd

    lo, hi = pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-07-02", tz="UTC")
    s = _spreads(lo, hi)
    off = pd.concat([s, pd.Series([0.0005], index=pd.DatetimeIndex([lo + pd.Timedelta("61s")]).as_unit("ns"))]).sort_index()
    assert any("grid" in f for f in hd.stage0_audit({"ETH": off}, lo, hi)["failures"])
    late = pd.concat([s, pd.Series([0.0005], index=pd.DatetimeIndex([hi]).as_unit("ns"))])
    assert any("window" in f for f in hd.stage0_audit({"ETH": late}, lo, hi)["failures"])


def test_a_failed_stage0_audit_makes_every_holdout_config_an_invalid_run():
    from luna.v6 import runner

    ev = SimpleNamespace(invalid_run=[])
    runner.apply_audit({"x": ev}, {"pass": False, "failures": ["ETH: decision-window coverage 0.80 < 0.90"]})
    assert ev.invalid_run and "Stage 0" in ev.invalid_run[0]


def test_blind_runs_use_invariant_mode_and_report_no_cash_counter(monkeypatch):
    from luna.v6 import blind, runner

    seen = {}

    def fake(inp, name, start, end, rows=None, models=None, invariant_mode=False):
        seen[name] = invariant_mode
        raise StopIteration

    monkeypatch.setattr(blind, "run_config", fake)
    monkeypatch.setattr(blind, "load_dev", lambda root: SimpleNamespace(funding=None))
    monkeypatch.setattr(blind, "availability", lambda inp, s, e: {})
    monkeypatch.setattr(blind, "H2a", lambda f, h: None)
    monkeypatch.setattr(blind, "_pit_checks", lambda inp, s, e: {})
    with pytest.raises(StopIteration):
        blind.blind_dev_run(Path("."))
    assert seen and all(seen.values())
    import inspect
    assert "skipped_for_cash" not in inspect.getsource(blind)


@pytest.mark.parametrize("rec,ok", [
    ({"review_pass_at": "2026-10-13T15:20:00Z", "T_F": "2026-10-15T00:00:00Z"}, True),
    ({"review_pass_at": "2026-10-13T00:00:00Z", "T_F": "2026-10-14T00:00:00Z"}, True),
    ({"review_pass_at": "2026-10-13T15:20:00Z", "T_F": "2026-10-14T00:00:00Z"}, False),  # < 24 h
    ({"review_pass_at": "2026-10-13T15:20:00Z", "T_F": "2026-10-16T00:00:00Z"}, False),  # not the first
    ({"review_pass_at": "2026-10-13T15:20:00Z", "T_F": "2026-10-15T06:00:00Z"}, False),  # not 00:00
    ({"review_pass_at": "2026-10-13T10:00:00Z", "T_F": "2026-10-15T00:00:00Z"}, True),
    ({"review_pass_at": "2026-10-13T10:00:00Z", "T_F": "2026-10-14T00:00:00Z"}, False),  # only 14 h
    ({"T_F": "2026-10-15T00:00:00Z"}, False),
])
def test_t_f_is_the_first_midnight_at_least_24h_after_the_review_pass(tmp_path, rec, ok):
    p = tmp_path / guard.FREEZE_RECORD_REL
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    if ok:
        assert guard.forward_start(tmp_path) == pd.Timestamp(rec["T_F"])
    else:
        with pytest.raises(guard.HoldoutBlocked, match="freeze record"):
            guard.forward_start(tmp_path)


def test_holdout_report_directory_is_claimed_exclusively_before_anything_is_written(tmp_path, monkeypatch):
    from luna.v6 import report

    def boom(*a, **k):
        raise RuntimeError("crash mid-write")

    monkeypatch.setattr(report, "_summary", boom)
    with pytest.raises(RuntimeError):
        report.write_holdout({}, tmp_path / "out", {})
    monkeypatch.undo()
    with pytest.raises(FileExistsError):  # a crashed first write still blocks a second one
        report.write_holdout({}, tmp_path / "out", {})
