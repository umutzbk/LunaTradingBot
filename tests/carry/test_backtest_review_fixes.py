"""Regression tests for the independent code / methodology review (2026-10-11) of the V5 backtest
implementation. Fabricated data only."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from luna.carry import backtest as bt
from luna.carry import outcome_guard as og
from luna.carry import parity, report, stats
from luna.carry.history import SnapshotBuilder
from tests.carry.backtest_fixtures import ASSETS, T, base_cfg, instruments, make_data

M15 = pd.Timedelta("15min")
D1 = pd.Timedelta("1D")


# --- C1: every path that produces outcome-window numbers needs the authorization on real data ------


def test_c1_snapshot_builder_refuses_real_data_without_authorization():
    data = make_data(2)
    data.real = True
    with pytest.raises(bt.OutcomeBlocked):
        SnapshotBuilder(data)
    with pytest.raises(bt.OutcomeBlocked):
        bt.ideal_reference(data, base_cfg(), None, T, T + D1, 265.0)


def test_c1_w_v4_with_any_bridge_step_on_real_inputs_needs_authorization():
    from tests.carry.test_bridge import RELS, _cfg, _days, _hist
    from tests.carry.test_parity import _inputs

    inp = _inputs(RELS)
    inp.real = True
    hist = _hist(inp)
    for flags in (parity.BridgeFlags(slip2=True), parity.BridgeFlags(no_resize=True),
                  parity.BridgeFlags(exec_v5=True), parity.BridgeFlags(gates_v5=True)):
        with pytest.raises(bt.OutcomeBlocked):
            parity.run_w_v4(inp, _days(), bridge=flags, hist=hist, cfg=_cfg())
    parity.run_w_v4(inp, _days())  # the V4 reproduction itself (parity, §5) stays allowed


# --- M1: a cycle with entries blocked never uses up the scheduled permission ------------------------


def test_m1_blocked_cycle_without_orders_keeps_the_permission_open():
    ev = bt.arm_config(base_cfg(), "W").evaluation
    s = bt.Schedule(ev)
    assert s.allow(T)
    from types import SimpleNamespace

    blocked = SimpleNamespace(forced_blocked=False, records=[], fills=[], orders=[], opens_allowed=False)
    for i in range(10):
        s.after(T + i * M15, blocked, True, True)
        assert s.allow(T + (i + 1) * M15)
    assert s.log == []


def test_m1_real_core_r3_block_never_consumes_a_scheduled_slot():
    """P-C8 (slots 00/08/16 UTC). The book enters at 00:00; the bar closing at 06:00 moves the long E
    -10 % and the short A +10 % (~5 % day loss): R3 blocks entries for the rest of the day. The 08:00
    and 16:00 slots must stay open (superseded by the next slot), never be logged as completed."""
    data = make_data(3)
    for a, f in (("E", 0.90), ("A", 1.10)):
        for src in (data.mark[a], data.perp[a]):
            src.loc[src.index >= T + pd.Timedelta("5h45min"), "close"] = 100.0 * f
    r = bt.run_arm(data, bt.arm_config(base_cfg(), "C", variant="P-C8"), instruments(), start=T, end=T + 2 * D1)
    assert r.counters["r3_blocked_days"] >= 1 and r.counters["scheduled_blocked_cycles"] > 0
    day = [x for x in r.schedule if T + pd.Timedelta("8h") <= x["slot"] < T + D1]
    assert day and all(x["outcome"] == "superseded" for x in day)


# --- M2: the outcome config is the committed YAML with the frozen DEFAULT values ---------------------


def test_m2_frozen_config_ignores_environment_overrides(monkeypatch):
    monkeypatch.setenv("LUNA_CARRY__SWITCH_HORIZON_HOURS", "72")
    cfg, sha = og.frozen_carry_config()
    assert cfg.switch_horizon_hours == 168 and len(sha) == 64


@pytest.mark.parametrize("old,new", [("min_hold_hours: 24", "min_hold_hours: 12"),
                                     ("max_spread_bps: 15", "max_spread_bps: 30"),
                                     ("min_funding_coverage: 0.90", "min_funding_coverage: 0.50"),
                                     ("funding_unknown_grace_hours: 3", "funding_unknown_grace_hours: 9")])
def test_m2_frozen_config_refuses_values_other_than_the_protocol_defaults(tmp_path, old, new):
    from pathlib import Path

    y = (Path(og.__file__).resolve().parents[2] / "config" / "default.yaml").read_text(encoding="utf-8")
    assert old in y
    bad = tmp_path / "default.yaml"
    bad.write_text(y.replace(old, new), encoding="utf-8")
    with pytest.raises(bt.OutcomeBlocked):
        og.frozen_carry_config(bad)


# --- M3: the guard checks the running checkout; the marker cannot be moved --------------------------


def test_m3_cli_has_no_repo_option_and_the_marker_is_pinned():
    from typer.testing import CliRunner

    from luna.cli import app

    r = CliRunner().invoke(app, ["v5-backtest", "--help"])
    assert "--repo" not in r.output and "--ops-dir" not in r.output
    assert og.code_repo() == og.code_repo().resolve()
    assert og.marker_paths(og.code_repo(), og.code_repo() / "x" / "history_v5" / "frozen") == [
        og.code_repo() / "var" / "ops" / "v5_backtest" / og.MARKER,
        og.code_repo() / "x" / "ops" / "v5_backtest" / og.MARKER]


# --- m1, m2, m4 -----------------------------------------------------------------------------------


def test_m1_minor_nan_daily_series_is_refused_not_turned_into_worse():
    idx = pd.date_range("2025-11-10", periods=60, freq="D", tz="UTC")
    w = stats.RowSeries(pd.Series(0.0, index=idx), pd.Series(0.0, index=idx), 0.0)
    e_c = pd.Series(1e-4, index=idx)
    e_c.iloc[3] = np.nan
    with pytest.raises(ValueError):
        stats.delta_carry(w, stats.RowSeries(e_c, pd.Series(0.0, index=idx), 0.0), B=100)


def test_m4_terminal_unwind_follows_the_row_cost_and_spread_mode():
    data = make_data(3)
    cfg = bt.arm_config(base_cfg(), "W")
    p = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["PRIMARY"], start=T, end=T + D1)
    c = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["S-COST2X"], start=T, end=T + D1)
    assert c.terminal["unwind_cost"] == pytest.approx(2 * p.terminal["unwind_cost"], rel=1e-3)
    f = bt.run_arm(data, cfg, instruments(), row=bt.ROWS["S-SPREAD-FIX"], start=T, end=T + D1)
    gross = sum(abs(q) * 100.0 for q in f.terminal["holdings"].values())
    assert f.terminal["unwind_cost"] == pytest.approx(gross * (0.0005 + 0.0005 + 0.0002), rel=1e-3)


# --- §6 diagnostics and the single-run tables (methodology review #2) -------------------------------


def test_counters_include_stops_funding_stale_and_decision_types():
    data = make_data(3)
    for src in (data.mark["A"], data.perp["A"]):  # short A stopped at 23:45
        src.loc[src.index >= T + pd.Timedelta("23h30min"), "close"] = 120.0
    r = bt.run_arm(data, bt.arm_config(base_cfg(), "E"), instruments(), start=T, end=T + 2 * D1)
    c = r.counters
    assert c["stops"] == 1 and "funding_stale_asset_cycles" in c
    assert c["decisions"]["ENTER"] >= 1 and c["decisions"]["EXIT:stop"] == 1
    assert {x["outcome"] for x in r.schedule} <= {"completed", "exhausted", "superseded"}


def test_report_writes_every_arm_row_table_with_pinned_shas_and_declarations(tmp_path):
    study = bt.run_study(make_data(3), base_cfg(), instruments(), start=T, end=T + 2 * D1, B=100,
                         with_variants=False, with_ideal=False)
    m = report.write_report(study, None, tmp_path / "res", {"synthetic": True})
    tables = m["tables"]
    assert {"W:PRIMARY:daily", "E:S-PIT10:fills", "C:PRIMARY:records", "D:S-NOKILL:schedule"} <= set(tables)
    for rel, sha in tables.items():
        from luna.carry.frozen import sha256_file

        assert sha256_file(tmp_path / "res" / sha["path"]) == sha["sha256"], rel
    md = (tmp_path / "res" / "report.md").read_text(encoding="utf-8")
    assert "## Declared interpretations" in md and "S-COST2X/NOKILL" in md and "maintenance margin" in md
    s = json.loads((tmp_path / "res" / "summary.json").read_text(encoding="utf-8"))
    e = s["arms"]["E"]["PRIMARY"]
    assert {"turnover_per_year_over_mean_equity", "decisions", "stops"} <= set(e)
    assert math.isfinite(e["turnover_per_year_over_mean_equity"])


def test_loader_requires_a_spread_file_for_every_asset(tmp_path):
    from luna.carry import frozen
    from tests.carry.test_frozen import _bars, _series, _v5_spreads

    var = tmp_path / "var"
    (var / "bars" / "kraken_futures").mkdir(parents=True)
    for a in ("AAA", "BBB"):
        _bars("2026-06-30T22:00Z", 4).to_parquet(var / "bars" / "kraken_futures" / f"{a}.parquet")
    d = var / "history_v4" / "funding"
    d.mkdir(parents=True)
    for a in ("AAA", "BBB"):
        f = _series("2026-06-30T20:00Z", 3, "1h", "funding_rel")
        f["funding_abs"] = f["funding_rel"]
        f.to_parquet(d / f"{a}.parquet")
    inst = tmp_path / "inst.json"
    inst.write_text('{"assets": {}}', encoding="utf-8")
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA", "BBB"], history_v5_spreads=_v5_spreads(tmp_path))
    with pytest.raises(ValueError):  # BBB has no 5-minute spread file
        bt.load_historical(dst, frozen.sha256_file(dst / "manifest.json"))


def test_bootstrap_reference_values_pinned_without_the_v4_worktree():
    """Values produced by the V4 ``mbb_pvalue`` (b074b69) on this exact series; kept so the equality
    test can never silently skip."""
    rng = np.random.default_rng(1)
    x = 1e-4 + 1e-3 * rng.standard_normal(233)
    for block, pinned in ((28, PINNED), (14, PINNED_14)):
        r = stats.mbb(x, block, B=2000, seed=stats.SEED)
        assert (r.p, round(r.lower_bound(0.05), 15), round(r.upper_bound(0.05), 15)) == pinned


PINNED = (0.3488255872063968, -4.1650964875e-05, 6.7006397525e-05)  # V4 mbb_pvalue @ b074b69, b = 28
PINNED_14 = (0.3168415792103948, -4.2858139022e-05, 7.9277719797e-05)  # b = 14
