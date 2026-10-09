"""Replay adapter: the same CarryCore/decide path over a stream of snapshots with simulated fills.
Synthetic data only: no backtest result is computed or tuned here."""

from __future__ import annotations

import pandas as pd
import pytest

from luna.carry import strategy
from luna.carry.funding import FundingStore
from luna.carry.market import parse_tickers
from luna.carry.replay import run_replay
from tests.carry.helpers import T0, carry_config, funding_envelope, instrument, ticker, tickers_envelope

FL = {"A": 5e-5, "B": 4e-5, "C": 1e-5, "D": 0.0, "E": -1e-5, "F": -3e-5}
H = pd.Timedelta("1h")
JUMP = T0 + 2 * 24 * H  # Wednesday 00:00: C's funding jumps


def funding() -> FundingStore:
    s = FundingStore()
    starts = pd.date_range(T0 - 720 * H, T0 + 21 * 24 * H, freq="1h")
    for a, r in FL.items():
        rows = [
            (t, (3e-3 if (a == "C" and t >= JUMP) else r) * 100.0, 3e-3 if (a == "C" and t >= JUMP) else r)
            for t in starts
        ]
        s.merge_envelope(funding_envelope(a, rows))
    return s


def snaps(hours: int):
    for i in range(hours):
        ts = T0 + i * H
        yield parse_tickers(tickers_envelope(ts, [ticker(a, 100.0, 2.0) for a in FL]))


def run(hours: int = 24 * 15, **cfg_over):
    cfg = carry_config(universe=sorted(FL), **cfg_over)
    return run_replay(cfg, snaps(hours), funding(), {a: instrument(a) for a in FL}, fee_rate=0.0005)


def switches(res) -> list[tuple[pd.Timestamp, str, str]]:
    r = res.records
    s = r[r["decision"].isin(["SWITCH", "ENTER", "EXIT"]) & (r["asset_in"] == "C")]
    return list(zip(s["ts"], s["decision"], s["asset_out"].fillna(""), strict=True))


def test_event_driven_cost_aware_switches_soon_after_the_change() -> None:
    res = run()
    sw = switches(res)
    assert sw, "C never entered"
    first = sw[0][0]
    assert JUMP < first < JUMP + 3 * 24 * H  # days, not a week: no calendar wait
    assert sw[0][1] == "SWITCH"


def test_weekly_rebalance_to_target_reproduces_the_v4_monday_baseline() -> None:
    res = run(decision_mode="rebalance_to_target", evaluation__trigger="weekly")
    sw = switches(res)
    assert sw and sw[0][0] == T0 + 7 * 24 * H  # the next Monday 00:00, nothing mid-week
    trades = res.records[res.records["decision"].isin(["ENTER", "EXIT", "SWITCH"])]
    assert set(pd.to_datetime(trades["ts"]).dt.weekday) == {0} and set(
        pd.to_datetime(trades["ts"]).dt.hour
    ) == {0}


def test_daily_interval_trigger_waits_for_its_slot() -> None:
    res = run(evaluation__trigger="daily", evaluation__at_hour=12)
    sw = switches(res)
    assert sw and all(t.hour == 12 for t, _, _ in sw)


def test_replay_and_paper_share_the_decision_function(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = strategy.decide

    def spy(inp, cfg):
        calls["n"] += 1
        return real(inp, cfg)

    monkeypatch.setattr("luna.carry.core.decide", spy)
    run(hours=3)
    assert calls["n"] >= 3


def test_replay_pnl_components_match_a_hand_calculation() -> None:
    """24 hourly snapshots before C's jump: the k=2 book (short A, B; long F, E), 0.6625 units each
    at a constant 100 mid (1 bp half spread), settles the 23 periods that end by the last snapshot."""
    res = run(hours=24)
    s = res.summary
    per_hour = 0.6625 * 100.0 * (5e-5 + 4e-5 + 3e-5 + 1e-5)  # shorts receive, longs in < 0 receive
    assert s["funding"] == pytest.approx(23 * per_hour, rel=1e-9)
    assert s["fees"] == pytest.approx(4 * 66.25 * 0.0005, rel=1e-9)
    assert s["spread_slip"] == pytest.approx(4 * 66.25 * 1e-4, rel=1e-6)
    assert s["price"] == pytest.approx(0.0, abs=1e-9)  # constant mid: no price P&L
    assert s["equity_end"] == pytest.approx(265.0 + 23 * per_hour - 4 * 66.25 * 0.0006, rel=1e-9)
    assert s["turnover"] == pytest.approx(4 * 66.25, rel=1e-3)
