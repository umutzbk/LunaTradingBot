"""Dashboard queries and the Adaptive Carry page (AppTest, no server)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from luna.carry import views as cv
from luna.store.db import Db
from tests.carry.helpers import T0
from tests.carry.test_engine import Clock, FakeSource, make_engine


def _run_engine(tmp_path: Path) -> None:
    src, clock = FakeSource(), Clock(T0)
    e = make_engine(tmp_path, src, clock)
    e.start()
    e.cycle(T0)
    src.set(T0 + pd.Timedelta("1min"))
    e.cycle(T0 + pd.Timedelta("1min"))
    e.close()
    e.db.close()


def test_views_expose_book_alternatives_signals_and_pnl(tmp_path: Path) -> None:
    _run_engine(tmp_path)
    db = Db(f"sqlite:///{(tmp_path / 'luna_carry.db').as_posix()}")
    eng = db.engine
    ev = cv.latest_evaluation(eng, "carry")
    assert ev is not None and ev["summary"] == "HOLD"
    book = cv.book_table(eng, "carry", ev)
    assert set(zip(book["asset"], book["side"], strict=True)) == {
        ("A", "SHORT"),
        ("B", "SHORT"),
        ("E", "LONG"),
        ("F", "LONG"),
    }
    sig = cv.signals_table(ev)
    assert sig.iloc[0]["asset"] == "A" and sig.iloc[0]["FL30 %/yr"] == pytest.approx(5e-5 * 8760 * 100)
    alts = cv.alternatives(eng, "carry", ev["id"])
    assert set(alts["decision"]) == {"HOLD"} and alts["value"].notna().all()
    log = cv.decision_log(eng, "carry")
    assert list(log["decision"]).count("ENTER") == 2
    pnl = cv.pnl_summary(eng, "carry", 265.0)
    assert pnl["fills"] == 4 and pnl["fees"] > 0 and pnl["turnover"] == pytest.approx(265.0, rel=0.01)
    assert pnl["net"] == pytest.approx(pnl["price"] + pnl["funding"] - pnl["fees"] - pnl["spread_slip"])
    db.close()


def test_adaptive_carry_page_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_engine(tmp_path)
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    at = AppTest.from_file("../../dashboard/app.py", default_timeout=60).run()
    at.sidebar.radio[0].set_value("Adaptive Carry").run()
    assert not at.exception
    labels = {m.label for m in at.metric}
    assert {"Equity", "Net P&L", "Funding P&L", "Price P&L", "Costs (fees + spread)", "Turnover"} <= labels
    heads = " ".join(h.value for h in at.subheader)
    assert "Current book" in heads and "alternative" in heads and "FL30" in heads


def test_page_without_carry_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUNA_VAR_DIR", str(tmp_path))
    at = AppTest.from_file("../../dashboard/app.py", default_timeout=60).run()
    at.sidebar.radio[0].set_value("Adaptive Carry").run()
    assert not at.exception
    assert any("bot-carry has not run" in i.value for i in at.info)
