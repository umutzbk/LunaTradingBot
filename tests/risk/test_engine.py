import dataclasses
import math
from datetime import date

import pandas as pd
import pytest

from luna.config import RiskLimits
from luna.risk.engine import (
    OrderCheck,
    RiskContext,
    RiskResult,
    apply_actions,
    check,
    portfolio_actions,
)
from luna.risk.state import RiskState, roll_day
from luna.types import OrderRequest

NOW = pd.Timestamp("2024-01-01 12:00", tz="UTC")
BAR = pd.Timedelta("15min")

PERP = dict(
    capital_usd=265,
    risk_per_trade=0.0075,
    max_risk_per_trade=0.01,
    max_open_risk=0.03,
    max_positions=3,
    max_gross_leverage=2.0,
    max_leg_notional_frac=1.0,
    daily_loss_limit=0.02,
    max_drawdown=0.15,
    max_orders_per_day=30,
    max_price_band=0.003,
    emergency_stop_frac=0.02,
    liq_distance_mult=10,
    allow_short=True,
    live_enabled=False,
    highvol_risk_mult=0.5,
    corr_bucket_threshold=0.7,
)
SPOT = {
    **PERP,
    "max_gross_leverage": 1.0,
    "max_leg_notional_frac": 0.5,
    "max_orders_per_day": 20,
    "max_price_band": 0.005,
    "allow_short": False,
}


def _state(**kw) -> RiskState:
    base = dict(day=date(2024, 1, 1), day_start_equity=1000.0, peak_equity=1000.0, orders_today=0)
    base.update(kw)
    return RiskState(**base)


def _ctx(**kw) -> RiskContext:
    kind = kw.pop("kind", "perp")
    limits_kw = kw.pop("limits_kw", {})
    state_kw = kw.pop("state_kw", {})
    base = dict(
        limits=RiskLimits(**{**(PERP if kind == "perp" else SPOT), **limits_kw}),
        kind=kind,
        mode="paper",
        state=_state(**state_kw),
        equity=1000.0,
        open_positions=0,
        open_risk=0.0,
        gross_notional=0.0,
        holdings={},
        last_bar_ts=NOW - BAR,  # last closed bar closes exactly at NOW
        now=NOW,
        liq_distance={"BTC": 0.5, "ETH": 0.5},
        min_qty={"BTC": 0.0001, "ETH": 0.001},
        min_notional={"BTC": 5.0, "ETH": 5.0},
        available_cash=1000.0,
        reconciliation_ok=True,
    )
    base.update(kw)
    return RiskContext(**base)


def _oc(**kw) -> OrderCheck:
    order_kw = {
        k: kw.pop(k)
        for k in ("client_id", "asset", "side", "qty", "limit_price", "intent", "position_id")
        if k in kw
    }
    order = OrderRequest(
        client_id=order_kw.get("client_id", "c1"),
        ts=NOW,
        asset=order_kw.get("asset", "BTC"),
        side=order_kw.get("side", "buy"),
        qty=order_kw.get("qty", 1.0),
        limit_price=order_kw.get("limit_price", 100.0),
        tif="IOC",
        intent=order_kw.get("intent", "open"),
        position_id=order_kw.get("position_id", "p1"),
        leg_group="g1",
    )
    base = dict(
        order=order,
        notional=100.0,
        new_position_risk=5.0,
        new_position_risk_frac=0.005,
        stop_distance_frac=0.05,
        has_stop=True,
        increases_abs_position=False,
        mid=100.0,
    )
    base.update(kw)
    return OrderCheck(**base)


def _close(**kw) -> OrderCheck:
    """A closing order: sells the 1 BTC that is held; carries no new risk and no stop."""
    kw.setdefault("intent", "close")
    kw.setdefault("side", "sell")
    kw.setdefault("new_position_risk", 0.0)
    kw.setdefault("new_position_risk_frac", 0.0)
    kw.setdefault("stop_distance_frac", None)
    kw.setdefault("has_stop", False)
    return _oc(**kw)


def _single(oc: OrderCheck, ctx: RiskContext) -> str | None:
    res = check([oc], ctx)
    assert len(res.approved) + len(res.vetoed) == 1
    return res.vetoed[0][1] if res.vetoed else None


