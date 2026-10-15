"""Task 1 tests: time, constants, spec pinning, windows, folds (spec S-1.1/1.2/1.4/1.8/10.2/10.4/15)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest

from luna.v7 import spec, windows
from luna.v7.constants import FROZEN
from luna.v7.timeutil import MIN, NS, D, H, from_ms, from_s, ns_array, to_ns, ts


def test_frozen_constants_values():
    F = FROZEN
    assert F.decision.minutes == (5, 485, 965)
    assert F.decision.fill_delay_min == 15
    assert F.horizons_h == (24, 72, 168)
    assert F.fees.maker == 0.0002 and F.fees.taker == 0.0005 and F.fees.bnb_discount is False
    assert F.costs.slip == 2e-4
    assert F.costs.fallback_half_spread == 25e-4
    assert F.costs.fallback_slip_mult == 3
    assert F.costs.liquidity_cap == 0.005
    assert F.spread.lam == 2.0 and F.spread.floor == 1e-4
    assert F.spread.window_ns == 24 * H + 15 * MIN and F.spread.max_pairs == 95
    u = F.universe
    assert (u.u1_days, u.u2_min_quote_vol, u.u2_days, u.u3_days) == (60, 20_000_000, 30, 30)
    assert (u.u3_gap_lookback_days, u.u4_min_coverage, u.u5_max_half_spread) == (7, 0.95, 7.5e-4)
    assert u.u7_slack_min == 15 and u.u8_window == (60, 1)
    assert F.instances.dormancy_days == 7
    assert F.funding.tol_ns == 60 * NS and F.funding.std_intervals_h == (1, 2, 4, 8)
    b = F.book
    assert (b.K, b.side_gross, b.neutral_tol, b.resize_drift, b.exit_rank_slack) == (5, 0.5, 0.05, 0.25, 2)
    r = F.risk
    assert (r.leg_stop, r.cooldown_h, r.daily_loss, r.dd_kill) == (0.15, 24, 0.98, 0.85)
    assert (r.gross_max, r.u5_exit, r.inelig_consecutive) == (1.0, 20e-4, 2)
    assert (F.hedge.open_thr, F.hedge.trade_thr, F.hedge.max_abs) == (0.05, 0.02, 0.5)
    be = F.beta
    assert (be.days, be.min_obs, be.w, be.prior, be.clip) == (30, 500, 0.7, 1.0, (0.0, 3.0))
    assert (F.a0.mult, F.a0.sides, F.a0.buffer, F.a1_veto) == (1.5, 2, 5e-4, 0.50)
    assert F.cv.purge_ns == 24 * H and F.cv.iso_clip == (0.001, 0.999)
    s = F.search
    assert s.en_alpha == (1e-3, 1e-2, 1e-1) and s.en_l1 == (0.2, 0.8)
    assert s.lgb_leaves == (7, 15, 31) and s.lgb_min_data == (500, 2000)
    assert s.logit == (1e-2, 0.5) and s.lgb_clf == (15, 2000)
    assert s.lgb_fixed == dict(
        learning_rate=0.03, lambda_l2=5, feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
        max_trees=1500, early_stopping=100,
    )
    st = F.stats
    assert (st.B, st.blocks, st.alpha, st.sel_block) == (10_000, (14, 28), 0.05, 14)
    assert (F.select.a3_margin_bpd, F.select.tie_bpd) == (1.0, 0.1)
    assert (F.kill.cexec_over_fnet, F.kill.min_pos_fold_share) == (1.0, 0.30)
    a = F.accept
    assert (a.p2_ratio, a.p3_mdd) == (0.60, 0.15)
    assert a.p4 == dict(active_days=60, gross_frac=0.10, quarters=48, closed_legs=100, clusters=20)
    assert a.p5 == dict(min_pos_q=2, max_q_share=0.60, top1=0.30, top3=0.60)
    assert a.p6 == dict(fee=1.5, slip=1.5, spread=3.0)
    assert a.p8 == (0.5, 1.5) and a.p9_sharpe == 1.0 and a.p11_m == (3, 0.05)
    assert (a.ece_max, a.ece_bins) == (0.05, 10)
    assert (F.cluster_gap_h, F.k10_tol, F.invalid_max) == (72, 1e-9, 0.005)
    f = F.forward
    assert (f.days, f.eval_quarter_share, f.min_eval_days) == (180, 0.90, 144)
    assert f.p4 == dict(active_days=30, closed_legs=50, clusters=10)
    assert (f.shift_max_h, f.gap_h, f.extension_days) == (2, 24, 90)
    d = F.deploy
    assert (d.E_265, d.E_10k, d.E_ref, d.d1, d.infra_usd_year) == (265, 10_000, 10_000, 0.90, 144)
    assert F.d0 == "2021-01-01"
    with pytest.raises(FrozenInstanceError):
        F.horizons_h = (1,)  # frozen


def test_to_ns_rejects_ms_ints():
    with pytest.raises(ValueError):
        to_ns(1_600_000_000_000)
    assert from_ms(1_600_000_000_000) == 1_600_000_000_000_000_000
    assert from_s(1_600_000_000) == 1_600_000_000 * NS
    assert to_ns(1_600_000_000_000_000_000) == 1_600_000_000_000_000_000
    assert to_ns("2023-01-01") == 1_672_531_200 * NS
    assert to_ns(pd.Timestamp("2023-01-01", tz="UTC")) == 1_672_531_200 * NS
    assert to_ns(pd.Timestamp("2023-01-01")) == 1_672_531_200 * NS  # naive = UTC
    assert ts(1_672_531_200 * NS) == pd.Timestamp("2023-01-01", tz="UTC")
    assert D == 24 * H


def test_decision_times_day():
    got = windows.decision_times(to_ns("2023-01-01"), to_ns("2023-01-02"))
    assert got.dtype == np.int64
    base = to_ns("2023-01-01")
    assert list(got) == [base + 5 * MIN, base + 8 * H + 5 * MIN, base + 16 * H + 5 * MIN]
    # half-open and unaligned bounds
    assert len(windows.decision_times(base + 5 * MIN + 1, base + 16 * H + 5 * MIN)) == 1
    assert len(windows.decision_times(base, base)) == 0


def test_outer_folds_d0_2021():
    folds = windows.outer_folds(to_ns("2021-01-01"))
    assert len(folds) == 10
    f0 = folds[0]
    assert (f0.test_lo, f0.test_hi) == (to_ns("2023-01-01"), to_ns("2023-04-01"))
    assert f0.tr == (to_ns("2021-01-01"), to_ns("2022-01-01"))
    assert f0.sel == (to_ns("2022-01-01"), to_ns("2022-07-01"))
    assert f0.cal == (to_ns("2022-07-01"), to_ns("2023-01-01"))
    assert folds[-1].test_hi == to_ns("2025-07-01")
    assert [f.fold_id for f in folds] == list(range(10))
    for a, b in zip(folds, folds[1:], strict=False):
        assert a.test_hi == b.test_lo
    assert not windows.stage0_fail(to_ns("2021-01-01"))


def test_outer_folds_late_d0_fails():
    d0 = to_ns("2021-08-01")
    assert len(windows.outer_folds(d0)) == 7
    assert windows.stage0_fail(d0)
    assert not windows.stage0_fail(to_ns("2021-07-01"))
    assert len(windows.outer_folds(to_ns("2021-07-01"))) == 8


def test_final_blocks():
    fb = windows.final_blocks()
    assert fb.tr == (to_ns("2021-01-01"), to_ns("2024-07-01"))
    assert fb.sel == (to_ns("2024-07-01"), to_ns("2025-01-01"))
    assert fb.cal == (to_ns("2025-01-01"), to_ns("2025-07-01"))
    assert fb.test_lo == to_ns("2025-07-01")


def test_windows_constants():
    assert windows.W_DEV == (to_ns("2021-01-01"), to_ns("2025-07-01"))
    assert windows.W_HOLD == (to_ns("2025-07-01"), to_ns("2026-07-01"))
    assert windows.overlaps(0, 10, (5, 20)) and not windows.overlaps(0, 5, (5, 20))
    assert not windows.overlaps(20, 30, (5, 20))


def test_v6_bounds_from_tag(monkeypatch):
    b = spec.v6_artifacts()
    assert b.holdout_start == to_ns("2026-07-01")
    assert b.holdout_end == to_ns("2026-10-09T11:52:48.861565107Z")
    assert b.t_f == to_ns("2026-10-14")
    assert b.looks == (to_ns("2027-01-12"), to_ns("2027-04-12"), to_ns("2027-10-14"))
    real = spec._git_show

    def tampered(repo, rev, path):
        return real(repo, rev, path) + b" "

    monkeypatch.setattr(spec, "_git_show", tampered)
    with pytest.raises(spec.SpecMismatch):
        spec.v6_artifacts()
    with pytest.raises(spec.SpecMismatch):
        spec.read_frozen_spec()


def test_verify_spec_passes_on_freeze(monkeypatch):
    spec.verify_spec()
    text = spec.read_frozen_spec()
    assert text.startswith("# V7 ML RESEARCH SPEC")
    assert "\r" not in text
    monkeypatch.setattr(spec, "_working_copy", lambda repo: b"edited")
    with pytest.raises(spec.SpecMismatch):
        spec.verify_spec()


def test_m6_and_w_hold_overlaps_nothing_in_m6():
    start, end = windows.m6()
    assert start == to_ns("2026-07-01") and end is None
    assert not windows.overlaps(*windows.W_HOLD, (start, end))
    assert not windows.overlaps(*windows.W_DEV, (start, end))
    assert windows.overlaps(start - 1, start + 1, (start, end))
    assert windows.overlaps(start + D, start + 2 * D, (start, end))  # open-ended span


@pytest.mark.parametrize("bad", [1e30, -1e30, np.inf, -np.inf, np.nan, 1.6e9 + 0.5])
def test_ns_array_rejects_bad_floats(bad):
    with pytest.raises(ValueError, match="known_at"):
        ns_array(np.array([1.7e18, bad]), "known_at")


def test_ns_array_rejects_non_integral_ns_float():
    # 1.5e18 + 0.5 is not representable in float64 (ULP 256 there) and collapses to the valid 1.5e18; every
    # float64 inside the plausible window is integral, so integrality uses a representable fraction
    assert np.float64(1.5e18) + 0.5 == np.float64(1.5e18)
    with pytest.raises(ValueError, match="non-integral"):
        ns_array(np.array([1.5e18, 1.6e15 + 0.5]), "x")


def test_ns_array_accepts_valid():
    t = to_ns("2024-06-01T00:00:00Z")
    out = ns_array(pd.Series([float(t), float(t + MIN)]), "known_at")
    assert out.dtype == np.int64 and list(out) == [t, t + MIN]
    ints = np.array([t - 5, t, t + 5], dtype=np.int64)  # in-window integers are accepted as-is
    assert ns_array(ints, "open_ts").tolist() == ints.tolist()
    with pytest.raises(ValueError, match="window"):  # integers are window-checked too (DK-106 / m1)
        ns_array(np.array([0, 5, t], dtype=np.int64), "open_ts")
    assert ns_array(np.array([], dtype=np.float64), "x").dtype == np.int64
    with pytest.raises(ValueError):
        ns_array(np.array([2**63], dtype=np.uint64), "x")
    with pytest.raises(ValueError):
        ns_array(pd.Series(["a"]), "x")
