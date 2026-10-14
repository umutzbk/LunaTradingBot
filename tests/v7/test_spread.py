import math

import numpy as np
import pandas as pd

from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, to_ns
from tests.v7.ar_reference import ar_window

T0 = to_ns("2024-03-01T00:00:00Z")
Q = 15 * MIN


def bars(rows, start=T0):
    """rows: list of (ln_high, ln_low, ln_close) or None (missing bar) on a consecutive 15m grid."""
    out = []
    for i, r in enumerate(rows):
        if r is None:
            continue
        h, lo, c = (math.exp(x) for x in r)
        t = start + i * Q
        out.append(dict(open_ts=t, o=c, h=h, l=lo, c=c, vol_base=1.0, known_at=t + Q))
    return pd.DataFrame(out)


A = (0.02, 0.0, 0.02)  # eta 0.01, c 0.02
B = (0.01, -0.01, 0.0)  # eta 0.0


def test_ar_single_pair():
    # pi = 4*(0.02-0.01)*(0.02-0.0) = 0.0008 ; S = sqrt(0.0008) ; h = lam*S/2 = S
    h = half_spread(bars([A, B]), T0 + 2 * Q + MIN)
    assert abs(h - math.sqrt(0.0008)) < 1e-12


def test_ar_known_at_excludes_unfinished_pair():
    b = bars([A, B])
    assert half_spread(b, T0 + 2 * Q) is None  # bar k+1 closes at tau, needs <= tau - 1 min
    assert half_spread(b, T0 + 2 * Q + MIN) is not None


def test_ar_negative_mean_gives_floor():
    neg = (0.05, 0.03, 0.04)  # eta1 = 0.04 > c0 -> pi < 0
    assert half_spread(bars([A, neg]), T0 + 2 * Q + MIN) == 1e-4


def test_ar_missing_bar_breaks_pair():
    assert half_spread(bars([A, None, B]), T0 + 3 * Q + MIN) is None
    h = half_spread(bars([A, B, None, A, B]), T0 + 5 * Q + MIN)  # pairs (0,1),(3,4) only
    assert abs(h - math.sqrt(0.0008)) < 1e-12


def test_ar_lambda_then_floor_order():
    # S = sqrt(2) e ; h = lam*S/2 = S. Below 1 bp -> floor; above -> S itself (floor not applied first)
    e = 0.5e-4
    assert half_spread(bars([(e, 0.0, e), (e / 2, -e / 2, 0.0)]), T0 + 2 * Q + MIN) == 1e-4
    e = 1.5e-4 / math.sqrt(2)
    h = half_spread(bars([(e, 0.0, e), (e / 2, -e / 2, 0.0)]), T0 + 2 * Q + MIN)
    assert abs(h - 1.5e-4) < 1e-12


def test_ar_scale_invariance():
    rng = np.random.default_rng(1)
    rows = []
    for _ in range(40):
        c = rng.normal(0, 0.01)
        rows.append((c + abs(rng.normal(0, 0.005)), c - abs(rng.normal(0, 0.005)), c))
    b1 = bars(rows)
    b2 = b1.copy()
    for col in ("o", "h", "l", "c"):
        b2[col] = b2[col] * 12345.0
    tau = T0 + 41 * Q + MIN
    assert abs(half_spread(b1, tau) - half_spread(b2, tau)) < 1e-12


def test_ar_constant_prices_floor():
    assert half_spread(bars([(0.0, 0.0, 0.0)] * 10), T0 + 11 * Q) == 1e-4


def test_ar_window_start_inclusive():
    b = bars([A, B])
    tau = T0 + 24 * 3600 * 10**9 + Q  # bar 0 opens exactly at tau - 24h - 15m
    assert half_spread(b, tau) is not None
    assert half_spread(b, tau + 1) is None


def test_ar_matches_independent_reference():
    rng = np.random.default_rng(7)
    n = 90
    rows = []
    for i in range(n):
        c = 0.01 * math.sin(i / 5) + rng.normal(0, 0.003)
        r = (c + abs(rng.normal(0, 0.004)), c - abs(rng.normal(0, 0.004)), c + rng.normal(0, 0.002))
        rows.append((max(r), min(r), r[2]))
    tau = T0 + (n + 1) * Q + MIN  # every bar 0..n-1 closed by tau - 1 min; window start before bar 0
    for gap in (None, 40, 55):
        rr = list(rows)
        if gap is not None:
            rr[gap] = None
        hi = [math.exp(r[0]) if r else math.nan for r in rr]
        lo = [math.exp(r[1]) if r else math.nan for r in rr]
        cl = [math.exp(r[2]) if r else math.nan for r in rr]
        ref = ar_window(hi, lo, cl, 1, n - 1)
        got = half_spread(bars(rr), tau)
        assert abs(got - max(2.0 * ref / 2, 1e-4)) < 1e-12


