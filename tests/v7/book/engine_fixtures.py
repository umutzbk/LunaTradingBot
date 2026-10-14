"""Synthetic fixtures for the execution / engine tests (no market data; dates in 2024, inside W_DEV).

Prices are flat per symbol unless a path is given; 15m bars have o = h = l = c, so every Abdi-Ranaldo
product is 0 and every proxy value is the 1 bp floor. 8 h funding records with explicit intervals and
``mark_at_settle`` = the price. The decision provider passes every gate, h-hat = 1 bp, and scores the
candidates through the real ``arms.arm_scores`` (A0) from per-symbol FL30 values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import numpy as np
import pandas as pd

from luna.v7.arms import cost_side
from luna.v7.book.engine import DecisionView, EngineInputs, Instrument
from luna.v7.data.instances import Instance
from luna.v7.data.terminal import TerminalEvent
from luna.v7.timeutil import MIN, D, H, to_ns
from luna.v7.universe import GateRow, eligible

Q = 15 * MIN
LO = to_ns("2024-03-04")
END = LO + 2 * D
S0 = LO - 3 * D
NOW = to_ns("2026-10-14")
HMIN = 1e-4  # proxy floor
F_T, F_M, SLIP = 5e-4, 2e-4, 2e-4

Path = Callable[[np.ndarray], np.ndarray]


def _px(px: float | Path, ts: np.ndarray) -> np.ndarray:
    return px(ts) if callable(px) else np.full(len(ts), float(px))


def m1(px: float | Path, start: int = S0, end: int = END,
       dead: tuple[tuple[int, int], ...] = ()) -> pd.DataFrame:
    """1m bars o = c = px(ts), volume 1; bars opening inside a ``dead`` [lo, hi) span are absent."""
    ts = np.arange(start, end, MIN, dtype=np.int64)
    keep = np.ones(len(ts), bool)
    for lo, hi in dead:
        keep &= ~((ts >= lo) & (ts < hi))
    ts = ts[keep]
    p = _px(px, ts)
    return pd.DataFrame(dict(open_ts=ts, o=p, h=p, l=p, c=p, vol_base=1.0, known_at=ts + MIN))


def b15(px: float | Path, start: int = S0, end: int = END) -> pd.DataFrame:
    ts = np.arange(start, end, Q, dtype=np.int64)
    p = _px(px, ts)
    return pd.DataFrame(dict(open_ts=ts, o=p, h=p, l=p, c=p, vol_base=1.0, vol_quote=1e6, known_at=ts + Q))


def fund(rate: float, mark: float, start: int = S0, end: int = END) -> pd.DataFrame:
    ts = np.arange(start, end, 8 * H, dtype=np.int64)
    return pd.DataFrame(dict(funding_ts=ts, rate=float(rate), interval_h_explicit=pd.array([8] * len(ts),
                             dtype="Int64"), known_at=ts + MIN, mark_at_settle=float(mark)))


def instrument(sym: str, px: float | Path, rate: float, *, last_trade: int | None = None,
               terminal: str | None = None, settle_price: float | None = None,
               dead: tuple[tuple[int, int], ...] = (), trades: pd.DataFrame | None = None) -> Instrument:
    end = END if last_trade is None else last_trade
    inst = Instance(0, S0, last_trade, None if last_trade is None else last_trade + 7 * D)
    bb = b15(px, end=end)
    terms: dict[int, TerminalEvent] = {}
    if terminal is not None:
        assert last_trade is not None
        terms[0] = TerminalEvent(inst, last_trade, terminal, last_trade if terminal != "C" else None,  # type: ignore[arg-type]
                                 settle_price if terminal == "A" else None,
                                 "official_historical" if terminal == "A" else None)
    flat = float(px) if not callable(px) else float(px(np.array([S0]))[0])
    return Instrument(sym, m1(px, end=end, dead=dead), bb, bb[["open_ts", "c"]].copy(), fund(rate, flat),
                      [inst], terms, trades)


def gate_row(t: int, **flags: bool) -> GateRow:
    f = {f"u{i}": True for i in range(1, 9)}
    f.update(flags)
    return GateRow(**f, reasons=tuple(k.upper() for k, v in f.items() if not v), max_known_at=t - MIN)


def provider(fl30: Mapping[str, float], betas: Mapping[str, float | None],
             gates: Callable[[int, str], GateRow] | None = None,
             h_hat: float = HMIN) -> Callable[[int], DecisionView]:
    """Every symbol of ``fl30`` is a candidate on both sides (A0 inputs); ``gates(t, sym)`` overrides."""
    syms = sorted(fl30)

    def view(t: int) -> DecisionView:
        g = {s: (gates(t, s) if gates is not None else gate_row(t)) for s in syms}
        ok = [s for s in syms if eligible(g[s])]
        rows = [dict(symbol=s, side=sd, gate_ok=True, fl30_r8=fl30[s], cost_side=cost_side(h_hat))
                for s in ok for sd in (1, -1)]
        cands = pd.DataFrame(rows, columns=["symbol", "side", "gate_ok", "fl30_r8", "cost_side"])
        qv = {s: 1e12 for s in syms}
        return DecisionView(t, g, {s: h_hat for s in syms}, dict(betas), qv, cands, False)

    return view


BTC = "BTCUSDT"


def base_inputs(*, aaa: Instrument | None = None, bbb: Instrument | None = None,
                extra: Mapping[str, Instrument] | None = None, fl30: Mapping[str, float] | None = None,
                betas: Mapping[str, float | None] | None = None,
                gates: Callable[[int, str], GateRow] | None = None) -> EngineInputs:
    """Long AAA (100, funding -1 bp), short BBB (50, +1 bp), BTC 1000 (+0.5 bp); beta AAA 2, BBB 1."""
    ins = {"AAAUSDT": aaa or instrument("AAAUSDT", 100.0, -1e-4),
           "BBBUSDT": bbb or instrument("BBBUSDT", 50.0, 1e-4)}
    ins.update(extra or {})
    f = {"AAAUSDT": -0.003, "BBBUSDT": 0.003}
    f.update(fl30 or {})
    b = {"AAAUSDT": 2.0, "BBBUSDT": 1.0, **{k: 1.0 for k in (extra or {})}}
    b.update(betas or {})
    return EngineInputs(ins, instrument(BTC, 1000.0, 5e-5), provider(f, b, gates), NOW)
