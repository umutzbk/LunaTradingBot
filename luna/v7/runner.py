"""Research runner: a deterministic orchestrator from per-symbol data and fitted fold models to the engine's
per-decision inputs, the S-10.6 / S-10.7 drivers and the holdout-input assembly (spec S-2.3, S-4.4, S-5.2,
S-5.5, S-7.1-S-7.3, §9, S-10.4-S-10.7, S-13.2 / S-13.4 inputs, K8, DT1, DT4, §17, DEC-19, DEC-27; AMB-25,
AMB-26, AMB-27).

The runner holds no research logic: every number comes from ``FROZEN`` or an existing module, and every
economic rule is the existing module's (``universe`` gates, ``spread.half_spread``, ``beta.beta_hat``,
``features``, ``arms``, ``labels``, ``cv``, ``book.engine``, ``accounting``, ``selection``, ``verdict``).
Built and tested on synthetic data only (never executed on research data in this phase). No I/O, no
wall-clock; the DT4 run record is written by ``luna.v7.runrecord`` to a caller-supplied path.

* ``ResearchUniverse``: per-symbol inputs (``SymbolInputs``) -> ``SymbolData`` (gates / features, with the
  ``intervals.annotate`` output), ``engine.Instrument`` and ``labels.LegData`` per instance. Required columns
  are checked up front (fail closed; nothing is defaulted). ``ResearchUniverse.from_source(source, lo, hi)``
  builds it from a ``UniverseSource`` (SC-01 part 1, AMB-36): every symbol's inputs restricted to the event
  times [lo, hi) of the window being evaluated, so the full history is never resident
  (``rawstore.StoreSource``: lazy, guarded loaders over the raw store; ``InMemorySource``: the same protocol
  over in-memory ``SymbolInputs``, e.g. the synthetic fixtures). The window must cover every lookback /
  label horizon of what is evaluated in it (the caller's responsibility; rows outside it do not exist for the
  universe).
* ``decision_pass`` (SC-01 part 2, AMB-38): the outcome-free decision universe at real scale. Decisions are
  processed in chunks; each chunk builds ``ResearchUniverse.for_decisions(source, lo, hi, history_lo=...)``
  (15m / R4 bars from ``lo - pitindex.LOOKBACK_15M``, funding from ``history_lo``, 1-minute bars only inside
  the U6 72 h windows of the symbol's scheduled delistings, no aggTrades: everything a decision view reads,
  nothing else) and writes every view to a shared ``viewstore.ViewStore``, so peak memory is one chunk's
  universe. A view equals the one of ``from_source(source, history_lo, hi)`` (the look-back covers every
  read; a window that could differ because older rows were not loaded is refused). A decisions-only
  universe refuses labels and books (``leg_data`` / ``engine_inputs``).
* ``DecisionProvider``: ``provider(t)`` -> ``engine.DecisionView``, point in time (every element uses rows
  with ``known_at <= t - 1 min``; the overall max known_at is asserted): U1-U8 ``GateRow`` per candidate
  symbol (``features.xs_input`` -> ``universe.gates``), h-hat(t) (``spread.half_spread`` on the current
  instance's PIT bars), beta-hat(t) (``beta.beta_hat`` with the hourly known_at series), qv24 (the S-4.4
  liquidity-cap input, AMB-25 (a)), the S-7.3 staleness flag (AMB-25 (b)) and the gated candidate rows
  (eligible symbols only, both sides) with the A0 inputs, the §9 features and their max known_at (DT1).
  Cached per t (and per (symbol, t)); ``record(t)`` is the provenance of the decision (gate rows, max
  known_at, staleness reasons, ...).
* The W_HOLD / M6 guard (S-1.3, S-1.7 "no W_HOLD features or labels" before the opening, K9; SC-03,
  AMB-35): ``ResearchUniverse.data_span()`` is the span of every input timestamp (frame event times, notice
  release / capture / receive times, terminal cessation / settlement times, instance start / last-trade /
  end-known times), recomputed by every ``check_data``. ``DecisionProvider`` (at construction; and on EVERY
  call and every public per-t method, cached or not, with the decision time t), ``label_panel``,
  ``run_book`` and ``engine_inputs`` pass it through ``guard.check_data_span`` with the provider's / caller's
  hardened authorization (``auth``; None before the opening). A W_HOLD t or input in W_HOLD is refused
  before the opening, M6 until the canonical R_V6; a refusal is raised and K9-logged. Every research entry
  point refuses to run without a configured K9 log (``k9.require``).
* ``FoldScoreModel``: ``engine.ScoreModel`` over the outer-fold models: a decision t uses the model whose test
  quarter contains t (S-10.6 "each quarter uses its own fold model") and t outside every test quarter is
  refused. Logs (fold id, model sha256) and the predictions per decision.
* ``label_panel``: the ruling-R2 ``samples`` of one horizon from ``labels.leg_label`` over the gated
  candidates, with the invalid-label rate (K8 / DEC-27); runs the outcome guard over the panel's span.
* ``run_book`` / ``decision_log``: one engine run through the provider, and its per-decision provenance
  (inputs' max known_at, gate rows, fold id / model sha256, scores, constructor / risk notes).
* ``luna.v7.runner_select``: the S-10.6 / S-10.7 driver and the holdout-input assembly;
  ``luna.v7.runrecord``: the DT4 run record.

Warning policy (AMB-26): solver ConvergenceWarnings are recorded per fit by ``models``
(``Fitted.convergence``), collected here (``fold_convergence``) and written to DT4; never raised, never
filtered globally.
"""

from __future__ import annotations

import hashlib
import pickle
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

import numpy as np
import pandas as pd

from luna.v7 import cv, features, guard, k9, labels, localdata
from luna.v7.arms import arm_scores, cost_side, families
from luna.v7.beta import beta_hat, beta_hat_sorted, hourly_close_from_15m, hourly_known_at_from_15m
from luna.v7.book import engine
from luna.v7.book.engine import BookRun, DecisionView, EngineInputs, Instrument
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.data.intervals import annotate
from luna.v7.data.notices import NoticeVersion, U6State
from luna.v7.data.terminal import TerminalEvent
from luna.v7.features import FEATURES, R4Bars, XSInput
from luna.v7.labels import LabelInputs, LegData, leg_label
from luna.v7.pitindex import LOOKBACK_15M
from luna.v7.spread import half_spread
from luna.v7.timeutil import MIN, H, ns_array
from luna.v7.universe import (
    BAR_15M,
    GateRow,
    Identity,
    LeakageError,
    SymbolCache,
    SymbolData,
    eligible,
    pit_rows,
)

