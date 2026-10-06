"""24/7 live engine, paper mode (spec §3.3, §6, §7, §10).

The engine runs the backtest's decision pipeline (``luna.engine.pipeline.Pipeline``) on a rolling
buffer of closed 15-minute bars from a ``LiveFeed`` and routes orders to a broker (``PaperBroker``:
simulated fills against live public data). Mode ``"live"`` exists only so the risk engine can veto
it: R19 rejects every order while ``live_enabled`` is false (no live broker is built).

Clock. ``tick(now)`` runs on every 15-minute boundary + 20 s; ``minute_check(now)`` every 60 s.
The decision bar ``t`` is the newest closed bar (``t + 15min <= now``; the feed's buffer is
re-filtered so a still-forming bar is never used); orders are stamped with the real tick time
``now`` (``RiskContext(now=now, last_bar_ts=t)``: R16 vetoes entries on stale bars; the UTC day
rolls on the first tick after midnight, once, even after downtime). A bar is decided at most once:
a tick whose newest bar was already processed (feed without new data) only books fills and records
equity. A tz-naive ``now`` raises.

tick(now):
  1. reload kill_switch / paused from the Db (others may set them; they are only written with
     ``Db.set_flag``); refit the model when due;
  2. ``feed.refresh(now)`` - ``FeedError``: skip the tick, record risk event ``feed_error``. Every
     tick without fresh data (feed error, no bars, no new bar, a stale newest bar) is a data miss
     (spec §7.4): 4 in a row -> notify; 2 in a row with open positions -> pause + notify;
  3. poll the broker and book every fill row recorded since the last one booked (the Db is the
     source of truth, so fills recorded before a crash are booked exactly once after a restart);
     resolve entry groups whose orders are all terminal;
  4. hourly funding (perp) at the first tick / minute check of each hour (rate from
     ``feed.funding_rates()``, None -> unknown);
  5. on a new bar: ``portfolio_step`` (mark, day roll, R3/R4/R18 actions) -> exits
     (``should_exit`` + ``flatten``) -> entries (ranker -> constructor -> planner -> risk -> broker),
     exactly the backtest's steps (4)-(6) on the last row of the buffer;
  6. record candidates (selected and rejected, with reasons), risk events, closed positions, equity;
     commit a snapshot.

minute_check(now): poll fills; emergency stops from live mids (a position whose loss reaches
``emergency_stop_frac * equity`` is closed at once: a resting post-only exit is cancelled and
replaced); every position exiting for ``emergency_stop`` / ``kill_switch`` without a working exit
order is (re-)flattened as IOC each minute (rejected / cancelled / partly filled IOCs are retried
with a fresh client id: ``plan_exit(attempt=n)``, a monotone per-position counter); hourly
funding; Telegram commands. ``run_forever`` refreshes the lock (and pings the dead-man's switch)
first, before any tick / minute check; a lost lock stops the engine.

Urgent exits (emergency stop, kill switch, R4 drawdown kill) always go out as IOC limits within the
band, also on spot. ``/resume`` clears both flags, resets the peak to the current equity, cancels
the kill switch's working exits and hands those positions back to their strategies.

Lock. Every order is preceded by a lock check (``Db.heartbeat``): if another process took the lock
the engine stops (risk event ``lock_lost``, notification) and never submits or writes state again.
A model fit runs with a background heartbeat. A client id the Db already knows is never
resubmitted (risk event ``duplicate_client_id``). Candidates are recorded every tick (selected and
rejected with reasons); a selected one's ``status`` goes submitted -> filled / partial / unfilled
when its orders are terminal, and the "entry" notification is sent on fill.

Persistence and restart safety. One Db transaction (``Db.save_snapshot``) holds the risk state
(without flags), the open/closed positions and the engine state (portfolio cash/holdings, positions
being exited, orders in flight with their reference prices, entry groups, strategy owners, last
bar / funding hour / booked fill id). A snapshot is committed before every order is handed to the
broker, so a fill recorded by the broker always belongs to a known order. ``start()`` takes the
per-account Db lock (``EngineLocked`` if another process holds it), reloads that state, cancels every
order still open (the broker's working state is not persisted; the next tick re-plans), books the
fills recorded but not yet booked, resolves entry groups, checks holdings against positions (R18:
mismatch -> pause + notify) and never resubmits an existing client_id.

Model. ``model_fn(now)`` returns the strategies / keys / ranker to trade (a ``WindowModel``); the
default (``StoredModel``) fits ``fit_window`` on the ``train_months`` ending at the last closed bar
from the bar store (trade lists cached under ``var/research``), keeps it in
``var/models/live_<account>.pkl`` and refits every ``test_months`` (or when missing). A model with
no accepted expectancy cell trades nothing (logged, risk event ``no_model``). Positions keep being
exited by the strategy instance (params) that opened them, as in the walk-forward.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import pickle
import socket
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from luna.config import Settings
from luna.engine.backtest import _Market, _Signal
from luna.engine.feeds import FeedError, LiveFeed
from luna.engine.pipeline import EntryGroup, Pipeline
from luna.engine.walkforward import STRATEGIES, Window, WindowModel, WindowStrategy, _select, fit_window
from luna.execution.costs import CostModel
from luna.notify.heartbeat import ping
from luna.notify.telegram import Notifier, handle_command
from luna.portfolio.constructor import Selection
from luna.portfolio.portfolio import Portfolio, Position
from luna.ranking.expectancy import ExpectancyRanker
from luna.risk.engine import STALE_AFTER
from luna.risk.state import state_to_dict
from luna.store import models as m
from luna.store.db import Db
from luna.types import AccountKind, Candidate, ExitSpec, Instrument, Leg, OrderRequest, StopSpec

log = logging.getLogger("luna.live")

BAR = pd.Timedelta("15min")
TICK_OFFSET = pd.Timedelta(seconds=20)
MINUTE = pd.Timedelta(seconds=60)
LIVE_BUFFER_BARS = 96 * 120  # 120 days: indicator / regime warm-up (regime lookback 8640 bars)
LOCK_STALE_S = 300  # a lock not refreshed for 5 minutes (heartbeat every 60 s) is stale
FEED_NOTIFY_AFTER = 4
FEED_PAUSE_AFTER = 2
ENGINE_KEY = "engine"
_OPEN = ("NEW", "OPEN", "PARTIAL")
_FLAGS = ("kill_switch", "paused")
_HOLDINGS_TOL = 0.01  # R18 reconciliation tolerance (±1%)
URGENT = ("emergency_stop", "kill_switch")  # exit reasons retried every minute, as IOC


class EngineLocked(RuntimeError):
    """Another process holds this account's Db lock."""


