import pandas as pd

from luna.v7.data.instances import Instance
from luna.v7.market import P, first_bar_within, first_executable_after, halted, mark_at_settle
from luna.v7.timeutil import MIN, D, H, to_ns

T0 = to_ns("2024-03-01T00:00:00Z")


def m1(spec):
    """spec: list of (minute offset, volume, open price); close = open + 0.5."""
    return pd.DataFrame(
        [dict(open_ts=T0 + m * MIN, o=p, h=p, l=p, c=p + 0.5, vol_base=v, known_at=T0 + (m + 1) * MIN)
         for m, v, p in spec]
    )


def test_halted_window_half_open():
    tau = T0 + 100 * MIN
    assert not halted(m1([(40, 1, 1)]), tau)  # opens exactly tau - 60 min: inside
    assert halted(m1([(39, 1, 1)]), tau)
    assert halted(m1([(100, 1, 1)]), tau)  # opens at tau: outside
    assert not halted(m1([(99, 1, 1)]), tau)
    assert halted(m1([(70, 0, 1)]), tau)  # zero volume does not count


def test_P_and_fallback():
    b1 = m1([(0, 1, 5.0)])
    b15 = pd.DataFrame([dict(open_ts=T0 + 15 * MIN, o=7.0), dict(open_ts=T0, o=6.0)])
    assert P(b1, b15, T0) == 5.0
    assert P(b1, b15, T0 + 15 * MIN) == 7.0  # 1m bar absent -> 15m open
    assert P(b1, b15, T0 + 30 * MIN) is None


def test_first_bar_within():
    b = m1([(10, 1, 3.0), (200, 1, 4.0)])
    assert first_bar_within(b, T0, 60 * MIN) == (T0 + 10 * MIN, 3.0)
    assert first_bar_within(b, T0 + 11 * MIN, 60 * MIN) is None


def test_no_retroactive_fill():
    inst = Instance(0, T0, None, None)
    b = m1([(0, 1, 1.0), (1, 1, 1.0), (300, 1, 9.0), (301, 1, 9.5)])
    tau = T0 + 120 * MIN
    assert halted(b, tau)
    ts, px = first_executable_after(b, tau, inst)
    assert ts == T0 + 301 * MIN and px == 9.0 + 0.5  # close time/close of the bar opening at minute 300
    assert first_executable_after(b.iloc[:2], tau, inst) is None


def test_zero_volume_bar_not_executable():
    inst = Instance(0, T0, None, None)
    b = m1([(130, 0, 2.0), (131, 1, 3.0)])
    assert first_executable_after(b, T0 + 120 * MIN, inst) == (T0 + 132 * MIN, 3.5)


def test_no_cross_instance_executable():
    first = Instance(0, T0, T0 + 150 * MIN, T0 + 150 * MIN + 7 * D)
    b = m1([(400, 1, 9.0)])  # a later instance's bar
    assert first_executable_after(b, T0 + 120 * MIN, first) is None
    second = Instance(1, T0 + 300 * MIN, None, None)
    assert first_executable_after(b, T0 + 120 * MIN, second) == (T0 + 401 * MIN, 9.5)
    # an earlier instance's bar is not usable by the later one even when opening after tau
    assert first_executable_after(m1([(130, 1, 1.0)]), T0 + 120 * MIN, second) is None
    # a bar closing past the instance end is excluded
    b3 = m1([(149, 1, 1.0), (150, 1, 1.0)])
    assert first_executable_after(b3, T0 + 120 * MIN, first) == (T0 + 150 * MIN, 1.5)


def test_mark_fallback_order():
    s = T0 + 8 * H
    mark15 = pd.DataFrame([dict(open_ts=s - 15 * MIN, c=11.0)])
    last15 = pd.DataFrame([dict(open_ts=s - 15 * MIN, c=12.0)])
    assert mark_at_settle({"mark_at_settle": 10.0}, mark15, last15, s) == 10.0
    assert mark_at_settle({"mark_at_settle": float("nan")}, mark15, last15, s) == 11.0
    assert mark_at_settle({"mark_at_settle": None}, mark15.iloc[:0], last15, s) == 12.0
    assert mark_at_settle({}, mark15.iloc[:0], last15.iloc[:0], s) is None
    other = pd.DataFrame([dict(open_ts=s, c=99.0)])  # bar starting at settlement is not the one ending there
    assert mark_at_settle({}, other, other, s) is None
