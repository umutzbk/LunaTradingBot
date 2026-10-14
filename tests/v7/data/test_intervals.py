import math

import pandas as pd

from luna.v7.data.instances import Instance
from luna.v7.data.intervals import annotate, u3_gap
from luna.v7.timeutil import MIN, NS, H, to_ns

T0 = to_ns("2022-01-01")
INSTS = [Instance(0, T0 - H, None, None)]


def _f(spacings_h, explicit=None, first_offset=0, rate=0.0001):
    """Funding frame: record 0 at T0, then cumulative spacings (hours); explicit aligned by index."""
    ts = [T0 + first_offset]
    for s in spacings_h:
        ts.append(ts[-1] + int(round(s * H)))
    n = len(ts)
    ex = list(explicit) if explicit is not None else [None] * n
    return pd.DataFrame({"funding_ts": ts, "rate": [rate] * n,
                         "interval_h_explicit": pd.array(ex, dtype="Int64")})


def test_explicit_controls():
    out = annotate(_f([8, 8, 8], explicit=[8, 8, 8, 8]), INSTS)
    assert out["reconciled"].all() and not out["invalid"].any() and not out["interval_transition"].any()
    assert out["nominal_h"].tolist() == [8, 8, 8, 8]


def test_spacing_inferred_transition_valid_flagged():
    out = annotate(_f([8, 4, 2, 2], explicit=[8, None, None, None, None]), INSTS)
    assert not out["invalid"].any() and out["reconciled"].all()
    assert out["interval_transition"].tolist() == [False, False, True, True, False]
    assert out["nominal_h"].tolist() == [8, 8, 4, 2, 2]


def test_undetermined_first_then_standard_spacing_is_valid_transition():
    out = annotate(_f([8, 8]), INSTS)  # ALGO shape: first record has no explicit interval
    assert out["invalid"].tolist() == [True, False, False]
    assert out["interval_transition"].tolist() == [False, True, False]
    assert out["nominal_h"].tolist()[1:] == [8, 8]


def test_off_schedule_six_hour_invalid_successor_valid():
    out = annotate(_f([8, 8, 6, 8], explicit=[8, None, None, None, None]), INSTS)
    assert out["invalid"].tolist() == [False, False, False, True, False]
    assert out.loc[3, "reconciled"] == False  # noqa: E712
    assert math.isnan(out.loc[3, "r8"])
    assert out.loc[3, "nominal_h"] == 8  # inherited previous nominal, flagged
    assert out.loc[3, "nominal_inherited"]
    # successor: 8 h spacing to the invalid record equals the previous nominal 8 -> valid, no transition
    assert out.loc[4, "reconciled"] and not out.loc[4, "invalid"]


def test_successor_of_invalid_is_valid_transition_when_prev_nominal_differs():
    out = annotate(_f([2, 6, 8], explicit=[2, None, None, None]), INSTS)
    assert out["invalid"].tolist() == [False, False, True, False]
    assert out.loc[3, "interval_transition"] and out.loc[3, "nominal_h"] == 8


def test_explicit_conflicts_spacing_invalid():
    out = annotate(_f([8, 4], explicit=[8, 8, 2]), INSTS)  # spacing 4 h, previous 8 h, explicit 2 h
    assert out.loc[2, "invalid"] and not out.loc[2, "reconciled"]
    assert out.loc[2, "nominal_h"] == 8
    assert math.isnan(out.loc[2, "r8"])


def test_first_record_needs_explicit():
    a = annotate(_f([8], explicit=[None, 8]), INSTS)
    assert a.loc[0, "invalid"] and not a.loc[0, "reconciled"] and pd.isna(a.loc[0, "nominal_h"])
    b = annotate(_f([8], explicit=[8, None]), INSTS)
    assert b.loc[0, "reconciled"] and not b.loc[0, "invalid"]


def test_spacing_tolerance_60s():
    f = _f([8, 8], explicit=[8, None, None])
    f.loc[2, "funding_ts"] += 59 * NS
    assert not annotate(f, INSTS).loc[2, "invalid"]
    f.loc[2, "funding_ts"] += 2 * NS
    assert annotate(f, INSTS).loc[2, "invalid"]


