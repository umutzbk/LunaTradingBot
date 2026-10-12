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
    ev = evaluate("H3-k4", "H3", "forward", r, {}, "VALID")
    assert any("funding unknown" in x for x in ev.invalid_run)
    # S-DATA-1: only forward makes unknown funding INVALID RUN; historically it is counted and settled as 0
    hist = evaluate("H3-k4", "H3", "holdout", r, {}, "VALID")
    assert not any("funding unknown" in x for x in hist.invalid_run)


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



# --- round 3: re-review findings ---------------------------------------------------------------------

def test_a_non_finite_newey_west_p_blocks_the_interim_rejection():
    fam = vd.ForwardFamily(cand=("H3-k6",), feas=())
    fam.look(90, {"H3-k6": 1e-9}, lambda c, rej: vd.Verdict(c, "FINAL GO" if rej else "INCONCLUSIVE", "", []),
             nw_p={"H3-k6": (1e-9, float("nan"))})
    assert fam.status["H3-k6"] == "PENDING"


def test_holdout_inputs_after_the_opening_load_only_through_the_forward_guard(tmp_path, monkeypatch):
    from luna.v6 import holdout_data as hd

    called, seen = {}, {}
    monkeypatch.setattr(hd, "_assemble", lambda frozen, root, scope="holdout": called.setdefault("ok", True))
    monkeypatch.setattr(hd, "_verify", lambda root, pinned: seen.setdefault("pin", pinned))
    pin = {"v": "abc"}  # a pin exists throughout: each refusal below is the forward guard's own
    monkeypatch.setattr(guard, "pinned_manifest", lambda repo=None: pin["v"])
    opened = {"v": False}
    monkeypatch.setattr(guard, "holdout_opened", lambda repo=None: opened["v"])
    with pytest.raises(guard.HoldoutBlocked, match="code freeze"):  # no T_F
        hd.load_holdout_inputs(Path("frozen"), Path("root"), repo=tmp_path)
    rec = tmp_path / guard.FREEZE_RECORD_REL
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"review_pass_at": "2026-11-18T12:00:00Z", "T_F": "2026-11-20T00:00:00Z"}))
    with pytest.raises(guard.HoldoutBlocked, match="holdout opening"):  # T_F, but not opened
        hd.load_holdout_inputs(Path("frozen"), Path("root"), repo=tmp_path)
    opened["v"], pin["v"] = True, None
    with pytest.raises(guard.HoldoutBlocked, match="pinned"):  # opened, but no pinned manifest
        hd.load_holdout_inputs(Path("frozen"), Path("root"), repo=tmp_path)
    assert not called and not seen
    pin["v"] = "abc"
    assert hd.load_holdout_inputs(Path("frozen"), Path("root"), repo=tmp_path) is True
    assert seen["pin"] == "abc"


def test_verify_refuses_a_manifest_or_file_other_than_the_pinned_one(tmp_path):
    import hashlib

    from luna.v6 import holdout_data as hd

    (tmp_path / "mark").mkdir()
    (tmp_path / "mark" / "ETH.parquet").write_bytes(b"data")
    man = {"files": {"mark/ETH.parquet": hashlib.sha256(b"data").hexdigest()}}
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    pin = hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()
    hd._verify(tmp_path, pin)
    with pytest.raises(guard.HoldoutBlocked, match="pinned"):
        hd._verify(tmp_path, "0" * 64)
    (tmp_path / "mark" / "ETH.parquet").write_bytes(b"changed")
    with pytest.raises(guard.HoldoutBlocked, match="changed"):
        hd._verify(tmp_path, pin)


def test_pins_need_the_opening_token_and_both_markers_must_agree(tmp_path, monkeypatch):
    ms = [tmp_path / "a" / guard.MARKER, tmp_path / "b" / guard.MARKER]
    for m in ms:
        m.parent.mkdir(parents=True)
        m.write_text(json.dumps({"spec_sha256": guard.SPEC_SHA256, "approved": True}))
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: ms)
    with pytest.raises(guard.HoldoutBlocked):
        guard.pin_manifest("abc", None)
    assert guard.pinned_manifest() is None
    for i, m in enumerate(ms):
        (m.parent / guard.PIN).write_text(json.dumps({"run_id": guard.RUN_ID, "spec_sha256": guard.SPEC_SHA256,
                                                      "manifest_sha256": "abc" if i == 0 else "xyz"}))
    assert guard.pinned_manifest() is None  # disagreeing pins
    (ms[1].parent / guard.PIN).write_text(json.dumps({"run_id": guard.RUN_ID, "spec_sha256": guard.SPEC_SHA256,
                                                       "manifest_sha256": "abc"}))
    assert guard.pinned_manifest() == "abc"