# --- (de)serialization of the engine state ------------------------------------------------------


def _f(x: Any) -> Any:
    """Plain Python scalar (numpy scalars break JSON)."""
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def _ts(s: str | None) -> pd.Timestamp | None:
    return None if s is None else pd.Timestamp(s)


def cand_to_dict(c: Candidate) -> dict[str, Any]:
    return {
        "ts": m.ts_to_str(c.ts), "strategy": c.strategy, "key": c.key,
        "legs": [[leg.asset, _f(leg.weight)] for leg in c.legs], "direction": int(c.direction),
        "entry_ref": _f(c.entry_ref),
        "stop": {"kind": c.stop.kind, "level": _f(c.stop.level), "distance_frac": _f(c.stop.distance_frac)},
        "exit": {"max_hold_bars": int(c.exit.max_hold_bars), "take_profit_z": _f(c.exit.take_profit_z),
                 "trailing_atr_mult": _f(c.exit.trailing_atr_mult)},
        "regime": c.regime, "reason": c.reason,
        "features": {k: _f(v) for k, v in c.features.items()},
        "score": _f(c.score), "edge_bps": _f(c.edge_bps),
    }


def cand_from_dict(d: dict[str, Any]) -> Candidate:
    return Candidate(
        ts=pd.Timestamp(d["ts"]), strategy=d["strategy"], key=d["key"],
        legs=tuple(Leg(a, w) for a, w in d["legs"]), direction=d["direction"],
        entry_ref=d["entry_ref"], stop=StopSpec(**d["stop"]), exit=ExitSpec(**d["exit"]),
        regime=d["regime"], reason=d["reason"], features=dict(d["features"]),
        score=d["score"], edge_bps=d["edge_bps"],
    )


def _order_to_dict(o: OrderRequest) -> dict[str, Any]:
    d = m.order_to_row("", o, "", None)
    return {k: d[k] for k in ("client_id", "ts", "position_id", "leg_group", "symbol", "side", "qty",
                              "price", "tif", "intent")}


def candidate_record(c: Candidate, status: str, reason: str | None,
                     params: dict | None = None) -> dict[str, Any]:
    """A ``Db.record_candidates`` row with plain Python types."""
    d = cand_to_dict(c)
    return {
        "ts": c.ts, "strategy": c.strategy, "key": c.key, "direction": int(c.direction),
        "score": _f(c.score), "edge_bps": _f(c.edge_bps), "regime": c.regime,
        "selected": status != "rejected", "reject_reason": reason,
        "status": None if status == "rejected" else status,
        "features": d["features"], "legs": d["legs"], "stop": d["stop"], "exit": d["exit"],
        "params": params,
    }


# --- model ------------------------------------------------------------------------------------------


