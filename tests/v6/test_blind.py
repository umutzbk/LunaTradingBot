"""The blind development runner exposes invariants only and has no unmask path."""

import inspect

import pytest
from typer.testing import CliRunner

from luna.cli import app
from luna.v6 import blind


def test_masking_refuses_any_performance_key():
    for key in ("net", "pnl_total", "equity_end", "carry_alpha", "sharpe", "max_drawdown", "kills", "stops",
                "calibration_R", "config_rank", "p_go"):
        with pytest.raises(RuntimeError):
            blind._assert_masked({"configs": {"H3-k4": {key: 1.0}}})
    blind._assert_masked({"configs": {"H3-k4": {"legs_per_ok_decision": {"n": 1},
                                                "accounting_identities": {"residual_daily_sum_vs_equity_change": 0.0}}}})


def test_report_builders_emit_only_whitelisted_invariants():
    import pandas as pd
    from types import SimpleNamespace
    r = SimpleNamespace(decisions=[{"status": "OK", "long": ["A"] * 4, "short": ["B"] * 4,
                                    "post": {"ok": True, "beta_net": 0.01, "q": 1.1},
                                    "build": {"dropped": [], "cap_bound": False, "ratio_bound": False,
                                              "beta_scaled": False}, "risk": {"RC_L": 0.5}}],
                        counters={})
    inv = blind._h3_invariants(r)
    blind._assert_masked({"x": inv})
    assert inv["risk_checks_satisfied_share"] == 1.0


def test_no_unmask_option_anywhere():
    src = inspect.getsource(blind)
    assert "unmask" not in src
    sig = inspect.signature(blind.blind_dev_run)
    assert set(sig.parameters) == {"frozen_root", "start", "end"}
    res = CliRunner().invoke(app, ["v6-blind-dev", "--help"])
    assert res.exit_code == 0 and "unmask" not in res.output.lower() and "--show" not in res.output


def test_blind_window_cannot_touch_the_holdout():
    import pandas as pd
    from luna.v6 import guard
    with pytest.raises(guard.HoldoutBlocked):
        guard.require_window(pd.Timestamp("2025-11-10T00:00Z"), pd.Timestamp("2026-07-02T00:00Z"), "dev")
