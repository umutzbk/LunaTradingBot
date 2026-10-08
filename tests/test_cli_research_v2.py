"""CLI research-v2 wiring (the research itself is covered in tests/v2/test_runner.py)."""

from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

import luna.v2.data as data_mod
import luna.v2.report as report_mod
import luna.v2.runner as runner_mod
from luna.cli import app


class _Res:
    configs = [1, 2]
    span = (pd.Timestamp("2024-09-01", tz="UTC"), pd.Timestamp("2026-07-01", tz="UTC"))


class _Rep:
    class gonogo:  # noqa: N801
        verdict, headline = "INSUFFICIENT DATA", "n=0"

    candidate = "aday yok"


def _no_report(monkeypatch, seen):
    monkeypatch.setattr(report_mod, "build_v2_report", lambda res, inp, settings: _Rep())
    monkeypatch.setattr(
        report_mod,
        "write_v2_report",
        lambda rep, out_dir: seen.setdefault("report", Path(out_dir) / "report.html"),
    )


def test_cli_research_v2_wiring(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setattr("luna.instruments.load_instruments", lambda settings, account: {})
    monkeypatch.setattr(
        data_mod,
        "load_v2_inputs",
        lambda settings, store, account, inst, assets=None: seen.update(assets=assets) or "INP",
    )

    def fake_run(settings, account, inp, out_dir, **kw):
        seen.update(account=account, inp=inp, out_dir=out_dir, **kw)
        return _Res()

    monkeypatch.setattr(runner_mod, "run_research", fake_run)
    _no_report(monkeypatch, seen)
    r = CliRunner().invoke(app, ["research-v2", "--account", "spot", "--jobs", "3", "--assets", "BTC, ETH"])
    assert r.exit_code == 0, r.output
    assert seen["account"] == "spot" and seen["inp"] == "INP" and seen["jobs"] == 3
    assert seen["assets"] == ["BTC", "ETH"] and seen["resume"] is False
    assert Path(seen["out_dir"]).name.startswith("research_v2_spot_")
    assert "2024-09-01 .. 2026-07-01" in r.output and "candidate: aday yok" in r.output
    assert not (tmp_path / "holdout_registry.json").exists()


def test_cli_research_v2_resume(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setattr("luna.instruments.load_instruments", lambda settings, account: {})
    monkeypatch.setattr(data_mod, "load_v2_inputs", lambda *a, **k: "INP")
    monkeypatch.setattr(
        runner_mod,
        "run_research",
        lambda s, a, i, out_dir, **kw: seen.update(out_dir=out_dir, **kw) or _Res(),
    )
    _no_report(monkeypatch, seen)
    r = CliRunner().invoke(app, ["research-v2", "--resume", str(tmp_path / "old")])
    assert r.exit_code == 0, r.output
    assert seen["resume"] is True and Path(seen["out_dir"]) == tmp_path / "old"