def test_marker_paths_do_not_depend_on_the_checkout_path(tmp_path):
    a = guard.marker_paths(guard.code_repo())
    assert all(guard.RUN_ID in str(m) for m in a)
    assert all(str(guard.code_repo()) not in str(m) or ".git" in str(m) for m in a)


def test_h1_reconciles_a_one_legged_pair_at_once():
    from luna.v6.h1 import _Pos, _St

    w, s = fwd_world()
    insts = {a: {"perp": {"step": 0.001, "min_qty": 0.001}, "spot": {"step": 0.001, "min_qty": 0.001}} for a in w.market.assets}
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    e = H1Engine(m, Fixed720(w.funding_levels), insts, H1Config("H1-m1.5", 1.5))
    e.counters = {"unfilled_pairs": 0, "hedge_reconciliations": 0}
    st = _St(usd=10_000.0)
    a, b = w.market.assets[1], w.market.assets[2]
    st.pos[a] = _Pos(2.0, 0.0, 100.0, 100.0, 0.0)  # spot filled, perp hedge missing
    st.pos[b] = _Pos(2.0, 2.0, 100.0, 100.0, 200.0)  # matched
    e._reconcile(st, w.start)
    assert e.counters["hedge_reconciliations"] == 1 and st.exiting == {a}
    (sfs, pfs), intent = st.pending[0]
    assert intent == "reconcile" and pfs == [] and sfs[0].dq == pytest.approx(-2.0)


def test_blind_sizing_does_not_depend_on_realised_pnl():
    w = build_world()
    rich = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                    equity0=10_000.0)
    r1 = rich.run(w.start, w.start + 3 * DAY, invariant_mode=True)
    w2 = build_world(shocks=[("A00", w.start + pd.Timedelta("30h"), 1.3)])  # different P&L path, same days
    r2 = H3Engine(w2.market, FixedModel(w2.funding_levels), w2.prices, w2.vol, w2.insts, H3Config("H3-k4", 4),
                  equity0=10_000.0).run(w2.start, w2.start + 3 * DAY, invariant_mode=True)
    e1 = [d.get("target") for d in r1.decisions if d.get("status") == "OK"]
    e2 = [d.get("target") for d in r2.decisions if d.get("status") == "OK"]
    assert e1 and e2
    g1 = [sum(abs(v) for v in t.values()) for t in e1]
    g2 = [sum(abs(v) for v in t.values()) for t in e2]
    assert g1[0] == pytest.approx(g2[0])  # first build identical (same E0)


def test_blind_builds_are_identical_when_only_the_realised_equity_differs():
    class Richer(H3Engine):
        def _equity(self, st, t):  # the same market, a different realised P&L path
            return super()._equity(st, t) + (6_000.0 if t > self._t0 + DAY else 0.0)  # beyond the 25 % band

    w = build_world()
    args = (w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4))
    base = H3Engine(*args, equity0=10_000.0).run(w.start, w.start + 4 * DAY, invariant_mode=True)
    rich = Richer(*args, equity0=10_000.0)
    rich._t0 = w.start
    r2 = rich.run(w.start, w.start + 4 * DAY, invariant_mode=True)
    t1 = [d["target"] for d in base.decisions if d.get("status") == "OK"]
    t2 = [d["target"] for d in r2.decisions if d.get("status") == "OK"]
    assert len(t1) >= 3 and len(t1) == len(t2)
    for a, b in zip(t1, t2):
        assert a == pytest.approx(b)


def test_forward_look_refuses_inputs_that_stop_before_the_holdout_end():
    hidx = pd.date_range("2026-05-01", "2026-06-30T23:00", freq="h", tz="UTC")
    short = SimpleNamespace(funding=FundingPanel(pd.DataFrame({"ETH": 1e-5}, index=hidx)),
                            prices=pd.DataFrame({"ETH": 1.0}, index=hidx))
    with pytest.raises(vd.LookError, match="holdout end"):
        fw.check_inputs_reach(short)
    full = pd.date_range(pd.Timestamp("2026-05-01", tz="UTC"), guard.HOLDOUT_END.floor("h") - pd.Timedelta("1h"),
                         freq="h")
    ok = SimpleNamespace(funding=FundingPanel(pd.DataFrame({"ETH": 1e-5}, index=full)),
                         prices=pd.DataFrame({"ETH": 1.0}, index=full + pd.Timedelta("1h")))
    fw.check_inputs_reach(ok)


