"""The V2 configuration grid (spec §2, plan P1-P5/P12): account x family x timeframe x universe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from luna.types import AccountKind
from luna.v2.params import FAMILY_TIMEFRAMES, breakout_grid, tsmom_grid, xsec_grid
from luna.v2.timeframe import Timeframe

Family = Literal["tsmom", "breakout", "xsec"]
UniverseMode = Literal["btc_eth", "broad"]
UNIVERSE_MODES: tuple[UniverseMode, ...] = ("btc_eth", "broad")
BTC_ETH = ("BTC", "ETH")


@dataclass(frozen=True)
class V2Config:
    account: AccountKind
    family: Family
    timeframe: Timeframe
    universe_mode: UniverseMode
    rebalance: Literal["weekly", "daily"] = "weekly"

    @property
    def id(self) -> str:
        base = f"{self.account}|{self.family}|{self.timeframe}|{self.universe_mode}"
        return base + ("|daily" if self.rebalance == "daily" else "")

    @property
    def strategy_name(self) -> str:
        """Strategy / trade-list name: ``tsmom_1h``, ``breakout_4h``, ``xsec`` (+ ``_daily``)."""
        if self.family == "xsec":
            return "xsec_daily" if self.rebalance == "daily" else "xsec"
        return f"{self.family}_{self.timeframe}"

    @property
    def engine(self) -> Literal["signal", "xsec"]:
        return "xsec" if self.family == "xsec" else "signal"

    def grid(self) -> list[dict]:
        if self.family == "tsmom":
            return tsmom_grid(self.timeframe)
        if self.family == "breakout":
            return breakout_grid(self.timeframe)
        return xsec_grid(self.account)


def primary_configs(account: AccountKind) -> list[V2Config]:
    """Weekly-rebalance grid: perp 18, spot 14 configurations."""
    out = []
    for family in ("tsmom", "breakout", "xsec"):
        for tf in FAMILY_TIMEFRAMES[account][family]:
            for mode in UNIVERSE_MODES:
                out.append(V2Config(account, family, tf, mode))
    return out


def sensitivity_configs(account: AccountKind) -> list[V2Config]:
    """Daily-rebalance cross-sectional sensitivity (D1: reported, never the candidate)."""
    return [V2Config(account, "xsec", "1d", mode, "daily") for mode in UNIVERSE_MODES]


def restrict_universe(universe: list[str], mode: UniverseMode) -> list[str]:
    return [a for a in universe if a in BTC_ETH] if mode == "btc_eth" else list(universe)
