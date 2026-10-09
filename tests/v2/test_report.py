import copy
import json
import math
import re

import pandas as pd
import pytest

from luna.engine.backtest import default_instruments
from luna.engine.walkforward import make_windows
from luna.ranking.ranker import AcceptAllRanker
from luna.report.walkforward import Inputs
from luna.v2 import report as report_mod
from luna.v2.configs import V2Config
from luna.v2.report import (
    SECTIONS,
    build_v2_report,
    naive_momentum_expectancy,
    summary_dict_v2,
    write_v2_report,
)
from luna.v2.runner import run_research

BAR = pd.Timedelta("15min")


@pytest.fixture(scope="module")
def run(v2_panel, v2_settings_small, tmp_path_factory):
    panel = {a: v2_panel[a] for a in ("BTC", "ETH", "AAA")}
    windows, _ = make_windows(
        panel["BTC"].index[0], panel["BTC"].index[-1] + BAR, v2_settings_small.walkforward
    )
    inp = Inputs(panel, {}, None, default_instruments(sorted(panel), "perp", v2_settings_small), [])
    out = tmp_path_factory.mktemp("rv2")
    res = run_research(
        v2_settings_small,
        "perp",
        inp,
        out,
        windows=windows[-3:],
        configs=[V2Config("perp", "tsmom", "4h", "broad"), V2Config("perp", "breakout", "4h", "btc_eth")],
        loo=True,
    )
    return res, inp, v2_settings_small, out


def test_naive_bar_is_momentum_only(run):
    res, inp, settings, _ = run
    r = copy.copy(res)
    mr = copy.deepcopy(res.naive["naive_momentum"])
    mr.trades = mr.trades.assign(r_multiple=1e6)
    r.naive = {**res.naive, "naive_mean_reversion": mr}
    rep = build_v2_report(r, inp, settings)
    bar = naive_momentum_expectancy(res.naive)
    detail = rep.gonogo.checks["complexity_adds_value"][1]
    assert "1000000" not in detail
    assert (f"{bar:.3f}" in detail) if math.isfinite(bar) else "cash" in detail


def test_verdict_is_v1_evaluate(run):
    from luna.report.gonogo import evaluate

    res, inp, settings, _ = run
    rep = build_v2_report(res, inp, settings)
    v = res.candidate_runs
    ref = evaluate(
        v["edge"].oos,
        v["realistic"].oos,
        v["cost_2x_edge"].oos,
        v["edge"].per_window,
        naive_momentum_expectancy(res.naive),
        dict(v["edge"].oos.funding_known_from),
    )
    assert rep.gonogo.verdict == ref.verdict and rep.gonogo.checks == ref.checks


def test_fragile_label_never_changes_verdict(run, monkeypatch):
    res, inp, settings, _ = run
    real = report_mod.robustness
    out = {}
    for flag in (True, False):
        monkeypatch.setattr(report_mod, "robustness", lambda *a, f=flag, **k: {**real(*a, **k), "fragile": f})
        rep = build_v2_report(res, inp, settings)
        out[flag] = (rep.gonogo.verdict, rep.gonogo.checks, rep.candidate)
        assert all(x["fragile"] is flag for x in rep.robustness.values())
    assert out[True] == out[False]


def test_report_contains_no_final_set_metrics(run):
    res, inp, settings, out = run
    rep = build_v2_report(res, inp, settings)
    write_v2_report(rep, out)
    js = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    flat = json.dumps(js)
    assert js["evaluation_set"].startswith("development walk-forward OOS")
    assert not re.search(r"holdout", flat, re.IGNORECASE)
    assert pd.Timestamp(js["span"][1]) <= pd.Timestamp("2026-07-01", tz="UTC")


def test_reports_are_per_account_and_aday_yok(run):
    res, inp, settings, _ = run
    s = summary_dict_v2(build_v2_report(res, inp, settings))
    assert s["account"] == "perp" and "spot" not in json.dumps(s["configs"])
    assert s["verdict"] != "GO" and s["candidate"] == "aday yok"
    assert set(s["fragile"]) == {"perp|tsmom|4h|broad", "perp|breakout|4h|btc_eth", "candidate"}
    assert set(s["variants"]) >= {"realistic", "edge", "cost_2x", "cost_2x_edge", "order_alt_edge"}


def test_html_has_all_sections(run):
    res, inp, settings, out = run
    path = write_v2_report(build_v2_report(res, inp, settings), out)
    html = path.read_text(encoding="utf-8")
    pos = [html.index(f"<section id='{sid}'>") for sid, _ in SECTIONS]
    assert pos == sorted(pos)
    assert "diagnostic, not part of the verdict" in html


def test_report_with_trades_and_missed_fills(run, monkeypatch):
    """Force trades (accept-all candidate) so the trade-based sections are exercised."""
    res, inp, settings, _ = run
    r = copy.deepcopy(res)
    for cws in r.fitted.values():
        for cw in cws:
            if cw.signal is not None and cw.signal.strategies:
                cw.signal.ranker = AcceptAllRanker()
                cw.accepted, cw.objective = True, 1.0
    from luna.v2.walkforward import run_selected_walkforward, select_candidate, v2_variants

    r.selected = select_candidate({c.id: r.fitted[c.id] for c in r.configs})
    r.candidate_runs = {
        v: run_selected_walkforward(r.selected, r.windows, r.start_k, inp, settings, "perp", **kw)
        for v, kw in v2_variants("perp", candidate=True).items()
    }
    rep = build_v2_report(r, inp, settings)
    assert rep.metrics["edge"]["n_trades"] > 0
    assert rep.extended["candidate:edge"]["holding_hours"]["median"] > 0
    assert list(rep.missed_fills.columns) == [
        "ts",
        "strategy",
        "key",
        "direction",
        "r_net_taker",
        "exit_reason",
    ]


def test_summary_records_data_end_and_last_bar(run):
    res, inp, settings, _ = run
    s = summary_dict_v2(build_v2_report(res, inp, settings))
    last = max(df.index[-1] for df in inp.panel.values())
    assert s["data"]["last_bar"] == last.isoformat()
    assert s["data"]["data_end"] == "2026-07-01T00:00:00+00:00"
    assert s["data"]["assets"] == sorted(inp.panel)
    assert {"last_funding", "universe_panel_last_bar"} <= set(s["data"])
