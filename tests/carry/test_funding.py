"""Kraken PF_ funding semantics (docs/results/v4-funding-semantics.md on the V4 branch):
period = 1h, API timestamp = start of the period the rate applies to, positive rate -> longs pay
shorts, negative -> shorts pay longs; accrual pro rata to the time held."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from luna.carry.funding import FundingLedger, FundingStore, carry_per_hour, funding_cashflow
from tests.carry.helpers import T0, flat_history, funding_envelope

H = pd.Timedelta("1h")


# --- sign -------------------------------------------------------------------------------------


def test_positive_rate_long_pays_short_receives() -> None:
    assert funding_cashflow(qty=2.0, funding_abs=0.5) == pytest.approx(-1.0)
    assert funding_cashflow(qty=-2.0, funding_abs=0.5) == pytest.approx(1.0)


def test_negative_rate_short_pays_long_receives() -> None:
    assert funding_cashflow(qty=2.0, funding_abs=-0.5) == pytest.approx(1.0)
    assert funding_cashflow(qty=-2.0, funding_abs=-0.5) == pytest.approx(-1.0)


def test_held_fraction_is_pro_rata_and_bounded() -> None:
    assert funding_cashflow(qty=-1.0, funding_abs=0.4, held_fraction=0.25) == pytest.approx(0.1)
    with pytest.raises(ValueError):
        funding_cashflow(1.0, 0.1, held_fraction=1.5)


def test_expected_carry_per_hour_sign_by_side() -> None:
    # high positive funding: the SHORT earns it, the long pays it
    assert carry_per_hour(side=-1, rel_rate=2e-5) == pytest.approx(2e-5)
    assert carry_per_hour(side=+1, rel_rate=2e-5) == pytest.approx(-2e-5)
    # negative funding: the LONG earns it
    assert carry_per_hour(side=+1, rel_rate=-3e-5) == pytest.approx(3e-5)
    assert carry_per_hour(side=-1, rel_rate=-3e-5) == pytest.approx(-3e-5)


# --- store / known-only trailing means ------------------------------------------------------------


def _store(rows: list[tuple[pd.Timestamp, float, float]], asset: str = "BTC") -> FundingStore:
    s = FundingStore()
    s.merge_envelope(funding_envelope(asset, rows))
    return s


def test_store_parses_kraken_payload_by_symbol() -> None:
    s = _store([(T0, 0.9, 1e-5)])
    assert s.assets() == ["BTC"]  # PF_XBTUSD -> BTC
    assert s.rate("BTC", T0) == (0.9, 1e-5)
    assert s.rate("BTC", T0 + H) is None


def test_trailing_mean_uses_only_ended_periods() -> None:
    rows = [(T0 - 2 * H, 0.0, 1e-5), (T0 - H, 0.0, 3e-5), (T0, 0.0, 9e-5)]
    s = _store(rows)
    # at T0 the period starting T0 has not ended: excluded; window [T0 - 2h, T0 - 1h]
    mean, n = s.trailing("BTC", T0, hours=2)
    assert n == 2 and mean == pytest.approx(2e-5)
    # one hour later the T0 period has ended and T0 - 2h has left the 2h window
    mean, n = s.trailing("BTC", T0 + H, hours=2)
    assert n == 2 and mean == pytest.approx(6e-5)


def test_trailing_mean_window_and_coverage_count() -> None:
    s = _store(flat_history(2e-5, T0, hours=720))
    mean, n = s.trailing("BTC", T0, hours=720)
    assert n == 720 and mean == pytest.approx(2e-5)
    mean, n = s.trailing("BTC", T0, hours=168)
    assert n == 168
    mean, n = s.trailing("ETH", T0, hours=168)
    assert n == 0 and math.isnan(mean)


def test_newest_end_and_merge_dedupes() -> None:
    s = _store([(T0 - H, 0.1, 1e-6)])
    s.merge_envelope(funding_envelope("BTC", [(T0 - H, 0.2, 2e-6), (T0, 0.3, 3e-6)]))
    assert s.rate("BTC", T0 - H) == (0.2, 2e-6)  # last write wins
    assert s.newest_end("BTC") == T0 + H
    assert s.newest_end("ETH") is None


def test_failed_envelope_is_ignored() -> None:
    env = funding_envelope("BTC", [(T0, 0.1, 1e-6)])
    env["ok"] = False
    s = FundingStore()
    s.merge_envelope(env)
    assert s.assets() == []


def test_prune_keeps_recent_periods() -> None:
    s = _store(flat_history(1e-5, T0, hours=48))
    s.prune(T0 - pd.Timedelta(hours=24))
    assert s.trailing("BTC", T0, hours=48)[1] == 24


# --- settlement ledger -----------------------------------------------------------------------------


def test_full_period_settles_after_period_end_with_known_rate() -> None:
    led = FundingLedger()
    led.on_fill("BTC", T0, qty=-2.0, position_id="p1")  # short 2 units from period start
    store = _store([(T0, 0.5, 5e-6)])
    assert led.settle(T0 + 30 * pd.Timedelta("1min"), store, grace=3 * H) == []  # period not ended
    out = led.settle(T0 + H, store, grace=3 * H)
    assert len(out) == 1
    s = out[0]
    assert s.period_start == T0 and s.known
    assert s.amounts == {"p1": pytest.approx(1.0)}  # short receives 2 x 0.5
    assert s.held_hours == pytest.approx(1.0)


def test_pro_rata_entry_and_exit_inside_a_period() -> None:
    led = FundingLedger()
    led.on_fill("ETH", T0 + 15 * pd.Timedelta("1min"), qty=4.0, position_id="L")  # long from :15
    led.on_fill("ETH", T0 + 45 * pd.Timedelta("1min"), qty=0.0, position_id="L")  # flat at :45
    store = _store([(T0, 0.1, 1e-6)], asset="ETH")
    out = led.settle(T0 + 2 * H, store, grace=3 * H)
    assert len(out) == 1
    assert out[0].amounts == {"L": pytest.approx(-4.0 * 0.1 * 0.5)}  # long pays, half an hour
    assert led.settle(T0 + 3 * H, store, grace=3 * H) == []  # flat: nothing more to settle
    assert led.assets() == []


def test_switch_within_a_period_attributes_each_position() -> None:
    led = FundingLedger()
    led.on_fill("SOL", T0, qty=-1.0, position_id="a")
    led.on_fill("SOL", T0 + 30 * pd.Timedelta("1min"), qty=0.0, position_id="a")
    led.on_fill("SOL", T0 + 30 * pd.Timedelta("1min"), qty=2.0, position_id="b")
    out = led.settle(T0 + H, _store([(T0, 1.0, 1e-2)], asset="SOL"), grace=3 * H)
    assert out[0].amounts == {"a": pytest.approx(0.5), "b": pytest.approx(-1.0)}


def test_unknown_rate_waits_for_grace_then_settles_zero_and_counts() -> None:
    led = FundingLedger()
    led.on_fill("BTC", T0, qty=1.0, position_id="p")
    empty = FundingStore()
    assert led.settle(T0 + 2 * H, empty, grace=3 * H) == []  # still waiting for the rate
    out = led.settle(T0 + 4 * H, empty, grace=3 * H)  # T0 ended at T0+1h, grace 3h passed
    assert [s.period_start for s in out] == [T0]
    assert not out[0].known and out[0].amounts == {"p": 0.0} and out[0].held_hours == pytest.approx(1.0)


def test_periods_settle_in_order_and_multi_hour_holding() -> None:
    led = FundingLedger()
    led.on_fill("BTC", T0, qty=1.0, position_id="p")
    store = _store([(T0, 0.1, 1e-6), (T0 + H, -0.2, -2e-6), (T0 + 2 * H, 0.3, 3e-6)])
    out = led.settle(T0 + 3 * H, store, grace=3 * H)
    assert [s.period_start for s in out] == [T0, T0 + H, T0 + 2 * H]
    assert [s.amounts["p"] for s in out] == pytest.approx([-0.1, 0.2, -0.3])


def test_ledger_round_trips_through_its_state_dict() -> None:
    led = FundingLedger()
    led.on_fill("BTC", T0 + 10 * pd.Timedelta("1min"), qty=1.0, position_id="p")
    clone = FundingLedger.from_dict(led.to_dict())
    store = _store([(T0, 0.6, 6e-6)])
    a = led.settle(T0 + H, store, grace=3 * H)
    b = clone.settle(T0 + H, store, grace=3 * H)
    assert a == b and a[0].amounts["p"] == pytest.approx(-0.6 * 50 / 60)