if TYPE_CHECKING:  # runtime import cycle: viewstore -> runstore -> rawstore -> runner
    from luna.v7.viewstore import ViewStore

HEDGE = engine.HEDGE_SYMBOL
HORIZONS = tuple(FROZEN.horizons_h)
FILL_DELAY = FROZEN.decision.fill_delay_min * MIN
QV24_NS = FROZEN.costs.liquidity_window_h * H  # S-4.4 "trailing 24 h quote volume"
CAND_COLS = ["symbol", "side", "gate_ok", "fl30_r8", "cost_side", "max_known_at"]

REQUIRED_COLS: dict[str, tuple[str, ...]] = {
    "bars15": ("open_ts", "o", "h", "l", "c", "vol_base", "vol_quote", "known_at"),
    "bars1m": ("open_ts", "o", "c", "vol_base", "known_at"),
    "funding": ("funding_ts", "rate", "interval_h_explicit", "known_at"),
    "r4": ("open_ts", "c", "known_at"),
}
# the event-time columns of every input frame (the guarded data span; ``known_at`` is not an event time)
EVENT_COLS: dict[str, tuple[str, ...]] = {"bars15": ("open_ts",), "bars1m": ("open_ts",),
                                          "funding": ("funding_ts",), "r4": ("open_ts",),
                                          "aggtrades": ("ts",)}
UNIVERSE = "research universe"
DECISION_CHUNK_NS = 30 * 24 * H  # decision_pass chunk length (implementation parameter, AMB-38)


def _need(df: pd.DataFrame | None, cols: Sequence[str], what: str) -> None:
    if not isinstance(df, pd.DataFrame):
        raise ValueError(f"{what}: required input missing")
    miss = [c for c in cols if c not in df.columns]
    if miss:
        raise ValueError(f"{what}: required columns missing {miss}")


def _mx(*vals: int | None) -> int | None:
    v = [int(x) for x in vals if x is not None]
    return max(v) if v else None


def _max_ka(df: pd.DataFrame) -> int | None:
    return int(ns_array(df["known_at"], "known_at").max()) if len(df) else None


# ------------------------------------------------------------------------------------------------ universe


@dataclass(frozen=True)
class SymbolInputs:
    """One symbol's normalised inputs (outputs of the ingestion / instance stages): R3 15m and R5 1m
    last-price bars, merged R2 funding records (``data.funding.merge``), instances, R4 bars, the R1
    identity, R11 notice versions, classified terminal events per instance idx and (E_M only) aggTrades.
    ``notices`` and ``terminals`` are REQUIRED (no default): an explicit empty tuple / mapping states "no
    notice" / "no classified terminal event"; a forgotten feed (None) is refused, never read as empty."""

    symbol: str
    bars15: pd.DataFrame
    bars1m: pd.DataFrame
    funding: pd.DataFrame
    insts: list[Instance]
    r4: R4Bars
    identity: Identity | None
    notices: tuple[NoticeVersion, ...]
    terminals: Mapping[int, TerminalEvent]
    aggtrades: pd.DataFrame | None = None


class UniverseSource(Protocol):
    """Where a ``ResearchUniverse`` gets its per-symbol inputs (SC-01 part 1): ``symbols()`` and
    ``load(symbol, lo, hi)`` = the symbol's ``SymbolInputs`` with every time-series row of event time in
    [lo, hi) (sorted, as stored), and its non-series inputs (instances, identity, notices, terminal
    events)."""

    def symbols(self) -> Sequence[str]: ...

    def load(self, symbol: str, lo: int, hi: int) -> SymbolInputs: ...


def _window(df: pd.DataFrame, col: str, lo: int, hi: int, what: str) -> pd.DataFrame:
    """The rows of ``df`` with lo <= ``col`` < hi via ``searchsorted`` (``df`` must be sorted by ``col``)."""
    if not len(df):
        return df
    ts = ns_array(df[col].to_numpy(), f"{what}.{col}")
    if (np.diff(ts) < 0).any():
        raise ValueError(f"{what}: not sorted by {col} (a window is a searchsorted slice)")
    return df.iloc[int(np.searchsorted(ts, lo, "left")):int(np.searchsorted(ts, hi, "left"))]


def halt_windows(symbol: str, notices: Sequence[NoticeVersion],
                 insts: list[Instance]) -> list[tuple[int, int]]:
    """[sched - 60 min, sched + 72 h) of every scheduled delisting U6 can test: the only 1-minute rows the
    72 h halt test reads (``notices.U6State``; HALTED(tau) reads bars opening in [tau - 60 min, tau))."""
    scheds = U6State(list(notices), symbol, insts).scheds()
    return [(s - H, s + FROZEN.notices.u6_clear_h * H) for s in scheds]


def _in_windows(df: pd.DataFrame, col: str, wins: Sequence[tuple[int, int]], lo: int, hi: int,
                what: str) -> pd.DataFrame:
    """The rows of a sorted frame with ``col`` in the union of ``wins``, clipped to [lo, hi)."""
    parts = [_window(df, col, max(a, lo), min(b, hi), what) for a, b in sorted(wins)
             if max(a, lo) < min(b, hi)]
    if not parts:
        return df.iloc[0:0]
    out = pd.concat(parts) if len(parts) > 1 else parts[0]
    ts = ns_array(out[col].to_numpy(), f"{what}.{col}") if len(out) else np.empty(0, np.int64)
    return out.iloc[np.unique(ts, return_index=True)[1]] if len(parts) > 1 else out


