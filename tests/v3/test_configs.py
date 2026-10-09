"""Research V3 configuration grid and trial counts (spec §3, §6)."""

from luna.v2.walkforward import count_trials
from luna.v3.configs import V3Config, v3_configs


def test_trial_counts_match_spec():
    perp, spot = count_trials(v3_configs("perp")), count_trials(v3_configs("spot"))
    assert (perp["configs"], perp["grid_points"]) == (4, 96)
    assert (spot["configs"], spot["grid_points"]) == (6, 28)


def test_families_per_account():
    assert sorted({c.signal for c in v3_configs("perp")}) == ["lowvol", "premium"]
    assert sorted({c.signal for c in v3_configs("spot")}) == ["lowvol", "premium", "volmanaged"]


def test_ids_are_prefixed_and_never_collide_with_v2():
    from luna.v2.configs import primary_configs, sensitivity_configs

    for acct in ("perp", "spot"):
        v3 = {c.id for c in v3_configs(acct)}
        v2 = {c.id for c in primary_configs(acct) + sensitivity_configs(acct)}
        assert not v3 & v2 and all("|v3_" in i for i in v3)


def test_configs_run_on_the_weekly_xsec_engine():
    for c in v3_configs("perp") + v3_configs("spot"):
        assert c.engine == "xsec" and c.rebalance == "weekly" and c.timeframe == "1d"
        assert c.strategy_name == f"v3_{c.signal}"


def test_premium_and_lowvol_grids_reuse_the_v2_xsec_grid():
    from luna.v2.params import xsec_grid

    c = V3Config("perp", "xsec", "1d", "broad", signal="lowvol")
    assert c.grid() == [{**g, "signal": "lowvol"} for g in xsec_grid("perp")]


def test_volmanaged_grid_is_all_assets_with_a_vol_target():
    vm = V3Config("spot", "xsec", "1d", "broad", signal="volmanaged")
    assert vm.grid() == [
        {"lookback_days": 30, "k": 99, "vol_target": 0.10, "signal": "momentum"},
        {"lookback_days": 30, "k": 99, "vol_target": 0.15, "signal": "momentum"},
    ]
