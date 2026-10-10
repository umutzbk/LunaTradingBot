"""Read-only queries for the dashboard's Adaptive Carry page (``var/luna_carry.db``)."""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from luna.carry.funding import HOURS_PER_YEAR

SIDE = {1: "LONG", -1: "SHORT"}


def _df(engine: Engine, sql: str, **params: Any) -> pd.DataFrame:
    try:
        with engine.connect() as c:
            return pd.read_sql_query(text(sql), c, params=params)
    except Exception:  # noqa: BLE001 - a missing table (no evaluation yet) is "no data"
        return pd.DataFrame()


def latest_evaluation(engine: Engine, account: str) -> dict[str, Any] | None:
    df = _df(engine, "SELECT * FROM carry_evaluations WHERE account = :a ORDER BY id DESC LIMIT 1", a=account)
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    for k in ("ideal", "holdings", "signals"):
        row[k] = json.loads(row[k]) if isinstance(row.get(k), str) else None
    return row


def _pct(x: Any) -> float | None:
    return (
        None
        if x is None or (isinstance(x, float) and math.isnan(x))
        else round(float(x) * HOURS_PER_YEAR * 100, 2)
    )


def _bps(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x) * 1e4, 2)


def signals_table(ev: dict[str, Any] | None) -> pd.DataFrame:
    """Universe at the latest evaluation: funding (annualized %), rank, spread, cost, eligibility."""
    if not ev or not ev.get("signals"):
        return pd.DataFrame()
    held = {h["asset"]: SIDE.get(h["side"], "?") for h in ev.get("holdings") or []}
    ideal = ev.get("ideal") or {}
    target = {**{a: "LONG" for a in ideal.get("long", [])}, **{a: "SHORT" for a in ideal.get("short", [])}}
    rows = []
    for s in ev["signals"]:
        rows.append(
            {
                "asset": s["asset"],
                "held": held.get(s["asset"], ""),
                "ideal": target.get(s["asset"], ""),
                "FL30 rank": s.get("rank"),
                "FL30 %/yr": _pct(s.get("fl")),
                "FL7 %/yr": _pct(s.get("fl_short")),
                "current %/yr": _pct(s.get("current_rel")),
                "next est %/yr": _pct(s.get("pred_rel")),
                "persistence": s.get("persistence"),
                "spread bps": _bps(s.get("spread_full")),
                "cost/side bps": _bps(s.get("side_cost")),
                "volume 24h $": s.get("volume_usd"),
                "eligible": s.get("eligible"),
                "reasons": ",".join(s.get("reasons") or []),
            }
        )
    df = pd.DataFrame(rows)
    return df.sort_values(["FL30 rank", "asset"], na_position="last").reset_index(drop=True)


def book_table(engine: Engine, account: str, ev: dict[str, Any] | None) -> pd.DataFrame:
    """Current legs with their funding signal and P&L components."""
    pos = _df(
        engine,
        "SELECT id, key, opened_at, entry, pnl_breakdown, data FROM positions "
        "WHERE account = :a AND status = 'open' ORDER BY key",
        a=account,
    )
    if pos.empty:
        return pd.DataFrame()
    sig = {s["asset"]: s for s in (ev or {}).get("signals") or []}
    held = {h["position_id"]: h for h in (ev or {}).get("holdings") or []}
    rows = []
    for _, p in pos.iterrows():
        d = json.loads(p["data"])
        pnl = json.loads(p["pnl_breakdown"])
        a = p["key"]
        if not d["qty"].get(a):
            continue  # a leg registered for an order that never filled
        s = sig.get(a, {})
        rows.append(
            {
                "asset": a,
                "side": SIDE.get(d["direction"], "?"),
                "qty": d["qty"].get(a),
                "notional $": round(held.get(p["id"], {}).get("notional") or 0.0, 2),
                "entry": (d["entry_price"] or {}).get(a),
                "opened": p["opened_at"],
                "FL30 %/yr": _pct(s.get("fl")),
                "current %/yr": _pct(s.get("current_rel")),
                "rank": s.get("rank"),
                "funding $": round(pnl.get("funding", 0.0), 4),
                "fees $": round(pnl.get("fees", 0.0), 4),
                "spread/slip $": round(pnl.get("spread_slip", 0.0), 4),
            }
        )
    return pd.DataFrame(rows)


def alternatives(engine: Engine, account: str, eval_id: int | None) -> pd.DataFrame:
    """Every alternative weighed at one evaluation and why it was taken or not."""
    if eval_id is None:
        return pd.DataFrame()
    df = _df(
        engine,
        "SELECT decision, reason, side, asset_out, asset_in, expected_carry, current_carry, "
        "incremental_carry, switch_cost, safety_buffer, value FROM carry_decisions "
        "WHERE account = :a AND eval_id = :e ORDER BY id",
        a=account,
        e=int(eval_id),
    )
    if not df.empty:
        df["side"] = df["side"].map(lambda s: SIDE.get(s, "") if pd.notna(s) else "")
    return df


def decision_log(engine: Engine, account: str, limit: int = 200) -> pd.DataFrame:
    """Recent decisions that changed or tried to change the book (HOLDs are only in alternatives)."""
    return _df(
        engine,
        "SELECT ts, decision, reason, side, asset_out, asset_in, incremental_carry, "
        "switch_cost, value FROM carry_decisions WHERE account = :a AND decision != 'HOLD' "
        "ORDER BY id DESC LIMIT :n",
        a=account,
        n=int(limit),
    )


def pnl_summary(engine: Engine, account: str, capital: float) -> dict[str, float] | None:
    """Cumulative P&L split: funding (settled), fees, spread/slippage vs decision mid, price (the
    rest of the mark-to-market change), net = equity - capital, turnover = traded notional."""
    eq = _df(engine, "SELECT equity FROM equity WHERE account = :a ORDER BY id DESC LIMIT 1", a=account)
    if eq.empty:
        return None
    fund = _df(engine, "SELECT COALESCE(SUM(amount), 0) AS v FROM funding WHERE account = :a", a=account)
    fills = _df(
        engine,
        "SELECT COALESCE(SUM(fee), 0) AS fees, COALESCE(SUM(qty * price), 0) AS turnover, "
        "COUNT(*) AS n FROM fills WHERE account = :a",
        a=account,
    )
    pos = _df(engine, "SELECT pnl_breakdown FROM positions WHERE account = :a", a=account)
    slip = sum(json.loads(x).get("spread_slip", 0.0) for x in pos["pnl_breakdown"]) if not pos.empty else 0.0
    net = float(eq.iloc[0]["equity"]) - capital
    funding = float(fund.iloc[0]["v"]) if not fund.empty else 0.0
    fees = float(fills.iloc[0]["fees"]) if not fills.empty else 0.0
    return {
        "equity": float(eq.iloc[0]["equity"]),
        "net": net,
        "funding": funding,
        "fees": fees,
        "spread_slip": slip,
        "price": net - funding + fees + slip,
        "turnover": float(fills.iloc[0]["turnover"]) if not fills.empty else 0.0,
        "fills": int(fills.iloc[0]["n"]) if not fills.empty else 0,
    }


def equity_curve(engine: Engine, account: str) -> pd.DataFrame:
    df = _df(engine, "SELECT ts, equity FROM equity WHERE account = :a ORDER BY id", a=account)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df
