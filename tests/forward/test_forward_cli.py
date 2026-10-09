"""CLI: luna collect / collector-health / forward-dq (no network: the loop is stubbed)."""

import json
import time

import pytest
from typer.testing import CliRunner

from luna.cli import app
from luna.forward import collector as coll
from luna.forward.envelope import iso_ns
from luna.forward.meta import Meta

S = 1_000_000_000


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """``luna collect`` must never reach Kraken from a test: every request fails at once."""
    import httpx

    import luna.http

    def refuse(request):
        raise httpx.ConnectError("network disabled in tests", request=request)

    real = luna.http.make_client
    monkeypatch.setattr(luna.http, "make_client", lambda transport=None: real(httpx.MockTransport(refuse)))


@pytest.fixture
def var(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("LUNA_FORWARD_BACKUP_DIR", raising=False)
    return tmp_path


def _write_status(var, backup_ok=True, ticker_age_s=30):
    now = time.time_ns()
    src = {s: {"last_ok_recv_ts": iso_ns(now - ticker_age_s * S)} for s in ("fut_tickers", "spot_ticker")}
    src.update({s: {"last_ok_recv_ts": iso_ns(now - 600 * S)}
                for s in ("fut_funding", "fut_instruments", "spot_assetpairs")})
    st = {"started_ts": iso_ns(now - 3 * 3600 * S), "sources": src, "backup_ok": backup_ok,
          "backup_reason": None if backup_ok else "LUNA_FORWARD_BACKUP_DIR not set", "backup_warnings": []}
    (var / "forward").mkdir(parents=True, exist_ok=True)
    (var / "forward" / "status.json").write_text(json.dumps(st), encoding="utf-8")


def test_collector_health_ok(var):
    _write_status(var)
    res = CliRunner().invoke(app, ["collector-health"])
    assert res.exit_code == 0, res.output
    assert "collector-health: ok" in res.output


def test_collector_health_stale_and_missing(var):
    res = CliRunner().invoke(app, ["collector-health"])
    assert res.exit_code == 1 and "status.json" in res.output
    _write_status(var, ticker_age_s=400)
    assert CliRunner().invoke(app, ["collector-health"]).exit_code == 1


def test_collector_health_backup_warning_and_strict(var):
    _write_status(var, backup_ok=False)
    res = CliRunner().invoke(app, ["collector-health"])
    assert res.exit_code == 0 and "WARNING backup" in res.output
    assert CliRunner().invoke(app, ["collector-health", "--strict"]).exit_code == 2


def test_forward_dq_prints_json(var):
    root = var / "forward"
    m = Meta.load(root)
    m.start(1_791_547_200 * S)
    m.save()
    args = ["forward-dq", "--since", "2026-10-09T12:00:00", "--until", "2026-10-09T13:00:00"]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    rep = json.loads(res.output)
    assert rep["window"]["since"] == "2026-10-09T12:00:00.000000000Z"
    assert rep["coverage"]["fut_tickers"]["2026-10-09"]["expected"] == 60


class _Stub:
    instances: list = []

    def __init__(self, root, client, **kw):
        self.root, self.kw = root, kw
        self.exit_code, self.exit_reason = 0, ""
        _Stub.instances.append(self)

    def start(self):
        pass

    def run_forever(self):
        self.exit_code, self.exit_reason = 3, "feed dead"

    def stop(self):
        pass

    def close(self):
        pass


def test_collect_uses_var_forward_and_exits_with_the_loop_code(var, monkeypatch):
    monkeypatch.setattr(coll, "Collector", _Stub)
    monkeypatch.setenv("LUNA_FORWARD_BACKUP_DIR", "")  # compose sets "" when unset
    res = CliRunner().invoke(app, ["collect"])
    assert res.exit_code == 3
    inst = _Stub.instances[-1]
    assert inst.root == var / "forward" and inst.kw["backup_dir"] is None


def test_collect_passes_backup_dir(var, monkeypatch, tmp_path):
    monkeypatch.setattr(coll, "Collector", _Stub)
    monkeypatch.setenv("LUNA_FORWARD_BACKUP_DIR", str(tmp_path / "drive"))
    CliRunner().invoke(app, ["collect"])
    assert _Stub.instances[-1].kw["backup_dir"] == tmp_path / "drive"


def test_second_collect_is_refused_with_exit_1(var):
    lock = var / "forward" / "collector.lock"
    lock.parent.mkdir(parents=True)
    now = time.time_ns()
    lock.write_text(json.dumps({"pid": 999999, "host": "elsewhere", "heartbeat_ns": now}), encoding="utf-8")
    res = CliRunner().invoke(app, ["collect"])
    assert res.exit_code == 1
    assert "lock" in res.output
    assert not (var / "forward" / "raw").exists()


def test_forward_dq_without_meta_exits_1(var):
    res = CliRunner().invoke(app, ["forward-dq"])
    assert res.exit_code == 1 and "collector_start_ts" in res.output


def test_collect_refuses_raw_without_meta_exit_1(var):
    d = var / "forward" / "raw" / "spot_time" / "date=2026-10-09"
    d.mkdir(parents=True)
    (d / "12.jsonl").write_bytes(b'{"x":1}\n')
    res = CliRunner().invoke(app, ["collect"])
    assert res.exit_code == 1 and "meta.json" in res.output
