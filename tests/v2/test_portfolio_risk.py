import math

import numpy as np
import pandas as pd
import pytest

from luna.v2.portfolio_risk import (
    CAT_STOP,
    CORR_GROUP_CAP,
    VAR_BUDGET,
    VAR_Z95,
    catastrophic_stop_hit,
    ewma_cov,
    project_weights,
    trailing_corr,
    xsec_limits,
)


def _case(rng, n):
    assets = [f"A{i}" for i in range(n)]
    a = rng.normal(0, 0.03, (n, n))
    cov = pd.DataFrame(a @ a.T / n + np.eye(n) * 1e-4, index=assets, columns=assets)
    sd = np.sqrt(np.diag(cov))
    corr = cov / np.outer(sd, sd)
    k = max(1, n // 2)
    raw = pd.Series(0.0, index=assets)
    raw.iloc[:k] = 1.0
    raw.iloc[n - k:] = -1.0 if n >= 2 * k else raw.iloc[n - k:]
    raw = raw / raw.abs().sum()
    return raw, cov, corr


def _groups(w, corr, side):
    names = [a for a in w.index if np.sign(w[a]) == side and w[a] != 0]
    seen, out = set(), []
    for a in names:
        if a in seen:
            continue
        comp, stack = set(), [a]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack += [y for y in names if y not in comp and corr.loc[x, y] > 0.8]
        seen |= comp
        out.append(sorted(comp))
    return out


def test_perp_projection_respects_every_cap():
    rng = np.random.default_rng(0)
    for _ in range(200):
        n = int(rng.integers(2, 11))
        raw, cov, corr = _case(rng, n)
        g = float(rng.choice([1.0, 1.5]))
        v = float(rng.choice([0.10, 0.15]))
        p = project_weights(raw * g, cov, corr, controls="perp", gross=g, vol_target=v)
        w = p.weights
        sd = math.sqrt(w @ cov @ w)
        assert (w.abs() <= 0.30 + 1e-12).all() and w.abs().sum() <= g + 1e-12
        assert abs(w.sum()) <= 0.10 + 1e-12
        assert VAR_Z95 * sd <= VAR_BUDGET + 1e-12
        assert sd * math.sqrt(365) <= v + 1e-12
        for side in (1, -1):
            for grp in _groups(w, corr, side):
                assert w[grp].abs().sum() <= CORR_GROUP_CAP + 1e-12
        assert (w.abs() <= (raw * g).abs() + 1e-12).all()  # only scales down
        assert p.var_95 == pytest.approx(VAR_Z95 * sd)


def test_vol_scale_capped_at_one():
    assets = ["A", "B"]
    cov = pd.DataFrame(np.eye(2) * 1e-10, index=assets, columns=assets)
    raw = pd.Series({"A": 0.25, "B": -0.25})
    p = project_weights(raw, cov, cov * 0, controls="perp", gross=0.5, vol_target=0.10)
    assert p.vol_scale == 1.0 and p.weights.equals(raw)


def test_net_cap_shrinks_heavier_side():
    assets = ["A", "B", "C"]
    cov = pd.DataFrame(np.eye(3) * 1e-10, index=assets, columns=assets)
    raw = pd.Series({"A": 0.3, "B": 0.3, "C": -0.1})
    p = project_weights(raw, cov, cov * 0, controls="perp", gross=0.7, vol_target=0.10)
    assert p.weights["C"] == -0.1
    assert p.weights.sum() == pytest.approx(0.10)
    assert p.weights["A"] == pytest.approx(p.weights["B"])


def test_corr_group_cap():
    assets = ["A", "B", "C"]
    cov = pd.DataFrame(np.eye(3) * 1e-10, index=assets, columns=assets)
    corr = pd.DataFrame([[1, .9, 0], [.9, 1, 0], [0, 0, 1]], index=assets, columns=assets, dtype=float)
    raw = pd.Series({"A": 0.3, "B": 0.3, "C": -0.3})
    w = project_weights(raw, cov, corr, controls="perp", gross=0.9, vol_target=0.10).weights
    assert w[["A", "B"]].sum() <= 0.5 + 1e-12


def test_var_budget_binds():
    assets = ["A", "B"]
    cov = pd.DataFrame([[0.01, 0.0], [0.0, 0.01]], index=assets, columns=assets)
    raw = pd.Series({"A": 0.3, "B": -0.3})
    p = project_weights(raw, cov, cov * 0, controls="perp", gross=0.6, vol_target=10.0)
    assert p.var_95 == pytest.approx(VAR_BUDGET)


def test_spot_projection_long_only_cap_040():
    assets = ["A", "B"]
    cov = pd.DataFrame(np.eye(2) * 1e-4, index=assets, columns=assets)
    p = project_weights(pd.Series({"A": .5, "B": .5}), cov, cov * 0, controls="spot", gross=1.0,
                        vol_target=None)
    assert p.weights.to_dict() == {"A": .4, "B": .4}
    p = project_weights(pd.Series({"A": 1.0, "B": -1.0}), cov, cov * 0, controls="spot", gross=1.0,
                        vol_target=None)
    assert p.weights.to_dict() == {"A": .4, "B": 0.0}


def test_projection_fails_closed_on_nan_cov_or_bad_equity():
    assets = ["A", "B"]
    cov = pd.DataFrame([[1e-4, np.nan], [np.nan, 1e-4]], index=assets, columns=assets)
    raw = pd.Series({"A": 0.25, "B": -0.25})
    p = project_weights(raw, cov, cov * 0, controls="perp", gross=0.5, vol_target=0.10)
    assert (p.weights == 0).all() and p.reason == "cov_unavailable"
    p = project_weights(raw, cov.loc[["A"], ["A"]], cov * 0, controls="perp", gross=0.5, vol_target=0.10)
    assert (p.weights == 0).all() and p.reason == "cov_unavailable"
    p = project_weights(pd.Series({"A": np.nan, "B": 0.1}), cov.fillna(0), cov * 0, controls="perp",
                        gross=0.5, vol_target=0.10)
    assert (p.weights == 0).all() and p.reason == "cov_unavailable"


def test_ewma_cov_and_corr_are_causal_windows():
    rng = np.random.default_rng(1)
    r = pd.DataFrame(rng.normal(0, 0.02, (60, 2)), columns=["A", "B"])
    c = ewma_cov(r)
    assert c.shape == (2, 2) and np.isfinite(c.to_numpy()).all()
    assert c.loc["A", "A"] == pytest.approx(r["A"].ewm(span=30, min_periods=30).var().iloc[-1])
    assert ewma_cov(r.iloc[:20]).isna().all().all()
    pd.testing.assert_frame_equal(trailing_corr(r), r.iloc[-30:].corr())


def test_catastrophic_stop_threshold():
    assert catastrophic_stop_hit(-1.5, 100.0) and not catastrophic_stop_hit(-1.49, 100.0)
    assert not catastrophic_stop_hit(-5.0, 0.0)


def test_xsec_limits_only_relax_r5_r6_r7_r9(settings):
    for kind in ("perp", "spot"):
        v1 = settings.accounts[kind]
        x = xsec_limits(v1)
        for f in type(v1).model_fields:
            if f in ("max_risk_per_trade", "max_open_risk", "max_positions", "max_leg_notional_frac"):
                continue
            assert getattr(x, f) == getattr(v1, f), f
        assert x.max_risk_per_trade >= 0.02 and x.max_positions >= 1000
        assert x.max_open_risk >= 1e6 and x.max_leg_notional_frac >= 1.0


def test_section12_consistency(settings):
    dl = settings.accounts["perp"].daily_loss_limit
    assert CAT_STOP < dl and VAR_BUDGET < dl


def test_ewma_cov_matches_pandas_exactly_with_gaps():
    rng = np.random.default_rng(5)
    r = pd.DataFrame(rng.normal(0, 0.02, (120, 5)), columns=list("ABCDE"))
    r.iloc[:50, 1] = np.nan  # late listing
    r.iloc[[10, 70, 71], 2] = np.nan  # gaps
    r.iloc[:, 3] = 0.01  # constant series
    ref = r.ewm(span=30, min_periods=30).cov().loc[r.index[-1]]
    np.testing.assert_allclose(ewma_cov(r).to_numpy(), ref.to_numpy(), rtol=1e-10, atol=1e-15)
    short = r.iloc[:40]
    ref_s = short.ewm(span=30, min_periods=30).cov().loc[short.index[-1]]
    np.testing.assert_allclose(ewma_cov(short).to_numpy(), ref_s.to_numpy(), rtol=1e-10, atol=1e-15)
