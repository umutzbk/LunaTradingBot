"""Per-asset carry signals and eligibility at one evaluation (pure).

Primary ranking signal (first strategy version, as V4 H2-L30): ``FL30`` = mean realized relative
funding of the ended hourly periods in the last ``signal_hours``. Everything else is monitored and
logged but does not rank: FL7, the current period's rate, percentile, persistence, liquidity,
spread and the estimated per-side trading cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from luna.carry.config import CarryConfig
from luna.carry.funding import HOURS_PER_YEAR, FundingStore
from luna.carry.market import MarketSnapshot

NAN = math.nan
BPS = 1e-4


@dataclass(frozen=True)
class AssetSignal:
    asset: str
    eligible: bool
    reasons: tuple[str, ...]
    fl: float  # FL30, relative funding per hour
    fl_n: int
    fl_short: float  # FL7
    current_rel: float  # current period's rate (ticker)
    pred_rel: float  # next period's estimate (ticker)
    rank: int | None  # 1 = highest FL among eligible assets
    pct: float | None  # percentile among eligible (1 = highest)
    persistence: float  # share of recent periods with the sign of FL
    mid: float
    mark: float
    spread_full: float
    half_spread: float
    top_book_usd: float
    depth_usd: float  # within +/- order_band, thinner side (top of book without a fresh book)
    volume_usd: float
    quote_age_s: float
    side_cost: float  # fee + half spread + slippage estimate, fraction of notional per side

    @property
    def fl_annual(self) -> float:
        return self.fl * HOURS_PER_YEAR

    def to_row(self) -> dict:
        def r(x: float | None) -> float | None:
            return None if x is None or not math.isfinite(x) else float(f"{x:.6g}")

        return {
            "asset": self.asset,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "fl": r(self.fl),
            "fl_n": self.fl_n,
            "fl_short": r(self.fl_short),
            "current_rel": r(self.current_rel),
            "pred_rel": r(self.pred_rel),
            "rank": self.rank,
            "pct": r(self.pct),
            "persistence": r(self.persistence),
            "mid": r(self.mid),
            "mark": r(self.mark),
            "spread_full": r(self.spread_full),
            "top_book_usd": r(self.top_book_usd),
            "depth_usd": r(self.depth_usd),
            "volume_usd": r(self.volume_usd),
            "quote_age_s": r(self.quote_age_s),
            "side_cost": r(self.side_cost),
        }


def compute_signals(
    snap: MarketSnapshot | None,
    store: FundingStore,
    now: pd.Timestamp,
    cfg: CarryConfig,
    leg_notional: float,
    fee_rate: float,
) -> dict[str, AssetSignal]:
    """Signals for every universe asset (ineligible ones carry their reasons)."""
    g = cfg.gates
    age = snap.age_s(now) if snap is not None else math.inf
    out: dict[str, AssetSignal] = {}
    for a in cfg.universe:
        reasons: list[str] = []
        q = snap.quotes.get(a) if snap is not None else None
        if q is None:
            reasons.append("no_quote")
        elif not q.valid:
            reasons.append("bad_quote")
        else:
            if not age <= g.max_quote_age_s:
                reasons.append("stale_quote")
            if not q.spread_full <= g.max_spread_bps * BPS:
                reasons.append("wide_spread")
            if not q.volume_usd >= g.min_quote_volume_usd:
                reasons.append("low_volume")
            depth = snap.depth_usd(a, cfg.order_band, g.max_book_age_s, now)
            if not depth >= g.min_depth_mult * leg_notional:
                reasons.append("thin_book")
        fl, n = store.trailing(a, now, cfg.signal_hours)
        if not n >= g.min_funding_coverage * cfg.signal_hours:
            reasons.append("funding_coverage")
        newest = store.newest_end(a)
        if newest is None or (now - newest) > pd.Timedelta(hours=g.max_funding_age_hours):
            reasons.append("funding_stale")
        fl7, _ = store.trailing(a, now, cfg.short_signal_hours)
        recent = store.window(a, now, cfg.persistence_hours)
        sign = np.sign(fl) if math.isfinite(fl) else 0.0
        persistence = float(np.mean(np.sign(recent) == sign)) if len(recent) and sign != 0 else NAN
        half = q.half_spread if q is not None and q.valid else NAN
        out[a] = AssetSignal(
            asset=a,
            eligible=not reasons,
            reasons=tuple(reasons),
            fl=fl,
            fl_n=n,
            fl_short=fl7,
            current_rel=q.funding_rel if q is not None else NAN,
            pred_rel=q.pred_rel if q is not None else NAN,
            rank=None,
            pct=None,
            persistence=persistence,
            mid=q.mid if q is not None else NAN,
            mark=q.mark if q is not None else NAN,
            spread_full=q.spread_full if q is not None else NAN,
            half_spread=half,
            top_book_usd=q.top_book_usd if q is not None else NAN,
            depth_usd=snap.depth_usd(a, cfg.order_band, g.max_book_age_s, now) if q is not None else NAN,
            volume_usd=q.volume_usd if q is not None else NAN,
            quote_age_s=age,
            side_cost=fee_rate + half + cfg.slippage_bps * BPS,
        )
    elig = sorted((a for a, s in out.items() if s.eligible), key=lambda a: (-out[a].fl, a))
    m = len(elig)
    for i, a in enumerate(elig):
        pct = 1.0 if m == 1 else (m - 1 - i) / (m - 1)
        out[a] = _with_rank(out[a], i + 1, pct)
    return out


def _with_rank(s: AssetSignal, rank: int, pct: float) -> AssetSignal:
    from dataclasses import replace

    return replace(s, rank=rank, pct=pct)
