"""S-10.6 selection (validity precondition, LB, simplicity, tie-break) and S-10.7 development kill."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from luna.v7 import accounting as acc
from luna.v7 import selection as sel
from luna.v7 import stats
from luna.v7.book.execution import INVALID_NO_EXIT, INVALID_TERMINAL
from tests.v7.run_fixtures import funding_run

BP = 1e-4
N_DAYS = 60


def _nets(mu, seed):
    return np.random.default_rng(seed).normal(mu, 0.004, N_DAYS)


def _runs(mus=None, **over):
    mus = mus or {}
    out = {}
    for i, (a, h) in enumerate(sel.REQUIRED):
        out[(a, h)] = funding_run(_nets(mus.get((a, h), 0.0), i), arm=a, h=h)
    out.update(over)
    return out


def test_selection_invalid_if_any_required_invalid():
    base = _runs()
    for key in sel.REQUIRED:  # every required run, A0 at each single horizon included
        for why in (INVALID_TERMINAL, INVALID_NO_EXIT):
            runs = dict(base)
            runs[key] = replace(runs[key], invalid=why, invalid_detail="X#0")
            out = sel.choose(runs, B=200)
            assert isinstance(out, sel.Invalid)
            assert out.reason == "INVALID RUN — historical terminal execution not reconstructible"
            assert out.runs == (f"{key[0]}@{key[1]}h: {why} (X#0)",)
    # reference arms: reported, never required
    runs = dict(base)
    runs[("TS", 24)] = funding_run([0.0] * 5, arm="TS", invalid=INVALID_TERMINAL)
    out = sel.choose(runs, B=200)
    assert isinstance(out, sel.Choice) and out.reference_invalid == (f"TS@24h: {INVALID_TERMINAL}",)
    # missing / unknown runs are refused
    runs = dict(base)
    del runs[("A0", 168)]
    with pytest.raises(ValueError, match="missing"):
        sel.choose(runs)
    with pytest.raises(ValueError, match="unknown"):
        sel.choose({**base, ("A1", 24): base[("A2", 24)]})


def test_selection_k10_failure_is_invalid():
    base = _runs()
    led = acc.account(base[("A0", 72)])
    d = led.daily_engine.copy()
    d.loc[0, "price"] = 1e-6  # PR != the daily price column
    out = sel.choose({**base, ("A0", 72): replace(led, daily_engine=d)}, B=200)
    assert isinstance(out, sel.Invalid) and out.reason == sel.INVALID_K10 and out.runs == ("A0@72h",)


def test_selection_lb_and_choice_from_runs():
    mus = {("A2", 72): 0.003, ("A3", 168): 0.001}
    runs = _runs(mus)
    out = sel.choose(runs, B=300, seed=5)
    assert isinstance(out, sel.Choice)
    for k in sel.CANDIDATES:
        r = stats.ledger_returns(acc.account(runs[k]))
        assert out.lbs[k] == stats.lower_bound(r, (14,), 300, 5)
    assert (out.arm, out.h) == ("A2", 72) and out.ell == 14 and out.seed == 5 and out.B == 300
    assert sel.choose(runs, B=300, seed=5) == out  # deterministic


def _lbs(**kw):
    lbs = {k: -5 * BP for k in sel.CANDIDATES}
    for name, v in kw.items():
        a, h = name.split("_")
        lbs[(a, int(h))] = v
    return lbs


def test_simplicity_and_tiebreak():
    ch = sel.choose_from_lbs
    # A3 must beat the best A2 by >= 1 bp/day
    assert (ch(_lbs(A2_24=0.0, A3_24=0.9 * BP)).arm) == "A2"
    assert (ch(_lbs(A2_24=0.0, A3_24=1.0 * BP)).arm) == "A3"
    assert (ch(_lbs(A2_24=0.0, A3_24=1.5 * BP)).arm, ch(_lbs(A2_24=0.0, A3_24=1.5 * BP)).h) == ("A3", 24)
    # highest LB otherwise
    c = ch(_lbs(A2_24=2 * BP, A2_72=1 * BP, A3_168=2.5 * BP))
    assert (c.arm, c.h) == ("A2", 24)
    # tie (|dLB| <= 0.1 bp/day) -> the longer horizon
    c = ch(_lbs(A2_24=2 * BP, A2_72=1.95 * BP, A2_168=1.5 * BP))
    assert (c.arm, c.h) == ("A2", 72)
    c = ch(_lbs(A2_24=2 * BP, A2_72=1.95 * BP, A2_168=1.9 * BP))
    assert (c.arm, c.h) == ("A2", 168)
    c = ch(_lbs(A2_24=2 * BP, A2_72=1.85 * BP))
    assert (c.arm, c.h) == ("A2", 24)
    # exact boundary: |dLB| = 0.1 bp/day is a tie
    c = ch(_lbs(A2_24=0.0, A2_168=-1e-5))
    assert (c.arm, c.h) == ("A2", 168)
    # within A3 as well
    c = ch(_lbs(A2_24=0.0, A3_72=3 * BP, A3_168=2.95 * BP))
    assert (c.arm, c.h) == ("A3", 168)
    with pytest.raises(ValueError):
        ch({k: 0.0 for k in sel.CANDIDATES[:5]})
    with pytest.raises(ValueError):
        ch(_lbs(A2_24=float("nan")))


def _ledger(nets, *, F_leg=1.0, fees=0.1):
    led = acc.account(funding_run(nets))
    return replace(led, F_leg=F_leg - led.F_hedge, fees_leg=fees)


def test_dev_kill_conditions():
    ok = _ledger([0.01] * 10, F_leg=1.0, fees=0.1)
    folds = [1.0, -1.0, 1.0]
    assert sel.dev_kill(ok, folds) == []
    # each condition alone
    assert sel.dev_kill(_ledger([0.01, -0.02], F_leg=1.0, fees=0.1), folds) == [sel.KILL_MEAN]
    assert sel.dev_kill(_ledger([0.0, 0.0], F_leg=1.0, fees=0.1), folds) == [sel.KILL_MEAN]
    assert sel.dev_kill(ok, [1.0, -1.0, -1.0, -1.0]) == [sel.KILL_FOLDS]  # 25 % < 30 %
    assert sel.dev_kill(ok, [1.0, 0.0, -1.0]) == []  # 33 %
    assert sel.dev_kill(_ledger([0.01] * 10, F_leg=1.0, fees=1.0), folds) == [sel.KILL_CAC]  # ratio 1
    # F_net <= 0 (with CAC <= 0: CAC = F_net - C_exec <= F_net)
    assert sel.dev_kill(_ledger([0.01] * 10, F_leg=0.0, fees=0.0), folds) == [sel.KILL_FNET, sel.KILL_CAC]
    # C_exec / F_net > 1 always implies CAC < 0 when F_net > 0
    assert sel.dev_kill(_ledger([0.01] * 10, F_leg=1.0, fees=1.01), folds) == [sel.KILL_RATIO, sel.KILL_CAC]
    with pytest.raises(ValueError):
        sel.dev_kill(ok, [])
    with pytest.raises(ValueError):
        sel.dev_kill(ok, [float("nan")])
