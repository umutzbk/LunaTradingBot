"""Counterfactual labeling of candidates (spec §9): what net R would a candidate have made?

Every recorded candidate, selected or rejected, is replayed alone as the research trade lists do
(``luna.research.tradelists.build_tradelist`` over ``luna.research.kernel``): entry at the open of
the bar after the candidate's bar, exits decided at bar closes by the candidate's own strategy
rules and filled on the next bar, the account's costs, perp funding. ``outcome_r`` is the trade
list's ``r_net`` (net return over the signal bar's stop distance); ``exit_reason`` is the kernel's.

Strategy parameters are not stored on candidate rows. The exit rules depend only on what the row
does store: the exit spec (``max_hold_bars``, ``take_profit_z``, ``trailing_atr_mult``) and the stop
distance. The strategy is therefore rebuilt as the grid point of its class whose exit spec equals the
stored one and whose stop distance at the candidate bar matches the stored ``distance_frac`` (entry
parameters such as ``z_in`` or the Donchian length are irrelevant to the replay: the entry is forced
at the candidate's bar). A candidate that matches no grid point is left unlabeled.

A candidate is labeled once its trade has closed within the bars closed at ``now`` (this can be
earlier than ``ts + (max_hold + 1)`` bars). Candidates whose entry never fills, that cannot trade
(a short on spot) or that have no data are left unlabeled and retried on the next run.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from luna.config import load_settings
from luna.data.store import BarStore, fill_missing
from luna.engine.walkforward import STRATEGIES
from luna.execution.costs import CostModel
from luna.research.tradelists import ResearchMarket, build_tradelist
from luna.store import models as m
from luna.store.db import Db
from luna.strategy.base import SignalFrame
from luna.types import AccountKind, ExitSpec

log = logging.getLogger(__name__)

BAR = pd.Timedelta("15min")
LOOKBACK_BARS = 96 * 120  # indicator warm-up, as the live engine's feed buffer
_VENUE = {"perp": "kraken_futures", "spot": "kraken_spot"}
_DIST_RTOL = 1e-3


def _unlabeled(db: Db, account: AccountKind) -> list[dict[str, Any]]:
    c, o = m.candidates, m.candidate_outcomes
    stmt = (select(c).outerjoin(o, o.c.candidate_id == c.c.id)
            .where(c.c.account == account, o.c.id.is_(None)).order_by(c.c.id))
    with db.engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def _exit_spec(raw: dict[str, Any]) -> ExitSpec:
    return ExitSpec(int(raw["max_hold_bars"]), raw.get("take_profit_z"), raw.get("trailing_atr_mult"))


class _Labeler:
    def __init__(self, store: BarStore, costs: CostModel, account: AccountKind, now: pd.Timestamp,
                 band: float, start: pd.Timestamp) -> None:
        self.start = start
        self.store, self.costs, self.account, self.now, self.band = store, costs, account, now, band
        self._bars: dict[str, pd.DataFrame] = {}
        self._funding: dict[str, pd.DataFrame] = {}
        self._frames: dict[tuple[str, str, str], tuple[Any, SignalFrame]] = {}
        self._markets: dict[str, tuple[dict, dict, ResearchMarket]] = {}

    def _asset_bars(self, asset: str) -> pd.DataFrame:
        if asset not in self._bars:
            end = self.now - BAR  # bars whose close <= now
            raw = self.store.read(_VENUE[self.account], asset, self.start, end)
            self._bars[asset] = fill_missing(raw) if len(raw) else raw
        return self._bars[asset]

    def _market(self, key: str) -> tuple[dict, dict, ResearchMarket] | None:
        if key not in self._markets:
            assets = key.split("/")
            panel = {a: self._asset_bars(a) for a in assets}
            if any(df.empty for df in panel.values()):
                self._markets[key] = None  # type: ignore[assignment]
            else:
                funding = {}
                if self.account == "perp":
                    for a in assets:
                        if a not in self._funding:
                            self._funding[a] = self.store.read_funding(a)
                        funding[a] = self._funding[a]
                self._markets[key] = (panel, funding, ResearchMarket(panel, funding, self.account))
        return self._markets[key]

    def _strategy(self, row: dict[str, Any], panel: dict, spec: ExitSpec, ts: pd.Timestamp):
        """(strategy instance, SignalFrame) of the grid point matching the stored exit and stop."""
        cls = STRATEGIES.get(row["strategy"])
        if cls is None:
            return None
        dist = json.loads(row["stop"])["distance_frac"]
        for params in cls.GRID:
            ck = (row["strategy"], json.dumps(params, sort_keys=True), row["key"])
            if ck not in self._frames:
                strat = cls(dict(params), use_regime=False)
                self._frames[ck] = (strat, strat.signals(panel, [row["key"]])[row["key"]])
            strat, sf = self._frames[ck]
            if sf.exit_spec != spec or ts not in sf.stop_distance.index:
                continue
            d = float(sf.stop_distance.loc[ts])
            if np.isfinite(d) and abs(d - dist) <= _DIST_RTOL * abs(dist):
                return strat, sf
        return None

    def label(self, row: dict[str, Any]) -> tuple[float, str] | None:
        ts = m.ts_from_str(row["ts"])
        mk = self._market(row["key"])
        if mk is None or row["stop"] is None or row["exit"] is None:
            return None
        panel, funding, market = mk
        found = self._strategy(row, panel, _exit_spec(json.loads(row["exit"])), ts)
        if found is None:
            log.warning("candidate %s: no grid point of %s matches its exit/stop; unlabeled",
                        row["id"], row["strategy"])
            return None
        strat, sf = found
        entry = pd.Series(0, index=sf.entry.index, dtype="int8")
        entry.loc[ts] = int(row["direction"])
        tl = build_tradelist(dataclasses.replace(sf, entry=entry), panel, funding, self.costs,
                             self.account, strategy=strat, band=self.band, market=market)
        if tl.empty:
            return None
        t = tl.iloc[0]
        return float(t["r_net"]), str(t["exit_reason"])


def label_candidates(db: Db, store: BarStore, costs: CostModel, account: AccountKind,
                     now: pd.Timestamp, *, band: float | None = None) -> int:
    """Label every unlabeled candidate of ``account`` whose counterfactual trade has closed within
    the bars closed at ``now`` (module docstring); returns how many outcomes were written.
    ``band`` is the account's ``max_price_band`` (default: from the settings)."""
    now = pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    rows = _unlabeled(db, account)
    if not rows:
        return 0
    if band is None:
        band = load_settings().accounts[account].max_price_band
    first = min(m.ts_from_str(r["ts"]) for r in rows)
    lab = _Labeler(store, costs, account, now, band, first - LOOKBACK_BARS * BAR)
    n = 0
    for row in rows:
        res = lab.label(row)
        if res is None:
            continue
        try:
            db.record_outcome(int(row["id"]), res[0], res[1], labeled_at=now)
        except IntegrityError:  # labeled concurrently
            continue
        n += 1
    return n
