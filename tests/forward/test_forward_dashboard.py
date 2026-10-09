"""§8 dashboard "Forward data": pure view over status.json + manifests, and the Streamlit page."""

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from luna.forward.envelope import iso_ns
from luna.forward.status import forward_view

S = 1_000_000_000
NOW = 1_791_547_200 * S
APP = str(Path(__file__).resolve().parents[2] / "dashboard" / "app.py")


def _seed(root, backup_ok=False):
    root.mkdir(parents=True, exist_ok=True)
    st = {"updated_ts": iso_ns(NOW), "collector_start_ts": iso_ns(NOW - 86400 * S), "open_hours": 3,
          "sources": {"fut_tickers": {"last_ok_recv_ts": iso_ns(NOW - 30 * S), "last_error": None,
                                      "today": {"date": "2026-10-09", "ok": 700, "error": 2}},
                      "spot_time": {"last_ok_recv_ts": None, "last_error": "HTTPStatusError: 500",
                                    "today": {"date": "2026-10-09", "ok": 0, "error": 5}}},
          "backup_ok": backup_ok,
          "backup_reason": None if backup_ok else "backup target unreachable: /backup",
          "backup_warnings": []}
    (root / "status.json").write_text(json.dumps(st), encoding="utf-8")
    (root / "manifest").mkdir()
    man = {"date": "2026-10-08", "closed_ts": iso_ns(NOW - 9 * 3600 * S), "files": [],
           "sources": {"fut_tickers": {"expected": 1440, "received": 1430, "lines": 1435, "errors": 5}}}
    (root / "manifest" / "date=2026-10-08.json").write_text(json.dumps(man), encoding="utf-8")


def test_forward_view(tmp_path):
    _seed(tmp_path)
    v = forward_view(tmp_path, NOW)
    assert v["status_found"] and v["backup_ok"] is False and "unreachable" in v["backup_reason"]
    rows = {r["source"]: r for r in v["sources"]}
    assert rows["fut_tickers"]["last_ok_age_s"] == 30.0 and rows["fut_tickers"]["ok_today"] == 700
    assert rows["spot_time"]["last_ok_age_s"] is None and rows["spot_time"]["errors_today"] == 5
    assert v["days"] == [{"date": "2026-10-08", "source": "fut_tickers", "expected": 1440, "received": 1430,
                          "missing": 10, "errors": 5, "backed_up": False}]
    assert v["open_hours"] == 3


def test_forward_view_without_status(tmp_path):
    v = forward_view(tmp_path / "nothing", NOW)
    assert v["status_found"] is False and v["sources"] == [] and v["days"] == []


def test_dashboard_forward_page_shows_red_backup_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    _seed(tmp_path / "forward")
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.sidebar.radio[0].set_value("Forward data").run()
    assert not at.exception
    assert any("backup" in e.value.lower() for e in at.error)
    assert not any("luna_perp.db" in w.value for w in at.warning)  # no account DB needed


def test_dashboard_forward_page_backup_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    _seed(tmp_path / "forward", backup_ok=True)
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.sidebar.radio[0].set_value("Forward data").run()
    assert not at.exception and not at.error


def test_unclosed_days_in_view_and_red_on_page(tmp_path, monkeypatch):
    _seed(tmp_path / "forward", backup_ok=True)
    st = json.loads((tmp_path / "forward" / "status.json").read_text(encoding="utf-8"))
    st["unclosed_days"] = ["2026-10-07"]
    (tmp_path / "forward" / "status.json").write_text(json.dumps(st), encoding="utf-8")
    assert forward_view(tmp_path / "forward", NOW)["unclosed_days"] == ["2026-10-07"]
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.sidebar.radio[0].set_value("Forward data").run()
    assert any("2026-10-07" in e.value for e in at.error)
