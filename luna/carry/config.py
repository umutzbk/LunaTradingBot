"""Configuration of the adaptive carry bot (``carry:`` section of config/default.yaml).

Every strategy knob lives here, none is hard-coded in the strategy. The defaults are initial
values (V4 H2-L30 economics, V1 risk invariants), not tuned on any backtest result.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator

from luna.config import RiskLimits, Settings, load_settings


class CarryGates(BaseModel):
    """Per-asset eligibility gates (signals.py)."""

    max_quote_age_s: float = 180.0  # newest ticker snapshot older than this: stale
    max_spread_bps: float = 15.0  # full quoted spread (ask - bid) / mid, for new legs
    exit_spread_bps: float = 40.0  # a held leg whose spread stays wider than this is illiquid;
    # < 2 x order_band, else the IOC exit (limit mid +/- order_band) could never cross the spread
    min_quote_volume_usd: float = 1_000_000.0  # 24h volumeQuote of the perp
    min_depth_mult: float = 1.0  # book depth within +/- order_band on the thinner side >= mult x leg
    max_book_age_s: float = 600.0  # collector fut_book older than this: depth = top of book only
    min_funding_coverage: float = 0.90  # known periods / signal_hours (V4 H2: 90%)
    max_funding_age_hours: float = 3.0  # newest ended funding period ended at most this long ago
    ineligible_exit_grace_s: float = 900.0  # a held asset ineligible this long is exited


class CarryEvaluation(BaseModel):
    """When strategy decisions may change the book. Monitoring (marks, funding, stops, logging)
    runs on every new snapshot regardless."""

    trigger: Literal["event", "interval", "daily", "weekly"] = "event"
    max_interval_s: float = 60.0  # evaluate at least this often, even without a new snapshot
    poll_s: float = 5.0  # how often the loop looks for a new collector snapshot
    interval_hours: float = 24.0  # trigger "interval"
    at_hour: int = 0  # trigger "daily" / "weekly": UTC hour
    weekday: int = 0  # trigger "weekly": 0 = Monday


class CarryConfig(BaseModel):
    account: str = "carry"  # Db account name -> var/luna_carry.db
    capital_usd: float = 265.0
    universe: list[str]
    k: int = 2  # legs per side
    gross: float = 1.0  # target gross exposure / equity; each leg gross / (2k)
    signal_hours: int = 720  # FL30: trailing mean of realized hourly relative funding
    short_signal_hours: int = 168  # FL7 (monitoring only)
    persistence_hours: int = 168  # funding persistence diagnostic window (monitoring only)
    decision_mode: Literal["cost_aware", "rebalance_to_target"] = "cost_aware"
    switch_horizon_hours: float = 168.0  # horizon of the expected carry advantage
    min_carry_improvement_bps: float = 5.0  # per unit leg notional, over the horizon
    switch_cost_multiplier: float = 1.5
    safety_buffer_bps: float = 5.0  # per leg traded, over the horizon
    min_hold_hours: float = 24.0  # a leg younger than this is never switched out economically
    hold_rank_buffer: int = 0  # a held leg ranked within k + buffer on its side is not replaced
    max_actions_per_cycle: int = 4
    leg_stop_frac: float = 0.15  # catastrophic stop: adverse mark move from the leg's entry
    stop_cooldown_hours: float = 24.0  # no new leg in an asset after its stop for this long
    max_net_frac: float = 0.10  # |sum signed notional| / equity above this: trim the heavier side
    slippage_bps: float = 2.0  # book-walk estimate per side in the switch cost (fills walk the book)
    order_band: float = 0.0025  # IOC limit = mid x (1 +/- band); <= risk.max_price_band
    funding_unknown_grace_hours: float = 3.0  # a period without a rate this long after it ended: unknown
    eval_retention_days: int = 30  # per-asset signal JSON kept this long (summaries forever)
    gates: CarryGates = CarryGates()
    evaluation: CarryEvaluation = CarryEvaluation()
    risk: RiskLimits

    @property
    def leg_frac(self) -> float:
        return self.gross / (2 * self.k)

    @model_validator(mode="after")
    def _consistent(self) -> CarryConfig:
        r = self.risk
        if r.live_enabled:
            raise ValueError("carry is paper-only: risk.live_enabled must be false")
        if self.k < 1:
            raise ValueError("k must be >= 1")
        if len(set(self.universe)) < 2 * self.k:
            raise ValueError(f"universe needs at least 2k = {2 * self.k} assets")
        if not 0 < self.order_band <= r.max_price_band:
            raise ValueError(f"order_band {self.order_band} must be in (0, risk.max_price_band]")
        if not self.gates.exit_spread_bps * 1e-4 < 2 * self.order_band:
            raise ValueError(
                f"gates.exit_spread_bps {self.gates.exit_spread_bps} must be < 2 x order_band "
                f"({2e4 * self.order_band:g} bps): an IOC exit could not cross that spread"
            )
        if r.max_positions < 2 * self.k:
            raise ValueError(f"risk.max_positions {r.max_positions} < 2k = {2 * self.k} legs")
        leg_risk = self.leg_stop_frac * self.leg_frac
        if leg_risk > r.max_risk_per_trade:
            raise ValueError(f"risk.max_risk_per_trade {r.max_risk_per_trade} < leg stop risk {leg_risk}")
        if 2 * self.k * leg_risk > r.max_open_risk:
            raise ValueError(f"risk.max_open_risk {r.max_open_risk} < book stop risk {2 * self.k * leg_risk}")
        if self.leg_frac > r.max_leg_notional_frac:
            raise ValueError(f"risk.max_leg_notional_frac {r.max_leg_notional_frac} < leg {self.leg_frac}")
        if self.gross > r.max_gross_leverage:
            raise ValueError(f"risk.max_gross_leverage {r.max_gross_leverage} < gross {self.gross}")
        if not 0 < self.leg_stop_frac < 1:
            raise ValueError("leg_stop_frac must be in (0, 1)")
        ev, g = self.evaluation, self.gates
        checks = {
            "switch_horizon_hours > 0": self.switch_horizon_hours > 0,
            "signal_hours > 0": self.signal_hours > 0 and self.short_signal_hours > 0,
            "min_hold_hours >= 0": self.min_hold_hours >= 0,
            "hold_rank_buffer >= 0": self.hold_rank_buffer >= 0,
            "max_actions_per_cycle >= 1": self.max_actions_per_cycle >= 1,
            "switch_cost_multiplier >= 0": self.switch_cost_multiplier >= 0,
            "gross > 0": self.gross > 0,
            "evaluation.poll_s > 0 and max_interval_s > 0": ev.poll_s > 0 and ev.max_interval_s > 0,
            "evaluation.interval_hours > 0": ev.interval_hours > 0,
            "evaluation.at_hour in 0..23": 0 <= ev.at_hour <= 23,
            "evaluation.weekday in 0..6": 0 <= ev.weekday <= 6,
            "gates.min_funding_coverage in (0, 1]": 0 < g.min_funding_coverage <= 1,
            # R16 (stale data) allows 30 min; the carry gate must be stricter to mean anything
            "gates.max_quote_age_s in (0, 1800]": 0 < g.max_quote_age_s <= 1800,
            "capital_usd == risk.capital_usd": self.capital_usd == r.capital_usd,
        }
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            raise ValueError("invalid carry config: " + "; ".join(bad))
        return self


def load_carry_config(settings: Settings | None = None) -> CarryConfig:
    """The validated ``carry:`` section (YAML + ``LUNA_CARRY__*`` env overrides)."""
    s = settings if settings is not None else load_settings()
    if not s.carry:
        raise ValueError("config has no carry section")
    return CarryConfig(**s.carry)
