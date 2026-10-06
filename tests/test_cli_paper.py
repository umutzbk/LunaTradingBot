import logging

from typer.testing import CliRunner

import luna.instruments
from luna.cli import app
from luna.config import load_settings
from luna.engine.backtest import default_instruments
from luna.store.db import Db


def test_paper_help_lists_options():
    res = CliRunner().invoke(app, ["paper", "--help"])
    assert res.exit_code == 0
    for opt in ("--account", "--db", "--once"):
        assert opt in res.output


def test_paper_refused_when_lock_held(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(luna.instruments, "load_instruments",
                        lambda settings, kind, *a, **k: default_instruments(["BTC"], kind, load_settings()))
    path = tmp_path / "paper.db"
    db = Db(f"sqlite:///{path.as_posix()}")
    assert db.acquire_lock("perp", "other-process")
    res = CliRunner().invoke(app, ["paper", "--account", "perp", "--db", str(path)])
    assert res.exit_code == 1
    assert "lock" in res.output
    assert logging.getLogger("httpx").level == logging.WARNING  # never logs the Telegram token URL
    assert db.get_state("perp", "lock")["owner"] == "other-process"
    db.close()
