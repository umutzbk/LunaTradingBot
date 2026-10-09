"""Luna monitoring dashboard (read-only). Run: streamlit run dashboard/app.py"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from luna import dashboard_queries as q
from luna.config import load_settings

st.set_page_config(page_title="Luna", layout="wide")

NO_DATA = "No data yet."

# Two R definitions (not interchangeable): see also the report's "Metric glossary".
R_DATASET = ("outcome_r (Dataset / Scanner) is the research r_net: the counterfactual trade's net "
             "return divided by the candidate's stop distance (both as fractions of notional), from "
             "`luna label`.")
R_TRADES = ("Trade R (engine r_multiple, exit notifications, report expectancy_r) is pnl_net / "
            "risk_amount in USD, where risk_amount is the sized position's planned loss at the stop "
            "incl. round-trip costs; fills, sizing clips and funding make it differ from r_net.")
CAPTIONS = {
    "stats": R_DATASET + " " + R_TRADES,
    "candidates": R_DATASET,
    "positions": "risk_amount is the denominator of the engine's trade R. " + R_TRADES,
}


def _var_dir() -> Path:
    try:
        return Path(load_settings().var_dir)
    except Exception as e:  # noqa: BLE001 - show a friendly message instead of a traceback
        st.error(f"Could not load settings ({type(e).__name__}): {e}")
        st.stop()


VAR_DIR = _var_dir()


def _query(fn: Callable[..., Any], var_dir: str, account: str, *args: Any) -> Any:
    """Run ``fn`` on the account's own database file (``var/luna_<account>.db``)."""
    eng = q.open_readonly(Path(var_dir), account)
    if eng is None:
        return None
    try:
        return fn(eng, *args)
    finally:
        eng.dispose()


@st.cache_data(ttl=30)
def _slow(name: str, var_dir: str, account: str, limit: int) -> tuple[Any, float]:
    fns = {
        "equity": lambda e: q.equity_curve(e, account),
        "positions": lambda e: q.open_positions_df(e, account),
        "candidates": lambda e: q.recent_candidates(e, account, limit),
        "risk": lambda e: q.risk_events_df(e, account, limit),
        "stats": lambda e: (q.candidate_outcome_stats(e, account), q.terminal_reasons_df(e, account)),
    }
    return _query(fns[name], var_dir, account), time.time()


@st.cache_data(ttl=5)
def _fast_state(var_dir: str, account: str) -> tuple[Any, float]:
    return _query(lambda e: q.state_df(e, account), var_dir, account), time.time()


@st.cache_data(ttl=30)
def _accounts(var_dir: str) -> list[str]:
    """Accounts with a ``luna_<account>.db`` file (perp/spot when none exists yet)."""
    try:
        return q.available_accounts(Path(var_dir))
    except OSError:  # selector falls back; the page itself reports a missing DB
        return ["perp", "spot"]


def _table(df: pd.DataFrame | None) -> None:
    if df is None or df.empty:
        st.info(NO_DATA)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def _num(v: object, fmt: str) -> str:
    return "-" if v is None or pd.isna(v) else format(v, fmt)


def _overview(account: str) -> None:
    stt, t_state = _fast_state(str(VAR_DIR), account)
    eq, _ = _slow("equity", str(VAR_DIR), account, 0)
    if stt is None:
        return
    s = stt.iloc[0]
    st.caption(f"State fetched {time.strftime('%H:%M:%S', time.localtime(t_state))} (refreshes every 5 s)")
    c = st.columns(4)
    c[0].metric("Kill switch", "ON" if s["kill_switch"] else "off")
    c[1].metric("Paused", "yes" if s["paused"] else "no")
    age = s["lock_heartbeat_age_s"]
    c[2].metric("Lock heartbeat age", "no lock" if pd.isna(age) else f"{age:.0f}s",
                help=f"owner: {s['lock_owner']}")
    c[3].metric("Peak equity", _num(s["peak_equity"], ",.2f"))
    if eq is None or eq.empty:
        st.info(NO_DATA)
        return
    last = eq.iloc[-1]
    c = st.columns(3)
    c[0].metric("Equity", _num(last["equity"], ",.2f"))
    c[1].metric("Gross leverage", _num(last["gross_leverage"], ".2f"))
    c[2].metric("Open risk", _num(last["open_risk"], ",.2f"))
    st.line_chart(eq.set_index("ts")["equity"])


