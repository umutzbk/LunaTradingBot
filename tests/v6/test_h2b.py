"""H2b (S-H2-2): diagnostic only; ridge on winsorised, z-scored FL30, FL7, FL1d, PREM24."""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.v6 import forecast_diag as fd
from luna.v6.data import BarPanel
from tests.v6.fixtures import T0, ar_funding

DAY = pd.Timedelta("1D")


def bars(n_assets=12, days=200, seed=2):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(T0, periods=days * 96, freq="15min")
    cols = [f"A{i:02d}" for i in range(n_assets)]
    idx_px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 1e-3, (len(idx), n_assets)), axis=0)),
                          index=idx, columns=cols)
    mark = idx_px * (1 + rng.normal(2e-4, 1e-4, idx_px.shape))
    return BarPanel(mark), BarPanel(idx_px)


def test_prem24_uses_the_last_96_closed_bars_and_needs_86():
    mark, index = bars()
    t = T0 + 30 * DAY
    p = fd.prem24(mark, index, t)
    sl = slice(t - pd.Timedelta("24h"), t - pd.Timedelta("15min"))
    w = mark.close.loc[sl] / index.close.loc[sl] - 1
    assert len(w) == 96 and p["A00"] == pytest.approx(w["A00"].mean())
    m2 = mark.close.copy()
    m2.loc[t - pd.Timedelta("4h"): t - pd.Timedelta("15min"), "A01"] = np.nan  # 16 missing -> 80 < 86
    assert np.isnan(fd.prem24(BarPanel(m2), index, t)["A01"])
    m3 = mark.close.copy()
    m3.loc[t:, "A02"] = 999.0  # the bar opening at t is not closed at t
    assert fd.prem24(BarPanel(m3), index, t)["A02"] == pytest.approx(p["A02"])


def test_ridge_closed_form_with_fixed_lambda():
    rng = np.random.default_rng(0)
    Z = rng.normal(size=(400, 4))
    y = 1.0 + Z @ np.array([0.5, -0.2, 0.0, 0.1]) + rng.normal(0, 0.1, 400)
    a, b = fd.ridge(Z, y, 0.01)
    n = len(y)
    Zc = Z - Z.mean(0)
    exp_b = np.linalg.solve(Zc.T @ Zc / n + 0.01 * np.eye(4), Zc.T @ (y - y.mean()) / n)
    assert b == pytest.approx(exp_b) and a == pytest.approx(y.mean() - Z.mean(0) @ exp_b)


def test_h2b_fit_and_forecast_run_pit():
    p = ar_funding()
    mark, index = bars()
    m = fd.H2b(p, mark, index, 168)
    t = T0 + 120 * DAY
    f = m.fit(t)
    assert f is not None and f.max_sample_date + pd.Timedelta(hours=168) <= t and f.lam == 0.01
    assert m.forecast(t).notna().sum() >= 10


def test_no_decision_module_imports_the_diagnostic_model():
    root = Path(fd.__file__).parent
    allowed = {"forecast_diag.py", "report.py", "blind.py"}
    for py in root.glob("*.py"):
        if py.name in allowed:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not any("forecast_diag" in n for n in names), f"{py.name} imports H2b"
