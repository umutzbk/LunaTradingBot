"""CarryCore: sizing, risk-engine integration, two-phase execution, funding P&L, forced exits."""

from __future__ import annotations

import pandas as pd
import pytest

from luna.carry.core import CarryCore, SimExecutor, Trigger
from luna.carry.funding import FundingStore
from luna.carry.market import parse_tickers
from tests.carry.helpers import (
    T0,
    carry_config,
    flat_history,
    funding_envelope,
    instrument,
    ticker,
    tickers_envelope,
)

FL = {"A": 5e-5, "B": 4e-5, "C": 1e-5, "D": 0.0, "E": -1e-5, "F": -3e-5}
MIN = pd.Timedelta("1min")
H = pd.Timedelta("1h")


def make(fl=None, **cfg_over):
    fl = fl or FL
    cfg = carry_config(universe=sorted(fl), **cfg_over)
    core = CarryCore(cfg, {a: instrument(a) for a in fl}, fee_rate=0.0005, mode="backtest")
    store = FundingStore()
    for a, r in fl.items():
        store.merge_envelope(funding_envelope(a, flat_history(r, T0)))
    return core, store


def snap(ts, mids=None, **ticker_over):
    mids = mids or {}
    rows = [ticker(a, mids.get(a, 100.0), 2.0, **ticker_over.get(a, {})) for a in FL]
    return parse_tickers(tickers_envelope(ts, rows))


def cycle(core, store, ts, mids=None, allow=True, **tk):
    return core.run_cycle(ts, snap(ts, mids, **tk), store, SimExecutor(), allow_rebalance=allow)


def holdings(core):
    return {p.key: p.direction for p in core.portfolio.positions.values()}


def test_k2_book_is_dollar_neutral_with_half_over_k_legs() -> None:
    core, store = make()
    res = cycle(core, store, T0)
    assert holdings(core) == {"A": -1, "B": -1, "E": 1, "F": 1}
    notionals = {a: q * 100.0 for a, q in core.portfolio.holdings.items()}
    assert sorted(abs(v) for v in notionals.values()) == pytest.approx([66.25] * 4, rel=1e-3)
    assert sum(notionals.values()) == pytest.approx(0.0, abs=0.05)
    # 4 x 66.25 USD: 0.05% taker fee and 1 bp half spread each
    assert res.equity == pytest.approx(265.0 - 265.0 * (0.0005 + 0.0001), rel=1e-4)
    assert {r.decision for r in res.records} >= {"ENTER"}


def test_restart_state_round_trip_produces_no_new_orders() -> None:
    core, store = make()
    cycle(core, store, T0)
    clone = CarryCore.from_state(
        core.to_state(), core.cfg, core.instruments, fee_rate=0.0005, mode="backtest"
    )
    assert holdings(clone) == holdings(core)
    assert clone.portfolio.cash == pytest.approx(core.portfolio.cash)
    res = cycle(clone, store, T0 + 2 * MIN)
    assert res.orders == []  # same signals, same book: HOLD, never close-and-reopen


def test_risk_engine_veto_rejects_the_whole_pair_atomically() -> None:
    core, store = make(risk__max_orders_per_day=3)  # two pairs = 4 opens: the second pair cannot fit
    res = cycle(core, store, T0)
    assert len(core.portfolio.positions) == 2  # one long+short pair, never a lone leg
    assert sum(core.portfolio.holdings.values()) * 100 == pytest.approx(0.0, abs=0.05)
    rej = [r for r in res.records if r.decision == "REJECT"]
    assert rej and "R14_orders_per_day" in rej[0].reason


def test_switch_closes_first_then_opens_through_the_risk_engine() -> None:
    core, store = make()
    cycle(core, store, T0)
    store.merge_envelope(funding_envelope("C", flat_history(3e-4, T0 + 48 * H)))
    for a in "ABDEF":
        store.merge_envelope(funding_envelope(a, flat_history(FL[a], T0 + 48 * H)))
    res = cycle(core, store, T0 + 48 * H)
    assert holdings(core) == {"A": -1, "C": -1, "E": 1, "F": 1}
    sw = [r for r in res.records if r.decision == "SWITCH"]
    assert sw and sw[0].asset_out == "B" and sw[0].asset_in == "C"
    phases = [o.intent for o, _ in res.orders]
    assert phases == ["close", "open"]


def test_short_leg_in_positive_funding_receives_cash() -> None:
    core, store = make()
    cycle(core, store, T0)
    cash0 = core.portfolio.cash
    for a, r in FL.items():  # the period starting T0 settles at T0 + 1h with the same rates
        store.merge_envelope(funding_envelope(a, [(T0, r * 100.0, r)]))
    res = cycle(core, store, T0 + H + MIN)
    by_asset = {s.asset: sum(s.amounts.values()) for s in res.settlements}
    assert by_asset["A"] > 0 and by_asset["B"] > 0  # shorts in positive funding are paid
    assert by_asset["E"] > 0 and by_asset["F"] > 0  # longs in negative funding are paid
    total = sum(by_asset.values())
    assert core.portfolio.cash == pytest.approx(cash0 + total)
    short_a = next(p for p in core.portfolio.positions.values() if p.key == "A")
    assert short_a.pnl["funding"] == pytest.approx(by_asset["A"])
    # 0.6625 units short x 5e-5 x 100 USD for the whole hour
    assert by_asset["A"] == pytest.approx(0.6625 * 5e-5 * 100.0, rel=1e-3)