class InMemorySource:
    """``UniverseSource`` over in-memory ``SymbolInputs`` (synthetic fixtures, small runs): ``load`` returns
    the frames' rows with event time in [lo, hi) (``EVENT_COLS``; positional slices), other inputs as
    given."""

    def __init__(self, inputs: Mapping[str, SymbolInputs]) -> None:
        self.inputs = dict(inputs)

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.inputs))

    def load(self, symbol: str, lo: int, hi: int) -> SymbolInputs:
        lo, hi = int(lo), int(hi)
        if hi <= lo:
            raise ValueError(f"empty window [{lo}, {hi})")
        x = self.inputs[symbol]

        def w(df: pd.DataFrame, name: str) -> pd.DataFrame:
            return _window(df, EVENT_COLS[name.split(".")[0]][0], lo, hi, f"{symbol}.{name}")

        r4 = R4Bars(*(w(getattr(x.r4, k), f"r4.{k}") for k in ("premium", "mark", "index")))
        agg = None if x.aggtrades is None else w(x.aggtrades, "aggtrades")
        return replace(x, bars15=w(x.bars15, "bars15"), bars1m=w(x.bars1m, "bars1m"),
                       funding=w(x.funding, "funding"), r4=r4, aggtrades=agg)

    def load_decision_inputs(self, symbol: str, lo: int, hi: int, history_lo: int) -> SymbolInputs:
        """The inputs a decision view reads (``ResearchUniverse.for_decisions``): bars / R4 of [lo, hi),
        funding of [history_lo, hi), 1-minute bars of the U6 halt windows in [history_lo, hi), no
        aggTrades."""
        lo, hi, history_lo = int(lo), int(hi), int(history_lo)
        x = self.load(symbol, lo, hi)
        full = self.inputs[symbol]
        wins = halt_windows(symbol, full.notices, list(full.insts))
        return replace(x, funding=_window(full.funding, "funding_ts", history_lo, hi, f"{symbol}.funding"),
                       bars1m=_in_windows(full.bars1m, "open_ts", wins, history_lo, hi, f"{symbol}.bars1m"),
                       aggtrades=None)


class ResearchUniverse:
    """The per-symbol objects every stage consumes, built once from ``SymbolInputs`` (``hedge`` = BTCUSDT)."""

    def __init__(self, inputs: Mapping[str, SymbolInputs], hedge: str = HEDGE) -> None:
        if hedge not in inputs:
            raise ValueError(f"the hedge instrument {hedge} is a required input")
        self.hedge = hedge
        self.inputs: dict[str, SymbolInputs] = {}
        self.funding_annot: dict[str, pd.DataFrame] = {}
        self.sd: dict[str, SymbolData] = {}
        self.instruments: dict[str, Instrument] = {}
        self._legs: dict[tuple[str, int], LegData] = {}
        for sym in sorted(inputs):
            x = inputs[sym]
            if x.symbol != sym:
                raise ValueError(f"inputs keyed {sym} hold symbol {x.symbol}")
            _need(x.bars15, REQUIRED_COLS["bars15"], f"{sym}.bars15")
            _need(x.bars1m, REQUIRED_COLS["bars1m"], f"{sym}.bars1m")
            _need(x.funding, REQUIRED_COLS["funding"], f"{sym}.funding")
            if not isinstance(x.r4, R4Bars):
                raise ValueError(f"{sym}.r4: required input missing")
            for k in ("premium", "mark", "index"):
                _need(getattr(x.r4, k), REQUIRED_COLS["r4"], f"{sym}.r4.{k}")
            if not x.insts:
                raise ValueError(f"{sym}: no instances (required)")
            if not isinstance(x.notices, tuple | list) or not all(isinstance(n, NoticeVersion)
                                                                  for n in x.notices):
                raise ValueError(f"{sym}.notices: required R11 input missing (pass () for none)")
            if not isinstance(x.terminals, Mapping) or not all(isinstance(e, TerminalEvent)
                                                               for e in x.terminals.values()):
                raise ValueError(f"{sym}.terminals: required input missing (pass {{}} for none)")
            if x.aggtrades is not None:
                _need(x.aggtrades, ("ts", "price", "qty"), f"{sym}.aggtrades")
            fa = annotate(x.funding, list(x.insts))
            self.inputs[sym], self.funding_annot[sym] = x, fa
            sd = SymbolData(sym, x.bars15, x.bars1m, fa, list(x.insts), list(x.notices), x.identity)
            self.sd[sym] = replace(sd, cache=SymbolCache(sd))  # indexed inputs (Task 26b, AMB-38)
            mark15 = x.r4.mark[["open_ts", "c"]]
            self.instruments[sym] = Instrument(sym, x.bars1m, x.bars15, mark15, x.funding, list(x.insts),
                                               dict(x.terminals), x.aggtrades)
        self.candidates: tuple[str, ...] = tuple(s for s in sorted(self.inputs) if s != hedge)
        self.decisions_only = False

    @classmethod
    def from_source(cls, source: UniverseSource, lo: int, hi: int, hedge: str = HEDGE) -> ResearchUniverse:
        """The universe of ``source.symbols()`` with every input restricted to the window [lo, hi) (loaded
        symbol by symbol; module docstring)."""
        lo, hi = int(lo), int(hi)
        if hi <= lo:
            raise ValueError(f"empty window [{lo}, {hi})")
        return cls({s: source.load(s, lo, hi) for s in sorted(source.symbols())}, hedge)

    @classmethod
    def for_decisions(cls, source: UniverseSource, lo: int, hi: int, *, history_lo: int,
                      hedge: str = HEDGE) -> ResearchUniverse:
        """A decisions-only universe for decision times in [lo, hi) (module docstring, ``decision_pass``):
        each symbol's ``source.load_decision_inputs(symbol, max(history_lo, lo - LOOKBACK_15M), hi,
        history_lo)``. Labels and books are refused on it."""
        lo, hi, history_lo = int(lo), int(hi), int(history_lo)
        if hi <= lo or lo < history_lo:
            raise ValueError(f"decision window [{lo}, {hi}) outside the history start {history_lo}")
        bars_lo = max(history_lo, lo - LOOKBACK_15M)
        u = cls({s: source.load_decision_inputs(s, bars_lo, hi, history_lo)  # type: ignore[attr-defined]
                 for s in sorted(source.symbols())}, hedge)
        u.decisions_only = True
        for s, sd in u.sd.items():  # the look-back argument needs the indexed path (sorted, monotone ka)
            c = sd.cache
            assert c is not None
            r4 = u.inputs[s].r4
            if not (c.b15.ok and all(c.frame_index(getattr(r4, k), ("c",)).ok
                                     for k in ("premium", "mark", "index"))):
                raise ValueError(f"{s}: a decisions-only universe needs 15m / R4 bars with strictly "
                                 "increasing open_ts and non-decreasing known_at (use from_source)")
            if bars_lo > history_lo:  # rows before bars_lo exist in the full history: refuse what they change
                c.bars_lo = bars_lo
        return u

    def _no_outcomes(self, what: str) -> None:
        if self.decisions_only:
            raise ValueError(f"{what}: a decisions-only universe (ResearchUniverse.for_decisions) holds no "
                             "outcome inputs")

    def data_span(self) -> tuple[int, int] | None:
        """(min, max + 1) of every input timestamp, recomputed from the current inputs: the event times of
        every frame (``EVENT_COLS``), the notices' ``release_ts`` / ``capture_ts`` / ``recv_ts``, the terminal
        events' ``cessation_ts`` / ``settle_ts`` and the instances' ``first_bar_ts`` / ``last_trade_ts`` /
        ``end_known_at`` (missing values skipped; anything unparseable refused by ``ns_array``)."""
        spans: list[tuple[int, int]] = []
        for sym, x in self.inputs.items():
            frames = [("bars15", x.bars15), ("bars1m", x.bars1m), ("funding", x.funding),
                      *((f"r4.{k}", getattr(x.r4, k)) for k in ("premium", "mark", "index"))]
            if x.aggtrades is not None:
                frames.append(("aggtrades", x.aggtrades))
            for name, df in frames:
                sp = localdata.frame_span(df, EVENT_COLS[name.split(".")[0]], f"{sym}.{name}")
                if sp is not None:
                    spans.append(sp)
            notices = [v for n in x.notices for v in (n.release_ts, n.capture_ts, n.recv_ts)]
            terminals = [v for e in x.terminals.values() for v in (e.cessation_ts, e.settle_ts)]
            insts = [v for i in x.insts for v in (i.first_bar_ts, i.last_trade_ts, i.end_known_at)]
            for name, vals in (("notices", notices), ("terminals", terminals), ("insts", insts)):
                vals = [v for v in vals if v is not None]
                if vals:
                    a = ns_array(np.asarray(vals, dtype=object), f"{sym}.{name}")
                    spans.append((int(a.min()), int(a.max()) + 1))
        return (min(a for a, _ in spans), max(b for _, b in spans)) if spans else None

    def check_data(self, auth: guard.HoldoutAuthorization | None) -> tuple[int, int] | None:
        """``guard.check_data_span`` over a freshly computed ``data_span()`` (refused and K9-logged when it
        reaches W_HOLD before the opening, or M6 before R_V6); returns the span."""
        span = self.data_span()
        if span is not None:
            guard.check_data_span(*span, auth=auth, what=UNIVERSE)
        return span

    def r4(self, sym: str) -> R4Bars:
        return self.inputs[sym].r4

    def leg_data(self, sym: str, t: int) -> LegData | None:
        """The label inputs of ``sym`` with the terminal event of its current instance at t (None: none)."""
        self._no_outcomes("leg_data")
        cur = current_instance(self.sd[sym].insts, int(t))
        if cur is None:
            return None
        key = (sym, cur.idx)
        if key not in self._legs:
            ins = self.instruments[sym]
            self._legs[key] = LegData(ins.bars1m, ins.bars15, ins.mark15, ins.funding, ins.insts,
                                      ins.terminals.get(cur.idx), OrderedDict())
        return self._legs[key]

    def engine_inputs(self, view: Callable[[int], DecisionView], now_ns: int,
                      auth: guard.HoldoutAuthorization | None = None) -> EngineInputs:
        k9.require()
        self._no_outcomes("engine_inputs")
        self.check_data(auth)
        return EngineInputs({s: self.instruments[s] for s in self.candidates}, self.instruments[self.hedge],
                            view, int(now_ns), auth)


