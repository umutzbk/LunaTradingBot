"""Frozen V4 dates and universe (spec §2, R1, §7.3, §7.5)."""

import pandas as pd

from luna.v4 import constants as c


def test_universe_is_the_collector_universe_without_mubarak():
    from luna.forward.config import UNIVERSE as COLLECTOR

    assert len(c.V4_UNIVERSE) == 20
    assert "MUBARAK" not in c.V4_UNIVERSE
    assert set(c.V4_UNIVERSE) == set(COLLECTOR) - {"MUBARAK"}
    assert list(c.V4_UNIVERSE) == sorted(c.V4_UNIVERSE)


def test_holdout_and_evidence_dates():
    assert c.V4_DATA_END == pd.Timestamp("2026-07-01T00:00:00Z")
    assert c.COLLECTOR_START_TS == pd.Timestamp("2026-10-09T11:52:48Z")
    assert c.V4_EVIDENCE_START_TS == pd.Timestamp("2026-11-10T00:00:00Z")
    # rule: first 00:00 UTC on or after collector_start + 31 days
    assert c.V4_EVIDENCE_START_TS == (c.COLLECTOR_START_TS + pd.Timedelta(days=31)).ceil("D")
    assert c.D0 == c.V4_EVIDENCE_START_TS
    assert [str(x.date()) for x in c.LOOKS] == ["2027-05-11", "2027-08-10", "2027-11-10"]


def test_stage1_window():
    assert c.STAGE1_START == pd.Timestamp("2025-10-06T00:00:00Z")
    assert c.STAGE1_EVAL_START == pd.Timestamp("2025-11-05T00:00:00Z")
    assert c.STAGE1_END == c.V4_DATA_END
    assert (c.STAGE1_END - c.STAGE1_EVAL_START).days >= 182
