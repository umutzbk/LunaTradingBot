import pandas as pd
from typer.testing import CliRunner

from luna.cli import app
from luna.store.db import Db


def _seed(path, rows):
    db = Db(f"sqlite:///{path.as_posix()}")
    for account, ts in rows:
        db.record_equity(account, ts, 265.0)
    db.close()


def _health(path, *extra):
    return CliRunner().invoke(app, ["health", "--account", "perp", "--db", str(path), *extra])


def test_health_command(tmp_path):
    now = pd.Timestamp.now(tz="UTC")
    fresh, stale = tmp_path / "fresh.db", tmp_path / "stale.db"
    _seed(fresh, [("perp", now - pd.Timedelta(hours=3)), ("perp", now - pd.Timedelta(minutes=5))])
    # stale for perp, though the other account is fresh
    _seed(stale, [("perp", now - pd.Timedelta(minutes=45)), ("spot", now - pd.Timedelta(minutes=1))])

    res = _health(fresh)
    assert res.exit_code == 0, res.output
    assert "ok" in res.output

    res = _health(stale)
    assert res.exit_code == 1
    assert "stale" in res.output
    assert _health(stale, "--max-age-min", "60").exit_code == 0


def test_health_fails_without_rows_or_db(tmp_path):
    empty = tmp_path / "empty.db"
    _seed(empty, [("spot", pd.Timestamp.now(tz="UTC"))])
    res = _health(empty)
    assert res.exit_code == 1
    assert "no equity" in res.output

    missing = tmp_path / "missing.db"
    res = _health(missing)
    assert res.exit_code == 1
    assert "missing" in res.output
    assert not missing.exists()  # a health probe never creates the database


def test_health_default_db_is_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    now = pd.Timestamp.now(tz="UTC")
    _seed(tmp_path / "luna_perp.db", [("perp", now)])
    res = CliRunner().invoke(app, ["health", "--account", "perp"])
    assert res.exit_code == 0, res.output
    res = CliRunner().invoke(app, ["health", "--account", "spot"])
    assert res.exit_code == 1 and "luna_spot.db" in res.output  # never reads the perp file