def test_leg_stop_forces_exit_and_cooldown_blocks_reentry() -> None:
    core, store = make()
    cycle(core, store, T0)
    res = cycle(core, store, T0 + 2 * MIN, mids={"A": 116.0})  # short A, +16% > 15% stop
    assert "A" not in holdings(core)
    ex = next(r for r in res.records if r.decision == "EXIT" and r.asset_out == "A")
    assert ex.reason == "stop"
    assert core.cooldown["A"] == T0 + 2 * MIN + pd.Timedelta(hours=24)
    res = cycle(core, store, T0 + 4 * MIN, mids={"A": 116.0})
    assert "A" not in holdings(core)  # still the best short, but in cooldown


def test_kill_switch_flattens_and_blocks_entries() -> None:
    core, store = make()
    cycle(core, store, T0)
    core.risk_state.kill_switch = True
    res = cycle(core, store, T0 + 2 * MIN)
    assert core.portfolio.positions == {} and core.portfolio.holdings == {}
    assert {r.reason for r in res.records if r.decision == "EXIT"} == {"kill_switch"}
    assert cycle(core, store, T0 + 4 * MIN).orders == []


def test_stale_snapshot_sends_nothing() -> None:
    core, store = make()
    stale = snap(T0)
    res = core.run_cycle(T0 + 10 * MIN, stale, store, SimExecutor(), allow_rebalance=True)
    assert res.orders == [] and core.portfolio.positions == {}
    assert any(r.decision == "REJECT" and r.reason == "stale_snapshot" for r in res.records)


def test_ineligible_held_asset_exits_only_after_its_grace() -> None:
    core, store = make()
    cycle(core, store, T0)
    # A's spread widens to 45 bps (> 40 bps exit limit, still crossable inside the 25 bps order
    # band): ineligible, but held for the 15-minute grace first

    def wide_snap(ts):
        rows = [ticker(a, 100.0, 45.0 if a == "A" else 2.0) for a in FL]
        return parse_tickers(tickers_envelope(ts, rows))

    core.run_cycle(T0 + 2 * MIN, wide_snap(T0 + 2 * MIN), store, SimExecutor(), allow_rebalance=True)
    assert "A" in holdings(core)
    res = core.run_cycle(T0 + 18 * MIN, wide_snap(T0 + 18 * MIN), store, SimExecutor(), allow_rebalance=True)
    assert "A" not in holdings(core)
    ex = next(r for r in res.records if r.asset_out == "A" and r.decision == "EXIT")
    assert ex.reason.startswith("ineligible") and "wide_spread" in ex.reason


def test_global_data_outage_freezes_instead_of_exiting() -> None:
    core, store = make()
    cycle(core, store, T0)
    gap = FundingStore()  # the collector's funding feed died: every asset funding_stale
    for a, r in FL.items():
        gap.merge_envelope(funding_envelope(a, flat_history(r, T0 - 6 * H)))
    res = cycle(core, gap, T0 + 30 * MIN)
    res = cycle(core, gap, T0 + 60 * MIN)
    assert holdings(core) == {"A": -1, "B": -1, "E": 1, "F": 1}
    assert res.orders == []
    assert any(r.decision == "REJECT" and r.reason == "global_data_outage" for r in res.records)


@pytest.mark.parametrize(
    ("trigger", "last", "now", "due"),
    [
        ("event", T0, T0 + MIN, True),
        ("interval", T0, T0 + 23 * H, False),
        ("interval", T0, T0 + 24 * H, True),
        ("daily", T0 + 5 * H, T0 + 23 * H, False),
        ("daily", T0 + 5 * H, T0 + 24 * H, True),
        ("weekly", T0, T0 + 6 * 24 * H, False),  # T0 is Monday 00:00
        ("weekly", T0, T0 + 7 * 24 * H, True),
        ("weekly", None, T0 + 3 * 24 * H, True),  # first evaluation always rebalances
    ],
)
def test_trigger_due(trigger, last, now, due) -> None:
    cfg = carry_config(evaluation__trigger=trigger)
    assert Trigger(cfg.evaluation).due(now, last) is due


def test_failed_exit_blocks_every_open_in_the_cycle() -> None:
    core, store = make()
    cycle(core, store, T0)
    # A's spread is 100 bps: the forced exit's IOC limit (mid + 25 bps) cannot cross it
    rows = [ticker(a, 100.0, 100.0 if a == "A" else 2.0) for a in FL]
    s1 = parse_tickers(tickers_envelope(T0 + 2 * MIN, rows))
    core.run_cycle(T0 + 2 * MIN, s1, store, SimExecutor(), allow_rebalance=True)
    s2 = parse_tickers(tickers_envelope(T0 + 18 * MIN, rows))
    res = core.run_cycle(T0 + 18 * MIN, s2, store, SimExecutor(), allow_rebalance=True)
    assert holdings(core) == {"A": -1, "B": -1, "E": 1, "F": 1}  # exit unfilled, no extra leg
    assert all(o.intent != "open" for o, _ in res.orders)
    # the neutrality unwind that assumed A was gone is held back too
    assert any(r.decision == "REJECT" and r.reason == "forced_exit_not_filled" for r in res.records)