# rule -> (ctx kwargs, order kwargs) that trip exactly that rule, and a sibling that passes.
CASES = {
    "R1_kill": (dict(state_kw={"kill_switch": True}), {}, dict(state_kw={"kill_switch": False}), {}),
    "R2_pause": (dict(state_kw={"paused": True}), {}, dict(state_kw={"paused": False}), {}),
    "R3_daily_loss": (dict(equity=980.0), {}, dict(equity=981.0), {}),
    "R4_drawdown": (
        dict(state_kw={"peak_equity": 1200.0}),
        {},
        dict(state_kw={"peak_equity": 1150.0}),
        {},
    ),
    "R5_risk_per_trade": ({}, dict(new_position_risk_frac=0.011), {}, dict(new_position_risk_frac=0.01)),
    "R6_open_risk": (dict(open_risk=26.0), {}, dict(open_risk=25.0), {}),
    "R7_max_positions": (dict(open_positions=3), {}, dict(open_positions=2), {}),
    "R8_gross_leverage": (dict(gross_notional=1950.0), {}, dict(gross_notional=1900.0), {}),
    "R9_leg_notional": ({}, dict(notional=1001.0), {}, dict(notional=1000.0)),
    "R10_pyramiding": ({}, dict(increases_abs_position=True), {}, dict(increases_abs_position=False)),
    "R11_no_stop": ({}, dict(has_stop=False), {}, dict(has_stop=True)),
    "R12_liquidation": (dict(liq_distance={"BTC": 0.49}), {}, dict(liq_distance={"BTC": 0.5}), {}),
    "R13_spot_short": (
        dict(kind="spot", holdings={"BTC": 0.5}),
        dict(side="sell"),
        dict(kind="spot", holdings={"BTC": 1.0}),
        dict(side="sell"),
    ),
    "R14_orders_per_day": (
        dict(state_kw={"orders_today": 30}),
        {},
        dict(state_kw={"orders_today": 29}),
        {},
    ),
    "R15_price_band": ({}, dict(limit_price=100.31), {}, dict(limit_price=100.29)),
    "R16_stale_data": (
        dict(last_bar_ts=NOW - BAR - pd.Timedelta("31min")),
        {},
        dict(last_bar_ts=NOW - BAR - pd.Timedelta("30min")),
        {},
    ),
    "R17_minimums_balance": ({}, dict(qty=0.00005), {}, dict(qty=0.0001)),
    "R18_reconciliation": (dict(reconciliation_ok=False), {}, dict(reconciliation_ok=True), {}),
    "R19_live_disabled": (dict(mode="live"), {}, dict(mode="live", limits_kw={"live_enabled": True}), {}),
}


@pytest.mark.parametrize("rule", sorted(CASES, key=lambda r: int(r.split("_")[0][1:])))
def test_rule_vetoes(rule):
    fail_ctx, fail_oc, pass_ctx, pass_oc = CASES[rule]
    reason = _single(_oc(**fail_oc), _ctx(**fail_ctx))
    assert reason is not None and reason.startswith(rule), reason
    assert _single(_oc(**pass_oc), _ctx(**pass_ctx)) is None


def test_all_nineteen_rules_covered():
    assert sorted(int(r.split("_")[0][1:]) for r in CASES) == list(range(1, 20))


def test_baseline_open_is_approved_and_result_shape():
    oc = _oc()
    res = check([oc], _ctx())
    assert isinstance(res, RiskResult)
    assert res.approved == [oc.order]
    assert res.vetoed == []


# --- exemptions -------------------------------------------------------------------------------


def test_close_orders_exempt_from_daily_loss_and_leverage():
    ctx = _ctx(
        equity=900.0,  # 10% day loss (R3) ...
        state_kw={"orders_today": 30},  # ... order budget exhausted (R14)
        gross_notional=5000.0,  # 5x gross (R8)
        open_positions=5,  # R7
        open_risk=100.0,  # R6
        last_bar_ts=NOW - pd.Timedelta("3h"),  # stale (R16)
        reconciliation_ok=False,  # R18
        holdings={"BTC": 1.0},
    )
    close = _close(notional=2000.0, new_position_risk_frac=0.5)  # also > leg notional (R9), R5
    assert _single(close, ctx) is None
    reduce = _close(intent="reduce", qty=0.5, notional=50.0)
    assert _single(reduce, ctx) is None
    reason = _single(_oc(), ctx)
    assert reason is not None and reason.startswith("R3_daily_loss")


