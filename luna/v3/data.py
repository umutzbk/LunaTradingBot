"""Research V3 inputs (spec §3A, E1): the account's V2 inputs plus a 15m ``premium`` column,
perp close / spot close - 1, built from the other venue's bars. Both venues go through the V2
cutoff check (V3_DATA_END = V2_DATA_END = 2026-07-01); the premium is NaN wherever either bar is
missing or forward-filled (no trade), so no stale price ever enters the signal."""

from __future__ import annotations

import dataclasses

import pandas as pd

from luna.config import Settings
from luna.data.store import BarStore
from luna.instruments import Instrument
from luna.report.walkforward import Inputs
from luna.types import AccountKind
from luna.v2.data import V2_DATA_END, assert_before_cutoff, load_v2_inputs

V3_DATA_END = V2_DATA_END


def add_premium(
    panel: dict[str, pd.DataFrame], other: dict[str, pd.DataFrame], account: AccountKind
) -> dict[str, pd.DataFrame]:
    out = {}
    for a, own in panel.items():
        o = other.get(a)
        if o is None or o.empty:
            out[a] = own.assign(premium=float("nan"))
            continue
        oc = o["close"].reindex(own.index)
        of = o["filled"].reindex(own.index).astype("boolean").fillna(True).astype(bool)
        valid = ~own["filled"].astype(bool) & ~of & oc.notna()
        prem = own["close"] / oc - 1.0 if account == "perp" else oc / own["close"] - 1.0
        out[a] = own.assign(premium=prem.where(valid))
    return out


def with_premium(inp: Inputs, other_panel: dict[str, pd.DataFrame], account: AccountKind) -> Inputs:
    assert_before_cutoff(other_panel, {}, V3_DATA_END)
    return dataclasses.replace(inp, panel=add_premium(inp.panel, other_panel, account))


def load_v3_inputs(
    settings: Settings,
    store: BarStore,
    account: AccountKind,
    instruments: dict[str, Instrument],
    other_instruments: dict[str, Instrument],
    *,
    assets: list[str] | None = None,
) -> Inputs:
    inp = load_v2_inputs(settings, store, account, instruments, assets=assets)
    other_kind: AccountKind = "spot" if account == "perp" else "perp"
    other = load_v2_inputs(settings, store, other_kind, other_instruments, assets=sorted(inp.panel))
    return with_premium(inp, other.panel, account)
