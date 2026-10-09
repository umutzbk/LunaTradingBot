"""Research V3 configuration grid (spec §3, §6): account x family x universe, weekly rebalance on the
V2 cross-sectional engine. Grids are pre-committed: premium / lowvol reuse the V2 cross-sectional grid
(L in {14, 30, 90} days, k in {1, 2}; perp adds gross {1.0, 1.5} x vol target {10%, 15%}); the
vol-managed spot book holds every eligible asset (k = 99) at vol target {10%, 15%}."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from luna.types import AccountKind
from luna.v2.configs import UNIVERSE_MODES, V2Config
from luna.v2.params import xsec_grid

Signal = Literal["premium", "lowvol", "volmanaged"]
FAMILIES: dict[AccountKind, tuple[Signal, ...]] = {
    "perp": ("premium", "lowvol"),
    "spot": ("premium", "lowvol", "volmanaged"),
}
VOLMANAGED_TARGETS = (0.10, 0.15)
ALL_ASSETS_K = 99


@dataclass(frozen=True)
class V3Config(V2Config):
    signal: Signal = "premium"

    @property
    def id(self) -> str:
        return f"{self.account}|v3_{self.signal}|{self.timeframe}|{self.universe_mode}"

    @property
    def strategy_name(self) -> str:
        return f"v3_{self.signal}"

    @property
    def engine(self) -> Literal["signal", "xsec"]:
        return "xsec"

    def grid(self) -> list[dict]:
        if self.signal == "volmanaged":
            return [
                {"lookback_days": 30, "k": ALL_ASSETS_K, "vol_target": v, "signal": "momentum"}
                for v in VOLMANAGED_TARGETS
            ]
        return [{**g, "signal": self.signal} for g in xsec_grid(self.account)]


def v3_configs(account: AccountKind) -> list[V3Config]:
    return [
        V3Config(account, "xsec", "1d", mode, "weekly", signal)
        for signal in FAMILIES[account]
        for mode in UNIVERSE_MODES
    ]
