"""Event-loop book engine (spec S-1.5, S-4.1, S-4.2, S-5.4, S-6.1, S-6.2, S-7.1-S-7.4, S-8.1, S-13.4, K11,
§14 L-norm; AMB-19).

``run(arm, inputs, window, h_star, model=None, execution="E_T", layer="L-norm") -> BookRun``. The window
[lo, end) starts flat at E = 1 (L-norm) and ends flat. ``guard.check_outcome_window(lo, end)`` runs once,
before anything is read. Everything is explicit input (``EngineInputs``): per-symbol market data, the BTC
hedge instrument, and a point-in-time decision provider (gates, h-hat(t), beta-hat(t), 24 h quote volume,
the already-gated candidate rows of ``arms.arm_scores`` and the staleness flag). No I/O, no wall-clock.

Events (heap; same timestamp: funding settlement, 15-minute tick, fills in booking order, terminal check,
decision, terminal unwind):
* **tick** (every 15 min from lo): marks every open position (mark-price bar ending at the tick, else the
  last-price bar, else the previous price event, AMB-19 (e)), equity, ``RiskLayer.on_tick``; its forced exits
  (kill, leg stop) fill per S-4.2; a kill also closes the hedge and cancels unfilled entries (AMB-19 (c)).
* **funding**: each settlement of each held instance: ``-q * M * r`` (S-4.6); missing / invalid records and
  marks are reported in ``integrity`` (never invented); the DEC-29 gap test runs per closed episode.
* **decision** (00:05 / 08:05 / 16:05 with t + 15 min before the unwind): provider view -> ``pre_decision``
  -> ``arm_scores`` -> ``constructor.targets`` -> ``hedge.decide`` -> orders. Fill timing: decision exits,
  neutrality closes, resizes, entries, U6 exits and hedge trades at t_e = t + 15 min (E_M: maker-first for
  the non-forced leg orders); kill, leg-stop and ineligibility exits on the first 1-minute bar >= trigger +
  1 min (S-4.2, AMB-19 (a)). A halted exit waits (``execution.halted_exit``).
* **terminal check** at a held instance's cessation (A: the official settlement time): A settles (DEC-43),
  B / C -> ``INVALID RUN — terminal settlement not reconstructible``.
* **terminal unwind** at the last reference minute before the end (end - 1 min): every leg and the hedge
  exit taker (S-5.3 costs); a halted leg / a still-waiting exit with no executable bar of its instance before
  the end -> ``INVALID RUN`` (S-5.4). The run stops at the first INVALID RUN.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

import numpy as np
import pandas as pd

from luna.v7 import guard, windows
from luna.v7.arms import arm_scores
from luna.v7.book import EPS, Leg, Order, hedge
from luna.v7.book.constructor import targets
from luna.v7.book.execution import (
    INVALID_NO_EXIT,
    ExecView,
    Fill,
    NoTrade,
    Outcome,
    Pending,
    RunInvalid,
    Trade,
    forced_at,
    halted_exit,
    maker_at,
    settle,
    taker_at,
)
from luna.v7.book.ledger import Ledger, Position
from luna.v7.book.risk import RiskActions, RiskLayer
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.data.terminal import TerminalEvent
from luna.v7.refs import FundingSchedule, funding_schedule, restrict
from luna.v7.timeutil import MIN, D
from luna.v7.universe import GateRow, assert_pit

EV_FUNDING, EV_TICK, EV_FILL, EV_TERMINAL, EV_DECISION, EV_UNWIND = range(6)
TICK = 15 * MIN
FILL_DELAY = FROZEN.decision.fill_delay_min * MIN
FORCED_TIMING = frozenset({"kill", "leg_stop", "ineligible"})  # S-4.2 timing; U6: t + 15 min (S-7.3)
HEDGE_SYMBOL = "BTCUSDT"
E0 = 1.0  # L-norm (§14)
ARMS = ("A0", "A1", "A2", "A3")
SCORE_COLS = ["symbol", "side", "rank_key", "passes_hurdle"]


@dataclass(frozen=True)
class Instrument:
    """One symbol's raw inputs: R5 1m bars (open_ts, o, c, vol_base), R3 15m bars (open_ts, o, h, l, c,
    vol_base, known_at), R4 15m mark bars (open_ts, c), raw funding records (funding_ts, rate,
    interval_h_explicit, known_at, optional mark_at_settle), instances, the classified terminal event per
    instance idx (required when a held position reaches that instance's cessation) and, for E_M, aggTrades
    (ts, price, qty)."""

    symbol: str
    bars1m: pd.DataFrame
    bars15: pd.DataFrame
    mark15: pd.DataFrame
    funding: pd.DataFrame
    insts: list[Instance]
    terminals: Mapping[int, TerminalEvent] = field(default_factory=dict)
    aggtrades: pd.DataFrame | None = None


@dataclass(frozen=True)
class DecisionView:
    """Point-in-time decision inputs at t (the provider's responsibility; every gate row is PIT-asserted).
    ``gates``: GateRow per held and candidate symbol; ``h_hat``/``betas``/``qv24``: h-hat(t), beta-hat(t), the
    trailing 24 h quote volume (USD) per symbol (None = missing); ``cands``: the gated candidate rows for
    ``arms.arm_scores`` (symbol, side, gate_ok, fl30_r8, cost_side, ...); ``stale``: S-7.3 staleness."""

    t: int
    gates: Mapping[str, GateRow]
    h_hat: Mapping[str, float | None]
    betas: Mapping[str, float | None]
    qv24: Mapping[str, float | None]
    cands: pd.DataFrame
    stale: bool


class ScoreModel(Protocol):
    """ML arms: ``predict`` adds the ``FoldModel.predict`` columns of the decision's fold model; ``ml_star``
    selects A1's classifier family."""

    ml_star: str

    def predict(self, t: int, cands: pd.DataFrame) -> pd.DataFrame: ...


@dataclass(frozen=True)
class EngineInputs:
    instruments: Mapping[str, Instrument]
    btc: Instrument
    view: Callable[[int], DecisionView]
    now_ns: int  # passed to the guard
    auth: guard.HoldoutAuthorization | None = None


@dataclass(frozen=True)
class BookRun:
    """One arm's executed book over one window (input of Task 15 accounting; schema in the 14b report)."""

    arm: str
    h_star: int
    execution: str
    layer: str
    window: tuple[int, int]
    fills: pd.DataFrame
    marks: pd.DataFrame
    positions: pd.DataFrame
    funding_flows: pd.DataFrame
    daily: pd.DataFrame
    decisions: pd.DataFrame
    integrity: tuple[str, ...]
    invalid: str | None
    invalid_detail: str | None = None

    @property
    def valid(self) -> bool:
        return self.invalid is None


@dataclass
class _Token:
    fill: Fill
    pid: str
    kind: str  # open | reduce | exit | hedge
    cancelled: bool = False


class _Sim:
    def __init__(self, arm: str, inputs: EngineInputs, window: tuple[int, int], h_star: int,
                 model: ScoreModel | None, execution: str) -> None:
        self.arm, self.inp, self.h_star, self.model, self.exe = arm, inputs, h_star, model, execution
        self.lo, self.end = window
        self.unwind_ts = self.end - MIN  # S-1.5 / S-8.1: the last reference minute before the end
        self.upu = float(FROZEN.deploy.E_ref)  # L-norm: liquidity cap (and E_M sizes) at E_ref (§14)
        self.risk = RiskLayer()
        self.state = self.risk.initial_state(E0, self.lo)
        self.led = Ledger(E0, self.lo)
        self.heap: list = []
        self.seq = 0
        self.views: dict[tuple[str, int], ExecView] = {}
        self.scheds: dict[tuple[str, int], FundingSchedule] = {}
        self.tokens: dict[str, list[_Token]] = {}
        self.integrity: list[str] = []
        self.decisions: list[dict] = []
        self.invalid: str | None = None
        self.invalid_detail: str | None = None
        self.fresh: dict[str, float] = {}
        self.kill_done = False
        self.n_hedge = 0
        self.notes: list[str] = []  # fill-time notes of the current decision
        self.open_meta: dict[str, tuple[int, float | None]] = {}  # pid -> (decision t, beta-hat(t))

    # --- infrastructure -----------------------------------------------------------------------------
    def push(self, ts: int, kind: int, payload: object) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (int(ts), kind, self.seq, payload))

    def instrument(self, sym: str) -> Instrument:
        if sym == HEDGE_SYMBOL:
            return self.inp.btc
        if sym not in self.inp.instruments:
            raise ValueError(f"no instrument data for {sym}")
        return self.inp.instruments[sym]

    def view_of(self, sym: str, t: int) -> ExecView | None:
        """The instrument's current instance at t (PIT, ``current_instance``), restricted (E10, S-1.5)."""
        ins = self.instrument(sym)
        cur = current_instance(ins.insts, t)
        if cur is None:
            return None
        key = (sym, cur.idx)
        if key not in self.views:
            x = restrict(ins.bars1m, ins.bars15, ins.mark15, ins.funding, cur, self.end)
            self.views[key] = ExecView.build(sym, x, ins.aggtrades if self.exe == "E_M" else None)
        return self.views[key]

    def sched(self, p: Position) -> FundingSchedule:
        key = (p.symbol, p.view.inst.idx)
        if key not in self.scheds:
            self.scheds[key] = funding_schedule(p.view.x, self.end - MIN)
        return self.scheds[key]

    def fail(self, r: RunInvalid) -> None:
        if self.invalid is None:
            self.invalid = r.reason
            self.invalid_detail = f"{r.symbol}#{r.instance}@{r.ts}" + (f": {r.detail}" if r.detail else "")

    def legs(self) -> list[Leg]:
        out = []
        for p in sorted(self.led.pos.values(), key=lambda x: x.symbol):
            if p.role == "leg" and p.q != 0 and p.p_last is not None:
                out.append(Leg(p.symbol, p.side, abs(p.q) * p.p_last, p.avg_entry, p.entry_decision_ts, p.pid,
                               p.pending_exit))
        return out

    def leg_pos(self, sym: str) -> Position:
        for p in self.led.pos.values():
            if p.role == "leg" and p.symbol == sym:
                return p
        raise RuntimeError(f"internal: no held leg {sym}")

    def hedge_pos(self) -> Position | None:
        return next((p for p in self.led.pos.values() if p.role == "hedge"), None)

    # --- scheduling -----------------------------------------------------------------------------------
    def cancel(self, pid: str) -> None:
        for tk in self.tokens.pop(pid, []):
            tk.cancelled = True

    def schedule(self, outs: list[Outcome], pid: str, kind: str, now: int, p: Position | None) -> None:
        for o in outs:
            if isinstance(o, Fill):
                if o.ts < now:
                    raise RuntimeError("internal: a fill before its scheduling time")
                tk = _Token(o, pid, kind)
                self.tokens.setdefault(pid, []).append(tk)
                self.push(o.ts, EV_FILL, tk)
            elif isinstance(o, NoTrade):
                self.notes.append(f"no_trade_{o.reason}:{o.trade.symbol}")
            elif isinstance(o, Pending):
                if kind != "exit" or p is None:
                    self.notes.append(f"unfilled_{kind}:{o.trade.symbol}")
                    continue
                r = halted_exit(p.view, o)
                if isinstance(r, Fill):
                    self.schedule([r], pid, kind, now, p)
                elif r is None:
                    p.waiting = True  # no executable bar of the instance before the end (INVALID at unwind)
                # "terminal": the instance ceases first; the terminal check at its cessation settles it

    def exit(self, p: Position, reason: str, forced: bool, trigger_ts: int | None, t_e: int,
             h_hat: float | None = None) -> None:
        self.cancel(p.pid)
        p.pending_exit = reason
        tr = Trade(p.symbol, -1 if p.q > 0 else 1, abs(p.q), None, False, reason, p.pid)
        if forced and reason in FORCED_TIMING:
            if trigger_ts is None:
                raise RuntimeError("internal: forced exit without a trigger time")
            outs: list[Outcome] = [forced_at(p.view, tr, trigger_ts)]
        elif forced or self.exe == "E_T" or p.role == "hedge":
            outs = [taker_at(p.view, tr, t_e)]
        else:
            outs = maker_at(p.view, tr, t_e, h_hat, self.upu)
        self.schedule(outs, p.pid, "exit", min(t_e, trigger_ts if trigger_ts is not None else t_e), p)

    def open_position(self, f: Fill, pid: str, role: str, side: int, decision_ts: int, beta: float | None,
                      view: ExecView) -> Position:
        p = Position(pid, f.symbol, role, view, side, decision_ts, f.ts, beta)  # type: ignore[arg-type]
        self.led.pos[pid] = p
        s = self.sched(p)
        i = int(np.searchsorted(s.ts, f.ts, "right"))
        if i < len(s.ts) and s.ts[i] < self.end:
            self.push(int(s.ts[i]), EV_FUNDING, (pid, i))
        last = view.inst.last_trade_ts
        if last is not None and last < self.end:
            ev = self.instrument(f.symbol).terminals.get(view.inst.idx)
            a_evt = ev is not None and ev.klass == "A" and ev.settle_ts is not None
            at = int(ev.settle_ts) if a_evt else last  # type: ignore[union-attr, arg-type]
            if at < f.ts:
                raise ValueError(f"{pid}: opened after its instance's official settlement time")
            self.push(at, EV_TERMINAL, pid)
        return p

    # --- event handlers -------------------------------------------------------------------------------
    def on_fill(self, tk: _Token) -> None:
        if tk.cancelled:
            return
        f = tk.fill
        self.tokens[tk.pid].remove(tk)
        p = self.led.pos.get(tk.pid)
        if p is None:
            if tk.kind not in ("open", "hedge"):
                return
            side = f.direction
            view = self.views[(f.symbol, f.instance)]
            p = self.open_position(f, tk.pid, "hedge" if tk.kind == "hedge" else "leg", side,
                                   *self.open_meta[tk.pid], view)
        btc = self.view_of(HEDGE_SYMBOL, f.ts)
        bref = btc.ref(f.ts) if btc is not None else None
        f = _with(f, beta=p.beta if tk.kind == "open" else None, btc_ref=None if bref is None else bref[0])
        closes = tk.kind == "exit" and f.qty >= abs(p.q) * (1 - 1e-9)
        if tk.kind == "hedge" and abs(p.q + f.dq) <= 1e-12 * max(abs(p.q), f.qty):
            closes = True
        self.led.apply_fill(f, p, closes)
        if p.q == 0:
            self.close_episode(p, f.ts)

    def close_episode(self, p: Position, ts: int) -> None:
        why = self.sched(p).check(p.opened_ts, ts)
        if why is not None:
            self.integrity.append(f"{why}:{p.pid}")
        if p.pending_exit == "leg_stop":
            self.state = self.risk.start_cooldown(self.state, p.symbol, ts)
        self.cancel(p.pid)
        del self.led.pos[p.pid]

    def on_funding(self, ts: int, pid: str, i: int) -> None:
        p = self.led.pos.get(pid)
        if p is None:
            return
        s = self.sched(p)
        mark = s.mark(i)
        if not s.usable(i):
            self.integrity.append(f"funding_invalid:{pid}@{ts}")
        elif mark is None:
            self.integrity.append(f"mark_missing:{pid}@{ts}")
        else:
            self.led.apply_funding(ts, p, float(s.rate[i]), mark)
        if i + 1 < len(s.ts) and s.ts[i + 1] < self.end:
            self.push(int(s.ts[i + 1]), EV_FUNDING, (pid, i + 1))

    def on_tick(self, ts: int) -> None:
        prices: dict[str, tuple[float, str]] = {}
        fresh: dict[str, float] = {}
        for pid, p in self.led.pos.items():
            m, src = p.view.mark(ts)
            if m is None:
                if p.p_last is None:
                    raise RuntimeError("internal: an open position without a price event")
                m, src = p.p_last, "stale"
            elif p.role == "leg":
                fresh[p.symbol] = m
            prices[pid] = (m, src)
        btc = self.view_of(HEDGE_SYMBOL, ts)
        eq = self.led.mark(ts, prices, btc.mark(ts)[0] if btc is not None else None)
        self.fresh = fresh
        self.state, acts = self.risk.on_tick(self.state, ts, eq, self.legs(), fresh)
        self.forced(acts, ts + FILL_DELAY)
        if ts % D == 0 and ts > self.led.day_start_ts:
            self.led.close_day(ts, eq)
        if ts + TICK < self.unwind_ts:
            self.push(ts + TICK, EV_TICK, None)

    def forced(self, acts: RiskActions, t_e: int) -> None:
        if acts.killed and not self.kill_done:
            self.kill_done = True
            for pid, tks in list(self.tokens.items()):  # no re-entry: unfilled entries are cancelled
                for tk in tks:
                    if tk.kind == "open":
                        tk.cancelled = True
                        self.integrity.append(f"entry_cancelled_by_kill:{pid}")
        for fx in acts.forced:
            p = self.leg_pos(fx.symbol)
            if p.pending_exit is None:
                self.exit(p, fx.reason, True, fx.trigger_ts, t_e)
        h = self.hedge_pos()
        if acts.killed and h is not None and h.pending_exit is None:  # "exit everything" incl. the hedge
            self.exit(h, "kill", True, acts.t, t_e)

    def on_terminal(self, ts: int, pid: str) -> None:
        p = self.led.pos.get(pid)
        if p is None or p.q == 0:
            return
        self.cancel(pid)
        tr = Trade(p.symbol, -1 if p.q > 0 else 1, abs(p.q), None, False, p.pending_exit or "terminal", pid)
        out = settle(p.view, tr, self.instrument(p.symbol).terminals.get(p.view.inst.idx))
        if isinstance(out, RunInvalid):
            self.fail(out)
            return
        if out.ts != ts:
            raise RuntimeError("internal: settlement time differs from the terminal check time")
        p.pending_exit = p.pending_exit or "terminal"
        self.led.apply_fill(out, p, True)
        self.close_episode(p, ts)

    def on_unwind(self) -> None:
        u = self.unwind_ts
        order = sorted(self.led.pos.values(), key=lambda x: (x.role != "leg", x.symbol))
        for p in order:
            if p.waiting:
                self.fail(RunInvalid(INVALID_NO_EXIT, p.symbol, p.view.inst.idx, u, "exit never executable"))
                return
            if any(not tk.cancelled for tk in self.tokens.get(p.pid, [])):
                self.integrity.append(f"fill_after_unwind_cancelled:{p.pid}")  # E_M remainder past the end
            self.cancel(p.pid)
            p.pending_exit = p.pending_exit or "terminal_unwind"
            tr = Trade(p.symbol, -1 if p.q > 0 else 1, abs(p.q), None, False, "terminal_unwind", p.pid)
            out = taker_at(p.view, tr, u)
            if isinstance(out, Pending):
                r = halted_exit(p.view, out)
                out = r if isinstance(r, Fill) else None  # type: ignore[assignment]
            if not isinstance(out, Fill) or out.ts >= self.end:
                self.fail(RunInvalid(INVALID_NO_EXIT, p.symbol, p.view.inst.idx, u, "halted at the unwind"))
                return
            self.on_fill_now(out, p)
        self.led.close_day(u, self.led.equity(), final=True)

    def on_fill_now(self, f: Fill, p: Position) -> None:
        btc = self.view_of(HEDGE_SYMBOL, f.ts)
        bref = btc.ref(f.ts) if btc is not None else None
        self.led.apply_fill(_with(f, btc_ref=None if bref is None else bref[0]), p, True)
        self.close_episode(p, f.ts)

    # --- decisions --------------------------------------------------------------------------------------
    def on_decision(self, t: int) -> None:
        self.notes = []
        t_e = t + FILL_DELAY
        v = self.inp.view(t)
        if v.t != t:
            raise ValueError("the decision view belongs to another time")
        for g in v.gates.values():
            assert_pit(g, t)
        legs = self.legs()
        held = {lg.symbol for lg in legs}
        marks = {s: m for s, m in self.fresh.items() if s in held}
        self.state, acts = self.risk.pre_decision(self.state, t, legs, v.gates, v.h_hat, marks, v.stale)
        scores = self.scores(t, v.cands)
        halted = {lg.symbol: self.leg_pos(lg.symbol).view.halted(t_e) for lg in legs}
        for sym in sorted(set(scores["symbol"].astype(str)) - held):
            cv = self.view_of(sym, t)
            halted[sym] = True if cv is None else cv.halted(t_e)
        eq = self.led.equity()
        tg = targets(t=t, equity=eq, held=tuple(legs), scores=scores, gates=v.gates, risk=acts, halted=halted,
                     betas=v.betas, quote_vol_24h=v.qv24, h_star_h=float(self.h_star), usd_per_unit=self.upu)
        for o in tg.orders:
            self.order(o, t, t_e, v)
        hd = self.hedge_step(t, t_e, tg.post, v, eq, acts.killed)
        self.decisions.append(dict(t=t, equity=eq, stale=acts.stale, killed=acts.killed, paused=acts.paused,
                                   n_orders=len(tg.orders), orders=tuple(f"{o.kind}:{o.reason}:{o.symbol}"
                                                                         for o in tg.orders),
                                   notes=tg.notes + acts.notes + tuple(self.notes), hedge_reason=hd.reason,
                                   hedge_target=hd.target, beta_book=hd.beta_book))

    def scores(self, t: int, cands: pd.DataFrame) -> pd.DataFrame:
        if len(cands) == 0:
            return pd.DataFrame({c: pd.Series(dtype=object) for c in SCORE_COLS})
        ml_star = None
        if self.arm != "A0":
            if self.model is None:
                raise RuntimeError("internal: ML arm without a model")
            cands = self.model.predict(t, cands)
            ml_star = self.model.ml_star
        return arm_scores(self.arm, cands, float(self.h_star), ml_star)

    def order(self, o: Order, t: int, t_e: int, v: DecisionView) -> None:
        sym = o.symbol
        if o.kind == "open":
            if o.notional <= EPS * E0:
                self.notes.append(f"zero_open:{sym}")
                return
            cv = self.view_of(sym, t)
            if cv is None:
                raise RuntimeError(f"internal: entry of {sym} without a current instance")
            pid = f"{sym}#{cv.inst.idx}@{t}"
            self.open_meta[pid] = (t, v.betas.get(sym))
            tr = Trade(sym, o.side, None, o.notional, True, o.reason, pid)
            outs = [taker_at(cv, tr, t_e)] if self.exe == "E_T" else maker_at(cv, tr, t_e, v.h_hat.get(sym),
                                                                                self.upu)
            self.schedule(outs, pid, "open", t_e, None)
            return
        p = self.leg_pos(sym)
        if o.kind == "reduce" and o.target > EPS * E0:
            leg_n = abs(p.q) * p.p_last  # type: ignore[operator]
            tr = Trade(sym, -o.side, abs(p.q) * o.notional / leg_n, None, False, o.reason, p.pid)
            outs = [taker_at(p.view, tr, t_e)] if self.exe == "E_T" else maker_at(p.view, tr, t_e,
                                                                                 v.h_hat.get(sym), self.upu)
            self.schedule(outs, p.pid, "reduce", t_e, None)
            return
        self.exit(p, o.reason, o.forced, o.trigger_ts, t_e, v.h_hat.get(sym))  # close (or reduce to 0)

    def hedge_step(self, t: int, t_e: int, post: Mapping[str, float], v: DecisionView, eq: float,
                   killed: bool) -> hedge.HedgeDecision:
        h = self.hedge_pos()
        cur = h.q * h.p_last if h is not None and h.p_last is not None else 0.0
        hd = hedge.decide(post, v.betas, eq, current=cur, killed=killed)
        if not hd.trade or hd.target is None:
            return hd
        if h is not None and h.pending_exit is not None:
            self.notes.append("hedge_exit_pending")
            return hd
        bv = self.view_of(HEDGE_SYMBOL, t)
        if bv is None or (h is not None and h.view is not bv):
            raise ValueError("BTC hedge instance missing or changed while hedged (E10)")
        if bv.halted(t_e):  # S-5.4 / S-7.1: no order of any kind while halted
            self.notes.append("hedge_halted")
            return hd
        if hd.target == 0.0:
            if h is not None:
                self.exit(h, "hedge", False, None, t_e)
            return hd
        r = bv.ref(t_e)
        if r is None:
            self.notes.append("hedge_price_missing")
            return hd
        q0 = h.q if h is not None else 0.0
        dq = hd.target / r[0] - q0
        if dq == 0.0:
            return hd
        opening = abs(q0 + dq) > abs(q0) or (q0 + dq) * q0 < 0
        if h is None:
            self.n_hedge += 1
            pid = f"{HEDGE_SYMBOL}#{bv.inst.idx}@hedge{self.n_hedge}"
            self.open_meta[pid] = (t, None)
        else:
            pid = h.pid
        tr = Trade(HEDGE_SYMBOL, 1 if dq > 0 else -1, abs(dq), None, opening, "hedge", pid)
        self.schedule([taker_at(bv, tr, t_e)], pid, "hedge", t_e, None)
        return hd

    # --- main loop --------------------------------------------------------------------------------------
    def run(self) -> None:
        self.push(self.lo, EV_TICK, None)
        for t in windows.decision_times(self.lo, self.end):
            if int(t) + FILL_DELAY < self.unwind_ts:
                self.push(int(t), EV_DECISION, None)
        self.push(self.unwind_ts, EV_UNWIND, None)
        while self.heap and self.invalid is None:
            ts, kind, _, payload = heapq.heappop(self.heap)
            if kind == EV_TICK:
                self.on_tick(ts)
            elif kind == EV_FUNDING:
                self.on_funding(ts, *payload)  # type: ignore[misc]
            elif kind == EV_FILL:
                self.on_fill(payload)  # type: ignore[arg-type]
            elif kind == EV_TERMINAL:
                self.on_terminal(ts, payload)  # type: ignore[arg-type]
            elif kind == EV_DECISION:
                self.on_decision(ts)
            else:
                self.on_unwind()
                break


def _with(f: Fill, **kw) -> Fill:
    return replace(f, **kw)


def _frame(rows: list[dict], cols: list[str] | None = None) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols or [])


