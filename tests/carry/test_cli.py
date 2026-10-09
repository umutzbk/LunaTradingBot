from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from luna.cli import app
from tests.carry.test_engine import Clock, FakeSource, make_engine


def _evaluate(tmp_path: Path, now: pd.Timestamp, snap_ts: pd.Timestamp) -> None:
    src, clock = FakeSource(), Clock(now)
    src.set(snap_ts)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(now)
    e.close()
    e.db.close()


def test_carry_health_ok_after_an_evaluation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = pd.Timestamp.now(tz="UTC").floor("s")
    _evaluate(tmp_path, now, now)
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    r = CliRunner().invoke(app, ["carry-health"])
    assert r.exit_code == 0, r.output
    assert "ok" in r.output


def test_carry_health_fails_without_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    r = CliRunner().invoke(app, ["carry-health"])
    assert r.exit_code == 1 and "missing" in r.output


def test_carry_health_fails_on_stale_collector_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = pd.Timestamp.now(tz="UTC").floor("s")
    _evaluate(tmp_path, now, now - pd.Timedelta(minutes=20))
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    r = CliRunner().invoke(app, ["carry-health"])
    assert r.exit_code == 1 and "stale" in r.output


def test_compose_has_a_paper_only_carry_service() -> None:
    dc = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    svc = dc["services"]["bot-carry"]
    assert svc["command"] == ["carry"]
    assert svc["healthcheck"]["test"][-1] == "carry-health"
    assert svc["restart"] == "unless-stopped"
