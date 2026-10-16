"""§15 forward replay (DK-105, ruling R39; AMB-43): the decision shift within 2 h, skipped decisions and their
exits, the stale-quarter decision no-op, the canonical L7 gate (no forged L7), and one synthetic end-to-end
forward pass into ``forward.forward_evaluate`` (byte-identical across two runs).

Synthetic only: engine fixtures (flat prices) on a short 2024 window for the engine hook; the canonical
R_V6 / T_F7 records from ``auth_fixtures.canonical_repo`` behind ``guard._roots`` for the gated replay; the
trusted clock pinned per test."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from luna.v7 import accounting, forward, forward_replay, guard, k9, release, runrecord, verdict, windows
from luna.v7.book.engine import EngineInputs, Instrument, run
from luna.v7.book.execution import INVALID_MARK
from luna.v7.data.instances import Instance
from luna.v7.features import FEATURES
from luna.v7.forward_model import ForwardScoreModel, forward_model, load_forward_model
from luna.v7.forward_replay import DecisionPlan, decision_plan, replay
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.timeutil import MIN, D, H, to_ns
from tests.v7 import auth_fixtures as af
from tests.v7.book.engine_fixtures import (
    BTC,
    LO,
    NOW,
    b15,
    base_inputs,
    fund,
    gate_row,
    instrument,
    m1,
    provider,
)

Q = 15 * MIN
G0 = LO + 5 * MIN  # the first S-8.1 grid decision of the engine window
TF7 = to_ns("2026-10-16")  # canonical T_F7 of auth_fixtures.canonical_repo (review PASS 2026-10-15)
L7 = to_ns("2027-10-15")  # max(T_F7 + 180 d, R_V6 = 2027-10-14) + 1 d
WIN = (TF7, TF7 + 180 * D)


def _ev(bad=()):
    bad = set(int(b) for b in bad)
    return lambda q: int(q) not in bad


# --------------------------------------------------------------------------------- the decision plan (pure)


def test_plan_on_time_shift_and_skip():
    win = (LO, LO + 2 * D)
    g1, g2, g3 = G0, G0 + 8 * H, G0 + 16 * H
    q2 = g2 - 5 * MIN
    bad = [LO] + [q2 + k * Q for k in range(9)]  # g1 shifted by one quarter; g2: no evaluable quarter in 2 h
    p = decision_plan(win, _ev(bad))
    assert isinstance(p, DecisionPlan)
    assert p.schedule[g1 + Q] is True and g1 not in p.schedule  # shifted, same offset inside the quarter
    assert p.shifted[0] == (g1, g1 + Q)
    assert p.skipped[0] == (g2, g2 + 9 * Q)  # skipped: its exits at the first evaluable quarter after t
    assert p.schedule[g2 + 9 * Q] is False  # no entries
    assert p.schedule[g3] is True and g2 not in p.schedule


def test_plan_shift_limit_is_two_hours():
    win = (LO, LO + 2 * D)
    q = G0 - 5 * MIN
    p = decision_plan(win, _ev([q + k * Q for k in range(8)]))  # quarter k = 8 (t + 2 h) is evaluable
    assert p.schedule[G0 + 8 * Q] is True and p.skipped == ()


def test_plan_skipped_exits_merge_with_the_next_full_decision():
    win = (LO, LO + 2 * D)
    q = G0 - 5 * MIN
    bad = [q + k * Q for k in range(32)]  # 8 h of stale quarters: the next grid decision is evaluable
    p = decision_plan(win, _ev(bad))
    assert p.skipped[0] == (G0, G0 + 8 * H)
    assert p.schedule[G0 + 8 * H] is True  # the full decision of the next grid point carries the exits


def test_plan_no_evaluable_quarter_left():
    win = (LO, LO + 2 * D)
    p = decision_plan(win, lambda q: False)
    assert p.schedule == {} and all(x is None for _, x in p.skipped)


# --------------------------------------------------------------------------------- the engine hook


def _with_plan(schedule, **kw):
    return EngineInputs(**{**base_inputs(**kw).__dict__, "decisions": dict(schedule)})


def test_engine_runs_exactly_the_scheduled_decisions():
    r = run("A0", _with_plan({G0 + Q: True, G0 + 8 * H: True}), (LO, LO + 2 * D), 24)
    assert r.valid and list(r.decisions["t"]) == [G0 + Q, G0 + 8 * H]
    legs = r.fills[r.fills["symbol"] != BTC]
    assert legs["ts"].min() == G0 + Q + 15 * MIN  # the shifted decision fills at its own t + 15 min


def test_skipped_decision_makes_no_entries():
    r = run("A0", _with_plan({G0: False, G0 + 8 * H: True}), (LO, LO + 2 * D), 24)
    plain = run("A0", base_inputs(), (LO, LO + 2 * D), 24)
    assert plain.fills["ts"].min() == G0 + 15 * MIN  # the grid decision would have entered
    assert r.valid and r.fills["ts"].min() == G0 + 8 * H + 15 * MIN
    assert "forward_exits_only" in r.decisions.iloc[0]["notes"]


def test_skipped_decision_still_exits():
    def g6(t, s):  # AAA's U6 gate fails from the second decision on: a decision (U6) exit
        return gate_row(t, u6=False) if (s == "AAAUSDT" and t >= LO + 8 * H) else gate_row(t)

    r = run("A0", _with_plan({G0: True, G0 + 8 * H: False}, gates=g6), (LO, LO + 2 * D), 24)
    assert r.valid
    f = r.fills[(r.fills["symbol"] == "AAAUSDT") & (r.fills["ts"] == G0 + 8 * H + 15 * MIN)]
    assert len(f) == 1 and f.iloc[0]["reason"] == "u6"
    assert not r.fills["position_id"].astype(str).str.endswith(f"@{G0 + 8 * H}").any()  # nothing opened


def _btc(r, ts):
    return r.fills[(r.fills["symbol"] == BTC) & (r.fills["ts"] == ts)]


def test_exits_only_decision_rehedges_after_its_exits():
    """AMB-43 (c): the S-7.4 hedge follows the exits-only post-decision book."""
    def g6(t, s):
        return gate_row(t, u6=False) if (s == "AAAUSDT" and t >= LO + 8 * H) else gate_row(t)

    r = run("A0", _with_plan({G0: True, G0 + 8 * H: False}, gates=g6), (LO, LO + 2 * D), 24)
    te = G0 + 8 * H + 15 * MIN
    assert r.valid and len(_btc(r, G0 + 15 * MIN)) == 1  # hedged at the entry decision
    assert len(_btc(r, te)) == 1  # AAA (beta 2) exits: the hedge is re-decided on the remaining book
    assert "forward_exits_only" in r.decisions.iloc[1]["notes"]


def test_exits_only_decision_drift_only_hedge_increase():
    """AMB-43 (c), drift-only case: no leg order at the exits-only decision, only beta-hat drifts (AAA 2 ->
    4); the hedge follows the unchanged legs' new beta book: the hedge INCREASES at an exits-only
    decision."""
    base = base_inputs()
    v0 = base.view

    def view(t):
        v = v0(t)
        return replace(v, betas={**v.betas, "AAAUSDT": 4.0}) if t >= LO + 8 * H else v

    inp = EngineInputs(**{**base.__dict__, "view": view, "decisions": {G0: True, G0 + 8 * H: False}})
    r = run("A0", inp, (LO, LO + 2 * D), 24)
    te = G0 + 8 * H + 15 * MIN
    legs = r.fills[(r.fills["symbol"] != BTC) & (r.fills["ts"] == te)]
    h0, h1 = _btc(r, G0 + 15 * MIN), _btc(r, te)
    assert r.valid and legs.empty and len(h0) == 1 and len(h1) == 1
    assert abs(h1.iloc[0]["q_after"]) > abs(h0.iloc[0]["q_after"])  # the hedge grew (an opening hedge trade)
    assert h1.iloc[0]["direction"] == h0.iloc[0]["direction"]


def test_engine_schedule_outside_the_window_refused():
    with pytest.raises(ValueError):
        run("A0", _with_plan({LO - Q: True}), (LO, LO + 2 * D), 24)


# --------------------------------------------------------------------------------- A-3: stale-quarter ticks


def _drop15(ins, *opens):
    """No mark-price and no last-price 15m close at the ticks the bars opening at ``opens`` end at."""
    return replace(ins, bars15=ins.bars15[~ins.bars15["open_ts"].isin(opens)].reset_index(drop=True),
                   mark15=ins.mark15[~ins.mark15["open_ts"].isin(opens)].reset_index(drop=True))


def _a3(schedule, tick_evaluable=None, **kw):
    return EngineInputs(**{**base_inputs(**kw).__dict__, "decisions": dict(schedule),
                           "tick_evaluable": tick_evaluable})


WIN2 = (LO, LO + 2 * D)


def test_a3_stale_quarter_tick_without_leg_mark_is_skipped():
    """A-3 (AMENDMENTS-2026-10-16) / controller ruling R59: in the forward replay the tick at ``tick`` needs
    the close of the quarter ENDING at it ([tick - 15 min, tick)); with that quarter stale (isolated) and its
    close missing, the dependent tick is skipped: no mark, no risk action, no INVALID. The tick at the stale
    quarter's own start (its needed quarter is fine) is marked."""
    tick = LO + 2 * H
    stale = tick - Q  # the quarter whose close the tick needs
    aaa = _drop15(instrument("AAAUSDT", 100.0, -1e-4), stale)  # held leg, no close ending at the tick
    r = run("A0", _a3({G0: True}, _ev([stale]), aaa=aaa), WIN2, 24)
    assert r.valid and r.invalid is None
    assert tick not in set(r.marks["ts"]) and tick not in set(r.positions["ts"])  # no mark taken
    assert {stale, tick + Q} <= set(r.marks["ts"])  # the tick at the stale quarter's start is marked
    assert not (r.fills["ts"].between(tick, tick + Q) & (r.fills["role"] == "leg")).any()  # no risk action
    led = accounting.account(r)
    assert accounting.daily_ok(led) and accounting.k10_ok(led)