# ------------------------------------------------------------------------------------------------ provider


@dataclass(frozen=True)
class DecisionRecord:
    """Provenance of one decision view (DT1): every gate row, the max known_at of every input used, the
    staleness reasons and the eligible symbols."""

    t: int
    gates: Mapping[str, GateRow]
    h_hat: Mapping[str, float | None]
    betas: Mapping[str, float | None]
    qv24: Mapping[str, float | None]
    stale: bool
    stale_reasons: tuple[str, ...]
    eligible: tuple[str, ...]
    n_cands: int
    max_known_at: int | None


class DecisionProvider:
    """Point-in-time ``DecisionView`` per decision t (see module docstring). ``with_features`` adds the §9
    features to the candidate rows (needed by the ML arms and the labels; A0 needs only its own inputs).

    Memory / compute (SC-01 part 2, AMB-38): the per-(symbol, t) bar slices are kept for the CURRENT t only
    (a new t evicts them) and are positional slices (no copy of the history) limited to the look-back the
    per-symbol inputs read (``universe.SymbolCache.window15``) when the bars are sorted by ``open_ts`` with
    non-decreasing ``known_at`` (else the boolean reference path). Views and records are an LRU of at most
    ``max_cached_decisions`` decisions (default 64; None = unbounded); an evicted decision is read back from
    ``store`` (a shared ``viewstore.ViewStore``: every book run of the S-10.6 driver replays the views
    computed once) or, without a store, recomputed (deterministic). A view read from the store equals the
    computed one (stored only when it round-trips canonically).

    Guard (SC-03): ``auth`` is the hardened ``HoldoutAuthorization`` (None before the opening); a K9 log must
    be configured. The universe's data span is computed and checked at construction; that span (the inputs
    are not copied: callers must not mutate them afterwards) and the decision time t are checked again on
    every call and every public per-t method (``instance_bars``, ``h_hat``, ``beta``, ``qv24``,
    ``stale_reasons``, ``xs``), cached or not, so a W_HOLD t / W_HOLD input is refused before the opening and
    M6 until R_V6 (K9-logged), and an authorization invalidated later (verdict) stops serving."""

    def __init__(self, u: ResearchUniverse, with_features: bool = True,
                 max_cached_decisions: int | None = 64, *,
                 auth: guard.HoldoutAuthorization | None = None, store: ViewStore | None = None) -> None:
        if max_cached_decisions is not None and int(max_cached_decisions) < 1:
            raise ValueError("max_cached_decisions must be >= 1 or None")
        if store is not None and bool(store.with_features) != bool(with_features):
            raise ValueError("the view store holds views of another with_features setting")
        k9.require()
        self._span = u.check_data(auth)
        self.auth = auth
        self.u = u
        self.with_features = bool(with_features)
        self.max_cached = None if max_cached_decisions is None else int(max_cached_decisions)
        self._views: OrderedDict[int, DecisionView] = OrderedDict()
        self._records: OrderedDict[int, DecisionRecord] = OrderedDict()
        self._bars: dict[str, pd.DataFrame | None] = {}
        self._bars_t: int | None = None
        self._pos: dict[str, tuple[bool, np.ndarray, np.ndarray]] = {}
        self._wins: dict[str, pd.DataFrame | None] = {}
        self._win_t: int | None = None
        self.store = store

    def _positions(self, sym: str) -> tuple[bool, np.ndarray, np.ndarray]:
        """(sliceable, open_ts, known_at) of the symbol's 15m bars, validated once."""
        if sym not in self._pos:
            b = self.u.sd[sym].bars15
            ot = ns_array(b["open_ts"], f"{sym}.bars15.open_ts") if len(b) else np.empty(0, np.int64)
            ka = ns_array(b["known_at"], f"{sym}.bars15.known_at") if len(b) else np.empty(0, np.int64)
            ok = bool(len(b) == 0 or ((np.diff(ot) >= 0).all() and (np.diff(ka) >= 0).all()))
            self._pos[sym] = (ok, ot, ka)
        return self._pos[sym]

    # per (symbol, t) inputs
    def _instance_bars(self, sym: str, t: int) -> pd.DataFrame | None:
        """PIT 15m bars (known_at <= t - 1 min) of the current instance at t; None without one."""
        t = int(t)
        if self._bars_t != t:
            self._bars, self._bars_t = {}, t
        if sym not in self._bars:
            self._bars[sym] = self._slice(sym, t)
        return self._bars[sym]

    def _slice(self, sym: str, t: int) -> pd.DataFrame | None:
        sd = self.u.sd[sym]
        cur = current_instance(sd.insts, t)
        if cur is None:
            return None
        ok, ot, ka = self._positions(sym)
        if ok:
            lo = int(np.searchsorted(ot, cur.first_bar_ts, "left"))
            hi = int(np.searchsorted(ka, t - MIN, "right"))
            return sd.bars15.iloc[lo:max(lo, hi)]
        return _instance_bars_reference(sd, cur, t)

    def _win(self, sym: str, t: int) -> pd.DataFrame | None:
        """The bars the per-symbol inputs read at t: the PIT current-instance 15m bars limited to
        ``pitindex.LOOKBACK_15M`` plus the last PIT bar (``SymbolCache.window15``: h-hat, beta-hat, qv24,
        staleness and the max known_at read nothing older; identical values), or ``_instance_bars`` when the
        index does not apply. None without a current instance. Kept for the current t only."""
        t = int(t)
        if self._win_t != t:
            self._wins, self._win_t = {}, t
        if sym not in self._wins:
            c = self.u.sd[sym].cache
            if c is not None and c.b15.ok:
                cur = current_instance(self.u.sd[sym].insts, t)
                self._wins[sym] = None if cur is None else c.window15(cur, t)
            else:
                self._wins[sym] = self._instance_bars(sym, t)
        return self._wins[sym]

    def _h_hat(self, sym: str, t: int) -> float | None:
        """S-4.4 decision-time h-hat(t): the S-4.5 proxy at t on the current instance (None = missing)."""
        b = self._win(sym, t)
        if b is None or len(b) == 0:
            return None
        c = self.u.sd[sym].cache
        if c is not None and c.b15.ok:
            return c.h_hat(current_instance(self.u.sd[sym].insts, int(t)), int(t), b)  # type: ignore[arg-type]
        return half_spread(b, int(t))

    def _beta(self, sym: str, t: int) -> float | None:
        """S-5.2 beta-hat(t) against the hedge instrument (current instances, hourly known_at series)."""
        a, b = self._win(sym, t), self._win(self.u.hedge, t)
        if a is None or b is None or len(a) == 0 or len(b) == 0:
            return None
        t = int(t)
        ca, cb = self.u.sd[sym].cache, self.u.sd[self.u.hedge].cache
        if ca is not None and cb is not None and ca.b15.ok and cb.b15.ok:  # hourly series once per decision
            ha = ca.hourly(current_instance(self.u.sd[sym].insts, t), t)  # type: ignore[arg-type]
            hb = cb.hourly(current_instance(self.u.sd[self.u.hedge].insts, t), t)  # type: ignore[arg-type]
            return beta_hat_sorted((ha.hours, ha.close, ha.ka), (hb.hours, hb.close, hb.ka), t)
        return beta_hat(hourly_close_from_15m(a), hourly_close_from_15m(b), t,
                        hourly_known_at_from_15m(a), hourly_known_at_from_15m(b))

    def _qv24(self, sym: str, t: int) -> tuple[float | None, int | None]:
        """S-4.4 trailing 24 h quote volume known at t - 1 min (AMB-25 (a)): the sum of ``vol_quote`` over the
        current instance's 15m bars whose close lies in (t - 24 h - 1 min, t - 1 min]; a non-finite value
        counts 0 (as ``universe.daily_sums``, AMB-07); no such bar -> None (missing). Returns (value, max
        known_at of the bars used)."""
        b = self._win(sym, t)
        if b is None or len(b) == 0:
            return None, None
        close = ns_array(b["open_ts"], "open_ts") + BAR_15M
        hi = int(t) - MIN
        m = (close > hi - QV24_NS) & (close <= hi)
        if not m.any():
            return None, None
        q = pd.to_numeric(b["vol_quote"], errors="coerce").to_numpy(np.float64)[m]
        return float(np.where(np.isfinite(q), q, 0.0).sum()), _max_ka(b[m])

    def _stale_reasons(self, t: int) -> tuple[str, ...]:
        """S-7.3 staleness (AMB-25 (b)): the decision-wide inputs every entry needs are those of the hedge
        instrument (S-7.4: beta_book is hedged at every decision). Stale when the hedge has no current
        instance, its last 15m bar (closing at the last 15-minute boundary <= t - 1 min) is missing /
        unpriced, or its h-hat(t) is missing (S-4.5: a missing proxy is a staleness failure at decisions).
        Per-symbol freshness (U4, U5, U7, U8) is already a gate of that symbol."""
        t = int(t)
        hb = self._win(self.u.hedge, t)
        if hb is None:
            return ("hedge_no_instance",)
        out: list[str] = []
        last_close = (t - MIN) // BAR_15M * BAR_15M
        ot = ns_array(hb["open_ts"], "open_ts") if len(hb) else np.empty(0, np.int64)
        c = pd.to_numeric(hb["c"], errors="coerce").to_numpy(np.float64) if len(hb) else np.empty(0)
        if not ((ot == last_close - BAR_15M) & np.isfinite(c) & (c > 0)).any():
            out.append("hedge_last_bar_missing")
        if self._h_hat(self.u.hedge, t) is None:
            out.append("hedge_h_hat_missing")
        return tuple(out)

    def _xs(self, t: int) -> list[XSInput]:
        u = self.u
        return [features.xs_input(u.sd[s], u.r4(s), int(t)) for s in u.candidates]

    def _cands(self, t: int, xs: list[XSInput],
               hh: Mapping[str, float | None]) -> tuple[pd.DataFrame, int | None]:
        u = self.u
        rows: list[dict] = []
        ka: int | None = None
        for x in xs:
            if not eligible(x.gate):
                continue
            fr = None
            if self.with_features:
                fr = features.compute_rows(u.sd[x.symbol], u.sd[u.hedge], xs, t, (1, -1), r4=u.r4(x.symbol))
            for side in (1, -1):
                h = hh[x.symbol]  # eligible => U5 => h-hat present; cost_side refuses None (fail closed)
                r: dict = dict(symbol=x.symbol, side=side, gate_ok=True, fl30_r8=x.values["fl30"],
                               cost_side=cost_side(h))  # type: ignore[arg-type]
                mk = x.max_known_at
                if fr is not None:
                    r.update(fr[side].values)
                    r["side"] = side
                    mk = _mx(mk, fr[side].max_known_at)
                r["max_known_at"] = mk
                ka = _mx(ka, mk)
                rows.append(r)
        return (pd.DataFrame(rows) if rows else pd.DataFrame(columns=CAND_COLS)), ka

    def _guard(self, t: int) -> int:
        """The decision time t and the universe span through ``guard.check_data_span`` (every call)."""
        t = int(t)
        guard.check_data_span(t, t + 1, auth=self.auth, what="decision t")
        if self._span is not None:
            guard.check_data_span(*self._span, auth=self.auth, what=UNIVERSE)
        return t

    # public per-t inputs: guarded, then the private implementation
    def instance_bars(self, sym: str, t: int) -> pd.DataFrame | None:
        return self._instance_bars(sym, self._guard(t))

    def h_hat(self, sym: str, t: int) -> float | None:
        return self._h_hat(sym, self._guard(t))

    def beta(self, sym: str, t: int) -> float | None:
        return self._beta(sym, self._guard(t))

    def qv24(self, sym: str, t: int) -> tuple[float | None, int | None]:
        return self._qv24(sym, self._guard(t))

    def stale_reasons(self, t: int) -> tuple[str, ...]:
        return self._stale_reasons(self._guard(t))

    def xs(self, t: int) -> list[XSInput]:
        return self._xs(self._guard(t))

    def __call__(self, t: int) -> DecisionView:
        t = self._guard(t)
        if t in self._views:
            self._views.move_to_end(t)
            self._records.move_to_end(t)
            return self._views[t]
        got = self.store.get(t) if self.store is not None else None
        if got is not None:
            self._views[t], self._records[t] = got  # type: ignore[assignment]
        else:
            u = self.u
            xs = self._xs(t)
            syms = u.candidates
            hh = {s: self._h_hat(s, t) for s in syms}
            betas = {s: self._beta(s, t) for s in syms}
            qv = {s: self._qv24(s, t) for s in syms}
            stale = self._stale_reasons(t)
            gates = {x.symbol: x.gate for x in xs}
            cands, ka = self._cands(t, xs, hh)
            bars_ka = [_max_ka(b) for s in (*syms, u.hedge) if (b := self._win(s, t)) is not None]
            mk = _mx(ka, *(x.max_known_at for x in xs), *(v[1] for v in qv.values()), *bars_ka)
            if mk is not None and mk > t - MIN:
                raise LeakageError(f"decision {t}: an input known at {mk} > t - 1 min")
            vols = {s: v[0] for s, v in qv.items()}
            self._views[t] = DecisionView(t, gates, hh, betas, vols, cands, bool(stale))
            el = tuple(x.symbol for x in xs if eligible(x.gate))
            self._records[t] = DecisionRecord(t, gates, hh, betas, vols, bool(stale), stale, el, len(cands),
                                              mk)
            if self.store is not None:
                self.store.put(self._views[t], self._records[t])
        while self.max_cached is not None and len(self._views) > self.max_cached:
            self._views.popitem(last=False)
            self._records.popitem(last=False)
        return self._views[t]

    def record(self, t: int) -> DecisionRecord:
        self(int(t))
        return self._records[int(t)]


