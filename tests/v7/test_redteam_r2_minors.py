"""Red-team round 2 minors (Task 29b): m4, the view store's own W_HOLD guard; m5, the cross-process stability
of the model identity (``runner.model_hash``) that the opening reproduces. Synthetic data only; no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from luna.v7 import guard, k9, runner, windows
from luna.v7.timeutil import MIN, D
from luna.v7.viewstore import ViewStore
from tests.v7 import leak_fixtures as lf

ROOT = Path(__file__).resolve().parents[2]


def test_view_store_get_is_guarded(tmp_path):
    """m4: ``ViewStore.get`` is public; a W_HOLD t is refused (K9-logged) without the verified opening before
    any file is read, as in ``runner.decision_pass``; a W_DEV t passes (a miss here)."""
    st = ViewStore(tmp_path / "v")
    st.bind({"universe": "synthetic"})
    t_hold = windows.W_HOLD[0] + 5 * MIN
    (tmp_path / "v" / f"{t_hold}.json").write_bytes(b"not read")  # a stored W_HOLD view file
    with k9.using(k9.K9Log(tmp_path / "k9")) as log:
        for auth in (None, object()):
            with pytest.raises(guard.HoldoutBlocked):
                st.get(t_hold, auth=auth)
        with pytest.raises(guard.HoldoutBlocked):
            st.get(t_hold)
        assert len(log.records()) == 3 and all("stored decision view" in json.dumps(r) for r in log.records())
        assert st.get(windows.W_DEV[0] + 30 * D + 5 * MIN) is None and st.stats["hits"] == 0


_HASHES = """
import json
from tests.v7 import leak_fixtures as lf
from luna.v7 import runner
out = {"final_en": runner.model_hash(lf.final(72, "en")),
       "final_lgb": runner.model_hash(lf.final(72, "lgb_reg")),
       "folds": [runner.model_hash(m) for m in lf.fold_models(horizons=(72,))[72]]}
print(json.dumps(out))
"""


def _hashes_in_subprocess(seed: str) -> dict:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    r = subprocess.run([sys.executable, "-c", _HASHES], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_model_identity_is_stable_across_processes():
    """m5: the fold and final model identity (``runner.model_hash``, the sha256 the LeakageReport and the
    opening's pin record: ``fold_model_hashes`` / ``final_model_hash``) is reproduced byte-identically by a
    fresh process with another ``PYTHONHASHSEED`` (EN and LightGBM final models, the fold models), so the
    opening process re-deriving the models reproduces the persisted, pinned identity."""
    here = {"final_en": runner.model_hash(lf.final(72, "en")),
            "final_lgb": runner.model_hash(lf.final(72, "lgb_reg")),
            "folds": [runner.model_hash(m) for m in lf.fold_models(horizons=(72,))[72]]}
    a, b = _hashes_in_subprocess("1"), _hashes_in_subprocess("4242")
    assert a == b == here
    assert len({here["final_en"], here["final_lgb"], *here["folds"]}) == 2 + len(here["folds"])
    rep = lf.run_checks("A2", 72)  # the identity the pre-opening record persists and the opening pins
    assert rep.final_model_hash == here["final_en"]
