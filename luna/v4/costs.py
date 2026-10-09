"""V4 cost rows (spec §5.3, §5.4).

Fees (SC-2): spot maker 0.40% / taker 0.80%, perp maker 0.02% / taker 0.05%; taker is primary.
Spread: the quoted FULL spread (ask - bid) / mid; a taker pays HALF of it on every trade.
- historical spot: 20 bps full (PRIMARY), 10 bps full (S-SPREAD10);
- historical perp: mean of the analytics `spreads` samples in [t - 1h, t), else 10 bps full
  (fallback, counted);
- forward (both legs): the executing snapshot's real bid / ask (plus book walk when the order exceeds
  the top of book), passed in as the observed half spread.

Cost definitions (T4): ``fee = rate * |q| * mid``, ``spread_cost = |q| * half_spread * mid``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from luna.v4.constants import HOUR

SC2 = {"spot": {"maker": 0.004, "taker": 0.008}, "perp": {"maker": 0.0002, "taker": 0.0005}}
PERP_FALLBACK_FULL = 0.0010


@dataclass(frozen=True)
class CostRow:
    name: str
    spot_fee: float
    perp_fee: float
    spot_full_spread_hist: float
    spread_mult: float
    counts_for_go: bool
    historical_only: bool = False

    def fee(self, leg: str) -> float:
        return self.spot_fee if leg == "spot" else self.perp_fee

    def half_spread(self, leg: str, observed: float | None, forward: bool = False) -> float:
        """Half spread paid per trade. ``observed`` is the perp analytics / forward bid-ask half
        spread; historical spot has none and uses the row's quoted full spread."""
        if leg == "spot" and not forward:
            return self.spot_full_spread_hist / 2 * self.spread_mult
        if observed is None:
            raise ValueError(f"{leg}: observed half spread required")
        return observed * self.spread_mult


def _fees(costs) -> dict:
    if costs is None:
        return SC2
    return {k: {"maker": costs[k].maker_fee, "taker": costs[k].taker_fee} for k in ("spot", "perp")}


def cost_rows(costs=None) -> dict[str, CostRow]:
    """PRIMARY, S-SPREAD10, S-COST2X, S-MAKER (§5.4); ``costs`` = ``Settings.costs`` (SC-2)."""
    f = _fees(costs)
    st, pt = f["spot"]["taker"], f["perp"]["taker"]
    return {
        "PRIMARY": CostRow("PRIMARY", st, pt, 0.0020, 1.0, True),
        "S-SPREAD10": CostRow("S-SPREAD10", st, pt, 0.0010, 1.0, False, historical_only=True),
        "S-COST2X": CostRow("S-COST2X", 2 * st, 2 * pt, 0.0020, 2.0, True),
        "S-MAKER": CostRow("S-MAKER", f["spot"]["maker"], f["perp"]["maker"], 0.0020, 1.0, False),
    }


def perp_half_spread_hist(spreads: pd.DataFrame | None, t: pd.Timestamp) -> tuple[float, bool]:
    """(half spread, fallback used) for a perp trade decided at t: mean full spread of the analytics
    samples in the hour before the decision, [t - 1h, t) (§5.3)."""
    if spreads is not None and len(spreads):
        idx = spreads.index
        lo, hi = idx.searchsorted(t - HOUR, "left"), idx.searchsorted(t, "left")
        w = spreads["spread_full"].iloc[lo:hi].dropna()
        if len(w):
            return float(w.mean()) / 2, False
    return PERP_FALLBACK_FULL / 2, True


def trade_cost(qty: float, mid: float, fee_rate: float, half_spread: float) -> tuple[float, float]:
    return fee_rate * abs(qty) * mid, abs(qty) * half_spread * mid
