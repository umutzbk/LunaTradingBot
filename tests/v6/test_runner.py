"""The runner path end to end on synthetic inputs: 4 configs x rows -> gate -> evaluation -> the one
holdout decision. (Synthetic data, fixed forecasts; no market data.)"""

import pandas as pd
import pytest

from luna.v6 import guard, runner
from luna.v6.data import BarPanel
from luna.v6.devdata import Inputs
from luna.v6.verdict import FAMILY, holdout_decision
from tests.v6.world import FixedModel, build_world

DAY = pd.Timedelta("1D")


class Fixed720(FixedModel):
    h = 720


def synthetic_inputs():
    w = build_world()
    m = w.market
    insts = {a: {"perp": {"step": 0.001, "min_qty": 0.001}, "spot": {"step": 0.001, "min_qty": 0.001},
                 "step": 0.001, "min_qty": 0.001} for a in m.assets}
    return w, Inputs("synthetic", m.mark, BarPanel(m.mark.close), m.volume_usd, m.mark, m.volume_usd, m.mark,
                     m.funding, m.spreads, insts, w.prices)


def test_all_rows_of_every_config_run_and_the_holdout_decision_is_built():
    w, inp = synthetic_inputs()
    start, end = w.start, w.start + 6 * DAY
    models = {168: FixedModel(w.funding_levels), 720: Fixed720(w.funding_levels * 4)}
    evals = {}
    for name in FAMILY:
        runs = runner.run_config(inp, name, start, end, models=models)
        exp = set(runner.H3_ROWS if name.startswith("H3") else runner.H1_ROWS)
        assert set(runs) == exp
        ev, g = runner.evaluate_config(inp, name, runs, "holdout", end, B=500)
        assert g.cls == "INCONCLUSIVE"  # 6 days: below the minimum evidence
        evals[name] = ev
    d = holdout_decision(evals)
    assert set(d.verdicts) == set(FAMILY)
    assert all(v.cls != "FINAL GO" for v in d.verdicts.values())
    assert all(v.cls in ("MODEL INCONCLUSIVE", "INSUFFICIENT", "INCONCLUSIVE", "NO-GO", "MODEL INVALID")
               for v in d.verdicts.values())


def test_run_holdout_refuses_without_the_token():
    w, inp = synthetic_inputs()
    with pytest.raises(guard.HoldoutBlocked):
        runner.run_holdout(inp, None)


def test_report_copies_verdicts_and_pins_every_file(tmp_path):
    import json
    from luna.v6 import report
    w, inp = synthetic_inputs()
    start, end = w.start, w.start + 3 * DAY
    models = {168: FixedModel(w.funding_levels), 720: Fixed720(w.funding_levels * 4)}
    runs, evals, gates = {}, {}, {}
    for name in FAMILY:
        runs[name] = runner.run_config(inp, name, start, end, rows=("PRIMARY",), models=models)
        evals[name], gates[name] = runner.evaluate_config(inp, name, runs[name], "holdout", end, B=200)
    res = {"runs": runs, "evals": evals, "gates": gates, "decision": holdout_decision(evals), "references": {}}
    man = report.write_holdout(res, tmp_path / "out", {"synthetic": True})
    s = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    for c, v in res["decision"].verdicts.items():
        assert s["verdicts"][c]["class"] == v.cls
    assert len(man["tables"]) == 12 and "declared_interpretations" in s
    with pytest.raises(FileExistsError):
        report.write_holdout(res, tmp_path / "out", {})