def test_close_orders_pass_kill_and_pause():
    for state_kw, rule in (({"kill_switch": True}, "R1_kill"), ({"paused": True}, "R2_pause")):
        ctx = _ctx(state_kw=state_kw, holdings={"BTC": 1.0})
        assert _single(_close(), ctx) is None
        assert _single(_close(intent="reduce", qty=0.5, notional=50.0), ctx) is None
        reason = _single(_oc(), ctx)
        assert reason is not None and reason.startswith(rule)


def test_kill_from_flatten_action_still_lets_closes_through():
    ctx = _ctx(equity=840.0, state_kw={"day_start_equity": 840.0}, holdings={"BTC": 1.0})
    actions = portfolio_actions(ctx)
    assert "FLATTEN_AND_KILL" in actions
    killed = dataclasses.replace(ctx, state=apply_actions(ctx.state, actions, NOW))
    assert killed.state.kill_switch
    assert _single(_close(), killed) is None
    assert _single(_oc(), killed).startswith("R1_kill")


def test_close_orders_still_subject_to_band_minimums_and_live():
    ctx = _ctx(holdings={"BTC": 1.0})
    assert _single(_close(limit_price=99.0), ctx).startswith("R15_price_band")
    assert _single(_close(qty=0.00001, notional=0.001), ctx).startswith("R17_minimums_balance")
    live = _ctx(holdings={"BTC": 1.0}, mode="live")
    assert _single(_close(), live).startswith("R19_live_disabled")


def test_close_that_grows_position_is_pyramiding():
    ctx = _ctx(holdings={"BTC": 1.0})
    assert _single(_close(increases_abs_position=True), ctx).startswith("R10_pyramiding")


@pytest.mark.parametrize(
    "state_kw,holdings,side,qty",
    [
        ({"kill_switch": True, "paused": True}, {}, "sell", 1.0),  # close opens a short
        ({"kill_switch": True, "paused": True}, {}, "buy", 1.0),  # close opens a long
        ({}, {"BTC": 1.0}, "sell", 5.0),  # close flips 1 long into a 4 short
        ({}, {"BTC": 1.0}, "buy", 1.0),  # same side as the holding: grows it
        ({}, {"BTC": -1.0}, "sell", 0.5),  # same side as a short holding
        ({}, {"BTC": -1.0}, "buy", 1.5),  # flips a short into a long
    ],
)
def test_close_must_not_open_or_flip_exposure(state_kw, holdings, side, qty):
    ctx = _ctx(state_kw=state_kw, holdings=holdings)
    for intent in ("close", "reduce"):
        reason = _single(_close(intent=intent, side=side, qty=qty, notional=100.0 * qty), ctx)
        assert reason is not None and reason.startswith("R10_close_invalid"), reason


def test_close_valid_against_short_and_exact_size():
    assert _single(_close(side="buy", qty=1.0), _ctx(holdings={"BTC": -1.0})) is None
    assert _single(_close(qty=1.0 + 1e-12), _ctx(holdings={"BTC": 1.0})) is None  # float drift


def test_batch_close_updates_running_holdings():
    ctx = _ctx(state_kw={"kill_switch": True}, holdings={"BTC": 1.0})
    res = check([_close(client_id="a", qty=0.6), _close(client_id="b", qty=0.6)], ctx)
    assert [o.client_id for o in res.approved] == ["a"]
    assert res.vetoed[0][1].startswith("R10_close_invalid")


@pytest.mark.parametrize("intent", ["OPEN", "Close", "", "flatten"])
def test_unknown_intent_vetoed_first(intent):
    ctx = _ctx(state_kw={"kill_switch": True}, holdings={"BTC": 1.0})
    assert _single(_oc(intent=intent), ctx).startswith("R0_bad_intent")
    assert _single(_oc(intent=intent), _ctx()).startswith("R0_bad_intent")


# --- batches ----------------------------------------------------------------------------------


def test_batch_cannot_exceed_leverage_jointly():
    ctx = _ctx(gross_notional=1000.0)
    a = _oc(client_id="a", position_id="pa", notional=600.0)
    b = _oc(client_id="b", position_id="pb", notional=600.0)
    assert _single(a, ctx) is None and _single(b, ctx) is None  # each fine alone
    res = check([a, b], ctx)
    assert res.approved == [a.order]
    assert [(o.client_id, r.split(":")[0]) for o, r in res.vetoed] == [("b", "R8_gross_leverage")]


