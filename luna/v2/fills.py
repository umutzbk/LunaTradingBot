"""Maker execution diagnostics (spec §6): post-only fill rate and missed-fill opportunity cost.

A missed fill is a candidate whose post-only entry never filled (status ``unfilled``). Its
opportunity cost is the counterfactual net R of the same entry taken as a taker: the candidate's own
strategy instance (the window model active at its bar) replayed alone with the entry forced at the
candidate's bar, as ``luna.labeling`` replays candidates, but with ``order_model="taker"``.
"""

from __future__ import annotations

import dataclasses
import math

import pandas as pd

from luna.config import Settings
from luna.engine.backtest import BacktestResult
from luna.engine.walkforward import WindowModel
from luna.execution.costs import CostModel
from luna.research.tradelists import ResearchMarket, build_tradelist
from luna.types import AccountKind

MISSED_COLUMNS = ["ts", "strategy", "key", "direction", "r_net_taker", "exit_reason"]


def maker_fill_rate(candidates: pd.DataFrame) -> float:
    """(filled + partial) / (filled + partial + unfilled) over resolved submitted entries."""
    st = candidates["status"] if "status" in candidates else pd.Series(dtype=object)
    filled = int(st.isin(["filled", "partial"]).sum())
    total = filled + int((st == "unfilled").sum())
    return filled / total if total else math.nan


def _model_at(models: list[WindowModel], ts: pd.Timestamp) -> WindowModel | None:
    for m in models:
        if m.window.test_start <= ts < m.window.test_end:
            return m
    return None


def missed_fills(
    res: BacktestResult,
    models: list[WindowModel],
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    settings: Settings,
    kind: AccountKind,
) -> pd.DataFrame:
    """One row per ``unfilled`` candidate of ``res`` (MISSED_COLUMNS); ``r_net_taker`` NaN when the
    taker replay produced no closed trade (exit_reason ``no_trade``) or no model owns the bar
    (``no_model``)."""
    cands = res.candidates
    if cands is None or cands.empty or "status" not in cands:
        return pd.DataFrame(columns=MISSED_COLUMNS)
    miss = cands[cands["status"] == "unfilled"]
    if miss.empty:
        return pd.DataFrame(columns=MISSED_COLUMNS)
    costs = CostModel(settings.costs[kind])
    band = settings.accounts[kind].max_price_band
    market = ResearchMarket(panel, funding, kind)
    frames: dict[tuple[int, str], tuple[object, object]] = {}
    rows = []
    for c in miss.itertuples(index=False):
        ts = pd.Timestamp(c.ts)
        row = {"ts": ts, "strategy": c.strategy, "key": c.key, "direction": int(c.direction),
               "r_net_taker": math.nan, "exit_reason": "no_model"}
        m = _model_at(models, ts)
        strat = next((s for s in (m.strategies if m else []) if s.name == c.strategy), None)
        if strat is not None:
            ck = (id(strat), c.key)
            if ck not in frames:
                frames[ck] = (strat, strat.signals(panel, [c.key])[c.key])
            _, sf = frames[ck]
            entry = pd.Series(0, index=sf.entry.index, dtype="int8")
            if ts in entry.index:
                entry.loc[ts] = int(c.direction)
            tl = build_tradelist(dataclasses.replace(sf, entry=entry), panel, funding, costs, kind,
                                 strategy=strat, band=band, market=market, order_model="taker")
            row["exit_reason"] = "no_trade"
            if len(tl):
                row["r_net_taker"] = float(tl.iloc[0]["r_net"])
                row["exit_reason"] = str(tl.iloc[0]["exit_reason"])
        rows.append(row)
    return pd.DataFrame(rows, columns=MISSED_COLUMNS)