class StoredModel:
    """Default model provider: fit on the ``train_months`` ending at the last closed bar, cached in
    ``var/models/live_<account>.pkl`` and refit once it is ``test_months`` old (or missing)."""

    def __init__(self, settings: Settings, account: AccountKind, instruments: dict[str, Instrument],
                 path: Path | None = None) -> None:
        self.settings = settings
        self.account = account
        self.instruments = instruments
        default = Path(settings.var_dir) / "models" / f"live_{account}.pkl"
        self.path = Path(path) if path is not None else default

    def due(self, model: WindowModel | None, now: pd.Timestamp) -> bool:
        return model is None or now >= model.window.test_end

    def __call__(self, now: pd.Timestamp) -> WindowModel:
        if self.path.exists():
            try:
                with self.path.open("rb") as fh:
                    model = pickle.load(fh)
                if not self.due(model, now):
                    log.info("live model loaded from %s (train end %s)", self.path, model.window.train_end)
                    return model
            except Exception as e:  # noqa: BLE001 - a corrupt cache is simply refit
                log.warning("live model cache unreadable (%s); refitting", e)
        model = fit_live_model(self.settings, self.account, self.instruments, now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        with tmp.open("wb") as fh:
            pickle.dump(model, fh)
        os.replace(tmp, self.path)
        return model


def fit_live_model(
    settings: Settings, account: AccountKind, instruments: dict[str, Instrument], now: pd.Timestamp,
) -> WindowModel:
    """``fit_window`` on the ``train_months`` ending at the close of the last closed bar, from the
    bar store (trade lists of the window's keys over the full history, cached)."""
    from luna.data.store import BarStore
    from luna.report.walkforward import load_inputs
    from luna.research.tradelists import build_all_tradelists

    train_end = pd.Timestamp(now).floor("15min")
    cfg = settings.walkforward
    w = Window(train_end - pd.DateOffset(months=cfg.train_months), train_end, train_end,
               train_end + pd.DateOffset(months=cfg.test_months))
    inp = load_inputs(settings, BarStore(settings.var_dir), account, instruments, data_end=train_end)
    universe, pairs = _select(w, inp.panel, settings, account, inp.universe_panel)
    keys = {"momentum": list(universe), "mean_reversion": list(universe), "pairs": list(pairs.selected)}
    tl = build_all_tradelists(
        STRATEGIES, inp.panel, inp.funding, CostModel(settings.costs[account]), account, keys,
        band=settings.accounts[account].max_price_band, use_regime=True,
        cache_dir=Path(settings.var_dir) / "research",
    )
    tl.attrs["use_regime"] = True
    model = fit_window(w, tl, inp.panel, settings, account, funding=inp.funding,
                       universe_panel=inp.universe_panel)
    log.info("live model fitted: train %s..%s universe=%s pairs=%s params=%s", w.train_start, w.train_end,
             model.universe, model.pairs.selected, model.params)
    return model


# --- broker wrapper ---------------------------------------------------------------------------------


class _Submitter:
    """What the pipeline submits to. Before handing an order to the broker it (1) verifies that
    this process still holds the account lock (refreshing it; on loss the engine stops and nothing
    more is submitted), (2) commits a snapshot (the order is already registered as in flight) and
    (3) refuses a client_id the Db already knows (risk event ``duplicate_client_id``)."""

    def __init__(self, engine: LiveEngine) -> None:
        self.engine = engine

    def submit(self, order: OrderRequest) -> None:
        eng = self.engine
        now = eng._utc(eng.clock())
        if eng._lock_lost or not eng.db.heartbeat(eng.account, eng.owner):
            eng._on_lock_lost(now, f"submit of {order.client_id} refused")
            return
        if order.intent == "open":
            eng._own(order.position_id)
        eng._snapshot()
        if eng.db.order_status(eng.account, order.client_id) is not None:
            log.warning("client_id %s already known; not resubmitted", order.client_id)
            eng._event(now, "duplicate_client_id", f"{order.client_id} {order.intent} not resubmitted",
                       "skip")
            return
        eng.broker.submit(order)


# --- engine -----------------------------------------------------------------------------------------


class LiveEngine:
    """Implements ``EngineControl`` (Telegram /status /positions /pause /resume /kill)."""

    def __init__(
        self,
        account: AccountKind,
        settings: Settings,
        feed: LiveFeed,
        broker: Any,
        db: Db,
        notifier: Notifier,
        clock: Callable[[], pd.Timestamp],
        mode: Literal["paper", "live"] = "paper",
        *,
        instruments: dict[str, Instrument] | None = None,
        model_fn: Callable[[pd.Timestamp], Any] | None = None,
        model_due: Callable[[Any, pd.Timestamp], bool] | None = None,
        run_id: str | None = None,
        owner: str | None = None,
        http_client: Any = None,
    ) -> None:
        if mode not in ("paper", "live"):
            raise ValueError(f"unknown mode {mode!r}")
        if account not in ("perp", "spot"):
            raise ValueError(f"unknown account {account!r}")
        self.account: AccountKind = account
        self.settings = settings
        self.limits = settings.accounts[account]
        self.costs = CostModel(settings.costs[account])
        self.feed = feed
        self.broker = broker
        self.db = db
        self.notifier = notifier
        self.clock = clock
        self.mode = mode
        self._instruments = instruments
        self._model_fn = model_fn
        self._model_due = model_due
        stamp = f"{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%M%S}"
        self.run_id = run_id or f"{mode}_{account}_{stamp}_{uuid.uuid4().hex[:6]}"
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.http_client = http_client
        self.model: Any = None
        self._entries_enabled = False
        self._model_retry: pd.Timestamp | None = None
        self.pipe: Pipeline | None = None
        self.trades: list[dict] = []  # closed trades of this process (pipeline trade rows)
        self._owners: dict[str, dict[str, Any]] = {}  # position id -> {"name", "params"}
        self._owner_cache: dict[str, Any] = {}
        self._last_bar: pd.Timestamp | None = None
        self._last_funding_hour: pd.Timestamp | None = None
        self._last_fill_id = 0
        self._closed_pending: list[tuple[Position, pd.Timestamp, str]] = []
        self._data_misses = 0  # consecutive ticks without fresh data (feed error / stale / none)
        self._started = False
        self._lock_lost = False
        self._cand_ids: dict[str, int] = {}  # entry position id -> candidates row id (Db)
        self._stop = threading.Event()

    # --- helpers ------------------------------------------------------------------------------------

    @staticmethod
    def _utc(now) -> pd.Timestamp:
        now = pd.Timestamp(now)
        if now.tzinfo is None:
            raise ValueError("LiveEngine requires a tz-aware timestamp (UTC); got a naive one")
        return now.tz_convert("UTC")

    def _event(self, ts, rule: str, detail: str = "", action: str = "") -> None:
        self.db.record_risk_event(self.account, ts, rule, detail[:1000], action, run_id=self.run_id)

    def _notify(self, text: str) -> None:
        try:
            self.notifier.send(f"[{self.account}] {text}")
        except Exception as e:  # noqa: BLE001 - notifications never break the engine
            log.warning("notify failed: %s", type(e).__name__)

    @property
    def pf(self) -> Portfolio:
        assert self.pipe is not None, "engine not started"
        return self.pipe.pf

    def _set_flag(self, key: str, value: bool) -> None:
        self.db.set_flag(self.account, key, value)  # type: ignore[arg-type]
        self.pipe.state = dataclasses.replace(self.pipe.state, **{key: bool(value)})

    def _reload_flags(self, now: pd.Timestamp) -> None:
        was_killed = self.pipe.state.kill_switch
        flags = {k: bool(self.db.get_state(self.account, k, False)) for k in _FLAGS}
        self.pipe.state = dataclasses.replace(self.pipe.state, **flags)
        if self.pipe.state.kill_switch and not was_killed:  # set by someone else (Db flag)
            self._event(now, "kill_switch", "flag set externally", "flatten")
            self._mark_killed(now)

    def _mark_killed(self, now: pd.Timestamp) -> None:
        """Kill switch newly on: every open position exits as ``kill_switch`` (overriding other
        exit reasons), working post-only orders (entries and exits) are cancelled, and the urgent
        exit path re-flattens the positions as IOC (``tick``'s flatten / ``_urgent_exits``). The
        backtest's pipeline keeps its own (setdefault) semantics."""
        for pid in self.pf.positions:
            self.pipe.exiting[pid] = "kill_switch"
        self._cancel_working(now, lambda o: o.tif != "IOC")
        for pid in self.pf.positions:  # incl. positions registered by fills booked just now
            self.pipe.exiting[pid] = "kill_switch"

    # --- start / restart reconciliation -------------------------------------------------------------

    def start(self) -> None:
        """Acquire the lock, reload state, reconcile, drain stale Telegram commands, fit the model."""
        if not self.db.acquire_lock(self.account, self.owner, stale_after_s=LOCK_STALE_S):
            raise EngineLocked(f"another process holds the {self.account} lock")
        try:
            self._start()
        except BaseException:
            self.db.release_lock(self.account, self.owner)
            raise

    def _start(self) -> None:
        now = self._utc(self.clock())
        if self._instruments is None:
            from luna.instruments import load_instruments

            self._instruments = load_instruments(self.settings, self.account)
        if self._model_fn is None:
            stored = StoredModel(self.settings, self.account, self._instruments)
            self._model_fn, self._model_due = stored, stored.due
        config = {"account": self.limits.model_dump(),
                  "costs": self.settings.costs[self.account].model_dump()}
        self.db.start_run(self.run_id, self.account, self.mode, config, started_at=now)
        state = self.db.load_risk_state(self.account)
        aux = self.db.get_state(self.account, ENGINE_KEY)
        pf = Portfolio(self.account, self.limits.capital_usd)
        if aux is not None:
            p = aux["portfolio"]
            pf.cash = float(p["cash"])
            pf.holdings = {a: float(q) for a, q in p["holdings"].items()}
            pf.avg_entry = {a: float(q) for a, q in p["avg_entry"].items()}
            pf.marks = {a: float(q) for a, q in p["marks"].items()}
        for pos in self.db.open_positions(self.account):
            pf.positions[pos.id] = pos
        self.pipe = Pipeline(pf, state, self.account, self.limits, self.costs, self._instruments,
                             _Submitter(self), self.mode, keep_candidates=True, track_attempts=True)
        self.pipe.on_entry_resolved = self._entry_resolved
        if aux is not None:
            self._restore(aux)
        else:
            self._last_fill_id = self.db.last_fill_id(self.account)
        self._reconcile(now)
        if self.pipe.state.kill_switch:  # restarted with the kill switch on: keep flattening
            self._mark_killed(now)
        self._snapshot()
        self._drain_commands()
        self._started = True
        self._ensure_model(now, force=True)
        self._notify(f"engine started ({self.mode}); equity {self.pf.equity():.2f}, "
                     f"{len(self.pf.positions)} open positions")

    def _restore(self, aux: dict[str, Any]) -> None:
        pipe = self.pipe
        pipe.exiting = dict(aux.get("exiting", {}))
        pipe.entry_notional.update({k: float(v) for k, v in aux.get("entry_notional", {}).items()})
        for cid, d in aux.get("submitted", {}).items():
            pipe.submitted[cid] = (m.order_from_row(d["order"]), float(d["ref"]))
        for pid, d in aux.get("entry_groups", {}).items():
            sel = Selection(cand_from_dict(d["cand"]), float(d["gross_notional"]), float(d["risk_amount"]))
            pipe.entry_groups[pid] = EntryGroup(sel, set(d["assets"]), -1, set(d["filled"]), set(d["cids"]))
        self._owners = dict(aux.get("owners", {}))
        pipe.exit_attempts = {k: int(v) for k, v in aux.get("exit_attempts", {}).items()}
        self._cand_ids = {k: int(v) for k, v in aux.get("cand_ids", {}).items()}
        self._last_bar = _ts(aux.get("last_bar"))
        self._last_funding_hour = _ts(aux.get("last_funding_hour"))
        self._last_fill_id = int(aux.get("last_fill_id", 0))

    def _reconcile(self, now: pd.Timestamp) -> None:
        pipe = self.pipe
        for o in self.db.open_orders(self.account):
            self.broker.cancel(o.client_id)  # broker state is not persisted: the next tick re-plans
            self._event(now, "restart_cancel", f"{o.client_id} {o.tif} {o.intent}", "cancel")
        self._book_fills(now)
        pipe.settle(self._bar_of(now), frozenset())
        self._collect(now)
        if pipe.state.day is not None:
            day0 = pd.Timestamp(pipe.state.day, tz="UTC")
            n = self.db.count_orders_since(self.account, day0)
            if n > pipe.state.orders_today:
                pipe.state = dataclasses.replace(pipe.state, orders_today=n)
        # R18: account holdings must equal the sum of the open positions' quantities
        pos_sum: dict[str, float] = {}
        for p in self.pf.positions.values():
            for a, q in p.qty.items():
                pos_sum[a] = pos_sum.get(a, 0.0) + q
        bad = []
        for a in set(pos_sum) | set(self.pf.holdings):
            x, y = pos_sum.get(a, 0.0), self.pf.holdings.get(a, 0.0)
            if abs(x - y) > _HOLDINGS_TOL * max(abs(x), abs(y)):
                bad.append(f"{a}: positions {x} vs account {y}")
        if bad:
            self._set_flag("paused", True)
            self._event(now, "R18_reconciliation", "; ".join(bad), "pause")
            self._notify("reconciliation mismatch, trading paused: " + "; ".join(bad))

    def _drain_commands(self) -> None:
        """Consume (never execute) Telegram commands queued while the engine was down."""
        try:
            stale = self.notifier.commands()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram drain failed: %s", type(e).__name__)
            return
        if stale:
            log.info("ignored %d Telegram commands queued before start", len(stale))

    # --- model --------------------------------------------------------------------------------------

    def _ensure_model(self, now: pd.Timestamp, force: bool = False) -> None:
        due = force or self.model is None or (
            self._model_due(self.model, now) if self._model_due is not None else False)
        if not due or (self._model_retry is not None and now < self._model_retry and not force):
            return
        try:
            model = self._with_heartbeat(self._model_fn, now)
        except Exception as e:  # noqa: BLE001 - keep running (exits still managed) on a failed fit
            log.exception("model fit failed")
            self._event(now, "model_fit_failed", f"{type(e).__name__}: {e}", "keep previous model")
            self._model_retry = now + pd.Timedelta(hours=6)
            return
        self._model_retry = None
        self.model = model
        if self._started and not self.db.heartbeat(self.account, self.owner):  # a fit takes minutes
            self._on_lock_lost(now, "heartbeat after model fit")
        table = getattr(model, "table", None) if model is not None else None
        accepted = sum(1 for c in (table or {}).values() if getattr(c, "accepted", False))
        self._entries_enabled = model is not None and not (
            isinstance(getattr(model, "ranker", None), ExpectancyRanker) and accepted == 0)
        if not self._entries_enabled:
            log.warning("model has no accepted expectancy cell: managing positions, opening nothing")
            self._event(now, "no_model", "no accepted expectancy cell", "no entries")
            self._notify("model has no accepted cell: no new entries until the next refit")

    def _with_heartbeat(self, fn: Callable[[pd.Timestamp], Any], now: pd.Timestamp) -> Any:
        """Run a long call (model fit) while a background thread keeps the lock fresh."""
        if not self._started:
            return fn(now)
        done = threading.Event()

        def beat() -> None:
            while not done.wait(MINUTE.total_seconds()):
                try:
                    if not self.db.heartbeat(self.account, self.owner):
                        self._on_lock_lost(now, "heartbeat during model fit")
                        return
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat during model fit failed")

        th = threading.Thread(target=beat, name="luna-fit-heartbeat", daemon=True)
        th.start()
        try:
            return fn(now)
        finally:
            done.set()
            th.join()

    def _own(self, pid: str) -> None:
        """Remember which strategy instance (params) opened position ``pid``."""
        if pid in self._owners or self.model is None:
            return
        name = pid.split(":", 1)[0]
        for s in self.model.strategies:
            if s.name == name:
                self._owners[pid] = {"name": name, "params": dict(getattr(s, "params", {}))}
                return

    def _owner_of(self, pos: Position) -> Any:
        inner = {s.name: s for s in (self.model.strategies if self.model is not None else [])}
        spec = self._owners.get(pos.id)
        cur = inner.get(pos.strategy)
        if spec is None or (cur is not None and dict(getattr(cur, "params", {})) == spec["params"]):
            return cur
        ck = json.dumps(spec, sort_keys=True, default=str)
        if ck not in self._owner_cache:
            try:
                self._owner_cache[ck] = STRATEGIES[spec["name"]](spec["params"])
            except Exception as e:  # noqa: BLE001
                log.warning("cannot rebuild strategy %s for %s (%s)", spec, pos.id, e)
                self._owner_cache[ck] = cur
        return self._owner_cache[ck]

    def _strategies(self) -> list[Any]:
        model = self.model
        inner = {s.name: s for s in (model.strategies if model is not None else [])}
        keys = model.keys if model is not None else {}
        carried: dict[str, dict[str, tuple[Any, str]]] = {}
        for pid, pos in self.pf.positions.items():
            owner = self._owner_of(pos)
            if owner is None:
                log.warning("no strategy to manage %s (exits only by stop/kill)", pid)
                continue
            if owner is inner.get(pos.strategy) and pos.key in keys.get(pos.strategy, []):
                continue
            carried.setdefault(pos.strategy, {})[pid] = (owner, pos.key)
        names = [s.name for s in (model.strategies if model is not None else [])]
        names += [n for n in carried if n not in names]
        return [WindowStrategy(n, inner.get(n), carried.get(n, {})) for n in names]

    def _feed_assets(self) -> list[str]:
        want: set[str] = set(self.pf.holdings)
        for p in self.pf.positions.values():
            want |= {leg.asset for leg in p.legs}
        if self.model is not None:
            for ks in self.model.keys.values():
                for k in ks:
                    want |= set(k.split("/"))
        return sorted(a for a in want if a in self._instruments)

    # --- clock steps --------------------------------------------------------------------------------

    @staticmethod
    def _bar_of(now: pd.Timestamp) -> pd.Timestamp:
        """Open time of the newest bar closed at ``now``."""
        return now.floor("15min") - BAR

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("LiveEngine.start() must run first (lock, state, reconciliation)")

    def tick(self, now) -> None:
        now = self._utc(now)
        self._require_started()
        if self._lock_lost:
            return
        pipe = self.pipe
        self._reload_flags(now)
        self._ensure_model(now)
        if hasattr(self.feed, "assets"):
            self.feed.assets = self._feed_assets()  # type: ignore[attr-defined]
        try:
            buffers = self.feed.refresh(now)
        except FeedError as e:
            log.warning("feed error: %s", e)
            self._data_miss(now, "feed_error", str(e), "skip_tick")
            self._book_and_settle(now)
            self._commit(now)
            return
        self._book_and_settle(now)
        panel = {a: df[df.index + BAR <= now] for a, df in buffers.items()
                 if a in self._instruments and len(df)}
        panel = {a: df for a, df in panel.items() if len(df)}
        if not panel:
            self._data_miss(now, "no_data", "feed returned no closed bars", "skip")
            self._commit(now)
            return
        idx = pd.DatetimeIndex([])
        for df in panel.values():
            idx = idx.union(df.index)
        idx = pd.DatetimeIndex(idx).sort_values()
        mkt = _Market(panel, idx)
        i = len(idx) - 1
        t = idx[i]

        def price(a: str) -> float:
            j = mkt.col.get(a)
            v = float(mkt.last[i, j]) if j is not None else math.nan
            return v if math.isfinite(v) and v > 0 else float(self.pf.marks[a])

        self._funding(now, price)
        if self._last_bar is not None and t <= self._last_bar:
            if self.pf.holdings:
                self.pf.mark({a: price(a) for a in self.pf.holdings})
            self._data_miss(now, "stale_data", f"no new closed bar since {self._last_bar}", "no decision")
            self._commit(now)
            return
        if now - (t + BAR) <= STALE_AFTER:
            self._data_misses = 0  # a fresh bar
        else:  # decided anyway (exits); entries are vetoed by R16
            self._data_miss(now, "stale_data", f"newest bar {t} is stale at {now}", "R16 blocks entries")
        bars = 1 if self._last_bar is None else max(1, int((t - self._last_bar) / BAR))
        self._last_bar = t  # decided at most once, also if the process dies mid-decision
        signals, firing = self._signals(panel, idx, i)
        before = pipe.state
        pipe.portfolio_step(t, {a: price(a) for a in self.pf.holdings}, now=now)
        self._sync_flags(now, before)
        pipe.exit_signals(i, mkt, signals, bars)
        pipe.flatten(t, price, now=now, adjust=self._urgent_ioc)
        if firing and self._entries_enabled and self.model is not None:
            pipe.entries(i, t, firing, mkt, signals, self.model.ranker, now=now)
        self._commit(now, equity=True)

    def minute_check(self, now) -> None:
        now = self._utc(now)
        self._require_started()
        if self._lock_lost:
            return
        self._reload_flags(now)
        self._book_and_settle(now)
        try:
            mids = self.feed.mids()
        except FeedError as e:
            log.warning("mids unavailable: %s", e)
            mids = {}

        def price(a: str) -> float:
            v = mids.get(a)
            return float(v) if v is not None and math.isfinite(v) and v > 0 else float(self.pf.marks[a])

        self._emergency(now, mids)
        self._urgent_exits(now, price)
        self._funding(now, price)
        self._commands()
        self._commit(now)

    # --- steps ------------------------------------------------------------------------------------

    def _signals(self, panel, idx, i) -> tuple[dict, list[tuple[str, str]]]:
        signals: dict[tuple[str, str], _Signal] = {}
        firing: list[tuple[str, str]] = []
        keys = self.model.keys if self.model is not None else {}
        for strat in self._strategies():
            ks = [k for k in keys.get(strat.name, []) if all(a in panel for a in k.split("/"))]
            for key, sf in strat.signals(panel, ks).items():
                rows = sf.entry.index.get_indexer(idx)
                signals[(strat.name, key)] = _Signal(strat, sf, rows)
                r = int(rows[i])
                if r >= 0 and sf.entry.iloc[r] != 0:
                    firing.append((strat.name, key))
        return signals, firing

    def _data_miss(self, now: pd.Timestamp, rule: str, detail: str, action: str) -> None:
        """A tick without fresh data (feed error, no bars, no new or a stale bar), spec §7.4:
        4 in a row -> notify; 2 in a row with open positions -> pause + notify."""
        self._data_misses += 1
        self._event(now, rule, detail, action)
        if self._data_misses == FEED_NOTIFY_AFTER:
            self._notify(f"feed failing: {self._data_misses} ticks in a row without fresh data ({rule})")
        if (self._data_misses >= FEED_PAUSE_AFTER and self.pf.positions
                and not self.pipe.state.paused):
            self._set_flag("paused", True)
            self._event(now, "feed_pause", f"{self._data_misses} ticks without data with open positions",
                        "pause")
            self._notify("no market data with open positions: trading paused (/resume to clear)")

    def _book_fills(self, now: pd.Timestamp) -> None:
        pipe = self.pipe
        for fid, f, pid in self.db.fills_after(self.account, self._last_fill_id):
            if f.client_id in pipe.submitted:
                pipe.apply_fill(f)
            else:
                self._set_flag("paused", True)
                self._event(now, "R18_reconciliation", f"fill {fid} of unknown order {f.client_id} ({pid})",
                            "pause")
                self._notify(f"unknown fill {f.client_id}: trading paused")
            self._last_fill_id = fid

    def _book_and_settle(self, now: pd.Timestamp) -> None:
        try:
            self.broker.poll(now)
        except Exception as e:  # noqa: BLE001 - booking reads the Db below anyway
            log.warning("broker poll failed: %s", e)
        self._book_fills(now)
        open_cids = {cid for cid in self.pipe.submitted
                     if self.db.order_status(self.account, cid) in _OPEN}
        self.pipe.settle(self._bar_of(now), open_cids)

    def _funding(self, now: pd.Timestamp, price: Callable[[str], float]) -> None:
        """Perp funding once per hour, at the first tick / minute check of the hour."""
        if self.account != "perp":
            return
        hour = now.floor("h")
        if self._last_funding_hour is not None and hour <= self._last_funding_hour:
            return
        self._last_funding_hour = hour
        pf = self.pf
        if not pf.holdings:
            return
        try:
            rates = self.feed.funding_rates()
        except FeedError as e:
            log.warning("funding rates unavailable (%s): unknown this hour", e)
            rates = {}
        for a in list(pf.holdings):
            rate = rates.get(a)
            rate = float(rate) if rate is not None and math.isfinite(rate) else None
            cash0 = pf.cash
            mark = price(a)
            pf.apply_funding(a, rate, mark, hour)
            self.db.record_funding(self.account, hour, a, pf.holdings.get(a), rate, pf.cash - cash0,
                                   run_id=self.run_id)

    def _emergency(self, now: pd.Timestamp, mids: dict[str, float]) -> None:
        pf, pipe = self.pf, self.pipe
        threshold = self.limits.emergency_stop_frac * pf.equity()
        if not pf.positions or not threshold > 0:
            return
        hit = False
        for pid, pos in pf.positions.items():
            legs = [(a, q) for a, q in pos.qty.items() if q != 0.0]
            if not legs or not all(mids.get(a, 0) > 0 for a, _ in legs):
                continue
            pnl = pos.net_pnl() + sum(q * (mids[a] - pos.entry_ref_price[a]) for a, q in legs)
            if pnl <= -threshold and pipe.exiting.get(pid) != "emergency_stop":
                pipe.exiting[pid] = "emergency_stop"
                pipe.event(self._bar_of(now), "emergency_stop", pid, ",".join(a for a, _ in legs),
                           f"loss {-pnl:.4f} >= {threshold:.4f} at live mids")
                self._notify(f"emergency stop {pid}: loss {-pnl:.2f}")
                hit = True
        if hit:  # a resting (post-only) exit of a stopped position is replaced, not waited for
            urgent = {pid for pid, r in pipe.exiting.items() if r == "emergency_stop"}
            self._cancel_working(
                now, lambda o: o.intent != "open" and o.position_id in urgent and o.tif != "IOC")

    def _urgent_exits(self, now: pd.Timestamp, price: Callable[[str], float]) -> None:
        """Every minute: (re-)flatten positions exiting for an emergency stop or the kill switch
        that have no working exit order (an IOC that was rejected, cancelled or partly filled is
        retried with a fresh client id)."""
        only = {pid for pid, r in self.pipe.exiting.items()
                if r in URGENT and pid in self.pf.positions}
        if only:
            self.pipe.flatten(self._bar_of(now), price, now=now, adjust=self._urgent_ioc, only=only)

    def _cancel_working(self, now: pd.Timestamp, which: Callable[[OrderRequest], bool]) -> None:
        """Cancel the working orders selected by ``which`` and book / settle them (an urgent exit
        replaces a resting post-only exit instead of waiting for it to expire)."""
        hit = [cid for cid, (o, _) in self.pipe.submitted.items() if which(o)]
        for cid in hit:
            self.broker.cancel(cid)
        if hit:
            self._book_and_settle(now)

    def _urgent_ioc(self, o: OrderRequest, mid: float) -> OrderRequest:
        """Emergency / kill exits on spot go out as IOC limits within the band (not post-only)."""
        if o.tif == "IOC" or self.pipe.exiting.get(o.position_id) not in URGENT:
            return o
        sign = 1.0 if o.side == "buy" else -1.0
        return dataclasses.replace(o, tif="IOC", limit_price=mid * (1.0 + sign * self.limits.max_price_band))

    def _sync_flags(self, now: pd.Timestamp, before) -> None:
        st = self.pipe.state
        if st.kill_switch and not before.kill_switch:
            self.db.set_flag(self.account, "kill_switch", True)
            self._notify(f"DRAWDOWN KILL SWITCH: equity {self.pf.equity():.2f} vs peak {before.peak_equity}; "
                         "flattening (/resume to clear)")
            self._mark_killed(now)
        if st.paused and not before.paused:
            self.db.set_flag(self.account, "paused", True)
            self._notify("trading paused (reconciliation)")
        if st.entries_blocked_day is not None and st.entries_blocked_day != before.entries_blocked_day:
            self._notify(f"daily loss limit: no new entries today (equity {self.pf.equity():.2f})")

    def _commands(self) -> None:
        try:
            cmds = self.notifier.commands()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram commands failed: %s", type(e).__name__)
            return
        for cmd, arg in cmds:
            try:
                reply = handle_command(cmd, arg, self)
            except Exception as e:  # noqa: BLE001 - a command never crashes the loop
                log.exception("command %s failed", cmd)
                reply = f"{cmd} failed: {type(e).__name__}"
            self._notify(reply)

    def heartbeat(self, now=None) -> None:
        """Refresh the account lock (stop on loss) and ping the dead-man's switch."""
        now = self._utc(now if now is not None else self.clock())
        if self._lock_lost or not self.db.heartbeat(self.account, self.owner):
            self._on_lock_lost(now, "heartbeat")
            return
        if self.http_client is not None:
            ping(self.http_client, self.settings.healthcheck_url)

    def _on_lock_lost(self, now: pd.Timestamp, where: str) -> None:
        """Another process took the lock: stop at once, never submit or write state again."""
        first = not self._lock_lost
        self._lock_lost = True
        self.stop()
        if first:
            log.critical("lost the %s lock (%s); stopping", self.account, where)
            try:
                self._event(now, "lock_lost", f"{self.owner}: {where}", "stop")
            except Exception:  # noqa: BLE001
                log.exception("could not record lock_lost")
            self._notify("lost the process lock: stopping (no further orders)")

    # --- records / snapshot -------------------------------------------------------------------------

    def _entry_resolved(self, pid: str, status: str, g: EntryGroup) -> None:
        """An entry's orders are all terminal: store the candidate's outcome; notify fills."""
        cid = self._cand_ids.pop(pid, None)
        if cid is not None:
            self.db.set_candidate_status(cid, status)
        if status in ("filled", "partial"):
            c = g.sel.cand
            pos = self.pf.positions.get(pid)
            qty = ", ".join(f"{a} {q:+.6g}" for a, q in (pos.qty.items() if pos else ()) if q)
            self._notify(f"entry {status}: {c.strategy} {c.key} dir {c.direction} ({qty}); "
                         f"stop {c.stop.level:.6g}, risk {g.sel.risk_amount:.2f}")

    def _collect(self, now: pd.Timestamp) -> None:
        """Move new pipeline records (candidates, events, closed trades) to the Db / notifier."""
        if self._lock_lost:
            return  # another process owns the account: write nothing more
        pipe = self.pipe
        if pipe.cand_rows:
            rows = [candidate_record(c, r["status"], r["reason"], p)
                    for c, r, p in zip(pipe.cand_objs, pipe.cand_rows, pipe.cand_params, strict=True)]
            ids = self.db.record_candidates(self.run_id, rows, account=self.account)
            for pid, g in pipe.entry_groups.items():
                if g.cand_row >= 0:
                    self._cand_ids[pid] = ids[g.cand_row]
            pipe.cand_rows.clear()
            pipe.cand_objs.clear()
            pipe.cand_params.clear()
            pipe._params_of.clear()
            for g in pipe.entry_groups.values():
                g.cand_row = -1
        for ev in pipe.events:
            detail = " ".join(str(x) for x in (ev["position_id"], ev["asset"], ev["detail"]) if x)
            self._event(ev["ts"], ev["kind"], detail, "")
        pipe.events.clear()
        for tr in pipe.trades:
            self.trades.append(tr)
            self._notify(f"exit {tr['strategy']} {tr['key']} ({tr['exit_reason']}): "
                         f"net {tr['pnl_net']:.2f} = {tr['r_multiple']:.2f}R")
        pipe.trades.clear()
        self._closed_pending.extend(self.pf.closed)
        self.pf.closed.clear()
        self.pf.funding_log.clear()

    def _commit(self, now: pd.Timestamp, equity: bool = False) -> None:
        if self._lock_lost:
            return
        self._collect(now)
        if equity:
            pf = self.pf
            self.db.record_equity(
                self.account, now, float(pf.equity()), _f(pf.gross_leverage()), float(pf.open_risk()),
                positions=[{"id": p.id, "key": p.key, "qty": {a: _f(q) for a, q in p.qty.items()}}
                           for p in pf.positions.values()],
                run_id=self.run_id)
        self._snapshot()

    def _snapshot(self) -> None:
        if self._lock_lost:
            return  # another process owns the account state now
        pipe = self.pipe
        pf = pipe.pf
        risk = {k: v for k, v in state_to_dict(pipe.state).items() if k not in _FLAGS}
        aux = {
            "portfolio": {"cash": _f(pf.cash), "holdings": {a: _f(q) for a, q in pf.holdings.items()},
                          "avg_entry": {a: _f(q) for a, q in pf.avg_entry.items()},
                          "marks": {a: _f(q) for a, q in pf.marks.items()}},
            "exiting": dict(pipe.exiting),
            "submitted": {cid: {"order": _order_to_dict(o), "ref": _f(ref)}
                          for cid, (o, ref) in pipe.submitted.items()},
            "entry_groups": {pid: {"cand": cand_to_dict(g.sel.cand),
                                   "gross_notional": _f(g.sel.gross_notional),
                                   "risk_amount": _f(g.sel.risk_amount), "assets": sorted(g.assets),
                                   "filled": sorted(g.filled), "cids": sorted(g.cids)}
                             for pid, g in pipe.entry_groups.items()},
            "entry_notional": {k: _f(v) for k, v in pipe.entry_notional.items()},
            "owners": {pid: o for pid, o in self._owners.items()
                       if pid in pf.positions or pid in pipe.entry_groups},
            "last_bar": m.ts_to_str(self._last_bar) if self._last_bar is not None else None,
            "last_funding_hour": (m.ts_to_str(self._last_funding_hour)
                                  if self._last_funding_hour is not None else None),
            "last_fill_id": int(self._last_fill_id),
            "exit_attempts": dict(pipe.exit_attempts or {}),
            "cand_ids": {pid: i for pid, i in self._cand_ids.items() if pid in pipe.entry_groups},
        }
        positions: list[tuple[Position, Any, str | None, pd.Timestamp | None]] = [
            (p, "open", None, None) for p in pf.positions.values()]
        positions += [(p, "closed", reason, ts) for p, ts, reason in self._closed_pending]
        self.db.save_snapshot(self.account, {**risk, ENGINE_KEY: aux}, positions, run_id=self.run_id)
        self._closed_pending.clear()

    # --- EngineControl ------------------------------------------------------------------------------

    def status(self) -> str:
        pf, st = self.pf, self.pipe.state
        return (f"{self.account} {self.mode}: equity {pf.equity():.2f} (cash {pf.cash:.2f}), "
                f"{len(pf.positions)} positions, gross lev {pf.gross_leverage():.2f}, "
                f"open risk {pf.open_risk():.2f}; kill_switch={st.kill_switch} paused={st.paused}; "
                f"day start {st.day_start_equity}, peak {st.peak_equity}, orders today {st.orders_today}; "
                f"last bar {self._last_bar}; entries {'on' if self._entries_enabled else 'off'}")

    def positions(self) -> str:
        if not self.pf.positions:
            return "no open positions"
        lines = []
        for p in self.pf.positions.values():
            qty = ", ".join(f"{a} {q:+.6g}" for a, q in p.qty.items() if q)
            lines.append(f"{p.id}: {qty}; stop {p.stop.level:.6g}; risk {p.risk_amount:.2f}; "
                         f"held {p.bars_held} bars; realized {p.net_pnl():.2f}"
                         + (f"; exiting ({self.pipe.exiting[p.id]})" if p.id in self.pipe.exiting else ""))
        return "\n".join(lines)

    def pause(self) -> None:
        self._set_flag("paused", True)
        self._event(self._utc(self.clock()), "pause", "telegram /pause", "pause")

    def resume(self) -> None:
        """Clear kill switch and pause; reset the peak to the current equity (EngineControl)."""
        now = self._utc(self.clock())
        self._set_flag("kill_switch", False)
        self._set_flag("paused", False)
        self._data_misses = 0
        # positions being flattened by the kill switch go back to strategy management
        killed = {pid for pid, r in self.pipe.exiting.items() if r == "kill_switch"}
        self._cancel_working(now, lambda o: o.intent != "open" and o.position_id in killed)
        for pid in killed:
            self.pipe.exiting.pop(pid, None)
        self.pipe.state = dataclasses.replace(self.pipe.state, peak_equity=float(self.pf.equity()))
        self._event(now, "resume", f"peak reset to {self.pf.equity():.4f}", "resume")
        self._snapshot()

    def kill(self) -> None:
        """Set the kill switch and flatten every position now (exits still pass the risk engine)."""
        now = self._utc(self.clock())
        self._set_flag("kill_switch", True)
        self._event(now, "kill", "telegram /kill", "flatten")
        self._book_and_settle(now)  # book fills already made (e.g. a strategy exit just filled)
        self._mark_killed(now)
        try:
            mids = self.feed.mids()
        except FeedError:
            mids = {}
        self._urgent_exits(now, lambda a: mids.get(a) or float(self.pf.marks[a]))
        self._commit(now)

    # --- loop -------------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        """Final snapshot and lock release (clean shutdown)."""
        try:
            if self._started and not self._lock_lost:
                self._snapshot()
        finally:
            self.db.release_lock(self.account, self.owner)  # no-op unless we still own it

    @staticmethod
    def next_tick(now: pd.Timestamp) -> pd.Timestamp:
        nxt = now.floor("15min") + TICK_OFFSET
        return nxt if nxt > now else nxt + BAR

    def _safe(self, fn: Callable[[pd.Timestamp], None], now: pd.Timestamp) -> None:
        try:
            fn(now)
        except Exception as e:  # noqa: BLE001 - the loop never dies on one bad cycle
            log.exception("%s failed", fn.__name__)
            try:
                self._event(now, "engine_error", f"{fn.__name__}: {type(e).__name__}: {e}", "")
            except Exception:  # noqa: BLE001
                log.exception("could not record engine_error")
            self._notify(f"{fn.__name__} error: {type(e).__name__}")

    def run_forever(self) -> None:
        """Tick at every 15-minute boundary + 20 s, minute check every 60 s, until ``stop()``."""
        now = self._utc(self.clock())
        nxt_tick = self.next_tick(now)
        nxt_min = now
        while not self._stop.is_set():
            now = self._utc(self.clock())
            if now >= nxt_tick or now >= nxt_min:
                self._safe(self.heartbeat, now)  # first: never trade on a lock we lost
                if self._stop.is_set():
                    break
            if now >= nxt_tick:
                self._safe(self.tick, now)
                nxt_tick = self.next_tick(now)
            if now >= nxt_min and not self._stop.is_set():
                self._safe(self.minute_check, now)
                nxt_min = now + MINUTE
            wait = (min(nxt_tick, nxt_min) - self._utc(self.clock())).total_seconds()
            if wait > 0:
                self._stop.wait(wait)