def decision_pass(source: UniverseSource, decision_ts: Sequence[int], store: ViewStore, *, history_lo: int,
                  with_features: bool = True, chunk_ns: int = DECISION_CHUNK_NS, hedge: str = HEDGE,
                  auth: guard.HoldoutAuthorization | None = None) -> dict[str, int]:
    """Every decision view of ``decision_ts`` into ``store`` (module docstring): one decisions-only universe
    per chunk of ``chunk_ns`` (released before the next), a ``DecisionProvider`` per chunk (the per-call guard
    as everywhere). Views already in the store are not recomputed. Returns counts."""
    k9.require()
    if bool(store.with_features) != bool(with_features):
        raise ValueError("the view store holds views of another with_features setting")
    ts = sorted({int(t) for t in decision_ts})
    if not ts or ts[0] < int(history_lo):
        raise ValueError("decision times must be non-empty and >= the history start")
    n = chunks = 0
    i = 0
    while i < len(ts):
        j = int(np.searchsorted(ts, ts[i] + int(chunk_ns), "left"))
        todo = [t for t in ts[i:j] if store.get(t) is None]
        if todo:
            u = ResearchUniverse.for_decisions(source, todo[0], todo[-1] + 1, history_lo=history_lo,
                                               hedge=hedge)
            p = DecisionProvider(u, with_features, 1, auth=auth, store=store)
            for t in todo:
                p(t)
            n += len(todo)
            chunks += 1
            del p, u
        i = j
    return {"decisions": len(ts), "computed": n, "chunks": chunks}


