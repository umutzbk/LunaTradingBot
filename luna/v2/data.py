"""Holdout isolation: every V2 loader stops at V2_DATA_END and verifies it."""

from __future__ import annotations

import pandas as pd

from luna.config import Settings
from luna.data.store import BarStore
from luna.engine.walkforward import Window, make_windows
from luna.instruments import Instrument
from luna.report.walkforward import Inputs, load_inputs
from luna.types import AccountKind

V2_DATA_END = pd.Timestamp("2026-07-01T00:00:00Z")
_BAR = pd.Timedelta(minutes=15)


class HoldoutLeak(RuntimeError):
    """Data at or after the V2 cutoff reached a V2 consumer."""


def assert_before_cutoff(
    panel: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame],
    cutoff: pd.Timestamp = V2_DATA_END,
) -> None:
    """Bars must close by ``cutoff`` (open <= cutoff - 15m); funding must be stamped before it."""
    for a, df in panel.items():
        if len(df) and df.index.max() > cutoff - _BAR:
            raise HoldoutLeak(f"{a}: bar {df.index.max()} closes after {cutoff}")
    for a, df in funding.items():
        if len(df) and df.index.max() >= cutoff:
            raise HoldoutLeak(f"{a}: funding {df.index.max()} at or after {cutoff}")


def v2_settings(settings: Settings) -> Settings:
    wf = settings.walkforward.model_copy(update={"holdout_months": 0})
    return settings.model_copy(update={"walkforward": wf})


def load_v2_inputs(
    settings: Settings, store: BarStore, account: AccountKind,
    instruments: dict[str, Instrument], *, assets: list[str] | None = None,
) -> Inputs:
    inp = load_inputs(settings, store, account, instruments, data_end=V2_DATA_END, assets=assets)
    assert_before_cutoff(inp.panel, inp.funding)
    if inp.universe_panel is not None:
        assert_before_cutoff(inp.universe_panel, {})
    return inp


def v2_windows(settings: Settings) -> list[Window]:
    s = v2_settings(settings)
    windows, _ = make_windows(settings.universe.history_start, V2_DATA_END, s.walkforward)
    if windows and windows[-1].test_end > V2_DATA_END:
        raise HoldoutLeak("last test window ends after the V2 cutoff")
    return windows
