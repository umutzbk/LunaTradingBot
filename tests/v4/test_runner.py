"""`luna research-v4` (spec R9, §7.3–§7.5, §9): refuses to compute anything unless the freeze tag
exists, is an ancestor of HEAD and the tree is clean; Stage 2 also refuses before D0 + 182 days.
The end-to-end run uses synthetic data only, with the guard bypassed by a test-only parameter that the
CLI does not expose."""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from luna.v4 import runner as rn
from luna.v4.constants import LOOKS
from luna.v4.data import HistoricalInputs
from luna.v4.strategies import CONFIGS
from tests.v4.synth import bars


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("a", encoding="utf-8")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-qm", "one")
    return r


def test_refuses_without_the_freeze_tag(repo: Path):
    with pytest.raises(rn.RefuseRun, match="research-v4-spec-frozen"):
        rn.check_freeze(repo)


def test_refuses_when_the_tag_is_not_an_ancestor_of_head(repo: Path):
    _git(repo, "checkout", "-qb", "side")
    (repo / "b.txt").write_text("b", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "tag", "-a", rn.FREEZE_TAG, "-m", "frozen")
    _git(repo, "checkout", "-q", "-")
    with pytest.raises(rn.RefuseRun, match="ancestor"):
        rn.check_freeze(repo)


def test_refuses_on_a_dirty_tree(repo: Path):
    _git(repo, "tag", "-a", rn.FREEZE_TAG, "-m", "frozen")
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(rn.RefuseRun, match="dirty"):
        rn.check_freeze(repo)