def _instance_bars_reference(sd: SymbolData, cur: Instance, t: int) -> pd.DataFrame:
    """The boolean definition of the PIT current-instance bars (``universe.pit_rows`` + instance start)."""
    b = pit_rows(sd.bars15, int(t), f"{sd.symbol}.bars15")
    return b[ns_array(b["open_ts"], "open_ts") >= cur.first_bar_ts]


# ------------------------------------------------------------------------------------------------ models


def model_hash(fm: object) -> str:
    """sha256 of a fold model's pickled state (fitted models, calibrators, preprocessing, fold): the model
    identity recorded per decision. An unpicklable model is refused (no provenance, fail closed)."""
    try:
        blob = pickle.dumps(fm, protocol=4)
    except Exception as e:
        raise ValueError(f"fold model is not hashable for provenance: {e}") from None
    return hashlib.sha256(blob).hexdigest()


class FoldScoreModel:
    """``engine.ScoreModel`` over outer-fold models (``cv.FoldModel`` or any object with ``fold``, ``horizon``
    and ``predict(df)``): decision t uses the model whose test quarter [test_lo, test_hi) contains t; t
    outside every test quarter raises. ``ml_star`` (A2 / A3) names the ranker family; ``classifier`` is
    A1's."""

    def __init__(self, fold_models: Sequence, ml_star: str, horizon: int) -> None:
        self.regressor, self.classifier = families(ml_star)
        self.ml_star = ml_star
        if horizon not in HORIZONS:
            raise ValueError(f"unknown horizon {horizon}")
        self.horizon = int(horizon)
        fms = sorted(fold_models, key=lambda m: int(m.fold.test_lo))
        if not fms:
            raise ValueError("no fold models")
        for m in fms:
            if int(m.horizon) != self.horizon:
                raise ValueError(f"fold {m.fold.fold_id} model is for h = {m.horizon}, not {horizon}")
        for a, b in zip(fms, fms[1:], strict=False):
            if int(b.fold.test_lo) < int(a.fold.test_hi):
                raise ValueError("fold test quarters overlap")
        self.models = tuple(fms)
        self.hashes = tuple(model_hash(m) for m in fms)
        self.log: dict[int, tuple[int, str]] = {}
        self.predictions: dict[int, pd.DataFrame] = {}

    @property
    def quarters(self) -> tuple[tuple[int, int], ...]:
        return tuple((int(m.fold.test_lo), int(m.fold.test_hi)) for m in self.models)

    def fold_index(self, t: int) -> int:
        for i, (lo, hi) in enumerate(self.quarters):
            if lo <= int(t) < hi:
                return i
        raise ValueError(f"decision {t} lies outside every fold model's test quarter (S-10.6)")

    def predict(self, t: int, cands: pd.DataFrame) -> pd.DataFrame:
        i = self.fold_index(t)
        fm = self.models[i]
        p = fm.predict(cands)
        need = [f"yhat_{self.regressor}", f"yhat_cal_{self.regressor}", f"p_{self.classifier}_cal"]
        miss = [c for c in need if c not in p.columns]
        if miss:
            raise ValueError(f"fold model output lacks {miss}")
        out = pd.concat([cands, p], axis=1)
        self.log[int(t)] = (int(fm.fold.fold_id), self.hashes[i])
        self.predictions[int(t)] = out
        return out