def test_batch_running_open_risk_positions_and_order_count():
    risk = check(
        [_oc(client_id=f"c{i}", position_id=f"p{i}", new_position_risk=8.0) for i in range(4)],
        _ctx(),
    )
    assert [o.client_id for o in risk.approved] == ["c0", "c1", "c2"]
    assert risk.vetoed[0][1].startswith("R6_open_risk")  # 4 * 8 = 32 > 30

    pos = check(
        [_oc(client_id=f"c{i}", position_id=f"p{i}", new_position_risk=1.0) for i in range(3)],
        _ctx(open_positions=1),
    )
    assert [o.client_id for o in pos.approved] == ["c0", "c1"]
    assert pos.vetoed[0][1].startswith("R7_max_positions")

    count = check(
        [_oc(client_id=f"c{i}", position_id=f"p{i}", new_position_risk=1.0) for i in range(3)],
        _ctx(state_kw={"orders_today": 28}, open_positions=0),
    )
    assert [o.client_id for o in count.approved] == ["c0", "c1"]
    assert count.vetoed[0][1].startswith("R14_orders_per_day")


def test_batch_closes_neither_consume_nor_free_open_budget():
    held = {"BTC": 10.0}
    # an approved close must not count against gross / risk / position slots ...
    res = check(
        [_close(client_id="x", notional=1500.0), _oc(client_id="o", position_id="p2", notional=1000.0)],
        _ctx(holdings=held, gross_notional=900.0, open_positions=2),
    )
    assert [o.client_id for o in res.approved] == ["x", "o"]
    # ... nor free it up for later orders in the same batch (it may not fill)
    res = check(
        [_close(client_id="x", notional=500.0), _oc(client_id="o", position_id="p2", notional=200.0)],
        _ctx(holdings=held, gross_notional=1900.0),
    )
    assert [o.client_id for o in res.approved] == ["x"]
    assert res.vetoed[0][1].startswith("R8_gross_leverage")
    # but it does count as an order today
    res = check(
        [_close(client_id="x"), _oc(client_id="o")], _ctx(holdings=held, state_kw={"orders_today": 29})
    )
    assert res.vetoed[0][1].startswith("R14_orders_per_day")


def test_pair_legs_share_one_position_slot():
    legs = [
        _oc(client_id="a", asset="BTC", position_id="pair1", new_position_risk=2.5),
        _oc(client_id="b", asset="ETH", side="sell", position_id="pair1", new_position_risk=2.5),
    ]
    res = check(legs, _ctx(open_positions=2))
    assert [o.client_id for o in res.approved] == ["a", "b"]


def test_vetoed_orders_do_not_consume_budget():
    big = _oc(client_id="big", position_id="p0", notional=1500.0)  # R9 veto (> 100% equity)
    ok = _oc(client_id="ok", position_id="p1", notional=1000.0)  # 1500 + 1000 would breach R8
    res = check([big, ok], _ctx())
    assert [o.client_id for o in res.approved] == ["ok"]
    assert res.vetoed[0][0].client_id == "big"


def test_spot_batch_tracks_holdings_and_cash():
    ctx = _ctx(kind="spot", holdings={"BTC": 1.0}, available_cash=150.0)
    sells = check(
        [
            _close(client_id="s1", qty=0.6, notional=60.0),
            _close(client_id="s2", qty=0.6, notional=60.0),
        ],
        ctx,
    )
    assert [o.client_id for o in sells.approved] == ["s1"]
    assert sells.vetoed[0][1].startswith("R10_close_invalid")

    buys = check(
        [
            _oc(client_id="b1", position_id="p1", notional=100.0),
            _oc(client_id="b2", position_id="p2", notional=100.0),
        ],
        ctx,
    )
    assert [o.client_id for o in buys.approved] == ["b1"]
    assert buys.vetoed[0][1].startswith("R17_minimums_balance")


def test_check_does_not_mutate_inputs():
    holdings = {"BTC": 1.0}
    ctx = _ctx(kind="spot", holdings=holdings)
    before = dataclasses.replace(ctx.state)
    check([_close(qty=0.5, notional=50.0), _oc(position_id="p9")], ctx)
    assert holdings == {"BTC": 1.0}
    assert ctx.state == before


