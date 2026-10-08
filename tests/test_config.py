import pandas as pd
from typer.testing import CliRunner

from luna.cli import app
from luna.config import load_settings


def test_default_settings_match_spec():
    s = load_settings()
    assert s.accounts["perp"].max_gross_leverage == 2.0 and s.accounts["spot"].max_gross_leverage == 1.0
    assert s.accounts["perp"].capital_usd == 265 and s.accounts["spot"].allow_short is False
    assert s.accounts["perp"].live_enabled is False
    assert s.ranking.n_min == 30 and s.pairs.fdr_q == 0.05
    assert s.accounts["spot"].max_leg_notional_frac == 0.5 and s.accounts["spot"].max_orders_per_day == 20
    assert s.costs["perp"].half_spread_bps["default"] == 3 and s.costs["spot"].taker_fee == 0.008
    assert s.pairs.always_watch == ["ETH/BTC"] and s.universe.min_history_days == 548
    assert s.var_dir.as_posix() == "var"


def test_env_override(monkeypatch):
    monkeypatch.setenv("LUNA_GBP_USD", "1.25")
    assert load_settings().gbp_usd == 1.25


def test_nested_env_override(monkeypatch):
    monkeypatch.setenv("LUNA_ACCOUNTS__PERP__CAPITAL_USD", "500")
    s = load_settings()
    assert s.accounts["perp"].capital_usd == 500 and s.accounts["perp"].max_positions == 3


def test_cli_help_lists_commands():
    out = CliRunner().invoke(app, ["--help"]).output
    for c in ["backfill", "data-report", "baseline", "walkforward", "holdout", "paper", "label"]:
        assert c in out


def test_backfill_options_are_passed_through(monkeypatch, tmp_path):
    from luna.data import backfill as bf

    seen = {}

    def fake_spot(client, store, settings, assets, start, end):
        seen.update(assets=assets, start=start, end=end)
        return ["BTC", "ETH"]

    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(bf, "backfill_spot", fake_spot)
    args = ["backfill", "--venue", "spot", "--assets", "BTC,ETH", "--start", "2024-01-01"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen["assets"] == "BTC,ETH" and seen["start"] == pd.Timestamp("2024-01-01", tz="UTC")
    assert seen["end"] == seen["end"].floor("15min")