def test_h1_closes_a_perp_only_remainder_through_the_perp_leg():
    from luna.v6.h1 import _Pos, _St

    w, s = fwd_world()
    insts = {a: {"perp": {"step": 0.001, "min_qty": 0.001}, "spot": {"step": 0.001, "min_qty": 0.001}} for a in w.market.assets}
    m = fx.FwdMarket(s, w.market.funding, w.market.assets)
    e = H1Engine(m, Fixed720(w.funding_levels), insts, H1Config("H1-m1.5", 1.5))
    e.counters = {"unfilled_pairs": 0}
    st = _St(usd=10_000.0)
    a = w.market.assets[1]
    st.pos[a] = _Pos(0.0, 2.0, 100.0, 100.0, 200.0)  # spot leg already sold, perp short left
    e._close(st, a, w.start, "stop")
    assert len(st.pending) == 1
    (sfs, pfs), _ = st.pending[0]
    assert sfs == [] and len(pfs) == 1 and pfs[0].dq == pytest.approx(2.0)


def test_pit10_lags_decision_quotes_but_not_marks_or_fill_reference_mids():
    from tests.v6.test_forward_exec import M, S, T, streams

    s = streams(ticks=[(-30 * S, 100.0, 100.2)], depths=[(1 * M, [(100.0, 5)], [(100.2, 5)])],
                fticks=[(-10 * M, 89.9, 90.1), (-1 * M, 99.9, 100.1)],
                fbooks=[(1 * M, [(98.9, 50.0)], [(99.1, 50.0)])])  # book mid 99 != ticker mid 100
    m = fx.FwdMarket(s, None, ["ETH"], pit_lag=pd.Timedelta("5min"))
    assert m.decision_mid("ETH", T) != m.decision_mid("ETH", T)  # the t-5m view has no fresh ticker (NaN)
    assert m.mark_mid("ETH", T) == pytest.approx(100.0)
    f = m.fill("ETH", 1.0, T)
    assert f.mid == pytest.approx(100.0)  # the fill's reference mid is the ticker at fill time, not lagged


def test_funding_delayed_view_shifts_visibility_only():
    hidx = pd.date_range("2026-05-01", periods=48, freq="h", tz="UTC")
    p = FundingPanel(pd.DataFrame({"ETH": 1e-5}, index=hidx), abs_=pd.DataFrame({"ETH": 1e-3}, index=hidx))
    d = p.delayed(pd.Timedelta("5min"))
    t = hidx[10] + pd.Timedelta("1h")  # period hidx[10] is known at exactly t
    assert p.abs_rate("ETH", hidx[10], t) == pytest.approx(1e-3)
    assert d.abs_rate("ETH", hidx[10], t) != d.abs_rate("ETH", hidx[10], t)
    assert d.abs_rate("ETH", hidx[10], t + pd.Timedelta("5min")) == pytest.approx(1e-3)


def test_feas_confirmation_coverage_uses_its_own_window():
    days = pd.date_range("2026-11-20", periods=180, freq="D", tz="UTC")
    cov = pd.DataFrame({"share": [0.5] * 90 + [1.0] * 90, "evaluable": [False] * 90 + [True] * 90}, index=days)
    assert fw.coverage_share(cov, days[0]) == pytest.approx(0.5)  # candidate: evaluable days 1..180
    assert fw.coverage_share(cov, days[90]) == pytest.approx(1.0)  # FEAS C: days 91..180


def test_coverage_is_the_share_of_evaluable_days_not_the_mean_snapshot_share():
    days = pd.date_range("2026-11-20", periods=90, freq="D", tz="UTC")
    cov = pd.DataFrame({"share": [0.85] * 90, "evaluable": [False] * 90}, index=days)  # every day 85 % fresh
    assert fw.coverage_share(cov, days[0]) == 0.0  # S-DATA-5: no evaluable day


def test_family_file_is_never_left_empty(tmp_path):
    fw.init_family(["H3-k4"], [], tmp_path)
    assert json.loads((tmp_path / fw.STATE_REL).read_text(encoding="utf-8"))["cand"] == ["H3-k4"]