def fit_fold_models(samples: pd.DataFrame, folds: Sequence, horizon: int) -> list[cv.FoldModel]:
    """``cv.run_fold`` per fold (S-10.4 1-5: purge, three blocks, selection, calibration), in fold order."""
    return [cv.run_fold(f, samples, horizon) for f in sorted(folds, key=lambda f: f.test_lo)]


def fold_convergence(fold_models: Mapping[int, Sequence] | Sequence, *,
                     allow_unavailable: bool = False) -> tuple[dict, ...]:
    """Every linear fit's convergence status (AMB-26) of the fold models (``cv.FoldModel.convergence``). A
    model without a convergence record raises, or with ``allow_unavailable`` gives one explicit row
    ``stage = "unavailable"`` (``converged`` None) for that fold: never a silent omission."""
    seqs = fold_models.values() if isinstance(fold_models, Mapping) else [fold_models]
    out: list[dict] = []
    for seq in seqs:
        for m in seq:
            if hasattr(m, "convergence"):
                out.extend(m.convergence())
            elif allow_unavailable:
                out.append(dict(fold_id=int(m.fold.fold_id), horizon=int(m.horizon), stage="unavailable",
                                family=None, params=(), converged=None, n_iter=None, max_iter=None,
                                messages=("no convergence record on this fold model",)))
            else:
                raise ValueError("fold model without a convergence record")
    return tuple(out)


# ------------------------------------------------------------------------------------------------ labels


@dataclass(frozen=True)
class LabelPanel:
    """``samples`` (ruling R2: decision_ts, symbol, side, h, label_known_at, y_reg, y_pos, net + FEATURES;
    valid labels only), every label result (DT2 subset) and the S-5.5 invalid-label rate (K8 / DEC-27)."""

    h: int
    samples: pd.DataFrame
    labels: pd.DataFrame
    n_labels: int
    n_invalid: int
    n_no_label: int
    invalid_rate: float
    invalid_by_reason: tuple[tuple[str, int], ...]