def test_a3_same_missing_mark_outside_the_forward_skip_is_invalid():
    """Controls: W_DEV / W_HOLD keep AMB-29 (a): the same held leg without a mark is INVALID_MARK on the S-8.1
    grid; in a forward schedule it is INVALID_MARK too when the stale quarter is not the one whose close the
    tick needs (the quarter [tick, tick + 15 min), the pre-R59 key) or when every quarter is evaluable."""
    tick = LO + 2 * H
    aaa = _drop15(instrument("AAAUSDT", 100.0, -1e-4), tick - Q)
    grid = run("A0", base_inputs(aaa=aaa), WIN2, 24)
    assert grid.invalid == INVALID_MARK and grid.invalid_ts == tick
    wrong_quarter = run("A0", _a3({G0: True}, _ev([tick]), aaa=aaa), WIN2, 24)
    assert wrong_quarter.invalid == INVALID_MARK and wrong_quarter.invalid_ts == tick
    all_ok = run("A0", _a3({G0: True}, _ev(), aaa=aaa), WIN2, 24)
    assert all_ok.invalid == INVALID_MARK and all_ok.invalid_ts == tick
    no_pred = run("A0", _a3({G0: True}, None, aaa=aaa), WIN2, 24)
    assert no_pred.invalid == INVALID_MARK and no_pred.invalid_ts == tick


