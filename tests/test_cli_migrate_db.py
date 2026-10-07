"""`luna migrate-db`: one-time move of the legacy shared var/luna.db to var/luna_<account>.db."""

import sqlite3

import pandas as pd
from typer.testing import CliRunner

from luna.cli import app
from luna.store.db import Db, db_path, default_url

T = pd.Timestamp("2026-10-06 12:00", tz="UTC")


def _migrate(tmp_path, monkeypatch, account="perp"):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    return CliRunner().invoke(app, ["migrate-db", "--account", account])


def test_per_account_paths(tmp_path):
    assert db_path(tmp_path, "spot") == tmp_path / "luna_spot.db"
    assert default_url(tmp_path, "perp") == f"sqlite:///{(tmp_path / 'luna_perp.db').as_posix()}"


def test_migrate_moves_legacy_db_with_all_rows(tmp_path, monkeypatch):
    legacy = tmp_path / "luna.db"
    d = Db(f"sqlite:///{legacy.as_posix()}")
    for k in range(3):
        d.record_equity("perp", T + pd.Timedelta(minutes=15 * k), 265.0 + k)
    assert d.acquire_lock("perp", "bot")
    d.release_lock("perp", "bot")  # clean stop
    d.close()
    res = _migrate(tmp_path, monkeypatch)
    assert res.exit_code == 0, res.output
    assert not legacy.exists() and not (tmp_path / "luna.db-wal").exists()
    target = tmp_path / "luna_perp.db"
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT count(*) FROM equity WHERE account='perp'").fetchone() == (3,)
    d = Db(default_url(tmp_path, "perp"))  # reopens (WAL again) and keeps appending
    d.record_equity("perp", T + pd.Timedelta(hours=1), 270.0)
    d.close()
    res = _migrate(tmp_path, monkeypatch)  # idempotent: nothing left to migrate
    assert res.exit_code == 0 and "nothing to migrate" in res.output


def test_migrate_refused_while_bot_holds_lock_or_target_exists(tmp_path, monkeypatch):
    legacy = tmp_path / "luna.db"
    d = Db(f"sqlite:///{legacy.as_posix()}")
    assert d.acquire_lock("perp", "running-bot")
    res = _migrate(tmp_path, monkeypatch)
    assert res.exit_code == 1 and "lock" in res.output
    assert legacy.exists() and not (tmp_path / "luna_perp.db").exists()
    d.release_lock("perp", "running-bot")
    d.close()
    (tmp_path / "luna_perp.db").write_bytes(b"")
    res = _migrate(tmp_path, monkeypatch)
    assert res.exit_code == 1 and "already exists" in res.output
    assert legacy.exists()
