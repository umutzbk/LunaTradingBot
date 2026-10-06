import pytest

from luna.config import CostConfig
from luna.execution.costs import CostModel


@pytest.fixture
def perp_costs() -> CostModel:
    return CostModel(
        CostConfig(
            maker_fee=0.0002,
            taker_fee=0.0005,
            half_spread_bps={"BTC": 1, "ETH": 1, "default": 3},
            slippage_bps=2,
        )
    )


def test_cost_round_trip(perp_costs):
    assert perp_costs.round_trip_frac("BTC") == pytest.approx(2 * (0.0005 + 0.0001 + 0.0002))
    assert perp_costs.round_trip_frac("BTC") == pytest.approx(0.0016)
    assert perp_costs.round_trip_frac("BTC", "maker") == pytest.approx(2 * (0.0002 + 0.0001 + 0.0002))


def test_half_spread_falls_back_to_default(perp_costs):
    assert perp_costs.half_spread("ETH") == pytest.approx(0.0001)
    assert perp_costs.half_spread("SOL") == pytest.approx(0.0003)
    assert perp_costs.round_trip_frac("SOL") == pytest.approx(2 * (0.0005 + 0.0003 + 0.0002))


def test_fee_is_notional_times_rate(perp_costs):
    assert perp_costs.fee(1000.0, "taker") == pytest.approx(0.5)
    assert perp_costs.fee(1000.0, "maker") == pytest.approx(0.2)
