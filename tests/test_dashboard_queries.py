import json
import time

import pandas as pd
import pytest
import sqlalchemy as sa

from luna import dashboard_queries as q
from luna.store.db import Db

T = pd.Timestamp("2026-01-01", tz="UTC")


@pytest.fixture
def db(tmp_path):
    d = Db(f"sqlite:///{(tmp_path / 'luna.db').as_posix()}")
    yield d
    d.close()


def cand(i, strat="mom", selected=True, reason=None):
    return {"ts": T + pd.Timedelta(hours=i), "strategy": strat, "key": f"K{i}", "direction": 1,
            "score": 0.5, "edge_bps": 10.0, "regime": "trend", "selected": selected,
            "reject_reason": reason}


def test_empty_db_all_queries(db, tmp_path):
    assert list(q.equity_curve(db, "perp").columns) == q.EQUITY_COLS
    assert q.equity_curve(db, "perp").empty
    assert q.open_positions_df(db, "perp").empty
    assert list(q.recent_candidates(db, "perp").columns) == q.CANDIDATE_COLS
    assert list(q.risk_events_df(db, "perp").columns) == q.RISK_COLS
    s = q.state_df(db, "perp")
    assert list(s.columns) == q.STATE_COLS and not s.iloc[0]["kill_switch"]
    assert q.candidate_outcome_stats(db, "perp").empty
    assert list(q.report_index(tmp_path).columns) == q.REPORT_COLS


def test_missing_tables_do_not_raise(tmp_path):
    eng = sa.create_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    assert q.equity_curve(eng, "perp").empty
    assert q.state_df(eng, "perp").shape[0] == 1
    assert q.open_readonly(tmp_path / "nope") is None


def test_readonly_engine_reads_and_cannot_write(db, tmp_path):
    db.record_equity("perp", T, 100.0)
    eng = q.open_readonly(tmp_path)
    assert q.equity_curve(eng, "perp")["equity"].tolist() == [100.0]
    with pytest.raises(sa.exc.OperationalError), eng.begin() as c:
        c.exec_driver_sql("DELETE FROM equity")
    eng.dispose()


def test_equity_risk_state(db):
    db.record_equity("perp", T, 100.0, 1.5, 2.0)
    db.record_equity("perp", T + pd.Timedelta(hours=1), 101.0, 1.0, 1.0)
    db.record_equity("spot", T, 5.0)
    df = q.equity_curve(db, "perp")
    assert df["equity"].tolist() == [100.0, 101.0] and df["ts"].is_monotonic_increasing
    db.record_risk_event("perp", T, "daily_loss", "d", "halt")
    db.record_risk_event("perp", T + pd.Timedelta(hours=1), "dd", "d2", "kill")
    assert q.risk_events_df(db, "perp")["rule"].tolist() == ["dd", "daily_loss"]
    db.set_flag("perp", "kill_switch", True)
    assert db.acquire_lock("perp", "me")
    s = q.state_df(db, "perp").iloc[0]
    assert s["kill_switch"] and not s["paused"] and s["lock_owner"] == "me"
    assert 0 <= s["lock_heartbeat_age_s"] < 5
    assert q.state_df(db, "perp", now=time.time() + 100).iloc[0]["lock_heartbeat_age_s"] >= 100


def test_candidates_and_stats(db):
    ids = db.record_candidates("r", [
        cand(0, selected=True), cand(1, selected=False, reason="max_positions"),
        cand(2, selected=False, reason="rank"), cand(3, selected=True)], account="perp")
    db.record_candidates("r", [cand(9)], account="spot")
    db.record_outcome(ids[0], 2.0, "tp")
    db.record_outcome(ids[1], -1.0, "stop")
    db.record_outcome(ids[2], 1.0, "time")
    db.record_outcome(ids[3], None, "no_data")
    df = q.recent_candidates(db, "perp")
    assert df["key"].tolist() == ["K3", "K2", "K1", "K0"]
    by = df.set_index("key")
    assert by.loc["K1", "reject_reason"] == "max_positions"
    assert by.loc["K0", "outcome_r"] == 2.0
    assert by.loc["K3", "outcome_reason"] == "no_data"
    assert len(q.recent_candidates(db, "perp", limit=2)) == 2
    st = q.candidate_outcome_stats(db, "perp")
    assert list(st.columns) == q.STATS_COLS
    r = st.iloc[0]
    assert r["n_labeled"] == 3 and r["n_selected"] == 1 and r["n_rejected"] == 2
    assert r["mean_r_selected"] == 2.0 and r["mean_r_rejected"] == 0.0


def test_open_positions(db):
    from luna.portfolio.portfolio import Position
    from luna.types import ExitSpec, Leg, StopSpec

    def mk(pid):
        return Position(
            id=pid, strategy="mom", key="BTC", legs=(Leg("BTC", 1.0),), direction=1,
            qty={"BTC": 1.0}, entry_price={"BTC": 100.0}, entry_ts=T, entry_ref=100.0,
            stop=StopSpec(kind="pct", level=90.0, distance_frac=0.1),
            exit=ExitSpec(max_hold_bars=10), risk_amount=5.0, regime="trend")

    db.upsert_position("perp", mk("p1"), "open")
    db.upsert_position("perp", mk("p2"), "closed")
    df = q.open_positions_df(db, "perp")
    assert df["id"].tolist() == ["p1"] and list(df.columns) == q.POSITION_COLS
    assert df.iloc[0]["legs"] == "BTC:1" and df.iloc[0]["risk_amount"] == 5.0


def test_report_index(tmp_path):
    r = tmp_path / "reports"
    wf = r / "walkforward_perp_20261006T121847"
    wf.mkdir(parents=True)
    (wf / "summary.json").write_text(json.dumps({"verdict": "PASS"}))
    (wf / "report.html").write_text("<p>x</p>")
    (r / "baseline_perp_20261007T000000").mkdir()
    (r / "baseline_perp_20261007T000000" / "summary.json").write_text("{}")
    (r / "data").mkdir()
    (r / "data" / "report.html").write_text("d")
    (r / "empty_dir").mkdir()
    df = q.report_index(tmp_path)
    assert len(df) == 3 and df.iloc[0]["kind"] == "baseline" and pd.isna(df.iloc[0]["verdict"])
    w = df[df["kind"] == "walkforward"].iloc[0]
    assert w["verdict"] == "PASS" and w["account"] == "perp" and w["path"].endswith("report.html")
    assert w["ts"] == pd.Timestamp("2026-10-06T12:18:47", tz="UTC")
