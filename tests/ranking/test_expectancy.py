import numpy as np
import pandas as pd
import pytest

from luna.ranking.expectancy import ExpectancyRanker, fit_expectancy
from luna.types import Candidate, ExitSpec, Leg, StopSpec

TS = pd.Timestamp("2024-01-01", tz="UTC")


def trades(specs, seed=0):
    """specs: list of (strategy, key, regime, n, mean, sd)."""
    rng = np.random.default_rng(seed)
    rows = []
    day = 0
    for s, k, rg, n, mean, sd in specs:
        for r in rng.normal(mean, sd, n):
            rows.append({"strategy": s, "params_id": "p1", "key": k, "regime": rg,
                         "r_net": float(r), "day": day // 2})
            day += 1
    return pd.DataFrame(rows)


def fit(df, **kw):
    args = dict(n0=50, n_min=30, n_boot=500, ci=0.90)
    args.update(kw)
    return fit_expectancy(df, **args)


def cand(strategy="s", key="k", regime="r", dist=0.02):
    return Candidate(TS, strategy, key, (Leg("BTC", 1.0),), 1, 100.0,
                     StopSpec("price", 98.0, dist), ExitSpec(10), regime, "t", {})


def test_small_n_shrinks_to_parent():
    df = trades([("s", "k", "a", 200, 0.3, 0.5), ("s", "k", "b", 5, 2.0, 0.1)])
    t = fit(df)
    parent, cell = t[("s", "k", "*")], t[("s", "k", "b")]
    assert cell.n == 5 and cell.mean_r == pytest.approx(2.0, abs=0.2)
    w = 5 / 55
    assert cell.shrunk_r == pytest.approx(w * cell.mean_r + (1 - w) * parent.shrunk_r)
    assert abs(cell.shrunk_r - parent.shrunk_r) < abs(cell.mean_r - parent.shrunk_r) * 0.2
    top = t[("s", "*", "*")]
    assert top.shrunk_r == pytest.approx(top.n / (top.n + 50) * top.mean_r)


def test_rejects_below_n_min():
    t = fit(trades([("s", "k", "a", 29, 1.0, 0.1)]))
    c = t[("s", "k", "*")]
    assert not c.accepted and c.reason == "n<30"


def test_rejects_when_ci_crosses_zero():
    t = fit(trades([("s", "k", "a", 100, 0.05, 2.0)]))
    c = t[("s", "k", "*")]
    assert not c.accepted and c.reason == "ci_lo<=0" and c.lo_r <= 0


def test_accepts_clear_edge():
    t = fit(trades([("s", "k", "a", 200, 0.3, 0.3)]))
    c = t[("s", "k", "*")]
    assert c.accepted and c.reason == "ok" and c.lo_r > 0 and c.shrunk_r > 0
    assert t[("s", "k", "a")].accepted


def test_regime_cell_cannot_accept_alone():
    df = trades([("s", "k", "a", 100, 1.0, 0.2), ("s", "k", "b", 100, -1.0, 0.2)])
    t = fit(df)
    assert not t[("s", "k", "*")].accepted
    cell = t[("s", "k", "a")]
    assert cell.shrunk_r > 0 and not cell.accepted


def test_regime_negative_cell_rejected_even_if_parent_accepted():
    df = trades([("s", "k", "a", 300, 0.5, 0.3), ("s", "k", "b", 100, -0.4, 0.1)])
    t = fit(df)
    assert t[("s", "k", "*")].accepted
    assert t[("s", "k", "b")].shrunk_r < 0 and not t[("s", "k", "b")].accepted


def test_fit_is_deterministic():
    df = trades([("s", "k", "a", 100, 0.2, 0.5)])
    assert fit(df) == fit(df)


def test_ranker_scores_and_rejects():
    df = trades([("s", "good", "a", 200, 0.3, 0.3), ("s", "bad", "a", 29, 1.0, 0.1)])
    t = fit(df)
    rk = ExpectancyRanker(t, {"s": "p1"})
    out = rk.score(cand(key="good", regime="a"), TS)
    sh = t[("s", "good", "a")].shrunk_r
    assert out.score == pytest.approx(sh)
    assert out.edge_bps == pytest.approx(sh * 0.02 * 1e4)
    # unseen regime falls back to the (strategy,key) cell
    out2 = rk.score(cand(key="good", regime="zzz"), TS)
    assert out2.score == pytest.approx(t[("s", "good", "*")].shrunk_r)
    assert rk.score(cand(key="bad", regime="a"), TS) is None
    assert rk.score(cand(key="unknown"), TS) is None
    assert rk.score(cand(strategy="other", key="good", regime="a"), TS) is None
