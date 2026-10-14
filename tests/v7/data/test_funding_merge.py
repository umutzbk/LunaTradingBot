import pandas as pd
import pytest

from luna.v7.data import funding
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.timeutil import MIN, NS, from_ms

T0_MS = 1_700_000_000_000
H_MS = 3_600_000 * 8


def _archive(n, skip=(), jitter_ms=0, rate=0.0001):
    lines = ["calc_time,funding_interval_hours,last_funding_rate"]
    for i in range(n):
        if i not in skip:
            lines.append(f"{T0_MS + i * H_MS + jitter_ms},8,{rate + i * 1e-6:.8f}")
    return funding.parse_archive_csv("\n".join(lines) + "\n", "ms")


def _rest(idx, jitter_ms=0, dr=0.0, rate=0.0001):
    recs = [{"fundingTime": T0_MS + i * H_MS + jitter_ms, "fundingRate": f"{rate + i * 1e-6 + dr:.8f}",
             "markPrice": "100.5"} for i in idx]
    return funding.normalise_rest(pd.DataFrame(recs), "ms")


def test_parse_archive_shapes():
    a = _archive(2)
    assert list(a.columns) == ["funding_ts", "rate", "interval_h_explicit", "mark_at_settle"]
    assert a["funding_ts"].iloc[0] == from_ms(T0_MS)
    assert a["interval_h_explicit"].iloc[0] == 8
    assert str(a["interval_h_explicit"].dtype) == "Int64"


def test_funding_merge_archive_wins_rest_fills_gap():
    a = _archive(5, skip=(3,))
    r = _rest([1, 2, 3], jitter_ms=20_000)  # 1,2 overlap within 60 s and agree; 3 fills
    out = funding.merge(a, r)
    assert list(out.columns) == list(funding.OUT_COLUMNS)
    assert len(out) == 5
    assert out["source"].tolist() == ["archive", "archive", "archive", "rest", "archive"]
    assert out["funding_ts"].is_monotonic_increasing
    assert (out["known_at"] - out["funding_ts"] == MIN).all()
    gap = out.iloc[3]
    assert gap["mark_at_settle"] == 100.5 and pd.isna(gap["interval_h_explicit"])
    assert out.iloc[1]["funding_ts"] == from_ms(T0_MS + H_MS)  # archive timestamp kept
    # overlapping rows: archive rate/ts/source kept, null mark filled from REST with explicit provenance
    assert out.iloc[1]["mark_at_settle"] == 100.5 and out.iloc[1]["mark_source"] == "rest"
    assert out.iloc[1]["source"] == "archive"
    assert pd.isna(out.iloc[0]["mark_at_settle"]) and out.iloc[0]["mark_source"] is None
    assert out.iloc[3]["mark_source"] == "rest"


def test_funding_conflict_raises():
    with pytest.raises(IntegrityBlocker):
        funding.merge(_archive(3), _rest([1], dr=1e-6))
    # within tolerance 1e-12 is not a conflict
    assert len(funding.merge(_archive(3), _rest([1], dr=1e-13))) == 3


def test_funding_timestamp_mapping_conflicts():
    a = _archive(3)
    r = funding.normalise_rest(pd.DataFrame([
        {"fundingTime": T0_MS + H_MS - 50_000, "fundingRate": "0.000101", "markPrice": "1"},
        {"fundingTime": T0_MS + H_MS + 50_000, "fundingRate": "0.000101", "markPrice": "1"},
    ]), "ms")
    with pytest.raises(IntegrityBlocker):
        funding.merge(a, r)  # two REST records map to one archive settlement (100 s apart)
    # a REST record 61 s off is a distinct key and fills (not silently merged)
    r2 = funding.normalise_rest(pd.DataFrame([
        {"fundingTime": T0_MS + H_MS + 61_000, "fundingRate": "0.000101", "markPrice": "1"}]), "ms")
    assert len(funding.merge(a, r2)) == 4
    assert 61 * NS > 60 * NS


def test_funding_per_symbol():
    a = pd.concat([_archive(2).assign(symbol="A"), _archive(2).assign(symbol="B")], ignore_index=True)
    r = _rest([2]).assign(symbol="B")
    out = funding.merge(a, r)
    assert out.groupby("symbol").size().to_dict() == {"A": 2, "B": 3}


def test_funding_rejects_ambiguous_units():
    with pytest.raises(ValueError):
        funding.parse_archive_csv(f"{T0_MS},8,0.0001\n", "ns")
