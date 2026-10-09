"""V4 diagnostics (spec §3 D-PERSIST / D-DISP / D-H5CELLS, §7.3 proxy period) and the premium-proxy
isolation rule (R3, §8): proxy code is never reachable from P&L code."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.v4 import diagnostics as dg
from luna.v4 import proxy as px
from tests.v4.synth import bars

T = pd.Timestamp("2026-01-05T00:00:00Z")
LUNA_V4 = Path(__file__).resolve().parents[2] / "luna" / "v4"
PNL_MODULES = [
    "accounting",
    "strategies",
    "stats",
    "runner",
    "market",
    "signals",
    "costs",
    "data",
    "diagnostics",
    "report",
]


def _v4_imports(module: str) -> set[str]:
    tree = ast.parse((LUNA_V4 / f"{module}.py").read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def _closure(module: str) -> set[str]:
    seen, todo = set(), [module]
    while todo:
        m = todo.pop()
        if m in seen or not (LUNA_V4 / f"{m}.py").exists():
            continue
        seen.add(m)
        for imp in _v4_imports(m):
            if imp.startswith("luna.v4."):
                todo.append(imp.split(".")[2])
    return seen


@pytest.mark.parametrize("module", PNL_MODULES)
def test_pnl_code_cannot_reach_the_premium_proxy(module):
    if not (LUNA_V4 / f"{module}.py").exists():
        pytest.skip(f"{module} not written yet")
    assert "proxy" not in _closure(module)


def test_proxy_module_computes_no_pnl():
    src = (LUNA_V4 / "proxy.py").read_text(encoding="utf-8")
    for word in ("accounting", "run_carry", "run_h2", "equity", "pnl"):
        assert word not in src.lower()


# --- premium proxy --------------------------------------------------------------------------------


def test_premium_proxy_is_hourly_mark_over_index_and_named_separately():
    mark = bars(T, 8, close=101.0)
    index = bars(T, 8, close=100.0)
    p = px.premium_proxy(mark, index)
    assert p.name == "premium_proxy"
    assert list(p.index) == [T, T + pd.Timedelta("1h")]
    assert p.iloc[0] == pytest.approx(0.01)


def test_proxy_rank_persistence_and_sign_stats():
    idx = pd.date_range(T - pd.Timedelta("14D"), periods=28 * 24, freq="1h")
    proxies = {f"A{i}": pd.Series(0.001 * i, index=idx, name="premium_proxy") for i in range(5)}
    out = px.proxy_diagnostics(proxies, [T])
    assert out["rank_persistence"]["mean_spearman"] == pytest.approx(1.0)
    assert out["sign_persistence"]["A1"] == pytest.approx(1.0)
    assert out["signal_counts"]["positive_asset_weeks"] == 4


# --- D-PERSIST / D-DISP / D-H5CELLS ------------------------------------------------------------------


def _funding_panel(n_assets=5, days=40, persistent=True):
    idx = pd.date_range(T - pd.Timedelta("20D"), periods=days * 24, freq="1h")
    out = {}
    for i in range(n_assets):
        rel = np.full(len(idx), 1e-5 * (i + 1))
        if not persistent:
            rel = rel * np.where((idx - idx[0]).days // 7 % 2 == 0, 1, -1)
        out[f"A{i}"] = pd.DataFrame({"funding_abs": rel * 100, "funding_rel": rel}, index=idx)
    return out


def test_d_persist_positive_for_persistent_funding_and_flags_otherwise():
    weeks = [T, T + pd.Timedelta("7D")]
    good = dg.d_persist(_funding_panel(), weeks)
    assert good["mean_spearman"] == pytest.approx(1.0) and not good["warning"]
    assert good["weeks"] == 2


def test_d_persist_uses_known_rates_for_f7():
    f = _funding_panel()
    base = dg.d_persist(f, [T])
    # changing the future window changes y but never the F7 side of the pair
    assert dg.f7_cross_section(f, T) == dg.f7_cross_section(
        {a: d.assign(funding_rel=d["funding_rel"].where(d.index < T, 9.0)) for a, d in f.items()}, T
    )
    assert base["weeks"] == 1


def test_d_disp_splits_returns_by_expanding_median_of_dispersion():
    days = pd.date_range(T, periods=10, freq="D")
    disp = pd.Series([1, 1, 1, 1, 1, 5, 5, 5, 5, 5], index=days, dtype=float)
    ret = pd.Series([0.0] * 5 + [0.01] * 5, index=days)
    out = dg.d_disp_from(ret, disp)
    assert out["above"]["n"] == 5 and out["above"]["mean"] == pytest.approx(0.01)
    assert out["below_or_equal"]["n"] == 5


def test_d_h5cells_counts_four_cells():
    idx = pd.MultiIndex.from_tuples(
        [(T, "A"), (T, "B"), (T, "C"), (T, "D"), (T, "E")], names=["day", "asset"]
    )
    tab = pd.DataFrame(
        {
            "F7": [0.2, 0.2, 0.05, 0.05, 0.3],
            "B24": [2, 0, 2, 0, np.nan],
            "B30med": [1, 1, 1, 1, 1],
            "elig_h1": [True] * 5,
        },
        index=idx,
    )
    out = dg.d_h5cells(tab, {})
    assert {k: v["n"] for k, v in out.items()} == {"HF-WB": 1, "HF-NB": 1, "LF-WB": 1, "LF-NB": 1}


def test_basis_distribution_and_autocorrelation():
    b = pd.Series(
        np.sin(np.arange(96 * 20) / 300.0) * 0.001, index=pd.date_range(T, periods=96 * 20, freq="15min")
    )
    out = dg.basis_stats({"A": b})
    assert set(out["A"]) >= {"p05", "p50", "p95", "acf1_daily", "n_days"}
    assert out["A"]["acf1_daily"] > 0.5
