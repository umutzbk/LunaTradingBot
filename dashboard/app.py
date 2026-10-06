"""Luna monitoring dashboard (read-only). Run: streamlit run dashboard/app.py"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from luna import dashboard_queries as q
from luna.config import load_settings

st.set_page_config(page_title="Luna", layout="wide")

VAR_DIR = Path(load_settings().var_dir)
NO_DATA = "No data yet."


@st.cache_data(ttl=30)
def _load(var_dir: str, account: str, limit: int) -> dict[str, pd.DataFrame]:
    eng = q.open_readonly(Path(var_dir))
    if eng is None:
        return {}
    try:
        return {
            "equity": q.equity_curve(eng, account), "positions": q.open_positions_df(eng, account),
            "candidates": q.recent_candidates(eng, account, limit),
            "risk": q.risk_events_df(eng, account, limit), "state": q.state_df(eng, account),
            "stats": q.candidate_outcome_stats(eng, account),
        }
    finally:
        eng.dispose()


def _table(df: pd.DataFrame | None) -> None:
    if df is None or df.empty:
        st.info(NO_DATA)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def _num(v: object, fmt: str) -> str:
    return "-" if v is None or pd.isna(v) else format(v, fmt)


def _overview(data: dict[str, pd.DataFrame]) -> None:
    eq = data.get("equity", pd.DataFrame())
    stt = data.get("state", pd.DataFrame())
    if not stt.empty:
        s = stt.iloc[0]
        c = st.columns(4)
        c[0].metric("Kill switch", "ON" if s["kill_switch"] else "off")
        c[1].metric("Paused", "yes" if s["paused"] else "no")
        age = s["lock_heartbeat_age_s"]
        c[2].metric("Lock heartbeat age", "no lock" if pd.isna(age) else f"{age:.0f}s",
                    help=f"owner: {s['lock_owner']}")
        c[3].metric("Peak equity", _num(s["peak_equity"], ",.2f"))
    if eq.empty:
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


def main() -> None:
    st.sidebar.title("Luna")
    account = st.sidebar.selectbox("Account", ["perp", "spot"])
    page = st.sidebar.radio("Page", ["Overview", "Positions", "Scanner", "Risk", "Research", "Dataset"])
    limit = st.sidebar.number_input("Rows", 20, 2000, 200, step=20)
    if st.sidebar.button("Refresh"):
        st.cache_data.clear()
        st.rerun()
    data = _load(str(VAR_DIR), account, int(limit))
    if not data:
        st.warning(f"No database at {VAR_DIR / 'luna.db'} yet. {NO_DATA}")
    st.title(f"{page} - {account}")
    if page == "Overview":
        _overview(data)
    elif page == "Positions":
        _table(data.get("positions"))
    elif page == "Scanner":
        _table(data.get("candidates"))
    elif page == "Risk":
        _table(data.get("risk"))
    elif page == "Research":
        _research()
    else:
        _table(data.get("stats"))


main()