def test_a3_stale_tick_takes_no_risk_action():
    """BBB (the only short) spikes 20 % in the bar [05:30, 05:45) only: on an evaluable tick its leg stop
    fires at 05:45 (and the one-sided flatten follows); with that quarter stale (R59: the quarter the 05:45
    tick needs) the tick is skipped and nothing fires."""
    spike_lo, tick = LO + 5 * H + 30 * MIN, LO + 5 * H + 45 * MIN

    def px(ts):
        return np.where((ts >= spike_lo) & (ts < tick), 60.0, 50.0)

    bbb = instrument("BBBUSDT", px, 1e-4)
    plain = run("A0", _a3({G0: True}, _ev(), bbb=bbb), WIN2, 24)
    assert plain.valid and (plain.fills["reason"] == "leg_stop").any()
    r = run("A0", _a3({G0: True}, _ev([spike_lo]), bbb=bbb), WIN2, 24)
    assert r.valid and not r.fills["reason"].isin(["leg_stop", "one_sided_flatten", "kill"]).any()
    assert tick not in set(r.marks["ts"]) and spike_lo in set(r.marks["ts"])


def test_a3_stale_midnight_tick_defers_the_day_close():
    """No mark at a skipped midnight tick (its needed quarter [23:45, 00:00) stale): the UTC day closes at the
    first marked tick after it (contiguous daily rows; the accounting identities hold)."""
    mid = LO + D
    r = run("A0", _a3({G0: True}, _ev([mid - Q])), WIN2, 24)
    assert r.valid and mid not in set(r.marks["ts"]) and mid - Q in set(r.marks["ts"])
    d = r.daily
    assert d["end_ts"].iloc[0] == mid + Q and d["start_ts"].iloc[1] == mid + Q
    assert d["day"].iloc[1] == mid // D
    led = accounting.account(r)
    assert accounting.daily_ok(led) and accounting.k10_ok(led)


def test_a3_tick_predicate_only_with_a_forward_schedule():
    """The tick skip is a §15 forward-replay hook only: refused on the S-8.1 grid (W_DEV / W_HOLD)."""
    inp = EngineInputs(**{**base_inputs().__dict__, "tick_evaluable": _ev()})
    with pytest.raises(ValueError):
        run("A0", inp, WIN2, 24)


