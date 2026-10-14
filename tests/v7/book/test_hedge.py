"""Single book-level residual BTC hedge (S-7.4, S-0.6): exact thresholds."""

from __future__ import annotations

import inspect

import pytest

from luna.v7.book import hedge
from luna.v7.book.hedge import HedgeDecision


def test_hedge_thresholds():
    legs = {"AAA": 0.1, "BBB": -0.1}  # signed post-decision notionals
    # beta_book = 0.1 * 1.5 - 0.1 * 1.0 = 0.05: not > 0.05 E -> target 0
    assert hedge.beta_book(legs, {"AAA": 1.5, "BBB": 1.0}) == pytest.approx(0.05, abs=1e-15)
    assert hedge.target(legs, {"AAA": 1.5, "BBB": 1.0}, 1.0) == 0.0
    # beta_book = 0.06 -> H_B = -0.06
    assert hedge.target(legs, {"AAA": 1.6, "BBB": 1.0}, 1.0) == pytest.approx(-0.06, abs=1e-15)
    # E scales the thresholds: with E = 2, 0.06 is not > 0.1
    assert hedge.target(legs, {"AAA": 1.6, "BBB": 1.0}, 2.0) == 0.0
    # negative beta_book -> long hedge
    assert hedge.target(legs, {"AAA": 1.0, "BBB": 1.7}, 1.0) == pytest.approx(0.07, abs=1e-15)
    # |H_B| <= 0.5 E (clipped)
    big = {f"L{i}": 0.1 for i in range(5)} | {f"S{i}": -0.1 for i in range(5)}
    betas = {f"L{i}": 3.0 for i in range(5)} | {f"S{i}": 0.0 for i in range(5)}
    assert hedge.beta_book(big, betas) == pytest.approx(1.5) and hedge.target(big, betas, 1.0) == -0.5
    # trade thresholds: |H_B - current| > 0.02 E, or target 0 with a hedge open
    assert hedge.should_trade(-0.06, 0.0, 1.0)
    assert not hedge.should_trade(-0.06, -0.04, 1.0)  # 0.02 is not > 0.02
    assert hedge.should_trade(-0.06, -0.039, 1.0)
    assert hedge.should_trade(0.0, -0.01, 1.0)  # target 0 with a hedge open: close it
    assert not hedge.should_trade(0.0, 0.0, 1.0)
    assert not hedge.should_trade(-0.06, -0.05, 1.0)
    # decide(): a missing beta for a held leg -> no target, no trade (fail closed, hedge kept)
    d = hedge.decide(legs, {"AAA": 1.6}, 1.0, current=-0.03, killed=False)
    assert d == HedgeDecision(target=None, trade=False, beta_book=None, reason="beta_missing:BBB")
    d = hedge.decide(legs, {"AAA": 1.6, "BBB": float("nan")}, 1.0, current=0.0, killed=False)
    assert d.target is None and not d.trade
    d = hedge.decide(legs, {"AAA": 1.6, "BBB": 1.0}, 1.0, current=0.0, killed=False)
    assert d.trade and d.target == pytest.approx(-0.06) and d.reason == "beta"
    # killed: exit everything, the hedge included, whatever the betas
    d = hedge.decide(legs, {}, 1.0, current=-0.2, killed=True)
    assert d == HedgeDecision(target=0.0, trade=True, beta_book=None, reason="kill")
    assert not hedge.decide({}, {}, 1.0, current=0.0, killed=True).trade
    # an empty book has beta 0 -> target 0
    assert hedge.decide({}, {}, 1.0, current=-0.1, killed=False) == HedgeDecision(0.0, True, 0.0, "beta")
    # S-0.6: the hedge has no score / model input
    for fn in (hedge.beta_book, hedge.target, hedge.should_trade, hedge.decide):
        params = set(inspect.signature(fn).parameters)
        assert not {p for p in params if any(w in p for w in ("score", "rank", "model", "yhat"))}
    with pytest.raises(ValueError):
        hedge.target(legs, {"AAA": 1.0, "BBB": 1.0}, 0.0)
