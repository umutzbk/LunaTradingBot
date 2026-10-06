"""Settings: YAML defaults overlaid by LUNA_* environment variables and .env."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from luna.types import AccountKind

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.yaml"

_ENV_CONFIG = SettingsConfigDict(
    env_prefix="LUNA_", env_nested_delimiter="__", env_file=".env", extra="ignore"
)


class RiskLimits(BaseModel):
    capital_usd: float
    risk_per_trade: float
    max_risk_per_trade: float
    max_open_risk: float
    max_positions: int
    max_gross_leverage: float
    max_leg_notional_frac: float
    daily_loss_limit: float
    max_drawdown: float
    max_orders_per_day: int
    max_price_band: float
    emergency_stop_frac: float
    liq_distance_mult: float
    allow_short: bool
    live_enabled: bool
    highvol_risk_mult: float
    corr_bucket_threshold: float


class CostConfig(BaseModel):
    maker_fee: float
    taker_fee: float
    half_spread_bps: dict[str, float]  # has key "default"
    slippage_bps: float


class UniverseConfig(BaseModel):
    min_adv_usd: float
    min_history_days: int
    prefilter_volume_usd: float
    history_start: str


class WalkForwardConfig(BaseModel):
    train_months: int
    test_months: int
    step_months: int
    holdout_months: int


class RankingConfig(BaseModel):
    n0: int
    n_min: int
    bootstrap_samples: int
    ci: float


class PairsConfig(BaseModel):
    corr_min: float
    fdr_q: float
    stability_p: float
    half_life_min: float
    half_life_max: float
    beta_change_max: float
    max_pairs: int
    always_watch: list[str]


class Settings(BaseSettings):
    model_config = _ENV_CONFIG

    var_dir: Path = Path("var")
    accounts: dict[AccountKind, RiskLimits]
    costs: dict[AccountKind, CostConfig]
    universe: UniverseConfig
    walkforward: WalkForwardConfig
    ranking: RankingConfig
    pairs: PairsConfig
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    healthcheck_url: str | None = None
    gbp_usd: float


class _EnvOverrides(BaseSettings):
    """Same env/.env sources as Settings, but every field optional (overlay only)."""

    model_config = _ENV_CONFIG

    var_dir: Path | None = None
    accounts: dict[str, dict[str, Any]] | None = None
    costs: dict[str, dict[str, Any]] | None = None
    universe: dict[str, Any] | None = None
    walkforward: dict[str, Any] | None = None
    ranking: dict[str, Any] | None = None
    pairs: dict[str, Any] | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    healthcheck_url: str | None = None
    gbp_usd: float | None = None


def _deep_update(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load_settings(path: Path | None = None) -> Settings:
    """YAML defaults + env overrides (LUNA_ prefix, nested delimiter `__`, `.env`)."""
    data = yaml.safe_load(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8")) or {}
    overrides = _EnvOverrides().model_dump(exclude_unset=True)
    return Settings(**_deep_update(data, overrides))
