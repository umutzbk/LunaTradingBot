"""H2 validity gate (S-H2-3): config-specific calibration on evaluation data, PIT forecasts only."""

import numpy as np
import pandas as pd
import pytest

from luna.v6 import forecast as fc
from luna.v6.data import FundingPanel

T0 = pd.Timestamp("2026-01-01T00:00Z")
DAY = pd.Timedelta("1D")


def constant_world(n_days=120, n_assets=12, ratio=1.0, noise=0.0, seed=0):
    """Each asset has a constant funding level; forecasts = level / ratio (so R = ratio)."""
    rng = np.random.default_rng(seed)
    levels = np.linspace(-6e-5, 6e-5, n_assets)
    idx = pd.date_range(T0, periods=(n_days + 40) * 24, freq="h")
    vals = levels[None, :] + rng.normal(0, noise, (len(idx), n_assets)) if noise else np.tile(levels, (len(idx), 1))
    cols = [f"A{i:02d}" for i in range(n_assets)]
    panel = FundingPanel(pd.DataFrame(vals, index=idx, columns=cols))
    recs = [fc.GateRecord(T0 + d * DAY, cols, pd.Series(levels / ratio, index=cols)) for d in range(n_days)]
    return panel, recs, cols, levels


def test_valid_when_forecasts_match_realised():
    panel, recs, *_ = constant_world(ratio=1.0)
    end = T0 + 130 * DAY
    g = fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=end, B=2000)
    assert g.cls == "VALID" and g.R == pytest.approx(1.0) and g.n_dates >= 60


def test_invalid_when_confidently_miscalibrated_and_inconclusive_without_evidence():
    panel, recs, *_ = constant_world(ratio=0.5, noise=1e-6)  # forecasts twice the realised
    end = T0 + 130 * DAY
    g = fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=end, B=2000)
    assert g.R == pytest.approx(0.5, rel=0.05) and g.cls == "INVALID"
    short = fc.gate(panel, recs[:40], kind="H3", k=4, h=168, window_end=T0 + 50 * DAY, B=2000)
    assert short.n_dates < 60 and short.cls == "INCONCLUSIVE"  # INVALID needs minimum evidence


def test_negative_slope_is_invalid_even_if_ratio_is_in_range():
    panel, recs, cols, levels = constant_world(noise=1e-6)
    flipped = [fc.GateRecord(r.s, r.eligible, pd.Series(-levels, index=cols)) for r in recs]
    g = fc.gate(panel, flipped, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=2000)
    assert g.ci_c[1] <= 0 and g.cls == "INVALID"


def test_only_dates_whose_label_ended_by_window_end_are_used():
    panel, recs, *_ = constant_world()
    end = T0 + 80 * DAY
    g = fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=end, B=500)
    assert max(g.dates) + pd.Timedelta(hours=168) <= end


def test_config_specific_selection_k4_vs_k6_and_h1_top3():
    panel, recs, cols, levels = constant_world(noise=1e-6)
    g4 = fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=500)
    g6 = fc.gate(panel, recs, kind="H3", k=6, h=168, window_end=T0 + 130 * DAY, B=500)
    assert g4.fs[0] == pytest.approx(levels[-4:].mean() - levels[:4].mean())
    assert g6.fs[0] == pytest.approx(levels[-6:].mean() - levels[:6].mean())
    g1 = fc.gate(panel, recs, kind="H1", k=None, h=720, window_end=T0 + 159 * DAY, B=500)
    assert g1.fs[0] == pytest.approx(levels[-3:].sum())


def test_k_prime_shrinks_with_few_eligible_and_dates_below_three_are_skipped():
    panel, recs, cols, levels = constant_world()
    few = [fc.GateRecord(r.s, cols[:7], r.fhat) for r in recs]  # N_e = 7 -> k' = 3
    g = fc.gate(panel, few, kind="H3", k=6, h=168, window_end=T0 + 130 * DAY, B=200)
    lv = levels[:7]
    assert g.fs[0] == pytest.approx(lv[-3:].mean() - lv[:3].mean())
    five = [fc.GateRecord(r.s, cols[:5], r.fhat) for r in recs]  # k' = 2 -> skipped
    assert fc.gate(panel, five, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=200).n_dates == 0


