"""Research V3 report: V2 report with the family null as criterion 8, plus the V3 block (spec §4, §9, E2)."""

import json

import pandas as pd
import pytest

from luna.engine.backtest import default_instruments
from luna.engine.walkforward import make_windows
from luna.report.walkforward import Inputs
from luna.v3.configs import V3Config
from luna.v3.report import write_v3_report
from luna.v3.runner import run_v3

BAR = pd.Timedelta("15min")
CFGS = [V3Config("spot", "xsec", "1d", "broad", signal="lowvol")]


@pytest.fixture(scope="module")
def summary(v2_panel, v2_settings_small, tmp_path_factory):
    panel = {a: v2_panel[a] for a in ("BTC", "ETH", "AAA")}
    windows, _ = make_windows(
        panel["BTC"].index[0], panel["BTC"].index[-1] + BAR, v2_settings_small.walkforward
    )
    inp = Inputs(panel, {}, None, default_instruments(sorted(panel), "spot", v2_settings_small), [])
    out = tmp_path_factory.mktemp("v3")
    res, null = run_v3(v2_settings_small, "spot", inp, out, windows=windows[-3:], configs=CFGS, loo=False)
    write_v3_report(res, null, inp, v2_settings_small, out)
    text = (out / "summary.json").read_text(encoding="utf-8")
    return json.loads(text), text


def test_summary_has_the_v3_block(summary):
    s, _ = summary
    v3 = s["v3"]
    assert v3["families"] == ["lowvol"] and v3["null_seeds"] == [1, 2, 3, 4, 5]
    assert v3["cumulative_trials"]["v1"] == 208
    assert v3["cumulative_trials"]["v2"] == {"perp": 184, "spot": 88}
    assert v3["cumulative_trials"]["v3"]["configs"] == 1


def test_criterion_8_compares_daily_sharpe_with_the_null(summary):
    s, _ = summary
    d = s["checks"]["complexity_adds_value"]["detail"]
    assert d.startswith("[V3 null baseline] daily Sharpe")


def test_verdict_is_consistent_with_the_checks(summary):
    s, _ = summary
    n_ok = s["checks"]["min_trades"]["passed"]
    all_ok = all(c["passed"] for c in s["checks"].values())
    expected = "INSUFFICIENT DATA" if not n_ok else ("GO" if all_ok else "NO-GO")
    assert s["verdict"] == expected


def test_html_names_the_null_baseline(summary, tmp_path_factory):
    s, _ = summary
    assert s["v3"]["criterion_8"] == "daily Sharpe (edge) > mean null daily Sharpe"


def test_dataset_reuse_caveat_comes_first(summary):
    s, _ = summary
    assert "reused development set" in s["caveats"][0] and "not generalization evidence" in s["caveats"][0]


def test_summary_never_names_the_untouched_set_by_that_word(summary):
    _, text = summary
    assert "holdout" not in text.lower()
