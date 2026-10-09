from __future__ import annotations

import pytest
from pydantic import ValidationError

from luna.carry.config import CarryConfig, load_carry_config
from tests.carry.helpers import carry_config


def test_default_yaml_has_a_valid_carry_section() -> None:
    cfg = load_carry_config()
    assert cfg.account == "carry"
    assert cfg.k == 2 and cfg.gross == 1.0
    assert cfg.signal_hours == 720  # FL30 = trailing 30-day realized funding mean
    assert cfg.risk.live_enabled is False
    assert "MUBARAK" not in cfg.universe and len(cfg.universe) == 20


def test_leg_fraction_is_half_gross_over_k() -> None:
    cfg = carry_config(k=2, gross=1.0)
    assert cfg.leg_frac == pytest.approx(0.25)
    assert carry_config(k=4, gross=1.0, risk__max_positions=8).leg_frac == pytest.approx(0.125)


def test_live_enabled_is_refused() -> None:
    base = carry_config().model_dump()
    base["risk"]["live_enabled"] = True
    with pytest.raises(ValidationError, match="paper-only"):
        CarryConfig(**base)


def test_order_band_must_fit_the_risk_price_band() -> None:
    base = carry_config().model_dump()
    base["order_band"] = base["risk"]["max_price_band"] * 2
    with pytest.raises(ValidationError, match="order_band"):
        CarryConfig(**base)


def test_risk_profile_must_admit_the_target_book() -> None:
    base = carry_config(k=2).model_dump()
    base["risk"]["max_positions"] = 3  # 2k = 4 legs
    with pytest.raises(ValidationError, match="max_positions"):
        CarryConfig(**base)
    base = carry_config(k=2).model_dump()
    base["risk"]["max_risk_per_trade"] = 0.01  # leg stop 15% x leg 25% = 3.75% of equity
    with pytest.raises(ValidationError, match="max_risk_per_trade"):
        CarryConfig(**base)


def test_universe_must_hold_two_k_assets() -> None:
    base = carry_config(k=2).model_dump()
    base["universe"] = ["BTC", "ETH", "SOL"]
    with pytest.raises(ValidationError, match="universe"):
        CarryConfig(**base)


def test_exit_spread_limit_must_be_crossable_inside_the_order_band() -> None:
    base = carry_config().model_dump()
    base["gates"]["exit_spread_bps"] = 60.0  # order band 25 bps -> an IOC crosses at most 50 bps
    with pytest.raises(ValidationError, match="exit_spread_bps"):
        CarryConfig(**base)


def test_env_overrides_reach_the_carry_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUNA_CARRY__SWITCH_HORIZON_HOURS", "72")
    monkeypatch.setenv("LUNA_CARRY__EVALUATION__TRIGGER", "weekly")
    cfg = load_carry_config()
    assert cfg.switch_horizon_hours == 72.0 and cfg.evaluation.trigger == "weekly"
    assert cfg.k == 2  # untouched keys keep their YAML values