def test_ar_tau_trims_leading_pairs_matches_reference():
    rng = np.random.default_rng(11)
    n = 150
    rows = []
    for i in range(n):
        c = 0.01 * math.sin(i / 7) + rng.normal(0, 0.003)
        r = (c + abs(rng.normal(0, 0.004)), c - abs(rng.normal(0, 0.004)), c + rng.normal(0, 0.002))
        rows.append((max(r), min(r), r[2]))
    tau = T0 + 140 * Q + MIN
    hi, lo, cl = ([math.exp(r[j]) for r in rows] for j in (0, 1, 2))
    first = 44  # bar k opens >= tau - 24h - 15m = T0 + 43Q + 1 min -> k >= 44
    ref = ar_window(hi, lo, cl, first + 1, 139)  # later bar 139 closes at tau - 1 min
    assert abs(half_spread(bars(rows), tau) - max(ref, 1e-4)) < 1e-12


def test_ar_mixed_sign_positive_mean_hand_worked():
    E = (0.05, 0.03, 0.04)  # eta 0.04, c 0.04
    rows = [A, E, A, B]
    # (A,E)=4*0.01*(0.02-0.04)=-0.0008 ; (E,A)=4*(0.04-0.04)*(0.04-0.01)=0 ; (A,B)=+0.0008 -> mean 0 -> floor
    assert half_spread(bars(rows), T0 + 4 * Q + MIN) == 1e-4
    rows = [A, E, A, B, A, B]
    # pairs: -0.0008, 0, +0.0008, (B,A)=0, +0.0008 -> mean 0.0008/5 = 0.00016 > 0 -> S = sqrt(0.00016)
    assert abs(half_spread(bars(rows), T0 + 6 * Q + MIN) - math.sqrt(0.00016)) < 1e-12


def test_nan_or_missing_known_at_raises():
    import pytest

    b = bars([A, B])
    b["known_at"] = b["known_at"].astype("float64")
    b.loc[1, "known_at"] = np.nan
    with pytest.raises(ValueError):
        half_spread(b, T0 + 2 * Q + MIN)
    with pytest.raises(ValueError):
        half_spread(bars([A, B]).drop(columns="known_at"), T0 + 2 * Q + MIN)


def test_out_of_range_known_at_raises():
    import pytest

    for bad in (1e30, -1e30, 1.6e9 + 0.5):
        b = bars([A, B])
        b["known_at"] = b["known_at"].astype("float64")
        b.loc[1, "known_at"] = bad
        with pytest.raises(ValueError):
            half_spread(b, T0 + 2 * Q + MIN)


def _window_reference(b: pd.DataFrame, tau: int) -> float | None:
    """The S-4.5 value straight from all pair products of the whole frame (no pre-trimming)."""
    from luna.v7.constants import FROZEN
    from luna.v7.spread import pair_products

    p = pair_products(b)
    sel = p[(p["open_ts"] >= tau - FROZEN.spread.window_ns) & (p["known_at"] <= tau - MIN)]
    if len(sel) == 0:
        return None
    return max(FROZEN.spread.lam * math.sqrt(max(float(sel["pi"].mean()), 0.0)) / 2.0, FROZEN.spread.floor)


def test_long_frame_equals_whole_frame_reference():
    """Task 19 (performance): half_spread trims a long instance frame to the window before the pair products;
    the value is bit-identical to the products of the whole frame, at every tau (incl. missing bars, an
    invalid-price bar and bars dated after tau)."""
    rng = np.random.default_rng(3)
    rows = [tuple(rng.normal(0, 1e-3, 3)) for _ in range(600)]
    rows = [(max(r), min(r), r[2]) for r in rows]
    for k in (100, 101, 350):
        rows[k] = None
    b = bars(rows)
    b.loc[200, "c"] = np.nan  # invalid price: dropped, breaks its pairs
    for i in (0, 1, 5, 96, 97, 98, 150, 201, 299, 451, 599, 610):
        tau = T0 + i * Q + 7 * MIN
        got, ref = half_spread(b, tau), _window_reference(b, tau)
        assert got == ref, (i, got, ref)


def test_invalid_known_at_outside_the_window_still_raises():
    import pytest

    rng = np.random.default_rng(4)
    b = bars([(1e-3, -1e-3, float(x)) for x in rng.normal(0, 1e-4, 300)])
    b["known_at"] = b["known_at"].astype("float64")
    b.loc[3, "known_at"] = np.nan  # far before the 24 h window of tau
    with pytest.raises(ValueError):
        half_spread(b, T0 + 290 * Q)
