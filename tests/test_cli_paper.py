import logging

import pytest
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


@pytest.fixture
def httpx_level():
    """The CLI sets the httpx logger level globally; restore it for the other tests."""
    lg = logging.getLogger("httpx")
    old = lg.level
    yield
    lg.setLevel(old)


def test_paper_refused_when_lock_held(tmp_path, monkeypatch, httpx_level):
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


def test_paper_default_db_is_per_account(tmp_path, monkeypatch, httpx_level):
    """Without --db the engine opens var/luna_<account>.db (a lock held there refuses the start)."""
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.setattr(luna.instruments, "load_instruments",
                        lambda settings, kind, *a, **k: default_instruments(["BTC"], kind, load_settings()))
    db = Db(f"sqlite:///{(tmp_path / 'luna_perp.db').as_posix()}")
    assert db.acquire_lock("perp", "other-process")
    res = CliRunner().invoke(app, ["paper", "--account", "perp"])
    assert res.exit_code == 1 and "lock" in res.output
    assert not (tmp_path / "luna.db").exists()
    db.close()


def test_paper_refuses_to_start_beside_unmigrated_legacy_db(tmp_path, monkeypatch, httpx_level):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    Db(f"sqlite:///{(tmp_path / 'luna.db').as_posix()}").close()
    res = CliRunner().invoke(app, ["paper", "--account", "perp"])
    assert res.exit_code == 1 and "migrate-db" in res.output
    assert not (tmp_path / "luna_perp.db").exists()
