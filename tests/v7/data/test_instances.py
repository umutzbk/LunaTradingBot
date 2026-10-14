import pandas as pd

from luna.v7.data.instances import Instance, assign, build_instances, current_instance, instance_of
from luna.v7.timeutil import MIN, NS, D, to_ns

DAY0 = to_ns("2022-05-01")


def _bars(days_active, vol_days=None):
    """One 15m bar per listed day (12:00 UTC)."""
    rows = []
    for d in days_active:
        rows.append((DAY0 + d * D + 12 * 3600 * NS, 1.0))
    for d in vol_days or ():
        rows.append((DAY0 + d * D + 12 * 3600 * NS, 0.0))
    df = pd.DataFrame(rows, columns=["open_ts", "vol_base"]).sort_values("open_ts").reset_index(drop=True)
    return df


END = DAY0 + 22 * D


def test_dormancy_needs_seven_calendar_days():
    six = build_instances(_bars([0, 7]), END)  # days 1..6 inactive = 6 days
    seven = build_instances(_bars([0, 8]), END)  # days 1..7 inactive = 7 days
    assert len(six) == 1
    assert len(seven) == 2
    assert [i.idx for i in seven] == [0, 1]


def test_zero_volume_bars_do_not_prevent_dormancy():
    insts = build_instances(_bars([0, 9], vol_days=[2, 3, 4, 5]), END)
    assert len(insts) == 2


def test_end_known_at_is_close_plus_7x24h():
    last_open = to_ns("2022-05-12 16:00")
    df = pd.DataFrame({"open_ts": [to_ns("2022-05-08 00:00"), last_open, to_ns("2022-06-30 00:00")],
                       "vol_base": [1.0, 2.0, 1.0]})
    a, _ = build_instances(df, to_ns("2022-07-02"))
    assert a.last_trade_ts == to_ns("2022-05-12 16:15")
    assert a.end_known_at == to_ns("2022-05-19 16:15")


def test_trailing_dormancy_needs_complete_days_before_data_end():
    df = _bars([0, 1])
    last = DAY0 + 1 * D + 12 * 3600 * NS + 15 * MIN
    assert build_instances(df, DAY0 + 8 * D)[0].last_trade_ts is None  # days 2..7 = 6 complete days
    i = build_instances(df, DAY0 + 9 * D)[0]  # days 2..8 = 7 complete days
    assert i.last_trade_ts == last and i.end_known_at == last + 7 * D


def test_leading_period_does_not_end_anything():
    df = pd.DataFrame({"open_ts": [DAY0, DAY0 + 20 * D + 3600 * NS], "vol_base": [0.0, 1.0]})
    insts = build_instances(df, END)
    assert len(insts) == 1 and insts[0].first_bar_ts == DAY0


def test_current_instance_before_end_known():
    insts = build_instances(_bars([0, 20]), END)
    a = insts[0]
    assert current_instance(insts, a.end_known_at - 1).idx == 0
    assert current_instance(insts, a.end_known_at) is None
    assert current_instance(insts, insts[1].first_bar_ts).idx == 1
    assert current_instance(insts, insts[0].first_bar_ts - 1) is None


def test_relist_new_instance():
    insts = build_instances(_bars([0, 20]), END)
    assert insts[1].first_bar_ts == DAY0 + 20 * D + 12 * 3600 * NS
    assert insts[1].last_trade_ts is None and insts[1].end_known_at is None


def test_dormant_rows_belong_to_no_instance():
    insts = build_instances(_bars([0, 20]), END)
    a, b = insts
    assert instance_of(insts, a.first_bar_ts) == 0
    assert instance_of(insts, a.last_trade_ts - 1) == 0
    assert instance_of(insts, a.last_trade_ts) is None
    assert instance_of(insts, b.first_bar_ts - 1) is None
    assert instance_of(insts, b.first_bar_ts) == 1
    s = assign(pd.DataFrame({"t": [a.first_bar_ts, a.last_trade_ts + 3600 * NS, b.first_bar_ts]}), insts, "t")
    assert s.tolist() == [0, None, 1]


def test_no_active_bars_gives_no_instances():
    assert build_instances(pd.DataFrame({"open_ts": [DAY0], "vol_base": [0.0]}), END) == []
    assert isinstance(Instance(0, 1, None, None), Instance)
