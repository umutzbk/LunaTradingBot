"""Development-period inputs (spec §7: dev = mechanical/invariant use only, S-GUARD-2) from the
frozen V5 input copy, verified against its pinned manifest sha256 before any read. Every row is
before 2026-07-01 (checked again here)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from luna.carry import frozen as v5frozen
from luna.v6 import guard
from luna.v6.data import BarPanel, FundingPanel
from luna.v6.spec import constants

V5_MANIFEST_SHA = "eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14"
UNIVERSE = constants()["common"]["universe"]
FIRST = pd.Timestamp("2025-07-01T00:00Z")  # >= 120 days before the first decision: vol/cov history


@dataclass
class Inputs:
    scope: str
    mark: BarPanel
    trade: BarPanel
    volume_usd: BarPanel
    spot: BarPanel
    spot_volume_usd: BarPanel
    index: BarPanel
    funding: FundingPanel
    spreads: dict
    insts: dict
    prices: pd.DataFrame  # hourly mark mids for the risk model


def _frame(root: Path, sub: str, col: str, lo: pd.Timestamp, hi: pd.Timestamp, assets: list) -> pd.DataFrame:
    out = {}
    for a in assets:
        d = pd.read_parquet(root / sub / f"{a}.parquet")
        d.index = d.index.as_unit("ns")
        d = d[(d.index >= lo) & (d.index < hi)]
        out[a] = d[col] if col != "__usd__" else d["volume"] * d["close"]
        if col == "close" and "filled" in d.columns and sub.endswith("kraken_futures"):
            out[a] = d["close"].where(~d["filled"].astype(bool))
    idx = pd.date_range(lo, hi - pd.Timedelta("15min"), freq="15min").as_unit("ns")
    return pd.DataFrame(out).reindex(idx)


def hourly_prices(mark: BarPanel) -> pd.DataFrame:
    """P_h = close of the bar that opened at h - 15m (S-H3-6)."""
    c = mark.close.copy()
    c.index = c.index + pd.Timedelta("15min")
    return c[c.index.minute == 0]


def load_dev(root: Path, assets: list | None = None) -> Inputs:
    root = Path(root)
    v5frozen.verify_frozen(root, V5_MANIFEST_SHA)
    assets = list(assets or UNIVERSE)
    lo, hi = FIRST, guard.HOLDOUT_START
    mark = BarPanel(_frame(root, "history_v4/mark", "close", lo, hi, assets))
    trade = BarPanel(_frame(root, "bars/kraken_futures", "close", lo, hi, assets))
    vol = BarPanel(_frame(root, "bars/kraken_futures", "__usd__", lo, hi, assets))
    spot = BarPanel(_frame(root, "bars/kraken_spot", "close", lo, hi, assets))
    svol = BarPanel(_frame(root, "bars/kraken_spot", "__usd__", lo, hi, assets))
    index = BarPanel(_frame(root, "history_v4/index", "close", lo, hi, assets))
    rel, ab = {}, {}
    for a in assets:
        f = pd.read_parquet(root / "history_v4" / "funding" / f"{a}.parquet")
        f.index = f.index.as_unit("ns")
        rel[a], ab[a] = f["funding_rel"], f["funding_abs"]
    R, A = pd.DataFrame(rel).sort_index(), pd.DataFrame(ab).sort_index()
    funding = FundingPanel(R, abs_=A)
    spreads = {}
    for a in assets:
        s = pd.read_parquet(root / "history_v5" / "spreads" / f"{a}.parquet")["spread_full"]
        s.index = s.index.as_unit("ns")
        spreads[a] = s
    raw = json.loads((root / "config" / "v4_instruments.json").read_text(encoding="utf-8"))["assets"]
    insts = {a: {"perp": raw[a]["perp"], "spot": raw[a]["spot"], "step": raw[a]["perp"]["step"],
                 "min_qty": raw[a]["perp"]["min_qty"]} for a in assets}
    last = max(mark.close.index.max(), spot.close.index.max(), R.index.max(), max(s.index.max() for s in spreads.values()))
    guard.require_window(lo, last, "dev")
    return Inputs("dev", mark, trade, vol, spot, svol, index, funding, spreads, insts, hourly_prices(mark))


def p90_fallback(spreads: dict, start: pd.Timestamp, days: int = 35, q: float = 0.90) -> dict:
    """S-FB-CONS: per asset the p90 of the valid snapshots in the 35 days before ``start``."""
    out = {}
    lo = pd.Timestamp(start) - pd.Timedelta(days=days)
    for a, s in spreads.items():
        v = s[(s.index >= lo) & (s.index < start)].to_numpy(float)
        v = v[np.isfinite(v) & (v > 0) & (v <= 0.05)]
        if len(v):
            out[a] = float(np.quantile(v, q))
    return out