# --- individual rule details -------------------------------------------------------------------


def test_r17_min_notional_and_spot_cash():
    assert _single(_oc(notional=4.0), _ctx()).startswith("R17_minimums_balance")
    spot = _ctx(kind="spot", available_cash=99.0)
    assert _single(_oc(), spot).startswith("R17_minimums_balance")
    assert _single(_oc(), _ctx(kind="spot", available_cash=100.0)) is None
    # perp: cash check does not apply (margin account)
    assert _single(_oc(), _ctx(available_cash=0.0)) is None


def test_r17_unknown_asset_minimums_fail_closed():
    assert _single(_oc(), _ctx(min_qty={}, min_notional={})).startswith("R17_minimums_balance")


def test_r12_perp_only_and_fail_closed():
    spot = _ctx(kind="spot", liq_distance={})
    assert _single(_oc(), spot) is None
    assert _single(_oc(), _ctx(liq_distance={})).startswith("R12_liquidation")
    assert _single(_oc(stop_distance_frac=None), _ctx()).startswith("R11_no_stop")


def test_r13_perp_short_allowed_unless_config_forbids():
    short = _oc(side="sell")
    assert _single(short, _ctx()) is None
    assert _single(short, _ctx(limits_kw={"allow_short": False})).startswith("R13_spot_short")
    assert _single(_oc(side="sell"), _ctx(kind="spot")).startswith("R13_spot_short")


def test_r3_r4_unknown_reference_equity_blocks_opens():
    assert _single(_oc(), _ctx(state_kw={"day_start_equity": None})).startswith("R3_daily_loss")
    assert _single(_oc(), _ctx(state_kw={"peak_equity": None})).startswith("R4_drawdown")


@pytest.mark.parametrize(
    "field,value,rule",
    [
        ("notional", math.nan, "R8_gross_leverage"),
        ("notional", math.inf, "R8_gross_leverage"),
        ("new_position_risk", math.nan, "R6_open_risk"),
        ("new_position_risk", -1.0, "R6_open_risk"),
        ("new_position_risk_frac", math.nan, "R5_risk_per_trade"),
        ("new_position_risk_frac", -0.001, "R5_risk_per_trade"),
        ("stop_distance_frac", math.nan, "R11_no_stop"),
        ("stop_distance_frac", math.inf, "R11_no_stop"),
        ("stop_distance_frac", 0.0, "R11_no_stop"),
        ("stop_distance_frac", -0.05, "R11_no_stop"),
        ("mid", math.nan, "R15_price_band"),
        ("mid", 0.0, "R15_price_band"),
    ],
)
def test_bad_order_inputs_fail_closed(field, value, rule):
    reason = _single(_oc(**{field: value}), _ctx())
    assert reason is not None and reason.startswith(rule), reason


@pytest.mark.parametrize(
    "field,rule",
    [("equity", "R3_daily_loss"), ("open_risk", "R6_open_risk"), ("gross_notional", "R8_gross_leverage")],
)
def test_non_finite_context_inputs_fail_closed(field, rule):
    reason = _single(_oc(), _ctx(**{field: math.nan}))
    assert reason is not None and reason.startswith(rule), reason


@pytest.mark.parametrize("mid", [100.0, 43251.7, 64000.1])
@pytest.mark.parametrize("kind", ["perp", "spot"])
def test_price_band_exact_edge_passes(mid, kind):
    band = (PERP if kind == "perp" else SPOT)["max_price_band"]
    ctx = _ctx(kind=kind, holdings={"BTC": 1.0}, available_cash=1e9)
    for side, sign in (("buy", 1), ("sell", -1)):
        edge = mid * (1 + sign * band)
        beyond = mid * (1 + sign * band * 1.001)
        assert _single(_oc(side=side, limit_price=edge, mid=mid), ctx) is None, (side, edge)
        reason = _single(_oc(side=side, limit_price=beyond, mid=mid), ctx)
        assert reason is not None and reason.startswith("R15_price_band"), (side, beyond)


