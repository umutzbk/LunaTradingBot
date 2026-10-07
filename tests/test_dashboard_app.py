"""Smoke test of the Streamlit app against per-account database files (AppTest, no server)."""

import pandas as pd
from streamlit.testing.v1 import AppTest

from luna.store.db import Db

T = pd.Timestamp("2026-10-06 12:00", tz="UTC")


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    return AppTest.from_file("../dashboard/app.py", default_timeout=60)


def test_dashboard_reads_the_selected_accounts_file(tmp_path, monkeypatch):
    d = Db(f"sqlite:///{(tmp_path / 'luna_spot.db').as_posix()}")
    d.record_equity("spot", T, 123.0)
    d.close()
    at = _app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert at.sidebar.selectbox[0].options == ["spot"]  # listed from the existing files
    assert any(m.label == "Equity" and m.value == "123.00" for m in at.metric)

    at.sidebar.radio[0].set_value("Dataset").run()
    assert not at.exception
    text = " ".join(c.value for c in at.caption)
    assert "r_net" in text and "pnl_net / risk_amount" in text


def test_dashboard_without_db_files_falls_back(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch).run()
    assert not at.exception
    assert at.sidebar.selectbox[0].options == ["perp", "spot"]
    assert any("luna_perp.db" in w.value for w in at.warning)