def test_missing_future_labels_are_imputed_not_dropped():
    panel, recs, cols, levels = constant_world()
    rel = panel.rel.copy()
    top = cols[-1]  # in the top set of every date
    rel.loc[T0 + 10 * DAY:, top] = np.nan  # delisted after selection
    g = fc.gate(FundingPanel(rel), recs, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=200)
    assert g.n_imputed > 0
    i = g.dates.index(T0 + 20 * DAY)
    exp = (levels[-4:-1].sum() + min(0.0, levels[-1])) / 4 - levels[:4].mean()  # min(0, F) for T
    assert g.rs[i] == pytest.approx(exp)


def test_partially_known_label_uses_the_known_mean_and_bottom_imputation_is_max0():
    panel, recs, cols, levels = constant_world()
    rel = panel.rel.copy()
    s = T0 + 20 * DAY
    rel.loc[s: s + pd.Timedelta(hours=59), cols[-1]] = np.nan  # 108/168 known = 64 %
    rel.loc[s: s + pd.Timedelta(hours=167), cols[0]] = np.nan  # bottom asset fully unknown
    g = fc.gate(FundingPanel(rel), recs, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=200)
    i = g.dates.index(s)
    exp = levels[-4:].mean() - (max(0.0, levels[0]) + levels[1:4].sum()) / 4
    assert g.rs[i] == pytest.approx(exp)


def test_zero_forecast_spread_is_inconclusive():
    panel, recs, cols, _ = constant_world()
    zero = [fc.GateRecord(r.s, r.eligible, pd.Series(0.0, index=cols)) for r in recs]
    assert fc.gate(panel, zero, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=200).cls == "INCONCLUSIVE"


def test_720_minimum_evidence_formula():
    assert not fc.min_evidence_720([T0 + d * DAY for d in range(0, 210)])  # floor(209/30)+1 = 7
    assert fc.min_evidence_720([T0 + d * DAY for d in range(0, 211)])  # floor(210/30)+1 = 8


def test_bootstrap_is_seeded_and_ratio_and_slope_share_resamples():
    panel, recs, *_ = constant_world(noise=5e-6, seed=3)
    a = fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=1000)
    b = fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=T0 + 130 * DAY, B=1000)
    assert a.ci_R == b.ci_R and a.ci_c == b.ci_c
    assert a.ci_R[0] <= a.R <= a.ci_R[1]


def test_invalid_wins_over_valid_conditions():
    # R point inside the range but the slope CI is entirely <= 0 -> INVALID
    assert fc.classify(True, 1.0, (0.9, 1.1), 0.0, (-2.0, -0.1)) == "INVALID"
    assert fc.classify(True, 1.0, (0.9, 1.1), 1.0, (0.2, 2.0)) == "VALID"
    assert fc.classify(True, 1.3, (1.26, 1.4), 1.0, (0.2, 2.0)) == "INVALID"
    assert fc.classify(True, 1.3, (0.7, 1.9), 1.0, (0.2, 2.0)) == "INCONCLUSIVE"
    assert fc.classify(False, 1.0, (0.9, 1.1), 1.0, (0.2, 2.0)) == "INCONCLUSIVE"


def test_bootstrap_replicates_with_an_undefined_ratio_are_dropped_and_counted():
    panel, recs, cols, levels = constant_world(ratio=1.0)
    end = T0 + 130 * DAY
    assert fc.gate(panel, recs, kind="H3", k=4, h=168, window_end=end, B=500).n_boot_dropped == 0
    sparse = [fc.GateRecord(r.s, r.eligible, r.fhat if i < 3 else r.fhat * 0.0) for i, r in enumerate(recs)]
    g = fc.gate(panel, sparse, kind="H3", k=4, h=168, window_end=end, B=500)
    assert 0 < g.n_boot_dropped < 500