def test_r7_existing_position_id_takes_no_new_slot():
    ctx = _ctx(open_positions=3, open_position_ids=frozenset({"pair1", "p2", "p3"}))
    retry = _oc(client_id="retry", asset="ETH", side="sell", position_id="pair1", new_position_risk=1.0)
    assert _single(retry, ctx) is None
    fresh = _oc(client_id="new", position_id="p4", new_position_risk=1.0)
    assert _single(fresh, ctx).startswith("R7_max_positions")
    res = check([retry, fresh], ctx)
    assert [o.client_id for o in res.approved] == ["retry"]


def test_rules_apply_in_spec_order():
    ctx = _ctx(state_kw={"kill_switch": True, "paused": True}, mode="live")
    assert _single(_oc(has_stop=False), ctx).startswith("R1_kill")
    ctx = _ctx(open_positions=3, gross_notional=1950.0)
    assert _single(_oc(), ctx).startswith("R7_max_positions")


def test_live_disabled_vetoes_live_mode_only():
    for mode in ("backtest", "paper"):
        assert _single(_oc(), _ctx(mode=mode)) is None
    assert _single(_oc(), _ctx(mode="live")).startswith("R19_live_disabled")
    assert _single(_oc(), _ctx(mode="live", limits_kw={"live_enabled": True})) is None
    spot_live = _ctx(kind="spot", mode="live", holdings={"BTC": 1.0})
    assert _single(_close(), spot_live).startswith("R19_live_disabled")


# --- portfolio actions ------------------------------------------------------------------------


def test_drawdown_from_peak_triggers_flatten_and_kill():
    # day start equal to equity: no day loss, only drawdown from the peak
    ctx = _ctx(equity=850.0, state_kw={"peak_equity": 1000.0, "day_start_equity": 850.0})
    assert portfolio_actions(ctx) == ["FLATTEN_AND_KILL"]
    ctx = _ctx(equity=851.0, state_kw={"peak_equity": 1000.0, "day_start_equity": 851.0})
    assert portfolio_actions(ctx) == []
    # measured from the peak, not from the day start
    ctx = _ctx(equity=1000.0, state_kw={"peak_equity": 1200.0, "day_start_equity": 1000.0})
    assert portfolio_actions(ctx) == ["FLATTEN_AND_KILL"]


def test_daily_loss_blocks_entries_today():
    ctx = _ctx(equity=980.0)
    assert portfolio_actions(ctx) == ["BLOCK_ENTRIES_TODAY"]
    assert portfolio_actions(_ctx(equity=981.0)) == []


def test_reconciliation_mismatch_pauses():
    ctx = _ctx(reconciliation_ok=False)
    actions = portfolio_actions(ctx)
    assert actions == ["PAUSE_RECONCILIATION"]
    state = apply_actions(ctx.state, actions, NOW)
    assert state.paused and not state.kill_switch
    assert ctx.state.paused is False  # input not mutated


def test_portfolio_actions_combined_and_ordered():
    ctx = _ctx(equity=800.0, reconciliation_ok=False)
    assert portfolio_actions(ctx) == ["FLATTEN_AND_KILL", "BLOCK_ENTRIES_TODAY", "PAUSE_RECONCILIATION"]
    state = apply_actions(ctx.state, portfolio_actions(ctx), NOW)
    assert state.kill_switch and state.paused


def test_apply_actions_block_entries_sets_only_the_day():
    state = _state()
    blocked = apply_actions(state, ["BLOCK_ENTRIES_TODAY"], NOW)
    assert blocked == dataclasses.replace(state, entries_blocked_day=date(2024, 1, 1))
    assert apply_actions(state, [], NOW) == state
    assert state.entries_blocked_day is None  # pure


def test_daily_loss_block_is_sticky_until_next_utc_day():
    ctx = _ctx(equity=980.0)  # -2.0% vs day start
    state = apply_actions(ctx.state, portfolio_actions(ctx), NOW)
    recovered = _ctx(equity=981.0, state_kw={"entries_blocked_day": state.entries_blocked_day})
    assert portfolio_actions(recovered) == []  # -1.9%: condition no longer holds ...
    assert _single(_oc(), recovered).startswith("R3_daily_loss")  # ... but entries stay blocked
    holding = dataclasses.replace(recovered, holdings={"BTC": 1.0})
    assert _single(_close(), holding) is None  # closes still allowed

    tomorrow = NOW + pd.Timedelta("1D")
    next_state = roll_day(state, tomorrow, 981.0)
    assert next_state.entries_blocked_day is None
    next_ctx = dataclasses.replace(recovered, state=next_state, now=tomorrow, last_bar_ts=tomorrow - BAR)
    assert _single(_oc(), next_ctx) is None


