"""V6 replay engine for H3 (spec §3 accounting, §5 H3). One engine for historical and forward
markets: the market prices a fill at the decision and the engine books it at the fill's own
timestamp (historical: t + 15m; forward: the first book snapshot after the decision).

Per 15-minute tick t, in this order:
 1. accrue funding for the quarter [t - 15m, t) with the position held during it;
 2. settle the hourly period that ended at t (USD per unit visible at t; missing -> 0, counted:
    unknown funding for a held period makes the run an INVALID RUN, S-DATA-1);
 3. book the fills whose timestamp is <= t;
 4. mark equity E_t = cash + sum q * mid(t);
 5. at 00:00 close the previous day and reset the R3 day start;
 -- the rest only when the market has a fresh snapshot at t (S-DATA-5; historical: always) --
 6. R4: E_t <= 0.85 * peak -> exit everything, no re-entry for the rest of the run;
 7. R3: E_t <= 0.98 * day start -> no entries or growth until the next 00:00;
 8. leg stop (15 % adverse from the position's average entry mid, 24 h cooldown) and V5
    ineligibility exits (two consecutive ineligible ticks);
 9. S-H3-10 repair after any intraday exit was booked;
10. the daily decision: due at 00:00, made at the first fresh tick within 6 h, else skipped;
11. the 24 h infeasible clock and the 24 h missing-forecast exit.
At the end: terminal unwind (S-ACC-4) at mid(end) with the decision-window spread.

``invariant_mode`` (blind development runs only, S-GUARD-2): R4, R3 and leg stops are off so that
no reported mechanical invariant depends on performance; never used for an evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6 import book as bk
from luna.v6 import riskmodel as rm
from luna.v6.forecast import GateRecord, H2a, ModelWarmup
from luna.v6.market import FALLBACK_FULL, FUT_TAKER, SLIP, HistMarket
from luna.v6.spec import constants

BAR = pd.Timedelta("15min")
HOUR = pd.Timedelta("1h")
DAY = pd.Timedelta("1D")
_K = constants()
_COMMON = _K["common"]
_H3 = _K["h3"]
MAX_SPREAD = _COMMON["gate_max_spread_bps"] * 1e-4
MIN_VOLUME = _COMMON["gate_min_quote_volume_usd"]
MAX_FUNDING_AGE = pd.Timedelta(hours=_COMMON["gate_max_funding_age_hours"])
KILL_DD = _COMMON["risk_max_drawdown_kill"]
DAILY_LOSS = 0.02  # V5 carry.risk daily_loss_limit (config/default.yaml at fabd419), D-E1
EXIT_SPREAD = 0.0040  # V5 gates exit_spread_bps 40, D-E2
LEG_STOP = _H3["leg_stop_frac"]
COOLDOWN = pd.Timedelta(hours=_H3["stop_cooldown_hours"])
GRACE = pd.Timedelta(hours=_H3["infeasible_grace_hours"])
MISSING_FC = pd.Timedelta(hours=_K["h2a"]["missing_forecast_exit_hours"])
DECISION_GAP = pd.Timedelta(hours=_H3["forward_rebalance_gap_hours"])
INTRADAY_EXITS = ("stop", "inelig", "missing_fc")

FILL_COLS = ["decision_ts", "ts", "asset", "leg", "dq", "px", "mid", "mid_decision", "half", "fee", "spread_slip",
             "fallback", "intent", "notional", "maker"]
SETT_COLS = ["period_start", "ts", "asset", "amount", "known", "qty_hours"]


def fill_row(f, intent: str) -> dict:
    return {"decision_ts": f.decision_ts, "ts": f.ts, "asset": f.asset, "leg": f.leg, "dq": f.dq, "px": f.px,
            "mid": f.mid, "mid_decision": f.mid_decision, "half": f.half, "fee": f.fee, "spread_slip": f.spread_slip,
            "fallback": f.fallback, "intent": intent, "notional": abs(f.dq) * f.mid, "maker": bool(f.maker)}


def fills_frame(rows: list) -> pd.DataFrame:
    F = pd.DataFrame(rows, columns=FILL_COLS)
    F["maker"] = F["maker"].astype(bool)
    return F


def terminal_cost(m, a: str, q: float, end: pd.Timestamp, last_mid: float, leg: str = "perp") -> tuple[float, float, float]:
    """(mid, fee, spread_slip) of the terminal unwind; without a mid at the end the last mid is used and
    the costs are priced on it (taker fee, 10 bps fallback spread, slip)."""
    mid, fee, ss = m.unwind_cost(a, q, end, leg=leg)
    if math.isfinite(mid):
        return mid, fee, ss
    mid = last_mid
    if not math.isfinite(mid) or q == 0:
        return mid, 0.0, 0.0
    from luna.v6.market import SPOT_TAKER

    rate = SPOT_TAKER if leg == "spot" else FUT_TAKER
    half = FALLBACK_FULL / 2 if leg == "perp" else max(FALLBACK_FULL, 0.0010)
    px = mid * (1 - math.copysign(1, q) * half) * (1 - math.copysign(1, q) * SLIP)
    return mid, rate * abs(q) * px, abs(q) * abs(px - mid)


@dataclass
class H3Config:
    name: str
    k: int
    row: str = "PRIMARY"


@dataclass
class RunResult:
    name: str
    row: str
    start: pd.Timestamp
    end: pd.Timestamp
    equity0: float
    fills: pd.DataFrame
    settlements: pd.DataFrame
    ticks: pd.DataFrame
    daily: pd.DataFrame
    decisions: list
    events: list
    gate_records: list
    counters: dict


@dataclass
class _State:
    cash: float
    pos: dict = field(default_factory=dict)  # asset -> qty
    entry: dict = field(default_factory=dict)  # asset -> average entry mid
    acc: dict = field(default_factory=dict)  # (asset, period) -> qty * hours
    pending: list = field(default_factory=list)  # (Fill, intent)
    exiting: set = field(default_factory=set)
    cooldown: dict = field(default_factory=dict)
    inelig_prev: set = field(default_factory=set)
    missing_fc: dict = field(default_factory=dict)
    infeasible_since: pd.Timestamp | None = None
    infeasible_exited: bool = False
    killed: bool = False
    r3_blocked: bool = False
    peak: float = -math.inf
    day_start: float = math.nan
    last_cov: rm.Cov | None = None
    last_mid: dict = field(default_factory=dict)
    retry: list = field(default_factory=list)
    decision_due: pd.Timestamp | None = None
    repair_due: bool = False


class H3Engine:
    def __init__(self, market: HistMarket, model: H2a, prices: pd.DataFrame, vol: pd.DataFrame,
                 insts: dict, cfg: H3Config, equity0: float = float(_COMMON["capital_usd"])) -> None:
        if model.h != _H3["horizon_hours"]:
            raise ValueError("H3 uses the 168 h H2a model")
        self.m, self.model, self.prices, self.vol, self.insts, self.cfg = market, model, prices, vol, insts, cfg
        self.E0 = equity0
        self._fl_cache: tuple | None = None

    # --- helpers -----------------------------------------------------------------------------------
    def _fresh(self, t: pd.Timestamp) -> bool:
        f = getattr(self.m, "has_fresh", None)
        return True if f is None else bool(f(t))

    def _mid(self, st: _State, a: str, t: pd.Timestamp) -> float:
        v = self.m.mid(a, t)
        if math.isfinite(v):
            st.last_mid[a] = v
            return v
        return st.last_mid.get(a, math.nan)

    def _equity(self, st: _State, t: pd.Timestamp) -> float:
        return st.cash + sum(q * self._mid(st, a, t) for a, q in st.pos.items() if q)

    def _inst(self, a: str, t: pd.Timestamp) -> bk.Inst:
        i = self.insts[a]
        return bk.Inst(self.m.mid(a, t), float(i["step"]), float(i["min_qty"]))

    def _fl(self, t: pd.Timestamp) -> pd.Series:
        if self._fl_cache is None or self._fl_cache[0] != t:
            self._fl_cache = (t, self.m.funding.fl(t, 720, 648))
        return self._fl_cache[1]

    def _holding_ineligible(self, a: str, t: pd.Timestamp) -> bool:
        mid = self.m.mid(a, t)
        if not math.isfinite(mid):
            return True
        half, _ = self.m.decision_half(a, t)
        vol = self.m.volume24(a, t)
        newest = self.m.funding.newest_end(a, t) if a in self.m.funding.assets else None
        fl = self._fl(t).get(a, np.nan) if a in self.m.funding.assets else np.nan
        return (2 * half > EXIT_SPREAD or not (vol >= MIN_VOLUME) or newest is None
                or t - newest > MAX_FUNDING_AGE or not math.isfinite(fl))

    def eligible(self, a: str, t: pd.Timestamp, fc: pd.Series | None) -> bool:
        """S-H3-1: V5 gates + H2a(168) forecast + >= 60 days of mark history."""
        if fc is None or not math.isfinite(float(fc.get(a, np.nan))):
            return False
        mid = self.m.mid(a, t)
        if not math.isfinite(mid):
            return False
        half, _ = self.m.decision_half(a, t)
        if 2 * half > MAX_SPREAD or not (self.m.volume24(a, t) >= MIN_VOLUME):
            return False
        newest = self.m.funding.newest_end(a, t)
        if newest is None or t - newest > MAX_FUNDING_AGE:
            return False
        return rm.has_history(self.prices, a, t)

    def _target_pos(self, st: _State, a: str) -> float:
        """Position after the orders already sent (pending fills included)."""
        return st.pos.get(a, 0.0) + sum(f.dq for f, _ in st.pending if f.asset == a)

    def _order(self, st: _State, a: str, dq: float, t: pd.Timestamp, intent: str) -> None:
        if not dq:
            return
        f = self.m.fill(a, dq, t)
        if f is None:
            self.counters["unfilled"] += 1
            if intent in ("exit", "stop", "kill", "inelig", "repair", "infeasible_exit", "missing_fc"):
                st.retry.append((a, dq, intent))
            return
        st.pending.append((f, intent))
        if intent != "open" and intent != "resize":
            st.exiting.add(a)

    def _exit_all(self, st: _State, t: pd.Timestamp, intent: str) -> None:
        for a, q in list(st.pos.items()):
            if q and a not in st.exiting:
                self._order(st, a, -q, t, intent)

    # --- run ---------------------------------------------------------------------------------------
    def run(self, start: pd.Timestamp, end: pd.Timestamp, invariant_mode: bool = False) -> RunResult:
        start, end = pd.Timestamp(start), pd.Timestamp(end).floor("15min")
        st = _State(cash=self.E0)
        self.counters = {"unfilled": 0, "funding_unknown": 0, "kill": 0, "liquidation": 0, "r3_days": 0,
                         "stops": 0, "inelig_exits": 0, "infeasible_decisions": 0, "infeasible_exit_all": 0,
                         "repairs": 0, "repair_exit_all": 0, "warmup_decisions": 0, "decisions": 0,
                         "band_repaired": 0, "missing_fc_exits": 0, "stale_ticks": 0, "deferred_decisions": 0,
                         "skipped_decision_days": 0}
        fills, sett, ticks, decisions, events, gates = [], [], [], [], [], []
        st.day_start = self.E0
        t = start
        while t <= end:
            E_pre = self._equity(st, t)
            if t > start:
                p = (t - BAR).floor("h")
                for a, q in st.pos.items():
                    if q:
                        st.acc[(a, p)] = st.acc.get((a, p), 0.0) + q * 0.25
                if t.minute == 0:
                    pe = t - HOUR
                    sp = getattr(self.m, "settlement", None) or self.m.funding
                    for key in [k for k in st.acc if k[1] == pe]:
                        a = key[0]
                        rate = sp.abs_rate(a, pe, t) if a in sp.assets else math.nan
                        if not math.isfinite(rate):
                            self.counters["funding_unknown"] += 1
                            amt = 0.0
                        else:
                            amt = -st.acc[key] * rate
                        st.cash += amt
                        sett.append({"period_start": pe, "ts": t, "asset": a, "amount": amt,
                                     "known": math.isfinite(rate), "qty_hours": st.acc[key]})
                        del st.acc[key]
            due = [x for x in st.pending if x[0].ts <= t]
            st.pending = [x for x in st.pending if x[0].ts > t]
            for f, intent in due:
                q0 = st.pos.get(f.asset, 0.0)
                q1 = q0 + f.dq
                st.cash -= f.dq * f.px + f.fee
                if abs(q1) < 1e-12:
                    q1 = 0.0
                if q0 == 0 or math.copysign(1, q1) != math.copysign(1, q0):
                    st.entry[f.asset] = f.mid
                elif abs(q1) > abs(q0):
                    st.entry[f.asset] = (st.entry[f.asset] * abs(q0) + f.mid * abs(f.dq)) / abs(q1)
                st.pos[f.asset] = q1
                if q1 == 0:
                    st.pos.pop(f.asset, None)
                    st.entry.pop(f.asset, None)
                if not any(x[0].asset == f.asset for x in st.pending):
                    st.exiting.discard(f.asset)
                fills.append(fill_row(f, intent))
                if intent in INTRADAY_EXITS:
                    st.repair_due = True
            E = self._equity(st, t)
            gross = sum(abs(q * self._mid(st, a, t)) for a, q in st.pos.items())
            ticks.append({"t": t, "equity_pre": E_pre, "equity": E, "gross": gross,
                          "n_long": sum(q > 0 for q in st.pos.values()),
                          "n_short": sum(q < 0 for q in st.pos.values())})
            if E <= 0:
                self.counters["liquidation"] += 1
            if t.hour == 0 and t.minute == 0 and t > start:
                st.r3_blocked = False
                st.day_start = E
            if t.hour == 0 and t.minute == 0 and t <= end - BAR:
                st.decision_due = t
            fresh = self._fresh(t)
            if not fresh:
                self.counters["stale_ticks"] += 1
                if st.decision_due is not None and t - st.decision_due > DECISION_GAP:
                    self.counters["skipped_decision_days"] += 1
                    st.decision_due = None
                t += BAR
                continue
            st.peak = max(st.peak, E)
            if not invariant_mode and not st.killed and E <= (1 - KILL_DD) * st.peak:
                st.killed = True
                self.counters["kill"] += 1
                events.append({"t": t, "event": "kill", "equity": E, "peak": st.peak})
                if t < end:
                    self._exit_all(st, t, "kill")
            if not invariant_mode and not st.r3_blocked and E <= (1 - DAILY_LOSS) * st.day_start:
                st.r3_blocked = True
                self.counters["r3_days"] += 1
            retry, st.retry = st.retry, []
            for a, dq, intent in retry:
                if st.pos.get(a) and a not in st.exiting:
                    self._order(st, a, -st.pos[a], t, intent)
            inelig_now = set()
            if t < end:
                for a, q in list(st.pos.items()):
                    if not q or a in st.exiting:
                        continue
                    mid = self.m.mid(a, t)
                    e = st.entry.get(a)
                    if (not invariant_mode and math.isfinite(mid) and e
                            and (1 if q > 0 else -1) * (mid - e) / e <= -LEG_STOP):
                        self.counters["stops"] += 1
                        st.cooldown[a] = t + COOLDOWN
                        events.append({"t": t, "event": "stop", "asset": a})
                        self._order(st, a, -q, t, "stop")
                        continue
                    if self._holding_ineligible(a, t):
                        inelig_now.add(a)
                        if a in st.inelig_prev:
                            self.counters["inelig_exits"] += 1
                            events.append({"t": t, "event": "ineligible_exit", "asset": a})
                            self._order(st, a, -q, t, "inelig")
            st.inelig_prev = inelig_now
            if st.repair_due and not st.killed and t < end and st.last_cov is not None and st.pos:
                st.repair_due = False
                self._repair(st, t, E, events)
            if st.decision_due is not None:
                if t - st.decision_due > DECISION_GAP:
                    self.counters["skipped_decision_days"] += 1
                    st.decision_due = None
                elif t <= end - BAR:
                    if t != st.decision_due:
                        self.counters["deferred_decisions"] += 1
                    if st.killed:
                        self._gate_only(t, gates)
                    else:
                        self._decide(st, t, E, decisions, events, gates)
                    st.decision_due = None
            if t < end and not st.killed:
                if st.infeasible_since is not None and t - st.infeasible_since > GRACE and not st.infeasible_exited:
                    st.infeasible_exited = True
                    self.counters["infeasible_exit_all"] += 1
                    events.append({"t": t, "event": "infeasible_exit_all"})
                    self._exit_all(st, t, "infeasible_exit")
                for a, since in list(st.missing_fc.items()):
                    if st.pos.get(a) and a not in st.exiting and t - since > MISSING_FC:
                        self.counters["missing_fc_exits"] += 1
                        self._order(st, a, -st.pos[a], t, "missing_fc")
            t += BAR
        for a, q in list(st.pos.items()):
            if not q:
                continue
            mid, fee, ss = terminal_cost(self.m, a, q, end, st.last_mid.get(a, math.nan))
            st.cash += q * mid - fee - ss
            fills.append({"decision_ts": end, "ts": end, "asset": a, "leg": "perp", "dq": -q,
                          "px": mid - math.copysign(ss / abs(q), q), "mid": mid, "mid_decision": mid, "half": math.nan,
                          "fee": fee, "spread_slip": ss, "fallback": False, "intent": "terminal",
                          "notional": abs(q) * mid, "maker": False})
        st.pos = {}
        E_end = st.cash
        ticks.append({"t": end, "equity_pre": E_end, "equity": E_end, "gross": 0.0, "n_long": 0, "n_short": 0,
                      "terminal": True})
        F = fills_frame(fills)
        S = pd.DataFrame(sett, columns=SETT_COLS)
        T = pd.DataFrame(ticks)
        daily = daily_table(F, S, T, start, end, self.E0)
        return RunResult(self.cfg.name, self.cfg.row, start, end, self.E0, F, S, T, daily, decisions, events,
                         gates, dict(self.counters))

    # --- intraday repair (S-H3-10) -----------------------------------------------------------------
    def _repair(self, st: _State, t: pd.Timestamp, E: float, events: list) -> None:
        n = {a: q * self._mid(st, a, t) for a, q in st.pos.items() if q and a not in st.exiting}
        if not n:
            return
        insts = {a: self._inst(a, t) for a in n}
        if any(not math.isfinite(i.mid) for i in insts.values()):
            return
        self.counters["repairs"] += 1
        step_n = max(i.qty_step * i.mid for i in insts.values())
        r = bk.intraday_repair(n, st.last_cov, E, insts, step_n)
        if r.status == "EXIT_ALL":
            self.counters["repair_exit_all"] += 1
            events.append({"t": t, "event": "repair_exit_all"})
            self._exit_all(st, t, "repair")
            return
        for a, v in r.n.items():
            dq = insts[a].round_qty(v) - st.pos[a]
            if dq and abs(insts[a].round_qty(v)) < abs(st.pos[a]):
                self._order(st, a, dq, t, "repair")
        for a in n:
            if a not in r.n:
                self._order(st, a, -st.pos[a], t, "repair")

    # --- daily decision (S-H3-1 ... S-H3-9, S-H3-12) -----------------------------------------------
    def _gate_only(self, t: pd.Timestamp, gates: list) -> None:
        """After a kill the strategy stops, but the H2a gate sample keeps its PIT records."""
        try:
            fc = self.model.forecast(t)
        except ModelWarmup:
            return
        elig = [a for a in self.m.assets if a in fc.index and self.eligible(a, t, fc)]
        gates.append(GateRecord(t, elig, fc.copy()))

    def _decide(self, st: _State, t: pd.Timestamp, E: float, decisions: list, events: list, gates: list) -> None:
        self.counters["decisions"] += 1
        try:
            fc = self.model.forecast(t)
        except ModelWarmup:
            fc = None
            self.counters["warmup_decisions"] += 1
        held = {a: (1 if q > 0 else -1) for a, q in st.pos.items() if q and a not in st.exiting}
        for a in list(held):
            if fc is None or not math.isfinite(float(fc.get(a, np.nan))):
                st.missing_fc.setdefault(a, t)
            else:
                st.missing_fc.pop(a, None)
        for a in list(st.missing_fc):
            if a not in held:
                st.missing_fc.pop(a, None)
        if fc is None:
            decisions.append({"t": t, "status": "MODEL WARMUP"})
            return
        elig = [a for a in self.m.assets if a in fc.index and self.eligible(a, t, fc)]
        gates.append(GateRecord(t, elig, fc.copy()))
        s = fc[elig]
        cost = pd.Series({a: self.m.decision_cost(a, t) for a in set(elig) | set(held)})
        blocked = {a for a, until in st.cooldown.items() if until > t} | set(st.exiting)
        if st.r3_blocked:
            blocked |= set(elig)
        sel = bk.select(s, held, cost, self.cfg.k, blocked)
        rec = {"t": t, "n_eligible": len(elig), "kp": sel.kp, "long": list(sel.long), "short": list(sel.short),
               "exits": list(sel.exits)}

        def infeasible(reason: str) -> None:
            self.counters["infeasible_decisions"] += 1
            if st.infeasible_since is None:
                st.infeasible_since = t
            rec.update(status="INFEASIBLE", reason=reason)
            decisions.append(rec)

        if sel.infeasible:
            return infeasible("k' < 3")
        if len(sel.long) < bk.K_MIN or len(sel.short) < bk.K_MIN:
            return infeasible("entry rule left fewer than 3 legs on a side")
        names = list(dict.fromkeys(list(elig) + list(held) + ["BTC"]))  # S-H3-6: eligible + held + BTC
        try:
            cov = rm.covariance(self.prices, self.vol, names, t)
        except rm.CovarianceUnavailable as e:
            return infeasible(f"covariance: {e}")
        insts = {a: self._inst(a, t) for a in names if a in self.insts}
        if any(not math.isfinite(insts[a].mid) for a in sel.long + sel.short):
            return infeasible("no mid")
        b = bk.build(sel.long, sel.short, cov, E, insts)
        rec["build"] = {k: v for k, v in b.diag.items() if k != "post"}
        if b.status != "OK":
            return infeasible(f"build: {b.reason}")
        held_n = {a: self._target_pos(st, a) * insts[a].mid for a in held if a in insts}
        step_n = max(insts[a].qty_step * insts[a].mid for a in b.n)
        band = bk.apply_band(b.n, held_n, sel.long, sel.short, cov, E, step_n)
        if band.status != "OK":
            return infeasible("band")
        self.counters["band_repaired"] += int(band.repaired)
        st.infeasible_since, st.infeasible_exited, st.last_cov = None, False, cov
        for a, v in band.n.items():
            if a in st.exiting:
                continue
            q0 = self._target_pos(st, a)
            q1 = insts[a].round_qty(v) if v else 0.0
            if st.r3_blocked and abs(q1) > abs(q0):
                q1 = q0  # R3: no entry, no growth
            dq = q1 - q0
            if abs(dq) < 1e-12:
                continue
            intent = "open" if q0 == 0 else ("exit" if q1 == 0 else "resize")
            self._order(st, a, dq, t, intent)
        rec.update(status="OK", target={a: v for a, v in band.n.items()}, risk=b.diag.get("RC"),
                   post=b.diag.get("post"))
        decisions.append(rec)


def daily_table(F: pd.DataFrame, S: pd.DataFrame, T: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
                equity0: float) -> pd.DataFrame:
    """S-ACC-3: day d = [d 00:00, d+1 00:00) by the events' own timestamps; E_d = the equity marked at
    d+1 00:00 before any event stamped d+1 00:00; the last (possibly partial) day ends at ``end`` and
    includes the events stamped ``end`` (terminal unwind). price = dE - funding + fees + spread_slip;
    carry = funding - fees - spread_slip."""
    bounds = [b for b in pd.date_range(start.floor("D") + pd.Timedelta("1D"), end, freq="D") if b < end] + [end]
    T = T.drop_duplicates("t", keep="last").set_index("t") if "t" in T.columns else T
    rows, prev, lo = [], equity0, start
    for hi in bounds:
        last = hi == end
        eq = float(T["equity"].iloc[-1]) if last else float(T.loc[hi, "equity_pre"])

        def sel(df: pd.DataFrame) -> pd.Series:
            if not len(df):
                return pd.Series([], dtype=bool)
            return (df["ts"] >= lo) & ((df["ts"] <= hi) if last else (df["ts"] < hi))

        fm, sm = sel(F), sel(S)
        fees = float(F.loc[fm, "fee"].sum()) if len(F) else 0.0
        ss = float(F.loc[fm, "spread_slip"].sum()) if len(F) else 0.0
        fund = float(S.loc[sm, "amount"].sum()) if len(S) else 0.0
        q = T.loc[(T.index >= lo) & (T.index < hi)] if not last else T.loc[(T.index >= lo) & (T.index <= hi)]
        aq = int((q["gross"] >= 0.10 * q["equity"]).sum()) if len(q) else 0
        rows.append({"day": lo.floor("D"), "start": lo, "end": hi, "equity": eq, "net": eq - prev,
                     "ret": (eq - prev) / prev if prev else math.nan, "funding": fund, "fees": fees,
                     "spread_slip": ss, "price": (eq - prev) - fund + fees + ss, "carry": fund - fees - ss,
                     "carry_ret": (fund - fees - ss) / prev if prev else math.nan,
                     "active_quarters": aq, "active": aq >= 48})
        prev, lo = eq, hi
    return pd.DataFrame(rows)
