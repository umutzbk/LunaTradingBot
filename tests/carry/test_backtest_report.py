"""Real-input loader (always flagged real), report files, CLI refusal (frozen protocol §12)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
from typer.testing import CliRunner

from luna.carry import backtest as bt
from luna.carry import frozen, report
from luna.cli import app
from tests.carry.backtest_fixtures import T, base_cfg, instruments, make_data
from tests.carry.helpers import carry_config
from tests.carry.test_frozen import _v5_spreads, src  # noqa: F401 - fixture


def test_loader_marks_the_frozen_inputs_real_and_builds_instruments(src, tmp_path):  # noqa: F811
    var, inst = src
    inst.write_text(json.dumps({"assets": {"AAA": {"perp": {"step": 0.01, "min_qty": 0.01, "min_cost": 1.0,
                                                            "tick": 0.1}}}}), encoding="utf-8")
    f = var / "history_v4" / "funding" / "AAA.parquet"
    fr = pd.read_parquet(f)
    fr["funding_abs"] = fr["funding_rel"] * 100.0
    fr.to_parquet(f)
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"], history_v5_spreads=_v5_spreads(tmp_path))
    data, insts = bt.load_historical(dst, frozen.sha256_file(dst / "manifest.json"))
    assert data.real is True and set(data.spreads) == {"AAA"} and data.spreads["AAA"].index.max() < frozen.CUTOFF
    i = insts["AAA"]
    assert (i.qty_step, i.min_qty, i.min_notional, i.price_tick, i.taker_fee) == (0.01, 0.01, 1.0, 0.1, 0.0005)
    with pytest.raises(bt.OutcomeBlocked):
        bt.run_arm(data, bt.arm_config(carry_config(universe=["AAA", "B1", "C1", "D1"]), "W"), insts)
    with pytest.raises(frozen.FrozenInputError):
        bt.load_historical(dst, "0" * 64)


def test_report_files_and_summary(tmp_path):
    data = make_data(3)
    study = bt.run_study(data, base_cfg(), instruments(), start=T, end=T + pd.Timedelta("2D"), B=200,
                         with_variants=True, with_ideal=True)
    meta = {"commit": "abc", "tag": "t", "protocol_sha256": "p", "manifest_sha256": "m", "synthetic": True}
    out = report.write_report(study, None, tmp_path / "res", meta)
    s = json.loads((tmp_path / "res" / "summary.json").read_text(encoding="utf-8"))
    m = json.loads((tmp_path / "res" / "manifest.json").read_text(encoding="utf-8"))
    assert m["summary_sha256"] == frozen.sha256_file(tmp_path / "res" / "summary.json")
    assert s["verdict"]["final"] in ("ADVANTAGE", "CARRY ONLY", "NO ADVANTAGE SHOWN", "WORSE")
    assert s["label"] == "EXPLORATORY — reused development period"
    assert set(s["arms"]) >= {"W", "D", "C", "E", "P-TIGHT", "P-LOOSE", "P-C8"}
    assert set(s["arms"]["E"]) >= {"PRIMARY", "S-COST2X", "S-SPREAD-FIX", "S-NOKILL", "S-UNWIND", "S-PIT10", "S-FB-CONS"}
    assert "capture_ratio" in s["arms"]["E"]["PRIMARY"] and "fallback_exposure" in s
    assert (tmp_path / "res" / "report.md").read_text(encoding="utf-8").startswith("# V5 historical backtest")
    assert out["summary_sha256"] == m["summary_sha256"]


def test_cli_refuses_without_the_approval_and_writes_no_marker(tmp_path):
    from luna.carry import outcome_guard as og

    fz = tmp_path / "v" / "history_v5" / "nofrozen"
    before = [m.exists() for m in og.marker_paths(og.code_repo(), fz)]
    r = CliRunner().invoke(app, ["v5-backtest", "--frozen", str(fz), "--out", str(tmp_path / "out")])
    assert r.exit_code != 0 and "refused" in r.output
    assert [m.exists() for m in og.marker_paths(og.code_repo(), fz)] == before
    assert not (tmp_path / "out").exists()


def test_bridge_table_ends_at_w_in_both_orders():
    w = SimpleNamespace(daily=pd.DataFrame({"equity": [266.0], "carry_net": [1e-4]}), counters={})
    s1 = SimpleNamespace(daily=pd.DataFrame({"equity": [265.5], "carry_net": [2e-4]}), counters={})
    rows = report.assemble_bridge([("W-V4", s1), ("+exec_v5", s1)], [("W-V4", s1), ("+no_resize", s1)], w)
    names = [n for n, _ in rows]
    assert names == ["forward:W-V4", "forward:+exec_v5", "forward:engine->W", "reverse:W-V4", "reverse:+no_resize",
                     "reverse:engine->W"]
    assert rows[2][1] is w and rows[-1][1] is w


def test_paper_engine_never_loads_the_backtest_modules():
    import subprocess
    import sys

    code = ("import sys; import luna.carry.engine, luna.cli; "
            "bad = [m for m in ('luna.carry.backtest', 'luna.carry.stats', 'luna.carry.outcome_guard', "
            "'luna.carry.report', 'luna.carry.parity', 'luna.carry.history', 'luna.carry.verdict') "
            "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
