"""Forward runner: scheduled looks only, after T_F and the holdout opening, in order; the family is
fixed once; FEAS measurement thresholds."""

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from luna.v6 import forward as fw
from luna.v6 import guard
from luna.v6.verdict import LookError


@pytest.fixture
def repo(tmp_path, monkeypatch):
    rec = tmp_path / guard.FREEZE_RECORD_REL
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"T_F": "2026-11-20T00:00:00Z"}), encoding="utf-8")
    ms = [tmp_path / "m1", tmp_path / "m2"]
    for m in ms:
        m.write_text("{}")
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: ms)
    return tmp_path


def test_day1_and_look_dates():
    assert fw.day1(pd.Timestamp("2026-11-20T00:00Z")) == pd.Timestamp("2026-11-20T00:00Z")
    assert fw.day1(pd.Timestamp("2026-11-20T00:01Z")) == pd.Timestamp("2026-11-21T00:00Z")
    assert fw.look_date(pd.Timestamp("2026-11-20T00:00Z"), 90) == pd.Timestamp("2027-02-18T00:00Z")


def test_looks_refused_before_their_date_and_before_the_code_freeze(repo, tmp_path):
    with pytest.raises(LookError):
        fw.check_look(90, pd.Timestamp("2027-02-17T23:59Z"), repo)
    assert fw.check_look(90, pd.Timestamp("2027-02-18T00:00Z"), repo) == pd.Timestamp("2027-02-18T00:00Z")
    with pytest.raises(LookError):
        fw.check_look(100, pd.Timestamp("2028-01-01T00:00Z"), repo)
    with pytest.raises(guard.HoldoutBlocked):
        fw.check_look(90, pd.Timestamp("2027-03-01T00:00Z"), tmp_path / "nowhere")


def test_family_is_fixed_once(repo):
    fw.init_family(["H3-k4"], ["H1-m1.5"], repo)
    with pytest.raises(LookError):
        fw.init_family(["H3-k6"], [], repo)
    fam = fw.load_state(repo)
    assert fam.status == {"H3-k4": "PENDING", "H3-k6": "CLOSED", "H1-m1.5": "PENDING", "H1-m2.5": "CLOSED"}


def test_feasibility_thresholds():
    ok = fw.FeasibilityMeasurement(0.5, 0.0125, True, True, 3)
    assert ok.established
    for bad in (fw.FeasibilityMeasurement(0.49, 0.01, True, True, 3), fw.FeasibilityMeasurement(0.9, 0.0126, True, True, 3),
                fw.FeasibilityMeasurement(0.9, 0.01, False, True, 3), fw.FeasibilityMeasurement(0.9, 0.01, True, False, 3),
                fw.FeasibilityMeasurement(0.9, 0.01, True, True, 2)):
        assert not bad.established


def test_nw_threshold_uses_the_rank_among_four():
    def e(p, nw):
        return (SimpleNamespace(stats={"p_go": p}), True, {"net": nw, "carry": nw})
    evals = {"H3-k4": e(0.0001, 0.0002), "H3-k6": e(0.5, 0.0001)}
    assert fw.nw_pass(evals, "H3-k4", 0.001)  # rank 1: 0.001 / 4 = 0.00025
    assert not fw.nw_pass({"H3-k4": e(0.0001, 0.0003)}, "H3-k4", 0.001)