def _research() -> None:
    idx = q.report_index(VAR_DIR)
    if idx.empty:
        st.info("No reports yet.")
        return
    _table(idx)
    htmls = idx[idx["path"].str.endswith(".html")]
    if not htmls.empty:
        pick = st.selectbox("Report", htmls["path"].tolist())
        components.html(Path(pick).read_text(encoding="utf-8"), height=900, scrolling=True)


def _forward() -> None:
    """V4 forward collector health: var/forward/status.json + day manifests only (no prices)."""
    from luna.forward.status import forward_view

    v = forward_view(VAR_DIR / "forward", time.time_ns())
    if not v["status_found"]:
        st.info(f"No collector status at {VAR_DIR / 'forward' / 'status.json'} yet.")
    if v["status_found"] and not v["backup_ok"]:
        st.error(f"Forward data backup NOT OK: {v['backup_reason'] or 'unknown reason'}")
    for w in v["backup_warnings"]:
        st.error(f"Backup warning: {w}")
    for d in v["unclosed_days"]:
        st.error(f"Day {d} has raw data but no manifest > 4h after its end: day close is failing")
    if v["status_found"]:
        st.caption(f"status.json updated {v['updated_ts']}; collector_start_ts {v['collector_start_ts']}; "
                   f"open hours {v['open_hours']}")
    st.subheader("Sources (last ok age, today's counts)")
    _table(pd.DataFrame(v["sources"]))
    st.subheader("Closed days: expected vs received (gaps)")
    _table(pd.DataFrame(v["days"]))


def _page(page: str, account: str, limit: int) -> None:
    v = str(VAR_DIR)
    if page == "Forward data":
        _forward()
    elif page == "Overview":
        _overview(account)
    elif page == "Research":
        _research()
    else:
        name = {"Positions": "positions", "Scanner": "candidates", "Risk": "risk", "Dataset": "stats"}[page]
        df, t = _slow(name, v, account, limit)
        if name in CAPTIONS:
            st.caption(CAPTIONS[name])
        if name == "stats":
            stats, reasons = df if df is not None else (None, None)
            _table(stats)
            if reasons is not None and not reasons.empty:
                st.subheader("Terminal (never labelable) outcomes")
                _table(reasons)
        else:
            if df is not None and df.attrs.get("skipped_bad_rows"):
                st.warning(f"{df.attrs['skipped_bad_rows']} unreadable position row(s) skipped")
            _table(df)
        if df is not None:
            st.caption(f"Fetched {time.strftime('%H:%M:%S', time.localtime(t))}")


def main() -> None:
    st.sidebar.title("Luna")
    account = st.sidebar.selectbox("Account", _accounts(str(VAR_DIR)))
    page = st.sidebar.radio(
        "Page", ["Overview", "Positions", "Scanner", "Risk", "Research", "Dataset", "Forward data"]
    )
    limit = st.sidebar.number_input("Rows", 20, 2000, 200, step=20)
    if st.sidebar.button("Refresh"):
        st.cache_data.clear()
        st.rerun()
    st.title(f"{page} - {account}")
    path = q.db_file(VAR_DIR, account)
    if page not in ("Research", "Forward data") and not path.exists():
        st.warning(f"No database at {path} yet. {NO_DATA}")
    try:
        _page(page, account, int(limit))
    except Exception as e:  # noqa: BLE001 - a locked/corrupt DB must not look like "no data"
        st.error(f"Query failed on this page: {type(e).__name__}: {e}")


main()
