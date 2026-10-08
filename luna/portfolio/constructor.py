"""Portfolio constructor (spec §5.6): picks and sizes candidates under the portfolio budget.

Candidates are taken in descending score order. Each is checked, in order, against:

1. ``short_not_allowed``   spot account and some leg would be short.
2. ``max_positions``       open + pending + already selected positions >= max_positions.
3. ``opposite_exposure``   a leg trades an asset that an open/pending/selected position holds in
                           the other direction (pairs legs included).
4. ``corr_bucket``         a leg has the same direction as an open/selected exposure whose 30-day
                           return correlation is > corr_bucket_threshold (same asset = corr 1):
                           one position per risk bucket. A pair with fewer than
                           MIN_CORR_OVERLAP joint return observations counts as uncorrelated.
5. ``invalid_candidate``   non-finite / non-positive stop distance, missing mid, or equity <= 0.
6. ``leverage_cap``        no gross-leverage headroom left (open + selected gross >= cap).
7. sizing                  ``risk_amt = risk_per_trade * equity * regime_mult``,
                           ``gross = risk_amt / (stop_distance + round_trip)``, clipped to the
                           remaining gross-leverage headroom and to the per-leg notional cap. The
                           selection's ``risk_amount`` is ``gross * (stop_distance + round_trip)``
                           (= risk_amt unless clipped).
8. ``below_min_notional``  a leg rounds below the instrument's qty step / min_qty / min_notional.
9. ``open_risk``           open + selected risk + this risk > max_open_risk * equity.

Round trip is the |weight|-weighted mean of ``CostModel.round_trip_frac`` over the legs, with taker
fees on perp (IOC) and maker fees on spot (post-only).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.config import RiskLimits
from luna.execution.costs import CostModel
from luna.planner import round_qty
from luna.portfolio.portfolio import Portfolio
from luna.types import Candidate, Instrument, OrderModel, default_order_model

MIN_CORR_OVERLAP = 96  # one day of 15-minute bars


@dataclass
class Selection:
    cand: Candidate
    gross_notional: float
    risk_amount: float


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _leg_signs(cand: Candidate) -> list[tuple[str, int]]:
    return [(leg.asset, _sign(leg.weight * cand.direction)) for leg in cand.legs if leg.weight != 0]


class _Corr:
    """Lazy, memoized pairwise correlation over the 30-day return window."""

    def __init__(self, returns: pd.DataFrame | None) -> None:
        self.returns = returns
        self._cache: dict[tuple[str, str], float] = {}

    def __call__(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        key = (a, b) if a < b else (b, a)
        if key not in self._cache:
            r = self.returns
            val = math.nan
            if r is not None and a in r.columns and b in r.columns:
                x = r[[a, b]].to_numpy(dtype=float)
                x = x[np.isfinite(x).all(axis=1)]
                if len(x) >= MIN_CORR_OVERLAP and x[:, 0].std() > 0 and x[:, 1].std() > 0:
                    val = float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])
            self._cache[key] = val
        return self._cache[key]


def construct(
    cands: list[Candidate],
    pf: Portfolio,
    limits: RiskLimits,
    costs: CostModel,
    instruments: dict[str, Instrument],
    mids: dict[str, float],
    returns_30d: pd.DataFrame,
    regime_mult: Callable[[Candidate], float],
    pending: Sequence[Candidate] = (),
    liquidity: OrderModel | None = None,
) -> tuple[list[Selection], list[tuple[Candidate, str]]]:
    """Select and size candidates. Returns (selections, [(candidate, skip_reason)]). Pure: `pf` is
    only read.

    ``pending``: candidates of entries still working (live: submitted, not yet registered as a
    position). They count as open positions for ``max_positions`` and their legs as exposures for
    ``opposite_exposure`` / ``corr_bucket`` (their risk / gross are checked by the risk engine).
    Always empty in the backtest (orders settle on the next bar).

    ``liquidity``: fee side of the round trip (None = the account default order model)."""
    corr = _Corr(returns_30d)
    equity = pf.equity()
    n_positions = len(pf.positions) + len(pending)
    open_risk = pf.open_risk()
    gross_used = pf.gross_notional()
    exposures: list[tuple[str, int]] = []  # (asset, sign) of open and selected positions
    for pos in pf.positions.values():
        for leg in pos.legs:
            if leg.weight != 0:
                exposures.append((leg.asset, _sign(leg.weight * pos.direction)))
    for c in pending:
        exposures.extend(_leg_signs(c))
    liquidity = liquidity or default_order_model(pf.kind)

    selected: list[Selection] = []
    skipped: list[tuple[Candidate, str]] = []
    for cand in sorted(cands, key=lambda c: c.score, reverse=True):  # stable for ties
        reason, sel = _evaluate(
            cand, pf.kind, limits, costs, instruments, mids, regime_mult, corr, equity,
            n_positions, open_risk, gross_used, exposures, liquidity,
        )
        if sel is None:
            skipped.append((cand, reason))
            continue
        selected.append(sel)
        n_positions += 1
        open_risk += sel.risk_amount
        gross_used += sel.gross_notional
        exposures.extend(_leg_signs(cand))
    return selected, skipped


def _evaluate(
    cand: Candidate,
    kind: str,
    limits: RiskLimits,
    costs: CostModel,
    instruments: dict[str, Instrument],
    mids: dict[str, float],
    regime_mult: Callable[[Candidate], float],
    corr: _Corr,
    equity: float,
    n_positions: int,
    open_risk: float,
    gross_used: float,
    exposures: list[tuple[str, int]],
    liquidity: str,
) -> tuple[str, Selection | None]:
    signs = _leg_signs(cand)
    if (kind == "spot" or not limits.allow_short) and any(s < 0 for _, s in signs):
        return "short_not_allowed", None
    if n_positions >= limits.max_positions:
        return "max_positions", None
    for asset, s in signs:
        if any(b == asset and t != s for b, t in exposures):
            return "opposite_exposure", None
    for asset, s in signs:
        for b, t in exposures:
            if t == s and corr(asset, b) > limits.corr_bucket_threshold:
                return "corr_bucket", None

    dist = cand.stop.distance_frac
    weights = [abs(leg.weight) for leg in cand.legs if leg.weight != 0]
    mult = regime_mult(cand)
    ok = (
        signs
        and math.isfinite(dist) and dist > 0
        and math.isfinite(equity) and equity > 0
        and math.isfinite(mult) and mult >= 0
        and all(math.isfinite(mids.get(a, math.nan)) and mids[a] > 0 for a, _ in signs)
    )
    if not ok:
        return "invalid_candidate", None
    total_w = sum(weights)
    legs = [leg for leg in cand.legs if leg.weight != 0]
    rt = sum(abs(leg.weight) * costs.round_trip_frac(leg.asset, liquidity) for leg in legs) / total_w
    headroom = limits.max_gross_leverage * equity - gross_used
    if not headroom > 0:
        return "leverage_cap", None
    risk_amt = limits.risk_per_trade * equity * mult
    gross = risk_amt / (dist + rt)
    gross = min(
        gross,
        headroom,
        limits.max_leg_notional_frac * equity * total_w / max(weights),
    )
    if not gross > 0:
        return "below_min_notional", None
    for leg in legs:
        inst = instruments[leg.asset]
        mid = mids[leg.asset]
        qty = round_qty(gross * abs(leg.weight) / total_w / mid, inst.qty_step)
        if qty <= 0 or qty < inst.min_qty or qty * mid < inst.min_notional:
            return "below_min_notional", None
    risk = gross * (dist + rt)
    if not (open_risk + risk <= limits.max_open_risk * equity):
        return "open_risk", None
    return "", Selection(cand, gross, risk)
