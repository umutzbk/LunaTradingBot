"""Provenance only: regenerates vectors.json. The committed vectors.json (literal hex floats) is the fixture;
nothing at comparison time depends on this script, numpy or any RNG.

Run once with a Python that has numpy (e.g. the repo .venv):  python gen_vectors.py vectors.json
Cases 01-11 replay tests/v7/test_spread.py (same ln-space rows, same numpy seeds); 12-19 are adversarial.
Prices are exp(ln-value); every window is (first, last) = 0-based indices of the LATER bar of the pair
products, the convention of tests/v7/ar_reference.py::ar_window."""

from __future__ import annotations

import json
import math
import sys

import numpy as np

NAN = float("nan")


def enc(x: float):
    return None if x != x else float(x).hex()


def from_ln(rows):
    """rows: (ln_high, ln_low, ln_close) or None (missing bar) -> price lists (NaN for missing)."""
    hi = [math.exp(r[0]) if r else NAN for r in rows]
    lo = [math.exp(r[1]) if r else NAN for r in rows]
    cl = [math.exp(r[2]) if r else NAN for r in rows]
    return hi, lo, cl


def full(n):
    return [[1, n - 1]]


cases = []


def add(name, note, hi, lo, cl, windows=None, rolling=None):
    n = len(cl)
    assert len(hi) == len(lo) == n
    cases.append(dict(name=name, note=note, n=n, high=[enc(x) for x in hi], low=[enc(x) for x in lo],
                      close=[enc(x) for x in cl], windows=windows or full(n), rolling_width=rolling))


A = (0.02, 0.0, 0.02)
B = (0.01, -0.01, 0.0)
NEG = (0.05, 0.03, 0.04)
E = (0.05, 0.03, 0.04)

add("01_single_pair", "test_ar_single_pair: pi = 0.0008", *from_ln([A, B]))
add("02_negative_mean", "test_ar_negative_mean_gives_floor: single negative pair (sign=TRUE matters)",
    *from_ln([A, NEG]))
add("03_missing_bar_all_nan", "test_ar_missing_bar_breaks_pair: no valid pair -> R NaN, ref None",
    *from_ln([A, None, B]))
add("04_missing_bar_two_pairs", "test_ar_missing_bar_breaks_pair: pairs (0,1),(3,4) only",
    *from_ln([A, B, None, A, B]))
e = 0.5e-4
add("05_below_floor", "test_ar_lambda_then_floor_order (below 1 bp)", *from_ln([(e, 0.0, e), (e / 2, -e / 2, 0.0)]))
e = 1.5e-4 / math.sqrt(2)
add("06_above_floor", "test_ar_lambda_then_floor_order (above 1 bp)", *from_ln([(e, 0.0, e), (e / 2, -e / 2, 0.0)]))

rng = np.random.default_rng(1)
rows = []
for _ in range(40):
    c = rng.normal(0, 0.01)
    rows.append((c + abs(rng.normal(0, 0.005)), c - abs(rng.normal(0, 0.005)), c))
hi, lo, cl = from_ln(rows)
add("07a_scale_invariance_x1", "test_ar_scale_invariance rng(1), unscaled", hi, lo, cl)
add("07b_scale_invariance_x12345", "test_ar_scale_invariance rng(1), prices * 12345",
    [x * 12345.0 for x in hi], [x * 12345.0 for x in lo], [x * 12345.0 for x in cl])
add("08_constant_prices", "test_ar_constant_prices_floor: all prices 1.0 -> mean exactly 0",
    *from_ln([(0.0, 0.0, 0.0)] * 10))

rng = np.random.default_rng(7)
n = 90
rows = []
for i in range(n):
    c = 0.01 * math.sin(i / 5) + rng.normal(0, 0.003)
    r = (c + abs(rng.normal(0, 0.004)), c - abs(rng.normal(0, 0.004)), c + rng.normal(0, 0.002))
    rows.append((max(r), min(r), r[2]))
for gap in (None, 40, 55):
    rr = list(rows)
    if gap is not None:
        rr[gap] = None
    add(f"09_reference_rng7_gap{gap}", "test_ar_matches_independent_reference rng(7), window (1, n-1)",
        *from_ln(rr))

rng = np.random.default_rng(11)
n = 150
rows = []
for i in range(n):
    c = 0.01 * math.sin(i / 7) + rng.normal(0, 0.003)
    r = (c + abs(rng.normal(0, 0.004)), c - abs(rng.normal(0, 0.004)), c + rng.normal(0, 0.002))
    rows.append((max(r), min(r), r[2]))
add("10_tau_trim_rng11", "test_ar_tau_trims_leading_pairs_matches_reference rng(11), window (45, 139)",
    *from_ln(rows), windows=[[45, 139], [1, 149]])

