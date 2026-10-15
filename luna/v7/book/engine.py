"""Event-loop book engine (spec S-1.5, S-4.1, S-4.2, S-5.4, S-6.1, S-6.2, S-7.1-S-7.4, S-8.1, S-13.4, K11,
§14 L-norm; AMB-19).

``run(arm, inputs, window, h_star, model=None, execution="E_T", layer="L-norm") -> BookRun``. The window
[lo, end) starts flat at E = 1 (L-norm) and ends flat. ``guard.check_outcome_window(lo, end)`` runs once,
before anything is read. Everything is explicit input (``EngineInputs``): per-symbol market data, the BTC
hedge instrument, and a point-in-time decision provider (gates, h-hat(t), beta-hat(t), 24 h quote volume,
the already-gated candidate rows of ``arms.arm_scores`` and the staleness flag). No I/O, no wall-clock.

Events (heap; same timestamp: funding settlement, 15-minute tick, fills in booking order, terminal check,
one-sided check, decision, terminal unwind):
* **tick** (every 15 min from lo): marks every open position (mark-price bar ending at the tick, else the
  last-price bar of the same period, S-4.6; nothing else: a held leg without either -> ``INVALID RUN —
  required leg mark missing``, round-2 override 3 / DK-70; only a HALTED leg keeps the previous price
  event, ``mark_source`` ``stale``, AMB-29 (a): a stopped sub-case, normative amendment required), equity,
  ``RiskLayer.on_tick``; its forced exits (kill, leg stop) fill per S-4.2; a kill also closes the hedge and
  cancels unfilled entries and in-flight hedge opens (AMB-19 (c), DK-78). After the tick's fills and terminal
  checks, the **one-sided check** (AMB-16 (d), user decision 2026-10-15): if holdable legs remain on exactly
  one side, they all exit with the S-4.2 timing (reason ``one_sided_flatten``; ``on_neutral``), and an
  in-flight hedge open is cancelled (DK-78).
* **required exits** (round-2 override 2 / DK-69): a non-halted exit (decision, risk / forced, U6, flatten,
  hedge) or reduce (drift, ``risk_gross``; controller ruling R42) with no 1-minute bar within the 60-minute
  data-gap window -> ``INVALID RUN — required exit without a reference bar within the 60-minute data-gap
  window`` (detail: symbol#instance@time), as an event at the order's due time (cancelled with the order);
  never carried to a later bar. A HALTED reduce is skipped (``unfilled_reduce``, S-5.4 no resizes while
  halted). A gap-rule fill is booked at the substitute bar's open time (DK-10), so funding and B(e) follow
  that time.
* **E_M resting exits** (DK-77): a leg's exit counts as a resting maker order (replaceable by a forced exit,
  shown active to the risk layer) only inside [post, post + 30 min) of a limit actually posted.
* **BTC price required** (AMB-21 (b), user decision 2026-10-15): a hedge trade due at t_e with no BTC
  reference (not halted), a held hedge without a BTC mark / last-price bar at a tick, or B(e) missing at a
  tick with a leg held or at a leg fill -> ``INVALID RUN — required BTC reference price missing``; when BTC
  is not required (flat, no hedge trade due), nothing is invalidated.
* **funding**: each settlement of each held instance: ``-q * M * r`` (S-4.6); missing / invalid records and
  marks are reported in ``integrity`` (never invented); the DEC-29 gap test runs per closed episode.
* **decision** (00:05 / 08:05 / 16:05 with t + 15 min before the unwind): provider view -> ``pre_decision``
  -> ``arm_scores`` -> ``constructor.targets`` -> ``hedge.decide`` -> orders. Fill timing: decision exits,
  drift resizes, entries, U6 exits, ``risk_gross`` reduces (taker) and hedge trades at t_e = t + 15 min
  (E_M: maker-first for the non-forced leg orders); kill, leg-stop, ineligibility, empty-side
  ``neutrality`` and ``one_sided_flatten`` exits on the first 1-minute bar >= trigger + 1 min (S-4.2,
  AMB-19 (a), AMB-28). A halted exit waits (``execution.halted_exit``).
* **terminal check** at a held instance's cessation (A: the official settlement time): A settles (DEC-43),
  B / C -> ``INVALID RUN — terminal settlement not reconstructible``.
* **terminal unwind** at the last reference minute before the end (end - 1 min): every leg and the hedge
  exit taker (S-5.3 costs); a halted leg / a still-waiting exit with no executable bar of its instance before
  the end -> ``INVALID RUN`` (S-5.4). The run stops at the first INVALID RUN.

Equity layers (§14, EC-6): the public ``run`` is the L-norm path only and accepts no exchange filters (no
parameter for them; any other layer is refused). The USD layers L-265 / L-10k run through ``_run`` with a
``Lots`` sizer supplied by ``luna.v7.deploy`` (E_T only, AMB-23): entry, reduce and hedge quantities are
rounded by the sizer at the fill price; an infeasible leg / reduce is not traded; if the S-7.4 hedge the book
requires is infeasible, the decision's entries are forbidden and the hedge is re-decided without them. Full
closes (decision, forced, terminal) are never filtered. Every decision row records ``n_post`` (legs in the
post-decision book), ``post`` / ``prices`` (that book and P(t + 15 min), the D1 report inputs), and in a USD
layer ``constructible`` / ``infeasible`` (D1).
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

import numpy as np
import pandas as pd

from luna.v7 import guard, k9, windows
from luna.v7.arms import arm_scores
from luna.v7.book import EPS, Leg, Order, hedge
from luna.v7.book.constructor import NON_NEUTRAL, Targets, targets
from luna.v7.book.execution import (
    INVALID_BTC,
    INVALID_MARK,
    INVALID_NO_EXIT,
    MAKER_WINDOW,
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
    maker_post,
    settle,
    taker_at,
)
from luna.v7.book.ledger import (
    DAILY_COLS,
    FILL_COLS,
    FUNDING_COLS,
    MARK_COLS,
    POSITION_COLS,
    Ledger,
    Position,
)
from luna.v7.book.risk import RiskActions, RiskLayer
from luna.v7.constants import FROZEN
from luna.v7.data.instances import Instance, current_instance
from luna.v7.data.terminal import TerminalEvent
from luna.v7.refs import FundingSchedule, funding_schedule, restrict
from luna.v7.timeutil import MIN, D
from luna.v7.universe import GateRow, assert_pit

EV_FUNDING, EV_TICK, EV_FILL, EV_TERMINAL, EV_NEUTRAL, EV_DECISION, EV_UNWIND = range(7)
TICK = 15 * MIN
FILL_DELAY = FROZEN.decision.fill_delay_min * MIN
# S-4.2 timing (U6: t + 15 min, S-7.3); one_sided_flatten and the constructor's empty-side neutrality close:
# AMB-16 (d), user decision 2026-10-15 (AMB-28)
FORCED_TIMING = frozenset({"kill", "leg_stop", "ineligible", "one_sided_flatten", "neutrality"})
HEDGE_SYMBOL = "BTCUSDT"
B_ACCOUNTING = "BTC reference B(e) required for canonical accounting"  # AMB-21 (b) detail
E0 = 1.0  # L-norm (§14)
ARMS = ("A0", "A1", "A2", "A3")
SCORE_COLS = ["symbol", "side", "rank_key", "passes_hurdle", "vetoed"]


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


class Lots(Protocol):
    """USD-layer order sizer (``luna.v7.deploy``): the exchange-rounded quantity of an order of ``qty`` at
    ``price`` for ``symbol`` with the filters applicable at decision ``t``; None = infeasible (not traded)."""

    def size(self, symbol: str, qty: float, price: float, t: int) -> float | None: ...


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
    invalid_ts: int | None = None  # the time of the RunInvalid event (structured; EC-11 A0 availability)
    equity0: float = E0  # starting equity (book units; USD in L-265 / L-10k)
    filter_provenance: tuple[str, ...] = ()  # USD layers: provenance of every supplied filter (S-14.1)
    filters_missing: tuple[str, ...] = ()  # USD layers: symbols excluded (never traded) for lack of filters
    # produced by a research entry point (``runner.run_book``): its DT4 record is always a research record,
    # refused from a dirty working tree (DK-114 / red-team m10; ``runrecord.run_record``)
    research: bool = False

    @property
    def valid(self) -> bool:
        return self.invalid is None

    @property
    def non_neutral_decisions(self) -> int:
        """DK-53 diagnostic (reporting only): decisions whose post-decision book ends outside the S-7.1
        neutrality band after the gross cuts (constructor note ``non_neutral_post``)."""
        d = self.decisions
        if d is None or len(d) == 0 or "notes" not in d.columns:
            return 0
        return int(sum(NON_NEUTRAL in tuple(n) for n in d["notes"]))


@dataclass
class _Token:
    fill: Fill | None  # None for an "invalid" token
    pid: str
    kind: str  # open | reduce | exit | hedge | invalid
    cancelled: bool = False
    # kind "invalid": a required order (exit / reduce / hedge) with no bar within 60 min (DK-69, R42): the run
    # becomes INVALID at the order's due time, unless the order is cancelled first (exit replaced, position
    # settled, unwind)
    invalid: RunInvalid | None = None


class _Sim:
    def __init__(self, arm: str, inputs: EngineInputs, window: tuple[int, int], h_star: int,
                 model: ScoreModel | None, execution: str, e0: float = E0, upu: float | None = None,
                 lots: Lots | None = None) -> None:
        self.arm, self.inp, self.h_star, self.model, self.exe = arm, inputs, h_star, model, execution
        self.lo, self.end = window
        self.unwind_ts = self.end - MIN  # S-1.5 / S-8.1: the last reference minute before the end
        # L-norm: liquidity cap (and E_M sizes) at E_ref USD per unit of E (§14); USD layers: 1
        self.upu = float(FROZEN.deploy.E_ref) if upu is None else float(upu)
        self.e0 = float(e0)
        self.lots = lots
        self.risk = RiskLayer()
        self.state = self.risk.initial_state(self.e0, self.lo)
        self.led = Ledger(self.e0, self.lo)
        self.heap: list = []
        self.seq = 0
        self.views: dict[tuple[str, int], ExecView] = {}
        self.scheds: dict[tuple[str, int], FundingSchedule] = {}
        self.tokens: dict[str, list[_Token]] = {}
        self.integrity: list[str] = []
        self.decisions: list[dict] = []
        self.invalid: str | None = None
        self.invalid_detail: str | None = None
        self.invalid_ts: int | None = None
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
            self.invalid_ts = int(r.ts)

    def btc_missing(self, ts: int, what: str) -> None:
        """AMB-21 (b) (user decision 2026-10-15): a BTC price required at ``ts`` is missing -> INVALID RUN
        (never an exception, never a stale / interpolated BTC price)."""
        bv = self.view_of(HEDGE_SYMBOL, ts)
        self.fail(RunInvalid(INVALID_BTC, HEDGE_SYMBOL, bv.inst.idx if bv is not None else -1, ts,
                             f"{what} missing at {ts}"))

    def btc_ref(self, ts: int) -> float | None:
        """B(ts): the BTC reference price P(ts) (S-5.2 rule) of its current instance; None if missing."""
        bv = self.view_of(HEDGE_SYMBOL, ts)
        r = bv.ref(ts) if bv is not None else None
        return None if r is None else float(r[0])

    def legs(self, now: int) -> list[Leg]:
        """Held legs at ``now``. A leg whose exit rests as an E_M maker limit at ``now`` is passed as active
        (``pending_exit`` None): a forced exit replaces it with a taker exit (S-6.2: forced exits are always
        taker). Outside the resting window (before the post, or once the remainder has gone taker / into a
        halted S-5.4 wait) the leg is pending and is never re-exited (DK-77)."""
        out = []
        for p in sorted(self.led.pos.values(), key=lambda x: x.symbol):
            if p.role == "leg" and p.q != 0 and p.p_last is not None:
                pend = None if p.maker_resting(now) else p.pending_exit
                out.append(Leg(p.symbol, p.side, abs(p.q) * p.p_last, p.avg_entry, p.entry_decision_ts, p.pid,
                               pend))
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
            if isinstance(o, RunInvalid):
                # a required exit / reduce of a NON-HALTED instrument without a bar within 60 min
                # (override 2 / DK-69; reduces incl. risk_gross: controller ruling R42): INVALID RUN at its
                # due time (lazy: an order cancelled before then, e.g. a resting maker exit replaced by a
                # forced exit, never invalidates); a halted reduce stays a Pending -> ``unfilled_reduce``
                tk = _Token(None, pid, "invalid", invalid=o)
                self.tokens.setdefault(pid, []).append(tk)
                self.push(max(int(o.ts), now), EV_FILL, tk)
                continue
            if isinstance(o, Fill):
                if o.ts < now:
                    raise RuntimeError("internal: a fill before its scheduling time")
                tk = _Token(o, pid, kind)
                self.tokens.setdefault(pid, []).append(tk)
                self.push(o.ts, EV_FILL, tk)
            elif isinstance(o, NoTrade):
                self.notes.append(f"no_trade_{o.reason}:{o.trade.symbol}")
                if o.reason == "after_unwind":  # an E_M order still resting at the unwind is cancelled
                    self.integrity.append(f"order_cancelled_at_unwind:{pid}")
            elif isinstance(o, Pending):
                if kind != "exit" or p is None:
                    self.notes.append(f"unfilled_{kind}:{o.trade.symbol}")
                    continue
                if o.terminal:  # the instance ceases inside the gap window: the terminal check settles it
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
        p.maker_rest = None
        tr = Trade(p.symbol, -1 if p.q > 0 else 1, abs(p.q), None, False, reason, p.pid)
        if forced and reason in FORCED_TIMING:
            if trigger_ts is None:
                raise RuntimeError("internal: forced exit without a trigger time")
            outs: list[Outcome] = [forced_at(p.view, tr, trigger_ts)]
        elif forced or self.exe == "E_T" or p.role == "hedge":
            outs = [taker_at(p.view, tr, t_e)]
        else:
            outs = maker_at(p.view, tr, t_e, h_hat, self.upu, self.unwind_ts)
            post = maker_post(p.view, t_e, h_hat)
            if post is not None:  # DK-77: only a limit actually posted rests, for W (cut at the unwind)
                p.maker_rest = (post, min(post + MAKER_WINDOW, self.unwind_ts))
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
        self.tokens[tk.pid].remove(tk)
        if tk.invalid is not None:  # a required order due now without a bar within 60 min (DK-69, R42)
            self.fail(tk.invalid)
            return
        f = tk.fill
        if f is None:
            raise RuntimeError("internal: a fill token without a fill")
        p = self.led.pos.get(tk.pid)
        if p is None and tk.kind not in ("open", "hedge"):
            return
        bref = self.btc_ref(f.ts)
        is_leg = p.role == "leg" if p is not None else tk.kind == "open"
        if bref is None and is_leg:  # B(e) of a leg fill (S-8.3 mkt_c; AMB-21 (b))
            self.btc_missing(f.ts, B_ACCOUNTING)
            return
        if p is None:
            side = f.direction
            view = self.views[(f.symbol, f.instance)]
            p = self.open_position(f, tk.pid, "hedge" if tk.kind == "hedge" else "leg", side,
                                   *self.open_meta[tk.pid], view)
        f = _with(f, beta=p.beta if tk.kind == "open" else None, btc_ref=bref)
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
                if p.role == "hedge":  # AMB-21 (b): a held hedge is marked at every tick, no stale BTC carry
                    self.btc_missing(ts, "BTC mark required for hedge marking")
                    return
                if p.p_last is None:
                    raise RuntimeError("internal: an open position without a price event")
                if not self.mark_exempt(p, ts):  # round-2 override 3 (DK-70): no stale leg mark
                    self.fail(RunInvalid(INVALID_MARK, p.symbol, p.view.inst.idx, ts,
                                         f"no mark-price or last-price 15-minute close at tick {ts} "
                                         "(not halted; DK-70)"))
                    return
                # AMB-29 (a) (normative amendment required; sub-case STOPPED, current code kept): a HALTED
                # held leg without any 15m close at the tick: no frozen rule supplies its mark -> the previous
                # price event is carried (it moves no P&L)
                m, src = p.p_last, "stale"
            elif p.role == "leg":
                fresh[p.symbol] = m
            prices[pid] = (m, src)
        btc = self.view_of(HEDGE_SYMBOL, ts)
        bmark = btc.mark(ts)[0] if btc is not None else None
        if bmark is None and any(p.role == "leg" for p in self.led.pos.values()):
            self.btc_missing(ts, B_ACCOUNTING)  # B(e) at the tick of a held leg (S-8.3 mkt_c; AMB-21 (b))
            return
        eq = self.led.mark(ts, prices, bmark)
        self.fresh = fresh
        self.state, acts = self.risk.on_tick(self.state, ts, eq, self.legs(ts), fresh)
        self.forced(acts, ts + FILL_DELAY)
        if ts % D == 0 and ts > self.led.day_start_ts:
            self.led.close_day(ts, eq)
        self.push(ts, EV_NEUTRAL, None)  # after this tick's fills and terminal checks (AMB-16 (d))
        if ts + TICK < self.unwind_ts:
            self.push(ts + TICK, EV_TICK, None)

    @staticmethod
    def mark_exempt(p: Position, ts: int) -> bool:
        """AMB-29 (a) (stopped sub-case of DK-70): the leg is HALTED at the tick (S-5.4: no volume > 0 1m bar
        in [ts - 60 min, ts)). Only then may a missing mark keep the current (stale) code. A ceased instance
        that is not (yet) halted gets no exemption (fail closed, fix round 1): DEC-43 supplies no mark."""
        return p.view.halted(ts)

    def on_neutral(self, ts: int) -> None:
        """AMB-16 (d) (user decision 2026-10-15), at every 15-minute tick after its fills: if held legs remain
        on exactly one side, the ENTIRE book is flattened: every remaining leg and the S-7.4 BTC hedge exit as
        forced exits with the S-4.2 timing (first 1m bar after the tick + 1 min, taker; halted -> S-5.4
        pending; reason ``one_sided_flatten``); a resting E_M maker exit is replaced by the forced exit
        (S-6.2), as the kill does. The minimum hold does not block it and nothing is entered to keep K. A leg
        whose exit is already issued (``pending_exit``, incl. a resting E_M exit) is not holdable. A side with
        an entry order still in flight (ordered for t + 15 min, or resting in an E_M maker window) is not
        empty (the book is being built); once those orders have filled or lapsed, a one-sided result is
        flattened at the next tick (AMB-28)."""
        holdable: dict[int, list[Position]] = {1: [], -1: []}
        for p in self.led.pos.values():
            if p.role == "leg" and p.q != 0 and p.pending_exit is None:
                holdable[p.side].append(p)
        alive = {s: bool(holdable[s]) for s in (1, -1)}
        for pid, tks in self.tokens.items():
            for tk in tks:
                if tk.kind == "open" and tk.fill is not None and not tk.cancelled and pid not in self.led.pos:
                    alive[tk.fill.direction] = True
        if alive[1] == alive[-1] or not holdable[1 if alive[1] else -1]:
            return  # balanced, empty, or the surviving side is only being built (nothing held to flatten)
        self.cancel_hedge_open("one_sided_flatten")  # DK-78: never opened after the flatten
        for p in sorted(self.led.pos.values(), key=lambda x: (x.role != "leg", x.symbol)):
            if p.q != 0 and (p.pending_exit is None or p.maker_resting(ts)):
                self.exit(p, "one_sided_flatten", True, ts, ts + FILL_DELAY)

    def cancel_hedge_open(self, why: str) -> None:
        """DK-78: a flatten of the entire book (one-sided flatten, kill) cancels in-flight hedge-open tokens
        (a hedge ordered at a decision whose position does not exist yet); an existing hedge's own in-flight
        trades are cancelled by its exit (``exit`` -> ``cancel``)."""
        for pid in sorted(self.tokens):
            if pid in self.led.pos:
                continue
            for tk in self.tokens[pid]:
                if tk.kind == "hedge" and not tk.cancelled:
                    tk.cancelled = True
                    self.integrity.append(f"hedge_open_cancelled_by_{why}:{pid}")

    def forced(self, acts: RiskActions, t_e: int) -> None:
        if acts.killed and not self.kill_done:
            self.kill_done = True
            for pid, tks in list(self.tokens.items()):  # no re-entry: unfilled entries are cancelled
                for tk in tks:
                    if tk.kind == "open":
                        tk.cancelled = True
                        self.integrity.append(f"entry_cancelled_by_kill:{pid}")
            self.cancel_hedge_open("kill")  # "exit everything": no hedge opened after the kill (DK-78)
        for fx in acts.forced:
            p = self.leg_pos(fx.symbol)
            if p.pending_exit is None or p.maker_resting(acts.t):  # a RESTING maker exit is replaced (S-6.2)
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
        bref = self.btc_ref(ts)
        if bref is None and p.role == "leg":  # B(e) of the settlement fill (S-8.3 mkt_c; AMB-21 (b))
            self.btc_missing(ts, B_ACCOUNTING)
            return
        p.pending_exit = p.pending_exit or "terminal"
        self.led.apply_fill(_with(out, btc_ref=bref), p, True)
        self.close_episode(p, ts)

    def on_unwind(self) -> None:
        u = self.unwind_ts
        for pid in sorted(self.tokens):  # orders due after the unwind (E_M remainders) never fill
            if pid not in self.led.pos and any(not tk.cancelled for tk in self.tokens[pid]):
                self.integrity.append(f"order_cancelled_at_unwind:{pid}")
                self.cancel(pid)
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
            out = taker_at(p.view, tr, u)  # no bar (not halted): RunInvalid -> the AMB-20 INVALID below
            if isinstance(out, Pending) and not out.terminal:
                r = halted_exit(p.view, out)
                out = r if isinstance(r, Fill) else None  # type: ignore[assignment]
            if not isinstance(out, Fill) or out.ts >= self.end:
                if p.view.halted(u):
                    why = "halted at the unwind"
                elif isinstance(out, Pending) and out.terminal:  # AMB-29 (f): cessation in (u, end)
                    why = ("no reference price at the unwind minute; the instance ceases after it, before "
                           "the end (its terminal check is never reached; AMB-29 (f))")
                else:
                    why = "no reference price at the unwind minute (data gap, not halted; AMB-20)"
                self.fail(RunInvalid(INVALID_NO_EXIT, p.symbol, p.view.inst.idx, u, why))
                return
            if not self.on_fill_now(out, p):
                return
        self.led.close_day(u, self.led.equity(), final=True)

    def on_fill_now(self, f: Fill, p: Position) -> bool:
        bref = self.btc_ref(f.ts)
        if bref is None and p.role == "leg":  # B(e) of a leg fill (S-8.3 mkt_c; AMB-21 (b))
            self.btc_missing(f.ts, B_ACCOUNTING)
            return False
        self.led.apply_fill(_with(f, btc_ref=bref), p, True)
        self.close_episode(p, f.ts)
        return True

    # --- decisions --------------------------------------------------------------------------------------
    def on_decision(self, t: int) -> None:
        self.notes = []
        t_e = t + FILL_DELAY
        v = self.inp.view(t)
        if v.t != t:
            raise ValueError("the decision view belongs to another time")
        for g in v.gates.values():
            assert_pit(g, t)
        legs = self.legs(t)
        held = {lg.symbol for lg in legs}
        marks = {s: m for s, m in self.fresh.items() if s in held}
        self.state, acts = self.risk.pre_decision(self.state, t, legs, v.gates, v.h_hat, marks, v.stale)
        scores = self.scores(t, v.cands)
        # PIT (AMB-16 (j)): the S-5.4 halt flag AT t (1m bars opening in [t - 60 min, t)); the fill-time
        # check at t_e is execution's (pending exit / no order)
        halted = {lg.symbol: self.leg_pos(lg.symbol).view.halted(t) for lg in legs}
        for sym in sorted(set(scores["symbol"].astype(str)) - held):
            cv = self.view_of(sym, t)
            halted[sym] = True if cv is None else cv.halted(t)
        eq = self.led.equity()
        tg = targets(t=t, equity=eq, held=tuple(legs), scores=scores, gates=v.gates, risk=acts, halted=halted,
                     betas=v.betas, quote_vol_24h=v.qv24, h_star_h=float(self.h_star), usd_per_unit=self.upu)
        extra: dict = {}
        if self.lots is None:
            for o in tg.orders:
                self.order(o, t, t_e, v)
            post: Mapping[str, float] = tg.post
            hd = self.hedge_step(t, t_e, tg.post, v, eq, acts.killed)
        else:
            post, hd, infeasible = self.lot_decision(tg, t, t_e, v, eq, acts.killed)
            extra = dict(constructible=not infeasible, infeasible=infeasible)
        self.decisions.append(dict(t=t, equity=eq, stale=acts.stale, killed=acts.killed, paused=acts.paused,
                                   n_orders=len(tg.orders), orders=tuple(f"{o.kind}:{o.reason}:{o.symbol}"
                                                                         for o in tg.orders),
                                   notes=tg.notes + acts.notes + tuple(self.notes), hedge_reason=hd.reason,
                                   hedge_target=hd.target, beta_book=hd.beta_book,
                                   n_post=sum(1 for x in post.values() if x != 0),
                                   post=tuple(sorted((k, float(x)) for k, x in post.items() if x != 0)),
                                   prices=self.decision_prices(post, t, t_e), **extra))

    def decision_prices(self, post: Mapping[str, float], t: int, t_e: int) -> tuple:
        """P(t + 15 min) of every post-decision leg and of BTC (None if missing): the price inputs of the
        path-independent D1 report (``deploy.d1_share_scaled``)."""
        out = []
        for sym in sorted({k for k, x in post.items() if x != 0} | {HEDGE_SYMBOL}):
            cv = self.view_of(sym, t)
            r = cv.ref(t_e) if cv is not None else None
            out.append((sym, None if r is None else float(r[0])))
        return tuple(out)

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
            if o.notional <= EPS * self.e0:
                self.notes.append(f"zero_open:{sym}")
                return
            cv = self.view_of(sym, t)
            if cv is None:
                raise RuntimeError(f"internal: entry of {sym} without a current instance")
            pid = f"{sym}#{cv.inst.idx}@{t}"
            self.open_meta[pid] = (t, v.betas.get(sym))
            tr = Trade(sym, o.side, None, o.notional, True, o.reason, pid)
            outs = [taker_at(cv, tr, t_e)] if self.exe == "E_T" else maker_at(cv, tr, t_e, v.h_hat.get(sym),
                                                                                self.upu, self.unwind_ts)
            self.schedule(outs, pid, "open", t_e, None)
            return
        p = self.leg_pos(sym)
        if o.kind == "reduce" and o.target > EPS * self.e0:
            leg_n = abs(p.q) * p.p_last  # type: ignore[operator]
            tr = Trade(sym, -o.side, abs(p.q) * o.notional / leg_n, None, False, o.reason, p.pid)
            taker = self.exe == "E_T" or o.forced  # a risk_gross reduce is a risk order: taker (S-6.2)
            outs = [taker_at(p.view, tr, t_e)] if taker else maker_at(
                p.view, tr, t_e, v.h_hat.get(sym), self.upu, self.unwind_ts)
            self.schedule(outs, p.pid, "reduce", t_e, None)
            return
        self.exit(p, o.reason, o.forced, o.trigger_ts, t_e, v.h_hat.get(sym))  # close (or reduce to 0)

    def hedge_step(self, t: int, t_e: int, post: Mapping[str, float], v: DecisionView, eq: float,
                   killed: bool) -> hedge.HedgeDecision:
        hd, act, _ = self.hedge_plan(t, t_e, post, v, eq, killed)
        self.hedge_exec(act, t, t_e)
        return hd

    def hedge_plan(self, t: int, t_e: int, post: Mapping[str, float], v: DecisionView, eq: float,
                   killed: bool) -> tuple[hedge.HedgeDecision, tuple | None, str | None]:
        """The S-7.4 hedge decision and its action (no side effect): None, ("note", text), ("exit", pos) or
        ("trade", view, dq, q0); the third item is "hedge" when a USD layer's sizer finds the required hedge
        trade infeasible (the action is then a note)."""
        h = self.hedge_pos()
        cur = h.q * h.p_last if h is not None and h.p_last is not None else 0.0
        hd = hedge.decide(post, v.betas, eq, current=cur, killed=killed)
        if not hd.trade or hd.target is None:
            return hd, None, None
        if h is not None and h.pending_exit is not None:
            return hd, ("note", "hedge_exit_pending"), None
        if hd.target == 0.0 and h is None:
            return hd, None, None
        bv = self.view_of(HEDGE_SYMBOL, t)
        if bv is None:  # a required hedge trade without any current BTC instance: no BTC price (AMB-21 (b))
            return hd, ("invalid", t_e), None
        if h is not None and h.view is not bv:
            raise ValueError("BTC hedge instance changed while hedged (E10)")
        if bv.halted(t_e):  # S-5.4 / S-7.1: no order of any kind while halted
            return hd, ("note", "hedge_halted"), None
        r = bv.ref(t_e)
        if r is None:  # required, not halted, no reference: INVALID RUN (AMB-21 (b)), never a stale price
            return hd, ("invalid", t_e), None
        if hd.target == 0.0:
            return hd, ("exit", h), None
        q0 = h.q if h is not None else 0.0
        dq = hd.target / r[0] - q0
        if dq == 0.0:
            return hd, None, None
        if self.lots is not None:  # USD layer: exchange rounding of the hedge order (section 14)
            q = self.lots.size(HEDGE_SYMBOL, abs(dq), r[0], t)
            if q is None:
                return hd, ("note", "hedge_infeasible"), "hedge"
            dq = q if dq > 0 else -q
        return hd, ("trade", bv, dq, q0), None

    def hedge_exec(self, act: tuple | None, t: int, t_e: int) -> None:
        if act is None:
            return
        if act[0] == "note":
            self.notes.append(act[1])
            return
        if act[0] == "invalid":
            self.btc_missing(act[1], "BTC price required for hedge construction")
            return
        if act[0] == "exit":
            self.exit(act[1], "hedge", False, None, t_e)
            return
        _, bv, dq, q0 = act
        h = self.hedge_pos()
        opening = abs(q0 + dq) > abs(q0) or (q0 + dq) * q0 < 0
        if h is None:
            self.n_hedge += 1
            pid = f"{HEDGE_SYMBOL}#{bv.inst.idx}@hedge{self.n_hedge}"
            self.open_meta[pid] = (t, None)
        else:
            pid = h.pid
        tr = Trade(HEDGE_SYMBOL, 1 if dq > 0 else -1, abs(dq), None, opening, "hedge", pid)
        self.schedule([taker_at(bv, tr, t_e)], pid, "hedge", t_e, None)

    def lot_decision(self, tg: Targets, t: int, t_e: int, v: DecisionView, eq: float,
                     killed: bool) -> tuple[dict[str, float], hedge.HedgeDecision, tuple[str, ...]]:
        """USD layer (section 14 L-265 / L-10k, E_T): size every entry / reduce with the sizer at its fill
        price; an infeasible one is not traded (fail closed); if the hedge the resulting book requires is
        infeasible, the beta rule forbids this decision's entries and the hedge is re-decided without them.
        Returns the post-decision book, the hedge decision and the infeasibilities (empty = constructible,
        D1)."""
        lots = self.lots
        if lots is None:
            raise RuntimeError("internal: lot decision without a sizer")
        infeasible: list[str] = []
        post = dict(tg.post)
        plan: dict[int, object] = {}
        n_entries = 0
        for i, o in enumerate(tg.orders):
            sym = o.symbol
            if o.kind == "open":
                if o.notional <= EPS * self.e0:
                    continue
                cv = self.view_of(sym, t)
                if cv is None:
                    raise RuntimeError(f"internal: entry of {sym} without a current instance")
                pid = f"{sym}#{cv.inst.idx}@{t}"
                out = taker_at(cv, Trade(sym, o.side, None, o.notional, True, o.reason, pid), t_e)
                if isinstance(out, Fill):
                    q = lots.size(sym, out.qty, out.ref_mid, t)
                    if q is None:
                        infeasible.append(f"leg:{sym}")
                        post.pop(sym, None)
                        plan[i] = "infeasible"
                        continue
                    out = _scaled(out, q)
                    post[sym] = o.side * q * out.ref_mid
                    n_entries += 1
                plan[i] = (pid, out)
            elif o.kind == "reduce" and o.target > EPS * self.e0:
                p = self.leg_pos(sym)
                leg_n = abs(p.q) * p.p_last  # type: ignore[operator]
                tr = Trade(sym, -o.side, abs(p.q) * o.notional / leg_n, None, False, o.reason, p.pid)
                out = taker_at(p.view, tr, t_e)
                if isinstance(out, Fill):  # sized at its fill's reference mid, like the entries
                    q = lots.size(sym, out.qty, out.ref_mid, t)
                    if q is None:
                        infeasible.append(f"reduce:{sym}")
                        post[sym] = p.side * leg_n
                        plan[i] = "infeasible"
                        continue
                    out = _scaled(out, q)
                    post[sym] = p.side * (abs(p.q) - q) * p.p_last  # type: ignore[operator]
                plan[i] = ("reduce", out)
        if n_entries and len({x > 0 for x in post.values() if x != 0}) == 1:
            # AMB-16 (d) / AMB-28: infeasible legs left one side empty -> no entry builds a one-sided book
            # (the remaining held legs, if any, are flattened by the next tick's one-sided check)
            infeasible.append("one_sided:entries_forbidden")
            for i, o in enumerate(tg.orders):
                if o.kind == "open" and isinstance(plan.get(i), tuple):
                    post.pop(o.symbol, None)
                    plan[i] = "forbidden"
            n_entries = 0
        hd, act, bad = self.hedge_plan(t, t_e, post, v, eq, killed)
        if bad is not None and n_entries:  # S-7.4 / section 14: an infeasible required hedge forbids entries
            infeasible.append("hedge:entries_forbidden")
            for i, o in enumerate(tg.orders):
                if o.kind == "open" and plan.get(i) != "infeasible":
                    post.pop(o.symbol, None)
                    plan[i] = "forbidden"
            hd, act, bad = self.hedge_plan(t, t_e, post, v, eq, killed)
        if bad is not None:
            infeasible.append("hedge")
        for i, o in enumerate(tg.orders):
            x = plan.get(i)
            if isinstance(x, str):
                self.notes.append(f"{x}_{o.kind}:{o.symbol}")
            elif o.kind == "open":
                if x is None:
                    self.notes.append(f"zero_open:{o.symbol}")
                    continue
                pid, out = x  # type: ignore[misc]
                self.open_meta[pid] = (t, v.betas.get(o.symbol))
                self.schedule([out], pid, "open", t_e, None)
            elif isinstance(x, tuple) and x[0] == "reduce":
                p = self.leg_pos(o.symbol)
                self.schedule([x[1]], p.pid, "reduce", t_e, None)
            else:
                self.order(o, t, t_e, v)  # full closes: never filtered (AMB-23)
        self.hedge_exec(act, t, t_e)
        return post, hd, tuple(infeasible)

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
            elif kind == EV_NEUTRAL:
                self.on_neutral(ts)
            elif kind == EV_DECISION:
                self.on_decision(ts)
            else:
                self.on_unwind()
                break


def _with(f: Fill, **kw) -> Fill:
    return replace(f, **kw)


def _scaled(f: Fill, qty: float) -> Fill:
    """The same fill at a rounded quantity: fee, spread and slippage are linear in the quantity (S-5.3)."""
    r = qty / f.qty
    return replace(f, qty=float(qty), fee=f.fee * r, spread_cost=f.spread_cost * r, slip_cost=f.slip_cost * r)


# the decision-log schema (``_Sim.on_decision``); the USD layers add ``constructible`` / ``infeasible``
DECISION_COLS = ("t", "equity", "stale", "killed", "paused", "n_orders", "orders", "notes", "hedge_reason",
                 "hedge_target", "beta_book", "n_post", "post", "prices")
LOT_DECISION_COLS = (*DECISION_COLS, "constructible", "infeasible")


def _frame(rows: list[dict], cols: tuple[str, ...]) -> pd.DataFrame:
    """A run table with its schema columns, also when empty (DK-113 / red-team m9: an empty decision log keeps
    ``killed``, so the P3 kill check returns instead of raising)."""
    if not rows:
        return pd.DataFrame(columns=list(cols))
    df = pd.DataFrame(rows)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise RuntimeError(f"internal: run table rows lack schema columns {missing}")
    return df


USD_LAYERS = ("L-265", "L-10k")


def run(arm: str, inputs: EngineInputs, window: tuple[int, int], h_star: int, model: ScoreModel | None = None,
        execution: str = "E_T", layer: str = "L-norm") -> BookRun:
    """Run one arm's executed book over ``window`` (see the module docstring). L-norm only: this path takes
    no exchange filters (EC-6, section 14); the USD layers run through ``luna.v7.deploy.run_layer``."""
    if layer != "L-norm":
        raise ValueError("only L-norm runs here; L-265 / L-10k run through luna.v7.deploy.run_layer")
    return _run(arm, inputs, window, h_star, model, execution, layer)


def _run(arm: str, inputs: EngineInputs, window: tuple[int, int], h_star: int, model: ScoreModel | None,
         execution: str, layer: str, *, equity0: float = E0, lots: Lots | None = None) -> BookRun:
    """The shared event loop. ``layer`` "L-norm" (E = 1, no sizer, liquidity cap at E_ref) or a USD layer
    (``equity0`` USD, a ``Lots`` sizer, liquidity cap in USD, E_T only; called by ``luna.v7.deploy``)."""
    k9.require()  # fail closed: no engine run without a K9 refusal log (SC-03, AMB-35 (h))
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    if (arm == "A0") != (model is None):
        raise ValueError("A0 takes no model; A1 / A2 / A3 need one (S-7.2)")
    if h_star not in FROZEN.horizons_h:
        raise ValueError(f"h_star must be one of {FROZEN.horizons_h}")
    if execution not in ("E_T", "E_M"):
        raise ValueError(f"unknown execution model {execution!r}")
    if layer == "L-norm":
        if lots is not None or equity0 != E0:
            raise ValueError("L-norm takes no exchange filters and starts at E = 1 (EC-6, section 14)")
        upu = None
    elif layer in USD_LAYERS:
        if lots is None:
            raise ValueError(f"{layer} needs the exchange-filter sizer (luna.v7.deploy)")
        if execution != "E_T":
            raise ValueError(f"{layer} runs under E_T only (AMB-23)")
        if not (math.isfinite(equity0) and equity0 > 0):
            raise ValueError("equity0 must be finite and > 0")
        upu = 1.0  # notionals are USD
    else:
        raise ValueError(f"unknown equity layer {layer!r}")
    lo, end = int(window[0]), int(window[1])
    if lo % TICK or end % MIN or end - lo <= 2 * FILL_DELAY:
        raise ValueError("the window must start on the 15-minute grid and span more than 30 min")
    guard.check_outcome_window(lo, end, auth=inputs.auth, now_ns=inputs.now_ns, fresh=True)  # full re-check
    sim = _Sim(arm, inputs, (lo, end), h_star, model, execution, e0=equity0, upu=upu, lots=lots)
    sim.run()
    led = sim.led
    return BookRun(
        arm=arm, h_star=h_star, execution=execution, layer=layer, window=(lo, end),
        fills=_frame(led.fills, FILL_COLS), marks=_frame(led.marks, MARK_COLS),
        positions=_frame(led.positions, POSITION_COLS), funding_flows=_frame(led.funding, FUNDING_COLS),
        daily=_frame(led.daily, DAILY_COLS),
        decisions=_frame(sim.decisions, DECISION_COLS if lots is None else LOT_DECISION_COLS),
        integrity=tuple(dict.fromkeys(sim.integrity)), invalid=sim.invalid, invalid_detail=sim.invalid_detail,
        invalid_ts=sim.invalid_ts, equity0=float(equity0),
    )


__all__ = ["BookRun", "DecisionView", "EngineInputs", "Instrument", "Lots", "ScoreModel", "USD_LAYERS",
           "run"]