def test_never_crosses_instance():
    insts = [Instance(0, T0 - H, T0 + 20 * H, T0 + 20 * H + 168 * H), Instance(1, T0 + 40 * H, None, None)]
    # records at 0, 8, 16, 24 (outside: last_trade at 20 h), 48 (new instance), 56, 64
    f = pd.DataFrame({"funding_ts": [T0 + k * H for k in (0, 8, 16, 24, 48, 56, 64)], "rate": 0.0001,
                      "interval_h_explicit": pd.array([8] + [None] * 6, dtype="Int64")})
    out = annotate(f, insts)
    assert out["instance"].tolist() == [0, 0, 0, None, 1, 1, 1]
    assert out["in_instance"].tolist() == [True, True, True, False, True, True, True]
    out_out = out.loc[3]
    assert not out_out["reconciled"] and not out_out["invalid"] and math.isnan(out_out["r8"])
    # first record of instance 1 has no explicit -> invalid; nothing inherited from instance 0
    assert out.loc[4, "invalid"] and pd.isna(out.loc[4, "nominal_h"])
    assert out.loc[5, "interval_transition"] and not out.loc[5, "invalid"]


def test_r8_normalisation():
    out = annotate(_f([4, 4], explicit=[4, 4, 4], rate=0.0001), INSTS)
    assert abs(out.loc[1, "r8"] - 0.0002) < 1e-15


def test_r8_nan_when_undetermined():
    out = annotate(_f([8]), INSTS)
    assert math.isnan(out.loc[0, "r8"]) and not math.isnan(out.loc[1, "r8"])


def test_u3_gap_adjacent_rule():
    assert not u3_gap(8, 4, 8 * H + 60 * NS)
    assert u3_gap(8, 4, 8 * H + 61 * NS)
    assert not u3_gap(2, 8, 8 * H)  # max of the two
    assert u3_gap(4, 2, 4 * H + MIN + 1)
    assert u3_gap(float("nan"), float("nan"), 8 * H)  # fail closed when both undetermined
    assert not u3_gap(float("nan"), 8, 8 * H)


def test_u3_gap_accepts_pd_na_and_annotate_output():
    assert not u3_gap(pd.NA, 8, 8 * H)
    assert u3_gap(pd.NA, pd.NA, 8 * H)
    out = annotate(_f([8]), INSTS)  # record 0 undetermined (pd.NA), record 1 nominal 8
    assert not u3_gap(out["nominal_h"].iloc[0], out["nominal_h"].iloc[1], 8 * H)
    assert u3_gap(out["nominal_h"].iloc[0], out["nominal_h"].iloc[1], 8 * H + 61 * NS)


def test_multi_symbol_annotation_and_errors():
    import pytest

    a = _f([8, 8], explicit=[8, None, None])
    b = _f([4, 4], explicit=[4, None, None], first_offset=H)
    a["symbol"], b["symbol"] = "AAA", "BBB"
    f = pd.concat([b, a], ignore_index=True)  # interleaved order, unique index
    out = annotate(f, {"AAA": INSTS, "BBB": INSTS})
    assert out.index.tolist() == f.index.tolist()
    assert out["nominal_h"].tolist() == [4, 4, 4, 8, 8, 8]
    assert not out["invalid"].any()
    with pytest.raises(ValueError):
        annotate(f, {"AAA": INSTS})  # BBB missing
    with pytest.raises(ValueError):
        annotate(f, INSTS)  # symbol column needs a dict
    with pytest.raises(ValueError):
        annotate(f.set_axis([0, 0, 1, 2, 3, 4]), {"AAA": INSTS, "BBB": INSTS})
    with pytest.raises(ValueError):
        annotate(_f([8]), {"AAA": INSTS})


def test_explicit_bearing_cases_pinned():
    # explicit 4 with spacing 4 after nominal 8: reconciled via own explicit, no transition flag
    out = annotate(_f([8, 4], explicit=[8, 8, 4]), INSTS)
    assert out.loc[2, "reconciled"] and not out.loc[2, "invalid"] and not out.loc[2, "interval_transition"]
    assert out.loc[2, "nominal_h"] == 4
    # explicit 4 with spacing 8 equal to previous nominal 8: reconciled (spec-literal), nominal 4
    out = annotate(_f([8, 8], explicit=[8, 8, 4]), INSTS)
    assert out.loc[2, "reconciled"] and not out.loc[2, "invalid"] and out.loc[2, "nominal_h"] == 4


def test_membership_end_exclusive_at_last_close():
    insts = [Instance(0, T0 - H, T0 + 8 * H, T0 + 8 * H + 168 * H)]
    f = pd.DataFrame({"funding_ts": [T0, T0 + 8 * H], "rate": 0.0001,
                      "interval_h_explicit": pd.array([8, None], dtype="Int64")})
    out = annotate(f, insts)
    assert out["in_instance"].tolist() == [True, False]  # a settlement exactly at the final close is outside