def test_accepts_a_clean_tree_at_or_after_the_tag(repo: Path):
    _git(repo, "tag", "-a", rn.FREEZE_TAG, "-m", "frozen")
    (repo / "c.txt").write_text("c", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "after")
    info = rn.check_freeze(repo)
    assert info["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert info["tag_sha"] == _git(repo, "rev-parse", f"{rn.FREEZE_TAG}^{{commit}}")


def test_refuses_when_git_is_unavailable(tmp_path: Path):
    with pytest.raises(rn.RefuseRun):
        rn.check_freeze(tmp_path / "not-a-repo")


def test_stage2_refuses_before_the_first_look():
    with pytest.raises(rn.RefuseRun, match="2027-05-11"):
        rn.check_stage2_time(LOOKS[0] - pd.Timedelta("1s"))
    assert rn.current_look(LOOKS[0]) == (1, LOOKS[0])
    assert rn.current_look(LOOKS[2] + pd.Timedelta("400D")) == (3, LOOKS[2])


# --- CLI --------------------------------------------------------------------------------------------


def test_cli_refuses_with_exit_2_before_loading_any_data(repo: Path, monkeypatch, tmp_path: Path):
    from luna.cli import app

    monkeypatch.setattr(rn, "REPO_ROOT", repo)
    loaded = []
    monkeypatch.setattr(rn, "load_stage_inputs", lambda *a, **k: loaded.append(1))
    out = CliRunner().invoke(app, ["research-v4", "--stage", "1", "--out", str(tmp_path / "o")])
    assert out.exit_code == 2
    assert "refused" in out.output and rn.FREEZE_TAG in out.output
    assert loaded == [] and not (tmp_path / "o").exists()


def test_cli_does_not_expose_the_test_bypass():
    from luna.cli import app, research_v4

    out = CliRunner().invoke(app, ["research-v4", "--help"], terminal_width=200)
    assert out.exit_code == 0 and "--stage" in out.output and "--out" in out.output
    for word in ("skip", "bypass", "unsafe", "force"):
        assert word not in out.output.lower()
    assert all("unsafe" not in p for p in inspect.signature(research_v4).parameters)
    assert "_unsafe_skip_freeze_check_for_tests" not in inspect.getsource(research_v4)


# --- synthetic end-to-end -------------------------------------------------------------------------


def _synthetic_hist(start, end, assets=("AAA", "BBB", "CCC", "DDD", "EEE")):
    n = int((end - start) / pd.Timedelta("15min"))
    idx = pd.date_range(start, periods=n, freq="15min")
    rng = np.random.default_rng(7)
    perp, spot, mark, fund, spreads = {}, {}, {}, {}, {}
    hours = pd.date_range(start, end - pd.Timedelta("1h"), freq="1h")
    for i, a in enumerate(assets):
        path = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
        s = pd.Series(path, index=idx)
        spot[a] = bars(start, n, close=s, volume=200.0)
        p = s * (1 + 0.0005 * (i - 1))
        perp[a] = bars(start, n, close=p, volume=200.0, high=p * 1.001, low=p * 0.999)
        mark[a] = perp[a][["open", "high", "low", "close"]]
        rel = pd.Series(2e-5 * (i - 1) + rng.normal(0, 5e-6, len(hours)), index=hours)
        fund[a] = pd.DataFrame({"funding_abs": rel * 100, "funding_rel": rel})
        spreads[a] = pd.DataFrame({"spread_full": 0.0004}, index=hours)
    index = {a: pd.DataFrame() for a in assets}
    return HistoricalInputs(perp=perp, spot=spot, mark=mark, index=index, funding=fund, spreads=spreads)


def test_full_synthetic_stage1_run_writes_manifest_summary_and_report(tmp_path: Path):
    start = pd.Timestamp("2025-10-06T00:00:00Z")
    eval_start, end = start + pd.Timedelta("30D"), start + pd.Timedelta("100D")
    hist = _synthetic_hist(start, end)
    inputs = rn.stage1_inputs(hist, meta={}, eval_start=eval_start, end=end)
    out = tmp_path / "run"
    summary = rn.run_stage(inputs, out, B=200, _unsafe_skip_freeze_check_for_tests=True)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert man["stage"] == 1 and man["configs"] == list(CONFIGS)
    assert man["null_seeds"] == [1, 2, 3, 4, 5] and man["bootstrap"]["B"] == 200
    assert man["bootstrap"]["seed"] == 20261009 and man["bootstrap"]["blocks"] == [28, 14]
    assert "git_sha" in man and "tag_sha" in man and man["freeze_check"] == "BYPASSED (test only)"
    s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert set(s["configs"]) == set(CONFIGS)
    assert all(v["verdict"] != "GO" for v in s["configs"].values())
    assert s["deployable_now"]["deployable_now"] == "NO"
    assert set(s["nulls"]["H1-Y10"]) == {f"N-RANDENTRY:{k}:H1-Y10" for k in range(1, 6)} | {"N-ALWAYS:H1-Y10"}
    assert set(s["nulls"]["H2-L7-k1"]) == {f"N-SHUFFLE:{k}:H2-L7-k1" for k in range(1, 6)}
    assert set(s["configs"]["H1-Y10"]["rows"]) == {"PRIMARY", "S-SPREAD10", "S-COST2X", "S-MAKER"}
    assert "D-PERSIST" in s["diagnostics"] and "D-H5CELLS" in s["diagnostics"]
    report = (out / "report.md").read_text(encoding="utf-8")
    first = [ln for ln in report.splitlines() if ln.strip()][1]
    assert "EXPLORATORY" in first and "not generalization evidence" in first
    assert "DEPLOYABLE NOW: NO" in report
    assert summary["stage"] == 1
    daily = pd.read_csv(out / "daily" / "H2-L7-k1__PRIMARY.csv")
    assert {"funding_cf", "fees", "spread_slippage", "carry_net"} <= set(daily.columns)


def test_run_stage_refuses_without_the_bypass(tmp_path: Path, repo: Path, monkeypatch):
    monkeypatch.setattr(rn, "REPO_ROOT", repo)
    with pytest.raises(rn.RefuseRun):
        rn.run_stage(object(), tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_stage2_inputs_read_nothing_after_the_look_except_the_valuation_snapshot(tmp_path: Path):
    from tests.v4.synth import write_env, write_meta

    root = tmp_path / "forward"
    write_meta(root)
    look = LOOKS[0]

    def pair(t, seq, px):
        write_env(
            root,
            "fut_tickers",
            t,
            {"tickers": [{"symbol": "PF_XBTUSD", "bid": px, "ask": px, "markPrice": px, "volumeQuote": 1e7}]},
            pair_seq=seq,
        )
        write_env(
            root,
            "spot_ticker",
            t,
            {
                "result": {
                    "XXBTZUSD": {
                        "a": [str(px), "1", "1"],
                        "b": [str(px), "1", "1"],
                        "v": ["1", "1"],
                        "p": ["1", "1"],
                    }
                }
            },
            pair_seq=seq,
        )

    pair(look + pd.Timedelta("5min"), 1, 100.0)  # valuation snapshot of the last evidence day
    pair(look + pd.Timedelta("1h"), 2, 1e9)  # after the look: never read
    rates = [
        {
            "timestamp": (look + pd.Timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "fundingRate": 1.0,
            "relativeFundingRate": 1e-5,
        }
        for h in (-1, 0, 1)
    ]
    write_env(
        root, "fut_funding", look + pd.Timedelta("2h5min"), {"rates": rates}, params={"symbol": "PF_XBTUSD"}
    )
    inp = rn.stage2_inputs(root, look, 1, meta={})
    ex = inp.market.fd.exec
    assert len(ex) == 1 and ex["perp_mid"].iloc[0] == 100.0
    assert inp.days[0] == rn.D0 and inp.days[-1] == look - pd.Timedelta("1D") and len(inp.days) == 182
    assert inp.funding["BTC"].empty  # the only envelope arrived after the look