def test_stage0_audit_fails_a_truncated_bar_download():
    from luna.v6 import holdout_data as hd

    lo, hi = pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-07-11", tz="UTC")
    full = pd.date_range(lo, hi - pd.Timedelta("15min"), freq="15min")
    cut = full[full < lo + pd.Timedelta("5D")]
    ok = hd.stage0_audit({"ETH": _spreads(lo, hi)}, lo, hi, bars={"mark": {"ETH": full}})
    assert ok["pass"]
    bad = hd.stage0_audit({"ETH": _spreads(lo, hi)}, lo, hi, bars={"mark": {"ETH": cut}})
    assert not bad["pass"] and any("truncated" in f for f in bad["failures"])


def test_h1_historical_closes_a_perp_only_remainder_through_the_perp_leg():
    from luna.v6.h1 import _Pos, _St
    from tests.v6.test_h1 import h1_world

    w, insts = h1_world()
    e = H1Engine(w.market, Fixed720(w.funding_levels), insts, H1Config("H1-m1.5", 1.5))
    e.counters = {"unfilled_pairs": 0, "hedge_reconciliations": 0}
    st = _St(usd=10_000.0)
    a = w.market.assets[1]
    st.pos[a] = _Pos(0.0, 2.0, 100.0, 100.0, 200.0)
    e._close(st, a, w.start, "stop")
    assert len(st.pending) == 1 and st.retry == []
    (sfs, pfs), _ = st.pending[0]
    assert sfs == [] and pfs[0].dq == pytest.approx(2.0)


def test_engines_value_positions_at_the_mark_view_not_the_lagged_decision_view():
    from luna.v6.engine import _State
    from luna.v6.h1 import _Pos, _St
    from tests.v6.test_forward_exec import M, S, T, streams

    s = streams(ticks=[(-10 * M, 89.9, 90.1), (-2 * M, 99.9, 100.1)], depths=[(1 * M, [(100.0, 5)], [(100.2, 5)])],
                fticks=[(-10 * M, 89.9, 90.1), (-2 * M, 99.9, 100.1)])
    m = fx.FwdMarket(s, None, ["ETH"], pit_lag=pd.Timedelta("5min"))
    h3 = H3Engine(m, FixedModel(pd.Series({"ETH": 0.0})), pd.DataFrame(), pd.DataFrame(), {}, H3Config("H3-k4", 4))
    st = _State(cash=0.0)
    st.pos["ETH"] = 1.0
    assert h3._equity(st, T) == pytest.approx(100.0)  # the decision view (t - 5 min) has no fresh ticker
    h1 = H1Engine(m, Fixed720(pd.Series({"ETH": 0.0})), {}, H1Config("H1-m1.5", 1.5))
    s1 = _St(usd=0.0)
    s1.pos["ETH"] = _Pos(1.0, 1.0, 100.0, 100.0, 100.0)
    assert h1.equity(s1, T) == pytest.approx(0.0)  # spot 100 - perp 100, both at the mark view


def test_a_repair_flag_left_by_a_full_exit_does_not_fire_on_the_next_build():
    w0 = build_world()
    first = H3Engine(w0.market, FixedModel(w0.funding_levels), w0.prices, w0.vol, w0.insts, H3Config("H3-k4", 4),
                     equity0=10_000.0).run(w0.start, w0.start + DAY + pd.Timedelta("1h"))
    ok = [d for d in first.decisions if d.get("status") == "OK"][0]
    when = w0.start + pd.Timedelta("12h")
    shocks = [(a, when, 1.25) for a in ok["short"]] + [(a, when, 0.75) for a in ok["long"]]  # every leg stops
    w = build_world(shocks=shocks)
    r = H3Engine(w.market, FixedModel(w.funding_levels), w.prices, w.vol, w.insts, H3Config("H3-k4", 4),
                 equity0=10_000.0).run(w.start, w.start + 6 * DAY)
    assert r.counters["stops"] == len(ok["long"]) + len(ok["short"])  # all legs exited intraday
    builds = [d["t"] for d in r.decisions if d.get("status") == "OK"]
    assert any(b > when for b in builds)  # rebuilt after the cooldown
    assert r.counters["repairs"] == 0  # the flag left by the full exit did not fire on the fresh book



def test_forward_look_refuses_when_any_asset_stops_before_the_holdout_end():
    full = pd.date_range(pd.Timestamp("2026-05-01", tz="UTC"), guard.HOLDOUT_END.floor("h") - pd.Timedelta("1h"),
                         freq="h")
    rel = pd.DataFrame({"ETH": 1e-5, "BTC": 1e-5}, index=full)
    rel.loc[rel.index > full[-1] - pd.Timedelta("3h"), "BTC"] = np.nan  # BTC stops 3 h early
    short = SimpleNamespace(funding=FundingPanel(rel), prices=pd.DataFrame({"ETH": 1.0, "BTC": 1.0},
                                                                           index=full + pd.Timedelta("1h")))
    with pytest.raises(vd.LookError, match="BTC"):
        fw.check_inputs_reach(short)