# --------------------------------------------------------------------------------- the gated replay


@pytest.fixture(scope="module")
def canon(tmp_path_factory):
    base = tmp_path_factory.mktemp("fwd_replay")
    return dict(ok=af.canonical_repo(base / "ok"),
                forged=af.canonical_repo(base / "forged", review_pass=None),
                home=base / "home")


def _forged_review(repo):
    af.add_review(repo, "2026-10-15T00:00:00Z", tamper_record=True)
    release.cache_clear()


def _point(monkeypatch, repo, home, now):
    monkeypatch.setattr(guard, "_roots", lambda: (repo, home))
    af.clock(monkeypatch, now)
    release.cache_clear()


def _instrument(sym, px, rate):
    s0 = TF7 - 3 * D
    bb = b15(px, s0, WIN[1])
    return Instrument(sym, m1(px, s0, WIN[1]), bb, bb[["open_ts", "c"]].copy(), fund(rate, px, s0, WIN[1]),
                      [Instance(0, s0, None, None)], {})


@pytest.fixture(scope="module")
def fwd_inputs():
    ins = {"AAAUSDT": _instrument("AAAUSDT", 100.0, -1e-4), "BBBUSDT": _instrument("BBBUSDT", 50.0, 1e-4)}
    def gates(t, s):  # eligible for the first 10 days only: the book is flat afterwards (a fast replay)
        return gate_row(t) if t < TF7 + 10 * D else gate_row(t, u2=False)

    view = provider({"AAAUSDT": -0.003, "BBBUSDT": 0.003}, {"AAAUSDT": 2.0, "BBBUSDT": 1.0}, gates)
    return EngineInputs(ins, _instrument(BTC, 1000.0, 5e-5), view, NOW)


class _Model:
    ml_star = "A2"

    def predict(self, t, cands):
        y = -cands["fl30_r8"].to_numpy() * cands["side"].to_numpy() * 1e4
        return cands.assign(yhat_en=y, yhat_cal_en=y)


def _evaluable(bad=()):
    qs = np.arange(WIN[0], WIN[1], Q)
    bad = set(int(b) for b in bad)
    return {int(q): int(q) not in bad for q in qs}


def test_replay_refused_before_l7(canon, monkeypatch, fwd_inputs, tmp_path):
    _point(monkeypatch, canon["ok"], canon["home"], L7 - 1)
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        with pytest.raises(SealedBeforeL7):
            replay("A0", fwd_inputs, _evaluable(), 24)
        assert log.records()[-1]["rule"] == "§15 W_FWD7 sealed before L7"


def test_forged_l7_is_impossible(canon, monkeypatch, fwd_inputs):
    params = set(inspect.signature(replay).parameters)
    assert not params & {"l7", "L7", "r_v6", "t_f7", "now", "now_ns", "repo", "window"}
    _forged_review(canon["forged"])  # a review-pass record whose record_sha256 does not verify
    _point(monkeypatch, canon["forged"], canon["home"], to_ns("2030-01-01"))
    assert release.l7(canon["forged"]) is None
    with pytest.raises(SealedBeforeL7):
        replay("A0", fwd_inputs, _evaluable(), 24)
    # a caller clock past L7 cannot open the engine's W_FWD7 seal either (trusted clock before L7)
    _point(monkeypatch, canon["ok"], canon["home"], L7 - 1)
    with pytest.raises(SealedBeforeL7):
        run("A2", EngineInputs(**{**fwd_inputs.__dict__, "now_ns": to_ns("2030-01-01")}), WIN, 24, _Model())


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    """A synthetic opening whose repository also carries the canonical R_V6 / T_F7 records, and its frozen
    forward model (refit inside the opening, pinned, then loaded through the record)."""
    from tests.v7 import test_forward_refit as tr

    o = af.opening_once(tmp_path_factory.mktemp("fwd_e2e"))
    af.add_release(o.repo, "2027-10-14T00:00:00Z")
    af.add_review(o.repo, "2026-10-15T00:00:00Z")
    release.cache_clear()
    windows._m6_by_repo.cache_clear()
    mp = pytest.MonkeyPatch()
    try:
        _point(mp, o.repo, o.home, L7)
        hs = tr._hold_samples()
        hs["y_reg"] = 200 * hs["f1_fl1"] + 1  # a strong W_HOLD signal: the frozen model trades in the replay
        hs["y_pos"] = (hs["y_reg"] > 0).astype(float)
        forward_model(tr.dev(), [tr.bridge(o), tr.issued(o, hs)], tr.final(), auth=o.auth)
    finally:
        mp.undo()
    release.cache_clear()
    return o


