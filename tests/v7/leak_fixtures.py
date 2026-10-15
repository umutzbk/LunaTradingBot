"""Synthetic inputs for the leakage machinery (``luna.v7.leakage``): small seeded label panels for the three
horizons, real ``cv`` fold models and final models under a tiny test-only search space, and cached passed
LeakageReports for the verdict / runner plumbing tests. No real data.

The panel has the three daily decisions (00:05 / 08:05 / 16:05) on every 4th UTC day and 2 symbols x 2 sides;
the label of the previous decision 8 h earlier is never known at t, so L1 refuses the L9b shifted-label
feature (at the first decision of a sampled day the previous decision is 4 days earlier and its 24 h / 72 h
label is already known: accepted there).
The signal is spread over four features (no single one carries > 50 % of the weight / gain: no K7 trigger);
the label has a persistent AR(1) part per (symbol, side), so the shifted label of L9b is predictive.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from luna.v7 import cv, leakage, models
from luna.v7.features import FEATURES
from luna.v7.models import Config
from luna.v7.timeutil import MIN, D, H
from luna.v7.windows import D0, W_HOLD, outer_folds

HORIZONS = (24, 72, 168)
FOLD = outer_folds(D0)[0]
SMALL = {"num_leaves": 4, "min_data_in_leaf": 20}
TINY = [  # test-only stand-in for the frozen search space: one config per family (runtime)
    Config("en", {"alpha": 1e-2, "l1_ratio": 0.2}),
    Config("lgb_reg", SMALL),
    Config("logit_en", {"alpha": 1e-2, "l1_ratio": 0.5}),
    Config("lgb_clf", SMALL),
]
SIGNAL = ("f2_same_sign_share_30d", "f5_premium_last", "f9_rvol_24h", "f10_ret_24h")  # not the sandbox column
AR_RHO, AR_SD = 0.9, 10.0  # overlapping-horizon labels are autocorrelated: the L9b shifted label predicts
STEP_DAYS = 4  # sampled days (runtime: the per-date IC loop dominates)
DECISIONS = np.array([5 * MIN, 8 * H + 5 * MIN, 16 * H + 5 * MIN])  # the S-4.1 decision clock
COMMIT = "a" * 40
MANIFESTS = {"W_DEV": "b" * 64}
PROV = dict(commit=COMMIT, dirty=False, data_manifests=MANIFESTS)


@contextmanager
def tiny_space():
    with mock.patch.object(models, "search_space", lambda h: list(TINY)):
        yield


@functools.lru_cache(maxsize=16)
def _panel(h: int, seed: int, leak: str | None, hi: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed + h)
    days = np.arange(D0 // D, hi // D, STEP_DAYS) * D
    ts = np.repeat((days[:, None] + DECISIONS[None, :]).reshape(-1), 4)
    n = len(ts)
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=list(FEATURES))
    df["side"] = np.tile([1.0, -1.0], n // 2)
    df["decision_ts"] = ts
    df["symbol"] = np.tile(["AAA", "AAA", "BBB", "BBB"], n // 4)
    df["h"] = h
    df["label_known_at"] = ts + h * H + MIN
    z = sum(df[c] for c in SIGNAL)
    e = np.zeros((n // 4, 4))  # persistent label component per (symbol, side): AR(1) over the decisions
    shocks = rng.normal(scale=AR_SD * np.sqrt(1 - AR_RHO**2), size=e.shape)
    e[0] = rng.normal(scale=AR_SD, size=4)
    for k in range(1, len(e)):
        e[k] = AR_RHO * e[k - 1] + shocks[k]
    df["y_reg"] = 1.5 * z + e.reshape(-1) + rng.normal(scale=4, size=n)
    df["y_pos"] = (df["y_reg"] > 0).astype(float)
    if leak is not None:  # the label copied into a feature, plus noise (a deliberately leaky panel)
        df[leak] = df["y_reg"] + rng.normal(scale=float(df["y_reg"].std()), size=n)
    df = df[df["label_known_at"] < W_HOLD[0]]
    return df.reset_index(drop=True)


def panel(h: int, seed: int = 0, leak: str | None = None, hi: int = W_HOLD[0]) -> pd.DataFrame:
    return _panel(h, seed, leak, hi).copy()


def panels(seed: int = 0, leak: str | None = None, hi: int = W_HOLD[0]) -> dict[int, pd.DataFrame]:
    return {h: panel(h, seed, leak, hi) for h in HORIZONS}


@functools.lru_cache(maxsize=16)
def fold_model(h: int, seed: int = 0, leak: str | None = None):
    with tiny_space():
        return cv.run_fold(FOLD, panel(h, seed, leak, FOLD.test_hi), h)


def fold_models(seed: int = 0, leak: str | None = None, horizons=HORIZONS) -> dict[int, tuple]:
    return {h: (fold_model(h, seed, leak),) for h in horizons}


@functools.lru_cache(maxsize=8)
def final(h: int, family: str, seed: int = 0, leak: str | None = None):
    with tiny_space():
        return cv.final_model(panel(h, seed, leak), h, family)


def run_checks(arm: str = "A2", h: int = 72, seed: int = 0, leak: str | None = None, **kw):
    """``leakage.run_leakage_checks`` on the synthetic panels (fold models / final model of ML*)."""
    reg = "en" if arm == "A2" else "lgb_reg"
    args = dict(samples=panels(seed, leak, FOLD.test_hi), fold_models=fold_models(seed, leak),
                final_model=final(h, reg, seed, leak), ml_star=(arm, h), **PROV)
    args.update(kw)
    with tiny_space():
        return leakage.run_leakage_checks(**args)


@functools.lru_cache(maxsize=8)
def passed_report(arm: str = "A2", h: int = 72) -> leakage.LeakageReport:
    r = run_checks(arm, h)
    assert r.passed, r.failures
    return r


def clean_records(t: int):
    """A decision record stand-in whose inputs were all known at t - 1 min (L1 holds)."""
    return SimpleNamespace(max_known_at=int(t) - MIN)


def holdout_report(pre, runs, records=clean_records) -> leakage.HoldoutLeakageReport:
    return leakage.run_holdout_checks(pre, runs=runs, records=records)