def test_pins_are_written_all_or_nothing(tmp_path, monkeypatch):
    ms = [tmp_path / "a" / guard.MARKER, tmp_path / "b" / guard.MARKER]
    for m in ms:
        m.parent.mkdir(parents=True)
    (ms[1].parent / guard.PIN).write_text("{}")  # the second pin cannot be created
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: ms)
    auth = guard.HoldoutAuthorization(guard.HoldoutAuthorization._secret, {})
    with pytest.raises(FileExistsError):
        guard.pin_manifest("abc", auth)
    assert not (ms[0].parent / guard.PIN).exists()  # rolled back


class _FlakyClient:
    pass


def test_holdout_download_resumes_after_a_transient_failure_without_refetching(tmp_path, monkeypatch):
    from luna.v6 import holdout_data as hd

    calls = {"n": 0, "fail": True}
    frame = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex([guard.HOLDOUT_START]).as_unit("ns"))

    def fetch(name):
        def f(client, a, auth, *rest, **kw):
            calls["n"] += 1
            if name == "spot" and a == "BTC" and calls["fail"]:
                raise RuntimeError("EGeneral:Too many requests")
            return frame
        return f

    for nm in ("funding", "spreads", "spot"):
        monkeypatch.setattr(hd, f"fetch_{nm}", fetch(nm))
    monkeypatch.setattr(hd, "fetch_candles", lambda client, tick, a, auth: fetch("candles")(client, a, auth))
    monkeypatch.setattr(hd, "RETRY_WAIT", 0.0)
    pinned = []
    monkeypatch.setattr(guard, "pin_manifest", lambda sha, auth, repo=None: pinned.append(sha))
    auth = guard.HoldoutAuthorization(guard.HoldoutAuthorization._secret, {})
    with pytest.raises(RuntimeError):
        hd.download(_FlakyClient(), tmp_path, ["ETH", "BTC"], auth)
    assert not (tmp_path / "manifest.json").exists() and not pinned
    first = calls["n"]
    calls["fail"] = False
    man = hd.download(_FlakyClient(), tmp_path, ["ETH", "BTC"], auth)  # the same authorised process resumes
    assert calls["n"] - first == 1  # only the missing file is fetched again
    assert len(man) == 12 and len(pinned) == 1
    with pytest.raises(FileExistsError):  # the manifest is write-once
        hd.download(_FlakyClient(), tmp_path, ["ETH", "BTC"], auth)


def test_transient_rate_limit_is_retried(monkeypatch):
    from luna.v6 import holdout_data as hd

    n = {"k": 0}

    def flaky():
        n["k"] += 1
        if n["k"] < 3:
            raise RuntimeError("EGeneral:Too many requests")
        return 7

    monkeypatch.setattr(hd, "RETRY_WAIT", 0.0)
    assert hd._retry(flaky) == 7 and n["k"] == 3


def test_a_completed_download_is_never_rewritten_even_if_a_part_was_altered(tmp_path, monkeypatch):
    from luna.v6 import holdout_data as hd

    calls = {"n": 0}
    frame = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex([guard.HOLDOUT_START]).as_unit("ns"))

    def f(client, a, auth, *rest, **kw):
        calls["n"] += 1
        return frame

    for nm in ("funding", "spreads", "spot"):
        monkeypatch.setattr(hd, f"fetch_{nm}", f)
    monkeypatch.setattr(hd, "fetch_candles", lambda client, tick, a, auth: f(client, a, auth))
    monkeypatch.setattr(guard, "pin_manifest", lambda sha, auth, repo=None: None)
    auth = guard.HoldoutAuthorization(guard.HoldoutAuthorization._secret, {})
    hd.download(_FlakyClient(), tmp_path, ["ETH"], auth)
    part = tmp_path / "mark" / "ETH.parquet"
    part.write_bytes(b"altered")
    n0 = calls["n"]
    with pytest.raises(FileExistsError):
        hd.download(_FlakyClient(), tmp_path, ["ETH"], auth)
    assert calls["n"] == n0 and part.read_bytes() == b"altered"  # no refetch, no rewrite after completion