add("11a_mixed_sign_zero_mean", "test_ar_mixed_sign_positive_mean_hand_worked: mean 0 in exact arithmetic",
    *from_ln([A, E, A, B]))
add("11b_mixed_sign_positive_mean", "test_ar_mixed_sign_positive_mean_hand_worked: mean 0.00016",
    *from_ln([A, E, A, B, A, B]))

# ---- adversarial
rows = [(0.03 * k + 0.02, 0.03 * k, 0.03 * k + 0.02 - 0.001 * (k % 3)) for k in range(8)]
rows[5] = (0.15 + 0.02, 0.15, 0.15 + 0.019)
rows[6] = (0.15 + 0.02, 0.15, 0.15 + 0.002)  # one positive pair (5,6) inside a negative window
add("12_negative_window_mean_multi", "rising staircase, close near the high, next mid-range above it: "
    "window mean clearly negative (one positive pair inside); sign=TRUE returns -sqrt(|mean|)",
    *from_ln(rows), windows=[[1, 7], [1, 4], [6, 6]])
rows = [(0.01 * k, 0.01 * k, 0.01 * k + (0.002 if k % 2 else -0.003)) for k in range(12)]
add("13_high_equals_low", "high == low on every bar (close off the range); also mean of log-range 0",
    *from_ln(rows), windows=[[1, 11], [3, 7]])
add("14_high_equals_low_equals_close", "high == low == close on varying bars: every pair product 0",
    *from_ln([(0.01 * k, 0.01 * k, 0.01 * k) for k in range(8)]))

rng = np.random.default_rng(20261015)
rows = []
p = 0.0
for i in range(60):
    p += rng.normal(0, 0.002)
    hh, ll = p + abs(rng.normal(0, 0.0015)), p - abs(rng.normal(0, 0.0015))
    rows.append((hh, ll, float(rng.uniform(ll, hh))))
for k in (5, 6, 7, 20, 33, 34, 58):
    rows[k] = None
add("15_nan_gaps", "60 bars with runs of missing bars (5-7, 20, 33-34, 58)", *from_ln(rows),
    windows=[[1, 59], [8, 19], [6, 9], [7, 8], [21, 40]], rolling=12)

hi, lo, cl = from_ln([(0.02, 0.0, 0.015), (0.025, 0.005, 0.01), (0.03, 0.01, 0.02), (0.02, 0.0, 0.005)])
cl[1] = NAN
add("16_partial_nan_close_only", "bar 1 has high/low but close NaN: R and the transcription keep pair (0,1) "
    "(uses c0, eta0, eta1); luna.v7.spread drops the whole bar", hi, lo, cl)
hi, lo, cl = from_ln([(0.02, 0.0, 0.015), (0.025, 0.005, 0.01), (0.03, 0.01, 0.02), (0.02, 0.0, 0.005)])
hi[2] = NAN
add("17_partial_nan_high_only", "bar 2 high NaN: both pairs touching eta2 are NaN in every implementation",
    hi, lo, cl)

rng = np.random.default_rng(424242)
rows = []
p = math.log(30000.0)
for i in range(2000):
    o = p
    steps = rng.normal(0, 0.0012, 15)
    path = o + np.cumsum(steps)
    sp = 0.0004  # bid-ask half-spread bounce on the observed close
    cl_ = path[-1] + (sp if rng.random() < 0.5 else -sp)
    hh = max(float(path.max()), o) + sp
    ll = min(float(path.min()), o) - sp
    rows.append((hh, ll, min(max(cl_, ll), hh)))
    p = float(path[-1])
add("18_random_walk_2000", "seeded (424242) random-walk OHLC, 15 sub-steps per bar, 4 bp half-spread bounce; "
    "rolling width 96 (= 95 pairs, the S-4.5 24 h window at tau = close of the last bar + 1 min)", *from_ln(rows),
    windows=[[1, 1999], [1, 96], [905, 1000], [1905, 1999]], rolling=96)
rows18 = list(rows)
for k in range(0, 2000, 37):
    rows18[k] = None
add("19_random_walk_2000_gappy", "case 18 with every 37th bar missing; rolling width 96", *from_ln(rows18),
    windows=[[1, 1999], [38, 73], [1905, 1999]], rolling=96)

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "vectors.json"
    doc = dict(
        schema="luna-v7-ar-vectors/1",
        description=("Fixed OHLC vectors for the CRAN bidask 2.1.5 AR (sign = TRUE, na.rm = TRUE) equality check. "
                     "Floats are Python float.hex() strings (exact); null = NaN/NA (missing). windows: "
                     "[first, last] = 0-based indices of the later bar of the pair products (ar_window "
                     "convention); rolling_width: also run spread(width = w)."),
        cases=cases,
    )
    with open(out, "w", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    print(out, len(cases), "cases")
