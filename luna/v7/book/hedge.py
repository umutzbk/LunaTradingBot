"""The single book-level residual BTC hedge (spec S-7.4, S-0.6, S-7.3 drawdown kill).

At each decision, after the leg targets are set: ``beta_book = sum_i n_i * beta_hat_i(t)`` over the signed
post-decision leg notionals ``n_i`` (``constructor.Targets.post``). If ``|beta_book| > 0.05 E`` the target is
``H_B = -beta_book``, else 0; ``|H_B| <= 0.5 E`` (clipped, AMB-18). The hedge trades (taker) only if
``|H_B - current| > 0.02 E``, or when the target is 0 and a hedge is open. After the drawdown kill the target
is 0 ("exit everything"). A missing / non-finite beta of a held leg gives no target and no trade (the hedge is
kept, fail closed, AMB-18). The hedge is never ML-controlled: no function here takes a score or model input.
Notionals and E are in book units; ``current`` is the signed BTC hedge notional (short < 0).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from luna.v7.book import EPS, positive
from luna.v7.constants import FROZEN

HG = FROZEN.hedge


@dataclass(frozen=True)
class HedgeDecision:
    target: float | None  # None: not computable (missing beta), keep the current hedge
    trade: bool
    beta_book: float | None
    reason: str


def _missing(legs: Mapping[str, float], betas: Mapping[str, float | None]) -> list[str]:
    out = []
    for sym, n in sorted(legs.items()):
        if n == 0:
            continue
        b = betas.get(sym)
        if b is None or not math.isfinite(float(b)):
            out.append(sym)
    return out


def beta_book(legs: Mapping[str, float], betas: Mapping[str, float | None]) -> float | None:
    """``sum_i n_i * beta_i`` (signed notionals); None if a held leg has no finite beta."""
    if _missing(legs, betas):
        return None
    return float(sum(float(n) * float(betas[s]) for s, n in sorted(legs.items()) if n != 0))  # type: ignore[arg-type]


def target(legs: Mapping[str, float], betas: Mapping[str, float | None], E: float) -> float | None:  # noqa: N803
    """Target hedge notional H_B (S-7.4), clipped to +-0.5 E; None if a beta is missing."""
    e = positive(E, "E")
    b = beta_book(legs, betas)
    if b is None:
        return None
    if abs(b) <= (HG.open_thr + EPS) * e:
        return 0.0
    return float(min(max(-b, -HG.max_abs * e), HG.max_abs * e))


def should_trade(target: float, current: float, E: float) -> bool:  # noqa: N803
    e = positive(E, "E")
    if target == 0.0:
        return current != 0.0
    return abs(target - current) > (HG.trade_thr + EPS) * e


def decide(
    legs: Mapping[str, float],
    betas: Mapping[str, float | None],
    E: float,  # noqa: N803
    *,
    current: float,
    killed: bool,
) -> HedgeDecision:
    """The hedge decision at one decision time (or a kill): target, whether to trade, beta_book, reason."""
    e = positive(E, "E")
    if not math.isfinite(current):
        raise ValueError("current hedge notional must be finite")
    if killed:
        return HedgeDecision(0.0, should_trade(0.0, current, e), None, "kill")
    miss = _missing(legs, betas)
    if miss:
        return HedgeDecision(None, False, None, "beta_missing:" + ",".join(miss))
    tgt = target(legs, betas, e)
    if tgt is None:
        raise RuntimeError("internal: hedge target missing after the beta check")
    return HedgeDecision(tgt, should_trade(tgt, current, e), beta_book(legs, betas), "beta")
