"""Task 26c carried items from the Task 26b review.

1. ``ResearchUniverse.for_decisions`` refuses an anchor row (a 15m / R4 row loaded from before the look-back
   start) whose ``known_at`` > lo - 1 min: anchors are exact only when they are point-in-time for every
   decision of the chunk (enforced for every source, ``InMemorySource`` included).
2. The view-store key body carries the Python / numpy / pandas / pyarrow / scikit-learn / lightgbm versions
   next to the code identity; a store keyed under other versions is refused.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from luna.v7 import runner, viewstore
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.timeutil import MIN, D
from luna.v7.viewstore import ViewStore
from tests.v7 import fixtures_v7 as fx
from tests.v7 import perf_fixtures as pf

LO = pf.START + 100 * D  # decision chunk start: GAPUSDT's premium bars are an anchor only (45-day hole)


@pytest.fixture(scope="module")
def full_inputs() -> dict[str, runner.SymbolInputs]:
    full = pf.build_inputs()
    out = {}
    for s in ("BTCUSDT", "GAPUSDT"):
        x = full[s]
        if s == "GAPUSDT":
            f = x.r4.premium
            keep = (f["open_ts"] < pf.START + 60 * D) | (f["open_ts"] >= pf.START + 105 * D)
            x = replace(x, r4=replace(x.r4, premium=f[keep].reset_index(drop=True)))
        out[s] = x
    return out


def _with_anchor_known_at(inputs, ka: int) -> fx.LedgeredSource:
    x = inputs["GAPUSDT"]
    f = x.r4.premium.copy()
    i = int((f["open_ts"] < pf.START + 60 * D).to_numpy().nonzero()[0][-1])  # the anchor row
    f.loc[i, "known_at"] = ka
    return fx.LedgeredSource({**inputs, "GAPUSDT": replace(x, r4=replace(x.r4, premium=f))})


def test_for_decisions_refuses_an_anchor_not_pit_for_the_whole_chunk(full_inputs):
    ok = runner.ResearchUniverse.for_decisions(_with_anchor_known_at(full_inputs, LO - MIN), LO, LO + D,
                                               history_lo=pf.START)
    prem = ok.inputs["GAPUSDT"].r4.premium
    assert len(prem) == 1 and int(prem["known_at"].iloc[0]) == LO - MIN  # the anchor, known at lo - 1 min
    with pytest.raises(ValueError, match="anchor"):
        runner.ResearchUniverse.for_decisions(_with_anchor_known_at(full_inputs, LO), LO, LO + D,
                                              history_lo=pf.START)


def test_view_key_body_carries_library_versions_and_a_mismatch_refuses(tmp_path, monkeypatch):
    libs = viewstore.library_versions()
    assert set(libs) == {"python", "numpy", "pandas", "pyarrow", "scikit-learn", "lightgbm"}
    assert all(isinstance(v, str) and v for v in libs.values())
    body = runner.view_body({"inputs": "x"}, 0, ["AUSDT"], "BTCUSDT", True)
    assert body["libraries"] == libs and body["code"] == viewstore.code_identity()
    assert body["format"] == viewstore.FORMAT == "decisionview/parquet+json/v3"
    ViewStore(tmp_path / "v").bind(body)
    ViewStore(tmp_path / "v").bind(runner.view_body({"inputs": "x"}, 0, ["AUSDT"], "BTCUSDT", True))
    monkeypatch.setattr(viewstore, "_LIBS", [{**libs, "pandas": "0.0.0"}])
    other = runner.view_body({"inputs": "x"}, 0, ["AUSDT"], "BTCUSDT", True)
    assert other["libraries"]["pandas"] == "0.0.0"
    with pytest.raises(IntegrityBlocker, match="another key"):
        ViewStore(tmp_path / "v").bind(other)