def label_panel(u: ResearchUniverse, provider: DecisionProvider, decision_ts: Sequence[int], h: int,
                window: tuple[int, int], now_ns: int,
                auth: guard.HoldoutAuthorization | None = None) -> LabelPanel:
    """Leg labels (``labels.leg_label``) of every gated candidate (both sides) at the given decisions."""
    if h not in HORIZONS:
        raise ValueError(f"unknown horizon {h}")
    k9.require()
    if not provider.with_features:
        raise ValueError("label_panel needs the §9 features (DecisionProvider(with_features=True))")
    ts = sorted({int(t) for t in decision_ts})
    if not ts:
        raise ValueError("no decision times")
    w_lo, w_end = int(window[0]), int(window[1])
    guard.check_outcome_window(ts[0], min(ts[-1] + FILL_DELAY + h * H + MIN, w_end), auth=auth, now_ns=now_ns)
    u.check_data(auth)
    rows: list[dict] = []
    res: list[labels.LabelResult | None] = []
    lab_rows: list[dict] = []
    for t in ts:
        for c in provider(t).cands.itertuples(index=False):
            sym, side = str(c.symbol), int(c.side)
            leg, btc = u.leg_data(sym, t), u.leg_data(u.hedge, t)
            if leg is None or btc is None:
                raise ValueError(f"{sym} @ {t}: a gated candidate without label inputs (instance missing)")
            r = leg_label(LabelInputs(sym, leg, btc, (w_lo, w_end), now_ns, auth), t, side, h)
            res.append(r)
            if r is None:
                continue
            lab_rows.append(dict(decision_ts=t, symbol=sym, side=side, h=h, net=r.net, y_reg=r.y_reg_bps,
                                 y_pos=r.y_pos, label_known_at=r.label_known_at,
                                 invalid_reason=r.invalid_reason))
            if r.invalid_reason is not None:
                continue
            d = {k: getattr(c, k) for k in FEATURES}
            d.update(decision_ts=t, symbol=sym, side=side, h=h, label_known_at=r.label_known_at,
                     y_reg=r.y_reg_bps, y_pos=float(r.y_pos), net=r.net)  # type: ignore[arg-type]
            rows.append(d)
    inv: dict[str, int] = {}
    for r in res:
        if r is not None and r.invalid_reason is not None:
            inv[r.invalid_reason] = inv.get(r.invalid_reason, 0) + 1
    n = sum(r is not None for r in res)
    return LabelPanel(h, pd.DataFrame(rows), pd.DataFrame(lab_rows), n, sum(inv.values()),
                      sum(r is None for r in res), labels.invalid_rate(res), tuple(sorted(inv.items())))


# ------------------------------------------------------------------------------------------------ books


def run_book(arm: str, u: ResearchUniverse, provider: DecisionProvider, window: tuple[int, int], h: int,
             now_ns: int, model: FoldScoreModel | None = None, execution: str = "E_T",
             auth: guard.HoldoutAuthorization | None = None) -> BookRun:
    """One ``engine.run`` (L-norm) through the provider; E_M needs every instrument's aggTrades; the ML arms
    need the §9 features in the candidate rows (``DecisionProvider(with_features=True)``). A research entry
    point: the run is marked ``research=True``, so its DT4 record is a research record (refused from a dirty
    working tree; DK-114 / red-team m10)."""
    k9.require()
    if arm != "A0" and not provider.with_features:
        raise ValueError(f"{arm} scores the §9 features: the provider must be built with_features=True")
    if arm != "A0" and model is not None and model.ml_star != arm and arm in ("A2", "A3"):
        raise ValueError(f"{arm} run with an ML* = {model.ml_star} score model")
    if model is not None and model.horizon != h:
        raise ValueError("score model horizon differs from the run's h*")
    if execution == "E_M":
        miss = [s for s in u.candidates if u.instruments[s].aggtrades is None]
        if miss:
            raise ValueError(f"E_M needs aggTrades for {miss}")
    inputs = u.engine_inputs(provider, now_ns, auth)  # checks the universe span (``check_data``)
    return replace(engine.run(arm, inputs, window, h, model=model, execution=execution), research=True)


def decision_log(run: BookRun, provider: DecisionProvider,
                 model: FoldScoreModel | None = None) -> pd.DataFrame:
    """Per-decision provenance of an executed book: the provider record (max known_at, gate failures,
    staleness, eligible symbols), the fold id / model sha256 used, the arm's scores (``arms.arm_scores``
    on the rows the engine scored) and the constructor / risk notes and orders of the engine."""
    rows: list[dict] = []
    dec = run.decisions
    for d in (dec.to_dict("records") if len(dec) else []):
        t = int(d["t"])
        rec = provider.record(t)
        fold_id = mh = None
        sc: pd.DataFrame | None = None
        if run.arm == "A0":
            c = provider(t).cands
            sc = arm_scores("A0", c, float(run.h_star)) if len(c) else None
        elif model is not None and t in model.log:
            fold_id, mh = model.log[t]
            sc = arm_scores(run.arm, model.predictions[t], float(run.h_star), model.ml_star)
        scores = () if sc is None else tuple(
            (str(r.symbol), int(r.side), float(r.rank_key), bool(r.passes_hurdle), bool(r.vetoed))
            for r in sc.itertuples())  # vetoed: A1's entry-only veto (DK-42)
        rows.append(dict(
            t=t, max_known_at=rec.max_known_at, stale=bool(d["stale"]), stale_reasons=rec.stale_reasons,
            eligible=rec.eligible,
            gate_failures=tuple((s, g.reasons) for s, g in sorted(rec.gates.items()) if g.reasons),
            fold_id=fold_id, model_hash=mh, scores=scores, orders=d["orders"], notes=d["notes"],
            hedge_reason=d["hedge_reason"], killed=bool(d["killed"]), paused=bool(d["paused"])))
    return pd.DataFrame(rows)


__all__ = ["DECISION_CHUNK_NS", "DecisionProvider", "DecisionRecord", "FoldScoreModel", "InMemorySource",
           "LabelPanel", "ResearchUniverse", "SymbolInputs", "UniverseSource", "decision_log",
           "decision_pass",
           "fit_fold_models", "fold_convergence", "halt_windows", "label_panel", "model_hash", "run_book"]
