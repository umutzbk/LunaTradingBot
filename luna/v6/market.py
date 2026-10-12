"""Historical market for the V6 replay (spec S-DATA-2/3, S-COST). Point-in-time:

- mid at t = close of the bar that opened at t - 15m (mark, else a real perp trade bar);
- spread: 5-minute analytics snapshots labelled T, known at T + 5m (S-PIT10: T + 10m); decision
  window [t - 1h, t) needs >= 6 valid, fill window [t, t + 15m) known at t + 15m needs >= 2 valid;
  valid = finite, > 0, <= 500 bps; else the 10 bps fallback (S-FB-CONS: the per-asset p90 of the
  35 days before the evaluation start);
- taker fill of the order decided at t: at t + 15m, px = mid_fill (1 +/- half)(1 +/- slip),
  fee = fee_rate * |qty| * px; costs scaled by ``cost_mult`` (S-COST2X);
- spot (H1): mid proxy = spot bar close; half spread = max(2 * the same window's perp half, 5 bps).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6.data import BarPanel, FundingPanel
from luna.v6.spec import constants

BAR = pd.Timedelta("15min")
HOUR = pd.Timedelta("1h")
SNAP = pd.Timedelta("5min")
_K = constants()["costs"]
_F = constants()["fees"]
FALLBACK_FULL = _K["spread_fallback_bps"] * 1e-4
MAX_VALID_FULL = _K["spread_valid_max_bps"] * 1e-4
DEC_MIN, DEC_N = _K["decision_window_min_valid"]
FILL_MIN, FILL_N = _K["fill_window_min_valid"]
SLIP = _K["slippage_per_side"]
FUT_TAKER, FUT_MAKER = _F["futures_taker"], _F["futures_maker"]
SPOT_TAKER, SPOT_MAKER = _F["spot_taker"], _F["spot_maker"]
SPOT_HALF_FLOOR = 0.0005
VOLUME_BARS = 96


@dataclass(frozen=True)
class SpreadObs:
    full: float
    n_valid: int
    fallback: bool


class Spread5m:
    """PIT view of one asset's 5-minute spread snapshots (label T known at T + known_after)."""

    def __init__(self, s: pd.Series | None, known_after: pd.Timedelta = SNAP) -> None:
        if known_after < SNAP:
            raise ValueError("known_after shorter than the snapshot interval")
        self.known_after = known_after
        if s is None or not len(s):
            s = pd.Series([], dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
        idx = s.index
        if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or not idx.is_monotonic_increasing \
                or idx.has_duplicates or (len(idx) and (idx.as_unit("ns").asi8 % SNAP.value).any()):
            raise ValueError("spread labels must be a strictly increasing tz-aware 5-minute grid")
        self._t = idx.as_unit("ns").asi8
        v = s.to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            self._ok = np.isfinite(v) & (v > 0) & (v <= MAX_VALID_FULL)
        self._v = v

    def _obs(self, lo: pd.Timestamp, hi: pd.Timestamp, now: pd.Timestamp, need: int) -> SpreadObs:
        a = np.searchsorted(self._t, lo.value, "left")
        b = min(np.searchsorted(self._t, hi.value, "left"),
                np.searchsorted(self._t, (now - self.known_after).value, "right"))
        b = max(a, b)
        ok = self._ok[a:b]
        n = int(ok.sum())
        if n >= need:
            return SpreadObs(float(self._v[a:b][ok].mean()), n, False)
        return SpreadObs(FALLBACK_FULL, n, True)

    def decision(self, t: pd.Timestamp) -> SpreadObs:
        return self._obs(t - HOUR, t, t, DEC_MIN)

    def fill(self, t: pd.Timestamp) -> SpreadObs:
        return self._obs(t, t + BAR, t + BAR, FILL_MIN)


@dataclass(frozen=True)
class Fill:
    asset: str
    leg: str  # "perp" | "spot"
    decision_ts: pd.Timestamp
    ts: pd.Timestamp
    dq: float
    px: float
    mid: float
    mid_decision: float
    half: float
    fee: float
    spread_slip: float
    fallback: bool
    maker: bool = False


@dataclass
class HistMarket:
    mark: BarPanel
    trade: BarPanel | None
    volume_usd: BarPanel | None  # perp volume x close per bar
    funding: FundingPanel
    spreads: dict  # asset -> pd.Series of spread_full
    spot: BarPanel | None = None
    spot_volume_usd: BarPanel | None = None
    known_after: pd.Timedelta = SNAP
    cost_mult: float = 1.0
    fb_full: dict | None = None  # S-FB-CONS per-asset fallback (full spread)
    maker_bound: bool = False  # S-MAKER-BOUND (H1 only): maker fees, no spread/slip, fill at the mid
    spot_maker: bool = False  # S-MIXED (H1, information): spot leg maker fee, no spot spread/slip
    spot_spread_mult: float = 1.0  # S-SPOTSPREAD2X (information)
    _sp: dict = field(default_factory=dict, repr=False)
    _vol: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._sp = {a: Spread5m(s, self.known_after) for a, s in self.spreads.items()}

    @property
    def assets(self) -> list:
        return list(self.mark.close.columns)

    # --- prices ------------------------------------------------------------------------------------
    def mid(self, a: str, t: pd.Timestamp) -> float:
        for panel in (self.mark, self.trade):
            if panel is None or a not in panel.close.columns:
                continue
            v = float(panel.mid(t).get(a, np.nan))
            if math.isfinite(v) and v > 0:
                return v
        return math.nan

    def spot_mid(self, a: str, t: pd.Timestamp) -> float:
        if self.spot is None or a not in self.spot.close.columns:
            return math.nan
        v = float(self.spot.mid(t).get(a, np.nan))
        return v if math.isfinite(v) and v > 0 else math.nan

    def volume24(self, a: str, t: pd.Timestamp, spot: bool = False) -> float:
        panel = self.spot_volume_usd if spot else self.volume_usd
        if panel is None or a not in panel.close.columns:
            return math.nan
        w = panel.closed_window(t, VOLUME_BARS)[a]
        return float(w.sum()) if len(w) else math.nan

    # --- spreads -----------------------------------------------------------------------------------
    def _full(self, a: str, obs: SpreadObs) -> float:
        if obs.fallback and self.fb_full and a in self.fb_full:
            return self.fb_full[a]
        return obs.full

    def decision_half(self, a: str, t: pd.Timestamp) -> tuple[float, bool]:
        sp = self._sp.get(a) or Spread5m(None, self.known_after)
        obs = sp.decision(t)
        return self._full(a, obs) / 2, obs.fallback

    def fill_half(self, a: str, t: pd.Timestamp) -> tuple[float, bool]:
        sp = self._sp.get(a) or Spread5m(None, self.known_after)
        obs = sp.fill(t)
        return self._full(a, obs) / 2, obs.fallback

    @staticmethod
    def spot_half(perp_half: float) -> float:
        return max(2 * perp_half, SPOT_HALF_FLOOR)

    def decision_cost(self, a: str, t: pd.Timestamp, leg: str = "perp") -> float:
        """One-way taker cost the strategy estimates at t (S-COST-3): fee + decision-window half spread
        + slip. Row multipliers (S-COST2X, S-SPOTSPREAD2X) and maker rows change realised fills only;
        decisions are the PRIMARY ones (declared D-S1). Known-time and fallback rows (S-PIT10,
        S-FB-CONS) change the information and therefore the decisions."""
        half, _ = self.decision_half(a, t)
        if leg == "spot":
            return SPOT_TAKER + self.spot_half(half) + SLIP
        return FUT_TAKER + half + SLIP

    # --- fills -------------------------------------------------------------------------------------
    def fill(self, a: str, dq: float, t: pd.Timestamp, leg: str = "perp") -> Fill | None:
        """Taker fill of the order decided at t, booked at t + 15m (None without a mid at t + 15m)."""
        ft = t + BAR
        if leg == "spot":
            mid, mid_d = self.spot_mid(a, ft), self.spot_mid(a, t)
        else:
            mid, mid_d = self.mid(a, ft), self.mid(a, t)
        if not math.isfinite(mid):
            return None
        ph, fb = self.fill_half(a, t)
        s = 1.0 if dq > 0 else -1.0
        if self.maker_bound or (self.spot_maker and leg == "spot"):
            half, slip = 0.0, 0.0
            fee_rate = SPOT_MAKER if leg == "spot" else FUT_MAKER
        else:
            half = self.spot_spread_mult * self.spot_half(ph) if leg == "spot" else ph
            half, slip = self.cost_mult * half, self.cost_mult * SLIP
            fee_rate = self.cost_mult * (SPOT_TAKER if leg == "spot" else FUT_TAKER)
        px = mid * (1 + s * half) * (1 + s * slip)
        fee = fee_rate * abs(dq) * px
        return Fill(a, leg, t, ft, dq, px, mid, mid_d, half, fee, abs(dq) * s * (px - mid), fb)

    def unwind_cost(self, a: str, q: float, t: pd.Timestamp, leg: str = "perp") -> tuple[float, float, float]:
        """Terminal unwind at the window end (S-ACC-4): (mid, fee, spread_slip) with the decision-window
        spread and mid known at t; no fill window beyond the end."""
        mid = self.spot_mid(a, t) if leg == "spot" else self.mid(a, t)
        if not math.isfinite(mid) or q == 0:
            return mid, 0.0, 0.0
        ph, _ = self.decision_half(a, t)
        if self.maker_bound or (self.spot_maker and leg == "spot"):
            half, slip, fr = 0.0, 0.0, (SPOT_MAKER if leg == "spot" else FUT_MAKER)
        else:
            half = self.spot_spread_mult * self.spot_half(ph) if leg == "spot" else ph
            half, slip = self.cost_mult * half, self.cost_mult * SLIP
            fr = self.cost_mult * (SPOT_TAKER if leg == "spot" else FUT_TAKER)
        s = -1.0 if q > 0 else 1.0  # selling a long, buying back a short
        px = mid * (1 + s * half) * (1 + s * slip)
        return mid, fr * abs(q) * px, abs(q) * abs(px - mid)

    def pair_fill(self, a: str, q: float, t: pd.Timestamp, economic: bool = True, perp_q: float | None = None):
        """H1 historical execution (S-H1-5): both legs taker at t + 15m, atomic (both or neither).
        ``perp_q``: the perp short quantity to close (default: matched, -q). Returns
        ([spot fills], [perp fills]) or None."""
        fs = self.fill(a, q, t, leg="spot")
        fp = self.fill(a, (-q if perp_q is None else math.copysign(perp_q, -q)), t, leg="perp")
        if fs is None or fp is None:
            return None
        return [fs], [fp]
