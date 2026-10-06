"""CLI walkforward / holdout on one shared fixture store (module scope: the perp walk-forward runs once
and its cached trade lists serve the later runs)."""

import dataclasses
import json
import re
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from luna.cli import app
from luna.config import load_settings
from luna.data.store import BarStore
from luna.engine.backtest import default_instruments
from luna.report.gonogo import CHECKS
from luna.report.html import EVAL_SECTIONS
from tests.helpers import synthetic_bars

N = 182 * 96  # 2024-01-01 .. 2024-07-01
VERDICTS = {"GO", "NO-GO", "INSUFFICIENT DATA"}


@pytest.fixture(scope="module")
def var(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("var")
    store = BarStore(tmp_path)
    for seed, (asset, kind) in enumerate([("BTC", "trend"), ("ETH", "ou"), ("SOL", "random_walk")]):
        store.write("kraken_futures", asset, synthetic_bars(N, kind, seed=seed, sigma=0.003))
    for seed, asset in enumerate(["BTC", "ETH"]):
        store.write("kraken_spot", asset, synthetic_bars(N, "ou", seed=10 + seed, sigma=0.003))
    hours = pd.date_range("2024-04-15", "2024-07-01", freq="1h", tz="UTC", inclusive="left")
    for asset in ("BTC", "ETH"):
        store.write_funding(asset, pd.DataFrame({"rate": 1e-5}, index=hours))
    settings = load_settings()
    d = tmp_path / "instruments"
    d.mkdir()
    for kind in ("perp", "spot"):
        inst = default_instruments(["BTC", "ETH", "SOL"], kind, settings)
        (d / f"{kind}.json").write_text(json.dumps({a: dataclasses.asdict(i) for a, i in inst.items()}))
    with pytest.MonkeyPatch.context() as mp:
        for k, v in {
            "VAR_DIR": str(tmp_path), "UNIVERSE__MIN_ADV_USD": "0", "UNIVERSE__MIN_HISTORY_DAYS": "70",
            "UNIVERSE__HISTORY_START": "2024-01-01", "WALKFORWARD__TRAIN_MONTHS": "2",
            "WALKFORWARD__TEST_MONTHS": "1", "WALKFORWARD__STEP_MONTHS": "1",
            "WALKFORWARD__HOLDOUT_MONTHS": "1", "RANKING__BOOTSTRAP_SAMPLES": "200", "RANKING__N_MIN": "5",
        }.items():
            mp.setenv(f"LUNA_{k}", v)
        yield tmp_path


def invoke(*args):
    return CliRunner().invoke(app, list(args))


def report_dir(res) -> Path:
    (path,) = re.findall(r"^report: (.+)$", res.output, flags=re.M)
    return Path(path.strip()).parent


@pytest.fixture(scope="module")
def perp_run(var):
    """The one full perp walk-forward of this module, run before any holdout."""
    res = invoke("walkforward", "--account", "perp")
    assert res.exit_code == 0, res.output
    assert not (var / "holdout_registry.json").exists()  # the holdout is never run by walkforward
    d = report_dir(res)
    return res, d, json.loads((d / "summary.json").read_text())


def test_walkforward_cli_writes_report(var, perp_run):
    res, d, s = perp_run
    assert d.parent == var / "reports" and d.name.startswith("walkforward_perp_")
    page = (d / "report.html").read_text(encoding="utf-8")
    assert re.findall(r"<h2>(.*?)</h2>", page) == [h.replace("&", "&amp;") for h in EVAL_SECTIONS]
    assert s["verdict"] in VERDICTS and s["mode"] == "walkforward"
    assert f"verdict: {s['verdict']}" in res.output
    assert set(s["checks"]) == set(CHECKS)


def test_walkforward_cli_summary_variants_and_cache(var, perp_run):
    _, _, s = perp_run
    assert set(s["variants"]) == {"realistic", "edge", "cost_2x", "cost_2x_edge", "no_regime",
                                  "random_ranker", "funding_zero", "funding_estimated",
                                  "funding_estimated_x2"}
    for m in s["variants"].values():
        assert {"n_trades", "expectancy_r", "ci_lo", "profit_factor", "max_dd", "pnl_net"} <= set(m)
    assert set(s["ablations"]) == {"no_regime", "random_ranker"}
    assert set(s["funding_known_from"]) == {"BTC", "ETH", "SOL"} and s["funding_known_from"]["SOL"] is None
    assert any(p.name.startswith("tradelists_perp_") for p in (var / "research").iterdir())  # cached


def test_walkforward_cli_spot(var, perp_run):
    res = invoke("walkforward", "--account", "spot")
    assert res.exit_code == 0, res.output
    s = json.loads((report_dir(res) / "summary.json").read_text())
    assert not any(k.startswith("funding_") for k in s["variants"])
    assert s["naive_skipped"]["naive_pairs"].startswith("pairs require shorting")
    assert "no funding (spot)" in s["checks"]["funding_known_net"]["detail"]
    assert s["funding_known_from"] is None


def test_walkforward_cli_data_end_and_assets(var, perp_run):
    res = invoke("walkforward", "--account", "perp", "--data-end", "2024-06-01", "--assets", "BTC,ETH")
    assert res.exit_code == 0, res.output
    s = json.loads((report_dir(res) / "summary.json").read_text())
    # holdout = last month before 2024-06-01; first window skipped (70-day history rule)
    assert s["span"] == ["2024-04-01T00:00:00+00:00", "2024-05-01T00:00:00+00:00"]
    assert s["setup"]["panel assets"] == "BTC, ETH"


def test_holdout_cli_runs_once(var, perp_run):
    res = invoke("holdout", "--account", "perp")
    assert res.exit_code == 0, res.output
    d = report_dir(res)
    assert d.name.startswith("holdout_perp_")
    s = json.loads((d / "summary.json").read_text())
    assert s["verdict"] in VERDICTS and s["mode"] == "holdout"
    assert set(s["variants"]) == {"realistic", "edge", "cost_2x", "cost_2x_edge", "funding_zero",
                                  "funding_estimated_x2"}
    assert s["span"] == ["2024-06-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00"]
    assert (d / "holdout_results.pkl").exists()  # raw results kept even if reporting had crashed
    assert "Ablations are run in the walk-forward only" in (d / "report.html").read_text(encoding="utf-8")
    assert set(json.loads((var / "holdout_registry.json").read_text())) == {"perp"}

    again = invoke("holdout", "--account", "perp")
    assert again.exit_code == 1
    assert "holdout refused" in again.output and "already run" in again.output
    assert len(list((var / "reports").glob("holdout_perp_*"))) == 1
