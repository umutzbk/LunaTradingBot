from __future__ import annotations

import pytest

from luna.config import Settings, load_settings
from tests.helpers import synthetic_bars

_DAY = 96
_SEEDS = {"BTC": 1, "ETH": 2, "AAA": 3, "BBB": 4, "CCC": 5, "DDD": 6}


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def v2_panel():
    panel = {}
    for a, seed in _SEEDS.items():
        df = synthetic_bars(240 * _DAY, "random_walk", seed=seed)
        if a == "DDD":
            df = df.iloc[60 * _DAY:]
        df = df.copy()
        for i in (500, 501, 502, 503, 1200):  # a few filled bars
            if i < len(df):
                df.iloc[i, df.columns.get_loc("filled")] = True
        panel[a] = df
    return panel


@pytest.fixture
def v2_settings_small(settings: Settings) -> Settings:
    wf = settings.walkforward.model_copy(
        update={"train_months": 2, "test_months": 1, "step_months": 1, "holdout_months": 0})
    uni = settings.universe.model_copy(update={"min_history_days": 20, "min_adv_usd": 0})
    return settings.model_copy(update={"walkforward": wf, "universe": uni})
