"""Counterfactual labeling of candidates (spec §9): what net R would a candidate have made?

Every recorded candidate, selected or rejected, is replayed alone as the research trade lists do
(``luna.research.tradelists.build_tradelist`` over ``luna.research.kernel``): entry at the open of
the bar after the candidate's bar, exits decided at bar closes by the candidate's own strategy
rules and filled on the next bar, the account's costs, perp funding. ``outcome_r`` is the trade
list's ``r_net`` (net return over the signal bar's stop distance); ``exit_reason`` is the kernel's.

Strategy parameters: candidates recorded by the live engine carry the owning strategy instance's
``params`` (JSON column), which the replay uses. Older rows have none; their exit rules depend only
on what the row stores (exit spec and stop distance), so the strategy is rebuilt from every grid
point whose exit spec equals the stored one and whose stop distance at the candidate bar matches the
stored ``distance_frac`` (entry parameters such as ``z_in`` are irrelevant: the entry is forced at
the candidate's bar). If several grid points match but replay to different outcomes the candidate
is ``ambiguous_params``.

A candidate is labeled once its trade has closed within the bars closed at ``now`` (this can be
earlier than ``ts + (max_hold + 1)`` bars).

Candidates that can never be labeled get a TERMINAL outcome (``outcome_r`` NULL, ``exit_reason``
one of ``TERMINAL_REASONS``) so they are not retried and do not anchor the data window:
``unknown_strategy``, ``pairs_on_spot``, ``untradeable_short`` (short leg on spot) and
``no_grid_match`` are decided from the row alone; ``entry_unfilled`` once at least
``UNFILLED_AFTER_BARS`` bars past the candidate are available and a probe replay (time exit after
one bar) shows the entry never opened; ``ambiguous_params`` as above. A candidate whose entry
opened but whose trade has not closed yet, or without bar data, stays pending.
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
UNFILLED_AFTER_BARS = 96
TERMINAL_REASONS = ("untradeable_short", "pairs_on_spot", "entry_unfilled", "unknown_strategy",
                    "no_grid_match", "ambiguous_params")
_VENUE = {"perp": "kraken_futures", "spot": "kraken_spot"}
_DIST_RTOL = 1e-3

_Market = tuple[dict, dict, ResearchMarket]


def _unlabeled(db: Db, account: AccountKind) -> list[dict[str, Any]]:
    c, o = m.candidates, m.candidate_outcomes
    stmt = (select(c).outerjoin(o, o.c.candidate_id == c.c.id)
            .where(c.c.account == account, o.c.id.is_(None)).order_by(c.c.id))
    with db.engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def _exit_spec(raw: dict[str, Any]) -> ExitSpec:
    return ExitSpec(int(raw["max_hold_bars"]), raw.get("take_profit_z"), raw.get("trailing_atr_mult"))


def _static_terminal(row: dict[str, Any], account: AccountKind) -> str | None:
    """Terminal reason decidable from the row alone (no data), else None."""
    if row["strategy"] not in STRATEGIES:
        return "unknown_strategy"
    if account == "spot":
        if "/" in row["key"]:
            return "pairs_on_spot"
        legs = json.loads(row["legs"]) if row["legs"] else []
        d = int(row["direction"])
        weights = [float(leg["weight"] if isinstance(leg, dict) else leg[1]) for leg in legs]
        if d < 0 or any(w * d < 0 for w in weights):
            return "untradeable_short"
    if row["stop"] is None or row["exit"] is None:
        return "no_grid_match"
    return None


class _Labeler:
    def __init__(self, store: BarStore, costs: CostModel, account: AccountKind, now: pd.Timestamp,
                 band: float, start: pd.Timestamp) -> None:
        self.start = start
        self.store, self.costs, self.account, self.now, self.band = store, costs, account, now, band
        self._bars: dict[str, pd.DataFrame] = {}
        self._funding: dict[str, pd.DataFrame] = {}
        self._frames: dict[tuple[str, str, str], tuple[Any, SignalFrame]] = {}
        self._markets: dict[str, _Market | None] = {}

    def _asset_bars(self, asset: str) -> pd.DataFrame:
        if asset not in self._bars:
            end = self.now - BAR  # bars whose close <= now
            raw = self.store.read(_VENUE[self.account], asset, self.start, end)
            self._bars[asset] = fill_missing(raw) if len(raw) else raw
        return self._bars[asset]

    def _market(self, key: str) -> _Market | None:
        if key not in self._markets:
            assets = key.split("/")
            panel = {a: self._asset_bars(a) for a in assets}
            if any(df.empty for df in panel.values()):
                self._markets[key] = None
            else:
                funding = {}
                if self.account == "perp":
                    for a in assets:
                        if a not in self._funding:
                            self._funding[a] = self.store.read_funding(a)
                        funding[a] = self._funding[a]
                self._markets[key] = (panel, funding, ResearchMarket(panel, funding, self.account))
        return self._markets[key]

    def _frame(self, name: str, params: dict, key: str, panel: dict) -> tuple[Any, SignalFrame]:
        ck = (name, json.dumps(params, sort_keys=True), key)
        if ck not in self._frames:
            strat = STRATEGIES[name](dict(params), use_regime=False)
            self._frames[ck] = (strat, strat.signals(panel, [key])[key])
        return self._frames[ck]

    def _strategies(self, row: dict[str, Any], panel: dict, ts: pd.Timestamp) -> list:
        """[(strategy, SignalFrame)]: the stored params, else every matching grid point."""
        name, key = row["strategy"], row["key"]
        if row.get("params"):
            try:
                return [self._frame(name, json.loads(row["params"]), key, panel)]
            except (ValueError, KeyError, TypeError):
                return []
        spec = _exit_spec(json.loads(row["exit"]))
        dist = json.loads(row["stop"])["distance_frac"]
        out = []
        for params in STRATEGIES[name].GRID:
            strat, sf = self._frame(name, params, key, panel)
            if sf.exit_spec != spec or ts not in sf.stop_distance.index:
                continue
            d = float(sf.stop_distance.loc[ts])
            if np.isfinite(d) and abs(d - dist) <= _DIST_RTOL * abs(dist):
                out.append((strat, sf))
        return out

    def _replay(self, strat: Any, sf: SignalFrame, ts: pd.Timestamp, direction: int, mk: _Market,
                *, max_hold: int | None = None) -> pd.DataFrame:
        panel, funding, market = mk
        entry = pd.Series(0, index=sf.entry.index, dtype="int8")
        entry.loc[ts] = direction
        sf = dataclasses.replace(sf, entry=entry)
        if max_hold is not None:
            sf = dataclasses.replace(sf, exit_spec=dataclasses.replace(sf.exit_spec, max_hold_bars=max_hold))
        return build_tradelist(sf, panel, funding, self.costs, self.account, strategy=strat,
                               band=self.band, market=market)

    def label(self, row: dict[str, Any]) -> tuple[float | None, str] | None:
        """(outcome_r, exit_reason) to record (``outcome_r`` None: terminal), or None: still pending."""
        terminal = _static_terminal(row, self.account)
        if terminal:
            return None, terminal
        ts = m.ts_from_str(row["ts"])
        mk = self._market(row["key"])
        if mk is None:
            return None
        found = self._strategies(row, mk[0], ts)
        if not found or ts not in found[0][1].entry.index:
            log.warning("candidate %s: no strategy params match; terminal", row["id"])
            return None, "no_grid_match"
        direction = int(row["direction"])
        results: set[tuple[float, str] | None] = set()
        for strat, sf in found:
            tl = self._replay(strat, sf, ts, direction, mk)
            results.add(None if tl.empty else (float(tl.iloc[0]["r_net"]), str(tl.iloc[0]["exit_reason"])))
        # a grid point whose entry features are not finite at ts cannot be the source: ignore it
        done = {r for r in results if r is not None}
        if len(done) > 1:
            return None, "ambiguous_params"
        if done:
            return done.pop()
        # no closed trade: still open, or the entry never opened (probe: time exit after one bar)
        if len(mk[2].idx[mk[2].idx > ts]) < UNFILLED_AFTER_BARS:
            return None
        if all(self._replay(st, sf, ts, direction, mk, max_hold=1).empty for st, sf in found):
            return None, "entry_unfilled"
        return None


def label_candidates(db: Db, store: BarStore, costs: CostModel, account: AccountKind,
                     now: pd.Timestamp, *, band: float) -> int:
    """Label every unlabeled candidate of ``account`` whose counterfactual trade has closed within
    the bars closed at ``now``, and write terminal outcomes for the never-labelable ones (module
    docstring); returns how many outcome rows were written. ``band`` is the account's
    ``max_price_band``."""
    now = pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    n = 0
    pending = []
    for r in _unlabeled(db, account):
        reason = _static_terminal(r, account)
        if reason is None:
            pending.append(r)
            continue
        try:
            db.record_outcome(int(r["id"]), None, reason, labeled_at=now)
            n += 1
        except IntegrityError:
            pass
    if not pending:
        return n
    first = min(m.ts_from_str(r["ts"]) for r in pending)
    lab = _Labeler(store, costs, account, now, band, first - LOOKBACK_BARS * BAR)
    for row in pending:
        res = lab.label(row)
        if res is None:
            continue
        try:
            db.record_outcome(int(row["id"]), res[0], res[1], labeled_at=now)
        except IntegrityError:  # labeled concurrently
            continue
        n += 1
    return n