def run(arm: str, inputs: EngineInputs, window: tuple[int, int], h_star: int, model: ScoreModel | None = None,
        execution: str = "E_T", layer: str = "L-norm") -> BookRun:
    """Run one arm's executed book over ``window`` (see the module docstring)."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    if (arm == "A0") != (model is None):
        raise ValueError("A0 takes no model; A1 / A2 / A3 need one (S-7.2)")
    if h_star not in FROZEN.horizons_h:
        raise ValueError(f"h_star must be one of {FROZEN.horizons_h}")
    if execution not in ("E_T", "E_M"):
        raise ValueError(f"unknown execution model {execution!r}")
    if layer != "L-norm":
        raise ValueError("only the L-norm layer runs here (L-265 / L-10k: Task 17)")
    lo, end = int(window[0]), int(window[1])
    if lo % TICK or end % MIN or end - lo <= 2 * FILL_DELAY:
        raise ValueError("the window must start on the 15-minute grid and span more than 30 min")
    guard.check_outcome_window(lo, end, auth=inputs.auth, now_ns=inputs.now_ns)
    sim = _Sim(arm, inputs, (lo, end), h_star, model, execution)
    sim.run()
    led = sim.led
    return BookRun(
        arm=arm, h_star=h_star, execution=execution, layer=layer, window=(lo, end),
        fills=_frame(led.fills), marks=_frame(led.marks), positions=_frame(led.positions),
        funding_flows=_frame(led.funding), daily=_frame(led.daily), decisions=_frame(sim.decisions),
        integrity=tuple(sim.integrity), invalid=sim.invalid, invalid_detail=sim.invalid_detail,
    )


__all__ = ["BookRun", "DecisionView", "EngineInputs", "Instrument", "ScoreModel", "run"]