def _with_features(inputs):
    v0 = inputs.view

    def view(t):  # the §9 feature columns the forward model scores (synthetic, deterministic)
        v = v0(t)
        c = v.cands.copy()
        for f in FEATURES:
            if f not in c.columns:  # "side" is itself a feature
                c[f] = 0.0
        c["f1_fl1"] = -c["fl30_r8"].astype(float) * c["side"].astype(float) * 1e3
        return replace(v, cands=c)

    return replace(inputs, view=view)


def test_replay_ml_arm_needs_the_frozen_model(frozen, monkeypatch, fwd_inputs):
    _point(monkeypatch, frozen.repo, frozen.home, L7)
    with pytest.raises(guard.HoldoutBlocked, match="frozen forward model"):
        replay("A2", fwd_inputs, _evaluable(), 72, _Model())
    fm = load_forward_model()
    with pytest.raises(ValueError, match="record's h"):
        replay("A2", fwd_inputs, _evaluable(), 24, ForwardScoreModel(fm, "A2"))


def _bytes(fr, v) -> bytes:
    body = {"run": runrecord.run_output_sha256(fr.run), "plan": [sorted(fr.plan.schedule.items()),
                                                                list(fr.plan.shifted), list(fr.plan.skipped)],
            "evaluable_days": fr.evaluable_days, "l7": fr.l7, "verdict": [v.cls, list(v.reasons)],
            "criteria": sorted(v.criteria.items()), "missing": list(v.evidence_missing),
            "details": {k: v.details[k] for k in sorted(v.details) if k in ("p_abs", "F_net", "mdd")}}
    return json.dumps(body, sort_keys=True, default=str).encode()


def test_end_to_end_forward_pass_byte_identical(frozen, monkeypatch, fwd_inputs):
    _point(monkeypatch, frozen.repo, frozen.home, L7)
    inputs = _with_features(fwd_inputs)
    g = TF7 + 8 * H + 5 * MIN  # the second grid decision: shifted by 2 quarters
    day3 = TF7 + 3 * D  # a whole stale day: its first decision is skipped, the day is not evaluable
    bad = [g - 5 * MIN, g + 10 * MIN] + list(range(day3, day3 + D, Q))
    outs = []
    for _ in range(2):
        model = ForwardScoreModel(load_forward_model(), "A2")  # the frozen model, through its record only
        fr = replay("A2", inputs, _evaluable(bad), 72, model)
        v = forward_replay.evaluate(fr, None, holdout_class=verdict.ADVANCE, integrity=(),
                                    outcome_before_l7=(), unrecovered_funding=(), terminal_events=(), B=200)
        outs.append(_bytes(fr, v))
    assert outs[0] == outs[1]
    assert fr.run.valid and fr.run.window == WIN and fr.l7 == L7
    assert (g, g + 2 * Q) in fr.plan.shifted and fr.plan.schedule[g + 2 * Q] is True
    assert fr.plan.skipped[0][0] == day3 + 5 * MIN
    assert fr.evaluable_days == 179
    assert list(fr.run.decisions["t"]) == sorted(fr.plan.schedule)
    assert model.log and set(model.log) <= set(fr.plan.schedule)  # the frozen model scored the decisions
    assert len(fr.run.fills) and fr.run.positions["ts"].max() < TF7 + 11 * D  # traded, then flat
    # A-3 / R59: the ticks that need a stale day's quarter close (day3 + 15 min .. day3 + D) are skipped (no
    # mark); the ticks around them are marked
    marked = set(fr.run.marks["ts"])
    assert not marked & set(range(day3 + Q, day3 + D + Q, Q)) and {day3, day3 + D + Q} <= marked
    # 10 active days then flat, costs > funding: NO-GO (H_F, P2, P6), P4 evidence short (DK-99 precedence)
    assert v.cls == forward.NOGO and v.reasons == ("H_F fails", "P2 fails", "P6 fails", "evidence: p4")
    assert v.details["evaluable_days"] == 179 and v.comparison == forward.INCOMPLETE  # no A0 supplied
    with pytest.raises(forward.NoForwardTest):
        forward_replay.evaluate(fr, None, holdout_class=verdict.NOGO, integrity=(), outcome_before_l7=(),
                                unrecovered_funding=(), terminal_events=(), B=200)
    assert isinstance(fr.run.decisions, pd.DataFrame)