def test_apply_actions_rejects_naive_now():
    with pytest.raises(ValueError):
        apply_actions(_state(), ["BLOCK_ENTRIES_TODAY"], pd.Timestamp("2024-01-01"))


def test_portfolio_actions_nan_equity_blocks_entries_without_flatten():
    # broken marks must not trigger a permanent kill, but entries are blocked
    assert portfolio_actions(_ctx(equity=math.nan)) == ["BLOCK_ENTRIES_TODAY"]
    assert _single(_oc(), _ctx(equity=math.nan)).startswith("R3_daily_loss")


def test_portfolio_actions_unknown_reference_equity():
    assert portfolio_actions(_ctx(state_kw={"day_start_equity": None})) == ["BLOCK_ENTRIES_TODAY"]
    assert portfolio_actions(_ctx(state_kw={"peak_equity": None})) == []


# --- risk state -------------------------------------------------------------------------------


def test_day_rollover_resets_once():
    s0 = RiskState()
    t1 = pd.Timestamp("2024-01-01 00:15", tz="UTC")
    s1 = roll_day(s0, t1, 1000.0)
    assert (s1.day, s1.day_start_equity, s1.peak_equity, s1.orders_today) == (
        date(2024, 1, 1),
        1000.0,
        1000.0,
        0,
    )
    s1 = dataclasses.replace(s1, orders_today=7)

    # same UTC day: day start unchanged, peak tracks max
    s2 = roll_day(s1, pd.Timestamp("2024-01-01 23:45", tz="UTC"), 1100.0)
    assert (s2.day, s2.day_start_equity, s2.peak_equity, s2.orders_today) == (
        date(2024, 1, 1),
        1000.0,
        1100.0,
        7,
    )
    s2b = roll_day(s2, pd.Timestamp("2024-01-01 23:50", tz="UTC"), 1050.0)
    assert (s2b.day_start_equity, s2b.peak_equity) == (1000.0, 1100.0)

    # first call next day: reset
    s3 = roll_day(s2b, pd.Timestamp("2024-01-02 00:00", tz="UTC"), 1040.0)
    assert (s3.day, s3.day_start_equity, s3.peak_equity, s3.orders_today) == (
        date(2024, 1, 2),
        1040.0,
        1100.0,
        0,
    )
    s3 = dataclasses.replace(s3, orders_today=3)
    s4 = roll_day(s3, pd.Timestamp("2024-01-02 00:15", tz="UTC"), 1030.0)
    assert (s4.day_start_equity, s4.orders_today) == (1040.0, 3)

    # downtime across two midnights: reset once, to the equity at the current tick
    s5 = roll_day(s4, pd.Timestamp("2024-01-04 09:30", tz="UTC"), 990.0)
    assert (s5.day, s5.day_start_equity, s5.orders_today) == (date(2024, 1, 4), 990.0, 0)
    s6 = roll_day(s5, pd.Timestamp("2024-01-04 09:45", tz="UTC"), 980.0)
    assert (s6.day, s6.day_start_equity) == (date(2024, 1, 4), 990.0)


def test_roll_day_uses_utc_date_and_preserves_flags():
    s = RiskState(kill_switch=True, paused=True)
    # 2024-01-01 23:30 in UTC-5 is 2024-01-02 04:30 UTC
    t = pd.Timestamp("2024-01-01 23:30", tz="America/New_York")
    out = roll_day(s, t, 500.0)
    assert out.day == date(2024, 1, 2)
    assert out.kill_switch and out.paused
    assert s.day is None  # pure


def test_roll_day_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        roll_day(RiskState(), pd.Timestamp("2024-01-01"), 1000.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_roll_day_rejects_non_finite_equity(bad):
    with pytest.raises(ValueError):
        roll_day(RiskState(), NOW, bad)


def test_roll_day_ignores_clock_going_backwards():
    s = roll_day(RiskState(), pd.Timestamp("2024-01-05", tz="UTC"), 1000.0)
    back = roll_day(s, pd.Timestamp("2024-01-04 23:00", tz="UTC"), 900.0)
    assert (back.day, back.day_start_equity) == (date(2024, 1, 5), 1000.0)
