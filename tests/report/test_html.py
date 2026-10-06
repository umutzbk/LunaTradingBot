import json
import re

import numpy as np
import pandas as pd
import pytest

import luna.report.gonogo as gonogo_mod
import luna.report.html as html_mod
from luna.config import load_settings
from luna.engine.backtest import default_instruments
from luna.engine.walkforward import universe_at
from luna.report.gonogo import CHECKS
from luna.report.html import EVAL_SECTIONS, SECONDARY_HEADING
from luna.report.walkforward import (
    Inputs,
    best_naive_expectancy,
    build_tradelists,
    walkforward_report,
    walkforward_variants,
    write_report,
)
from tests.helpers import synthetic_bars

T = pd.Timestamp
D0, D1 = T("2024-01-01", tz="UTC"), T("2024-07-01", tz="UTC")
FUNDING_FROM = T("2024-04-15", tz="UTC")


def _settings(var_dir):
    s = load_settings()
    return s.model_copy(update={
        "var_dir": var_dir,
        "universe": s.universe.model_copy(update={"min_adv_usd": 0.0, "min_history_days": 70}),
        "ranking": s.ranking.model_copy(update={"bootstrap_samples": 200, "n_min": 5}),
        "walkforward": s.walkforward.model_copy(update={"train_months": 2, "test_months": 1,
                                                        "step_months": 1, "holdout_months": 1}),
    })


@pytest.fixture(scope="module")
def full_case(tmp_path_factory):
    var = tmp_path_factory.mktemp("var")
    n = 182 * 96
    panel = {
        "BTC": synthetic_bars(n, "trend", seed=1, sigma=0.003, drift=0.00004),
        "ETH": synthetic_bars(n, "ou", seed=2, sigma=0.003),
        "SOL": synthetic_bars(n, "trend", seed=3, sigma=0.003, drift=-0.00003),
    }
    hours = pd.date_range(FUNDING_FROM, D1, freq="1h", inclusive="left")
    funding = {a: pd.DataFrame({"rate": 1e-5}, index=hours) for a in ("BTC", "ETH")}  # SOL: never known
    settings = _settings(var)
    inp = Inputs(panel, funding, None, default_instruments(sorted(panel), "perp", settings), [])
    tl = build_tradelists(inp, settings, "perp", use_regime=True, data_end=D1, cache_dir=var / "research")
    tl0 = build_tradelists(inp, settings, "perp", use_regime=False, data_end=D1, cache_dir=var / "research")
    rep = walkforward_report(inp, tl, tl0, settings, "perp", data_start=D0, data_end=D1,
                             registry=var / "registry.json")
    out = var / "report"
    path = write_report(rep, out, "Luna walk-forward - perp")
    summ = json.loads((out / "summary.json").read_text())
    return rep, path.read_text(encoding="utf-8"), summ, inp, settings


@pytest.fixture(scope="module")
def case(full_case):
    return full_case[:3]


def _sections(page):
    """{h2 heading: section html}"""
    parts = re.findall(r"<section[^>]*><h2>(.*?)</h2>(.*?)</section>", page, flags=re.S)
    return {h: body for h, body in parts}


def test_report_contains_required_sections(case):
    _, page, _ = case
    headings = re.findall(r"<h2>(.*?)</h2>", page)
    assert headings == [h.replace("&", "&amp;") for h in EVAL_SECTIONS]
    assert EVAL_SECTIONS == [
        "Verdict", "Summary", "P&L breakdown", "Funding known vs unknown",
        "Strategy × asset × regime heatmap", "Expectancy table", "Pair selection per window",
        "Ablations", "Baselines", "Sensitivity", "Data caveats",
    ]
    # "Sharpe" appears only inside the Secondary block of the Summary
    secondary = re.findall(r'<div id="secondary">(.*?)</div>', page, flags=re.S)
    assert len(secondary) == 1 and SECONDARY_HEADING in secondary[0]
    assert SECONDARY_HEADING == "Secondary — informational, daily returns"
    assert "Sharpe" in secondary[0]
    rest = page.replace(secondary[0], "")
    assert "sharpe" not in rest.lower()
    summ = _sections(page)["Summary"]
    assert summ.index("Primary metrics") < summ.index('<div id="secondary">')


def test_verdict_section_lists_every_check_and_variant_rule(case):
    rep, page, _ = case
    verdict = _sections(page)["Verdict"]
    assert rep.gonogo.verdict in ("GO", "NO-GO", "INSUFFICIENT DATA")
    assert f">{rep.gonogo.verdict}<" in verdict
    for name in CHECKS:
        assert name in verdict
    assert "realistic" in verdict and "edge" in verdict and "max_drawdown_override=1.0" in verdict


