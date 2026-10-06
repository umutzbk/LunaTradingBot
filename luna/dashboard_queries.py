"""Read-only dashboard queries: pure functions returning DataFrames (never write to the DB)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, create_engine, text

EQUITY_COLS = ["ts", "equity", "gross_leverage", "open_risk"]
POSITION_COLS = ["id", "strategy", "key", "direction", "opened_at", "bars_held", "regime",
                 "risk_amount", "legs", "qty", "entry_price", "stop", "pnl_net"]
CANDIDATE_COLS = ["ts", "strategy", "key", "direction", "score", "edge_bps", "regime", "selected",
                  "status", "reject_reason", "outcome_r", "outcome_reason"]
RISK_COLS = ["ts", "rule", "detail", "action"]
STATE_COLS = ["kill_switch", "paused", "day_start_equity", "peak_equity", "lock_owner",
              "lock_heartbeat_age_s"]
REPORT_COLS = ["kind", "account", "ts", "path", "verdict"]
STATS_COLS = ["strategy", "n_labeled", "n_selected", "n_rejected", "mean_r_selected",
              "mean_r_rejected"]

_REPORT_DIR = re.compile(r"^(?P<kind>[a-z]+)_(?P<account>[A-Za-z0-9]+)_(?P<ts>\d{8}T\d{6})$")


def open_readonly(var_dir: Path) -> Engine | None:
    """Read-only SQLite engine on ``var_dir/luna.db`` (None if it does not exist yet)."""
    path = Path(var_dir) / "luna.db"
    if not path.exists():
        return None
    return create_engine(f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true")


def _engine(db: Any) -> Engine:
    return db.engine if hasattr(db, "engine") else db


def _read(db: Any, sql: str, cols: list[str], **params: Any) -> pd.DataFrame:
    """Run a SELECT; a missing table/DB error yields an empty frame with ``cols``."""
    try:
        with _engine(db).connect() as c:
            return pd.read_sql_query(text(sql), c, params=params)
    except Exception:  # noqa: BLE001 - e.g. no such table: the dashboard shows "no data yet"
        return pd.DataFrame(columns=cols)


def _ts(df: pd.DataFrame, col: str = "ts") -> pd.DataFrame:
    if col in df and len(df):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    return df


def equity_curve(db: Any, account: str) -> pd.DataFrame:
    df = _read(db, "SELECT ts, equity, gross_leverage, open_risk FROM equity "
                   "WHERE account = :a ORDER BY ts, id", EQUITY_COLS, a=account)
    return _ts(df)


def open_positions_df(db: Any, account: str) -> pd.DataFrame:
    raw = _read(db, "SELECT id, strategy, key, opened_at, pnl_net, data FROM positions "
                    "WHERE account = :a AND status = 'open' ORDER BY opened_at, id",
                POSITION_COLS + ["data"], a=account)
    rows = []
    for r in raw.to_dict("records"):
        d = json.loads(r["data"])
        rows.append({
            "id": r["id"], "strategy": r["strategy"], "key": r["key"], "direction": d.get("direction"),
            "opened_at": r["opened_at"], "bars_held": d.get("bars_held"), "regime": d.get("regime"),
            "risk_amount": d.get("risk_amount"),
            "legs": ", ".join(f"{a}:{w:g}" for a, w in d.get("legs", [])),
            "qty": json.dumps(d.get("qty", {})), "entry_price": json.dumps(d.get("entry_price", {})),
            "stop": json.dumps(d.get("stop", {})), "pnl_net": r["pnl_net"],
        })
    return _ts(pd.DataFrame(rows, columns=POSITION_COLS), "opened_at")


def recent_candidates(db: Any, account: str, limit: int = 200) -> pd.DataFrame:
    df = _read(db, """
        SELECT c.ts, c.strategy, c."key", c.direction, c.score, c.edge_bps, c.regime, c.selected,
               c.status, c.reject_reason, o.outcome_r, o.exit_reason AS outcome_reason
        FROM candidates c LEFT JOIN candidate_outcomes o ON o.candidate_id = c.id
        WHERE c.account = :a ORDER BY c.ts DESC, c.id DESC LIMIT :n""",
               CANDIDATE_COLS, a=account, n=int(limit))
    if len(df):
        df["selected"] = df["selected"].astype(bool)
    return _ts(df)


def risk_events_df(db: Any, account: str, limit: int = 200) -> pd.DataFrame:
    df = _read(db, "SELECT ts, rule, detail, action FROM risk_events WHERE account = :a "
                   "ORDER BY ts DESC, id DESC LIMIT :n", RISK_COLS, a=account, n=int(limit))
    return _ts(df)


def state_df(db: Any, account: str, now: float | None = None) -> pd.DataFrame:
    """One row: kill switch / paused flags, risk-state anchors, lock owner + heartbeat age."""
    raw = _read(db, 'SELECT "key", value FROM state WHERE account = :a', ["key", "value"], a=account)
    kv = {r["key"]: json.loads(r["value"]) for r in raw.to_dict("records")}
    lock = kv.get("lock") or {}
    t = time.time() if now is None else now
    age = (t - lock["ts"]) if "ts" in lock else None
    row = {
        "kill_switch": bool(kv.get("kill_switch", False)), "paused": bool(kv.get("paused", False)),
        "day_start_equity": kv.get("day_start_equity"), "peak_equity": kv.get("peak_equity"),
        "lock_owner": lock.get("owner"), "lock_heartbeat_age_s": age,
    }
    return pd.DataFrame([row], columns=STATE_COLS)


def report_index(var_dir: Path) -> pd.DataFrame:
    """Reports under ``var_dir/reports`` (dirs with report.html and/or summary.json), newest first."""
    root = Path(var_dir) / "reports"
    rows = []
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir() or not ((d / "report.html").exists() or (d / "summary.json").exists()):
                continue
            m = _REPORT_DIR.match(d.name)
            if m:
                kind, account = m["kind"], m["account"]
                ts = pd.Timestamp(m["ts"], tz="UTC")
            else:
                kind, account = d.name, ""
                ts = pd.Timestamp(d.stat().st_mtime, unit="s", tz="UTC")
            verdict = None
            sj = d / "summary.json"
            if sj.exists():
                try:
                    v = json.loads(sj.read_text(encoding="utf-8")).get("verdict")
                    verdict = None if v is None else str(v)
                except (OSError, ValueError, AttributeError):
                    verdict = None
            html = d / "report.html"
            rows.append({"kind": kind, "account": account, "ts": ts,
                         "path": str(html if html.exists() else sj), "verdict": verdict})
    df = pd.DataFrame(rows, columns=REPORT_COLS)
    return df.sort_values("ts", ascending=False, ignore_index=True) if len(df) else df


def candidate_outcome_stats(db: Any, account: str) -> pd.DataFrame:
    """Per strategy: labeled counts and mean outcome_r of selected vs rejected candidates."""
    df = _read(db, """
        SELECT c.strategy, c.selected, o.outcome_r
        FROM candidates c JOIN candidate_outcomes o ON o.candidate_id = c.id
        WHERE c.account = :a AND o.outcome_r IS NOT NULL""", ["strategy", "selected", "outcome_r"],
               a=account)
    if df.empty:
        return pd.DataFrame(columns=STATS_COLS)
    df["selected"] = df["selected"].astype(bool)
    rows = []
    for strat, g in df.groupby("strategy", sort=True):
        s, r = g[g["selected"]]["outcome_r"], g[~g["selected"]]["outcome_r"]
        rows.append({
            "strategy": strat, "n_labeled": len(g), "n_selected": len(s), "n_rejected": len(r),
            "mean_r_selected": float(s.mean()) if len(s) else None,
            "mean_r_rejected": float(r.mean()) if len(r) else None,
        })
    return pd.DataFrame(rows, columns=STATS_COLS)
