"""The verdict-free V5 reference: needs the holdout token; the V5 cut-off is restored afterwards."""

import pandas as pd
import pytest

from luna.carry import backtest as bt
from luna.carry import frozen as fz
from luna.carry import history as hi
from luna.v6 import guard, v5ref


def test_refused_without_the_token():
    with pytest.raises(guard.HoldoutBlocked):
        v5ref.v5_reference(None, None)


def test_cutoff_is_restored_even_after_an_error():
    before = (fz.CUTOFF, hi.CUTOFF, bt.CUTOFF)
    with pytest.raises(RuntimeError):
        with v5ref._v5_cutoff(guard.HOLDOUT_END):
            assert fz.CUTOFF == hi.CUTOFF == bt.CUTOFF == guard.HOLDOUT_END
            raise RuntimeError("boom")
    assert (fz.CUTOFF, hi.CUTOFF, bt.CUTOFF) == before == (pd.Timestamp("2026-07-01T00:00Z"),) * 3
    with pytest.raises(Exception):
        bt.run_arm(None, None, {}, start=pd.Timestamp("2026-07-01T00:00Z"), end=pd.Timestamp("2026-07-03T00:00Z"))


def test_v5_files_are_not_modified_by_v6():
    import subprocess
    from luna.v6.guard import code_repo
    r = subprocess.run(["git", "-C", str(code_repo()), "diff", "--quiet", "fabd419", "HEAD", "--", "luna/carry",
                        "config/default.yaml"], capture_output=True)
    assert r.returncode == 0


def test_v5_w_and_e_run_unchanged_on_holdout_dated_synthetic_data(tmp_path, monkeypatch):
    from luna.v6.data import BarPanel
    from luna.v6.devdata import Inputs
    from luna.v6.spec import constants
    from tests.v6.world import build_world

    names = constants()["common"]["universe"]
    w = build_world(days=45, names=names, t0="2026-05-20T00:00Z")
    m = w.market
    insts = {a: {"perp": {"step": 0.001, "min_qty": 0.001, "tick": 0.01, "min_cost": 0.0},
                 "spot": {"step": 0.001, "min_qty": 0.001}} for a in names}
    inp = Inputs("synthetic", m.mark, BarPanel(m.mark.close), m.volume_usd, m.mark, m.volume_usd, m.mark, m.funding,
                 m.spreads, insts, w.prices)
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: [tmp_path / "a", tmp_path / "b"])
    monkeypatch.setattr(guard, "preflight", lambda repo=None: {"head": "test"})
    tok = guard.authorize_holdout(guard.approval_phrase())
    out = v5ref.v5_reference(inp, tok, end=pd.Timestamp("2026-07-03T00:00Z"))
    assert out["verdict_free"] and set(out) >= {"W", "E"} and len(out["W"]) == 2
    assert fz.CUTOFF == pd.Timestamp("2026-07-01T00:00Z")