def test_summary_json_has_verdict_checks_and_every_variant(case):
    rep, _, s = case
    assert s["verdict"] == rep.gonogo.verdict
    assert set(s["checks"]) == set(CHECKS)
    assert set(s["variants"]) == set(walkforward_variants("perp"))
    for m in s["variants"].values():
        assert {"n_trades", "expectancy_r", "ci_lo", "ci_hi", "pnl_net", "max_dd", "open_at_end",
                "funding_split", "windows"} <= set(m)
    pw = rep.variants["edge"].per_window
    assert [w["status"] for w in s["variants"]["edge"]["windows"]] == list(pw["status"])
    assert s["variants"]["edge"]["windows"][-1]["accepted_cells"] == pw["accepted_cells"].iloc[-1]
    assert set(s["naive"]) | set(s["naive_skipped"]) == {"naive_momentum", "naive_mean_reversion",
                                                          "naive_pairs"}


def test_naive_baselines_cover_exactly_the_oos_span(case):
    rep, page, _ = case
    s0, s1 = rep.span
    edge = rep.variants["edge"]
    assert s0 == edge.windows[0].window.test_start and s1 == edge.windows[-1].window.test_end
    assert rep.naive
    for res in rep.naive.values():
        assert res.equity.index[0] == s0 and res.equity.index[-1] == s1 - pd.Timedelta("15min")
        assert res.initial_equity == rep.variants["edge"].oos.initial_equity
    base = _sections(page)["Baselines"]
    for name in rep.naive:
        assert name in base
    assert "BTC buy &amp; hold" in base and "cash" in base


def test_funding_split_rows_and_open_positions_not_counted(case):
    rep, page, s = case
    fund = _sections(page)["Funding known vs unknown"]
    assert "known period" in fund and "unknown period" in fund and "SOL" in fund
    tr = rep.variants["edge"].oos.trades
    split = s["variants"]["edge"]["funding_split"]
    assert split["known"]["n_trades"] + split["unknown"]["n_trades"] == len(tr)
    known = tr[(tr["entry_ts"] >= FUNDING_FROM) & ~tr["key"].str.contains("SOL")]
    assert split["known"]["n_trades"] == len(known)
    summ = _sections(page)["Summary"]
    assert "Open positions at the end" in summ and "not counted" in summ
    assert s["variants"]["edge"]["n_trades"] == len(tr)  # closed trades only


def test_ablations_and_sensitivity_sections(case):
    rep, page, _ = case
    sec = _sections(page)
    for name in ("no_regime", "random_ranker"):
        assert name in sec["Ablations"]
    for name in ("cost_2x", "cost_2x_edge", "funding_zero", "funding_estimated", "funding_estimated_x2"):
        assert name in sec["Sensitivity"]
    assert all(s.use_regime is False for m in rep.variants["no_regime"].windows for s in m.strategies)
    assert "accepted" in sec["Expectancy table"] and "reason" in sec["Expectancy table"]
    assert "FDR pass" in sec["Pair selection per window"] and "stable" in sec["Pair selection per window"]
    assert "Survivorship" in sec["Data caveats"] and "Funding gap" in sec["Data caveats"]
    assert np.isfinite(rep.variants["edge"].oos.equity.iloc[-1])


def test_summary_json_ablations_caveats_and_funding_known_from(case):
    rep, _, s = case
    for n in ("no_regime", "random_ranker"):
        d = s["ablations"][n]
        assert d["d_expectancy_r"] == pytest.approx(
            rep.metrics[n]["expectancy_r"] - rep.metrics["edge"]["expectancy_r"])
        assert d["d_pnl_net"] == pytest.approx(rep.metrics[n]["pnl_net"] - rep.metrics["edge"]["pnl_net"])
    text = " ".join(s["caveats"])
    for word in ("Survivorship", "Funding gap", "Regime thresholds are rolling", "Pre-holdout gap"):
        assert word in text
    # data 2024-01-01 .. 2024-07-01, 1-month holdout from 2024-06-01; last test window ends 2024-06-01
    assert "Pre-holdout gap: [2024-06-01, 2024-06-01) (0 days)" in text
    assert s["funding_known_from"] == {"BTC": FUNDING_FROM.isoformat(), "ETH": FUNDING_FROM.isoformat(),
                                       "SOL": None}


def test_naive_universe_uses_the_walkforward_rule(full_case):
    rep, _, _, inp, settings = full_case
    first = rep.variants["edge"].windows[0]
    assert first.window.train_end == rep.span[0]
    assert universe_at(inp.panel, rep.span[0], settings, inp.universe_panel) == first.universe
    assert set(rep.naive["naive_momentum"].trades["key"]) <= set(first.universe)


def test_metrics_are_computed_once(case, tmp_path, monkeypatch):
    rep, _, _ = case

    def boom(*a, **kw):
        raise AssertionError("metrics recomputed")

    monkeypatch.setattr(html_mod, "metrics_with_ci", boom)
    monkeypatch.setattr(gonogo_mod, "metrics_with_ci", boom)
    write_report(rep, tmp_path, "again")  # renderer and summary reuse rep.metrics / rep.naive_metrics
    v = rep.variants
    g = gonogo_mod.evaluate(
        v["edge"].oos, v["realistic"].oos, v["cost_2x_edge"].oos, v["edge"].per_window,
        best_naive_expectancy(rep.naive), rep.funding_known_from,
        metrics={"edge": rep.metrics["edge"], "realistic": rep.metrics["realistic"],
                 "cost_x2_edge": rep.metrics["cost_2x_edge"]},
    )
    assert g == rep.gonogo