def test_three_vetoed_passes_still_book_the_approved_group() -> None:
    """Review C1: the last re-plan pass must not unregister the legs it is about to send."""
    import dataclasses

    fl = {f"S{i}": 1e-4 - i * 1e-6 for i in range(1, 7)} | {f"L{i}": -1e-4 + i * 1e-6 for i in range(1, 7)}
    cfg = carry_config(universe=sorted(fl))
    deep = {"S1", "S2", "S3", "S4", "S6"}  # maintenance margin 3: R12 liquidation distance veto
    inst = {a: dataclasses.replace(instrument(a), maintenance_margin=3.0 if a in deep else 0.01) for a in fl}
    core = CarryCore(cfg, inst, fee_rate=0.0005, mode="backtest")
    store = FundingStore()
    for a, r in fl.items():
        store.merge_envelope(funding_envelope(a, flat_history(r, T0)))
    snap0 = parse_tickers(tickers_envelope(T0, [ticker(a, 100.0, 2.0) for a in fl]))
    res = core.run_cycle(T0, snap0, store, SimExecutor(), allow_rebalance=True)
    assert holdings(core) == {"S5": -1, "L5": 1}
    assert all(p.qty[p.key] != 0.0 for p in core.portfolio.positions.values())
    rej = {r.asset_in for r in res.records if r.decision == "REJECT"}
    assert {"L1+S1", "L3+S3", "L6+S6"} <= rej


def test_asset_removed_from_the_universe_is_exited_without_crashing() -> None:
    """Review M4."""
    core, store = make()
    cycle(core, store, T0)
    smaller = carry_config(universe=[a for a in FL if a != "A"])
    moved = CarryCore.from_state(core.to_state(), smaller, core.instruments, fee_rate=0.0005, mode="backtest")
    res = cycle(moved, store, T0 + 2 * MIN)
    assert "A" not in holdings(moved)
    assert any(r.decision == "EXIT" and r.reason == "not_in_universe" for r in res.records)


def test_held_asset_without_an_instrument_is_rejected_not_crashing() -> None:
    core, store = make()
    cycle(core, store, T0)
    core.instruments = {a: i for a, i in core.instruments.items() if a != "A"}
    core.risk_state.kill_switch = True
    res = cycle(core, store, T0 + 2 * MIN)
    assert "A" in holdings(core)  # cannot be priced into an order: kept, reported
    assert any(r.decision == "REJECT" and "A" in r.reason for r in res.records)


def test_open_that_does_not_fill_puts_the_asset_in_a_short_cooldown() -> None:
    class NoFill(SimExecutor):
        def execute(self, batch, snap):
            return [f for f in super().execute(batch, snap) if f[0].asset != "F"]

    core, store = make()
    core.run_cycle(T0, snap(T0), store, NoFill(), allow_rebalance=True)
    assert "F" in core.cooldown and core.cooldown["F"] > T0
    assert [p.id for p in core.dropped if p.key == "F"]  # the empty leg is reported for the Db
    assert all(p.qty[p.key] != 0.0 for p in core.portfolio.positions.values())


def test_scheduled_rebalance_is_not_used_up_while_entries_are_blocked() -> None:
    core, store = make(evaluation__trigger="weekly")
    core.risk_state.entries_blocked_day = T0.date()
    core.risk_state.day = T0.date()
    core.risk_state.day_start_equity = 265.0
    core.risk_state.peak_equity = 265.0
    cycle(core, store, T0)
    assert core.last_rebalance is None and core.portfolio.positions == {}


def test_open_on_an_already_held_side_is_vetoed_by_r10() -> None:
    from luna.risk.engine import check
    from luna.types import OrderRequest

    core, store = make(risk__max_positions=8)  # room for a fifth leg: only R10 can object
    cycle(core, store, T0)
    o = OrderRequest("x", T0, "E", "buy", 0.1, 100.2, "IOC", "open", "new-pos", "g")
    ctx = core._ctx(T0, snap(T0), core.portfolio.equity(), core.portfolio.holdings)
    r = check([core._order_check(o, 100.0, core.portfolio.equity())], ctx)
    assert r.vetoed and r.vetoed[0][1].startswith("R10")


def test_exit_reasons_do_not_leak_past_the_cycle() -> None:
    core, store = make()
    cycle(core, store, T0)
    rows = [ticker(a, 100.0, 100.0 if a == "A" else 2.0) for a in FL]
    for ts in (T0 + 2 * MIN, T0 + 18 * MIN):
        core.run_cycle(
            ts, parse_tickers(tickers_envelope(ts, rows)), store, SimExecutor(), allow_rebalance=True
        )
    assert "A" in holdings(core) and core.exit_reason == {}
