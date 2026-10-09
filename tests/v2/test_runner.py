import pickle

import pandas as pd
import pytest

from luna.engine.backtest import default_instruments
from luna.engine.walkforward import make_windows
from luna.report.walkforward import Inputs
from luna.v2 import runner as runner_mod
from luna.v2.configs import V2Config
from luna.v2.runner import run_research

BAR = pd.Timedelta("15min")
CONFIGS = [
    V2Config("spot", "tsmom", "4h", "broad"),
    V2Config("spot", "xsec", "1d", "broad"),
    V2Config("spot", "xsec", "1d", "broad", "daily"),
]


@pytest.fixture(scope="module")
def small(v2_panel, v2_settings_small):
    panel = {a: v2_panel[a] for a in ("BTC", "ETH", "AAA")}
    windows, _ = make_windows(
        panel["BTC"].index[0], panel["BTC"].index[-1] + BAR, v2_settings_small.walkforward
    )
    inp = Inputs(panel, {}, None, default_instruments(sorted(panel), "spot", v2_settings_small), [])
    return v2_settings_small, inp, windows[-3:]


def test_run_research_produces_every_unit(small, tmp_path):
    settings, inp, windows = small
    res = run_research(settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS, loo=True)
    assert set(res.config_runs) == {c.id for c in CONFIGS}
    assert set(res.config_runs["spot|xsec|1d|broad"]) == {"realistic", "edge"}
    assert set(res.candidate_runs) == {"realistic", "edge", "cost_2x", "cost_2x_edge", "order_alt_edge"}
    assert "spot|xsec|1d|broad|daily" not in res.loo_assets  # sensitivity: reported, not a candidate
    assert set(res.loo_assets["spot|tsmom|4h|broad"]) == {"BTC", "ETH", "AAA"}
    assert set(res.candidate_loo) == {"BTC", "ETH", "AAA"}
    assert len(res.selected) == len(windows) and res.span == (
        windows[res.start_k].test_start,
        windows[-1].test_end,
    )
    assert "naive_momentum" in res.naive and res.trials["configs"] == 3
    assert res.trials["grid_points"] == 6 + 6 + 6  # tsmom 4h + xsec spot weekly + daily
    with open(tmp_path / "results.pkl", "rb") as fh:
        assert pickle.load(fh).account == "spot"
    assert any(p.name.startswith("loo__spot_tsmom_4h_broad__") for p in (tmp_path / "units").iterdir())


def test_resume_skips_finished_units(small, tmp_path, monkeypatch):
    settings, inp, windows = small
    run_research(settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS[:1], loo=False)
    calls = []
    real = runner_mod._u_run
    monkeypatch.setattr(runner_mod, "_u_run", lambda *a: calls.append(a) or real(*a))
    run_research(
        settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS[:1], loo=False, resume=True
    )
    assert calls == []
    run_research(settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS[:1], loo=False)
    assert len(calls) == 2  # realistic + edge recomputed without resume


def test_parallel_jobs_match_inline(small, tmp_path):
    settings, inp, windows = small
    one = run_research(settings, "spot", inp, tmp_path / "a", windows=windows, configs=CONFIGS[:2], loo=False)
    two = run_research(
        settings, "spot", inp, tmp_path / "b", windows=windows, configs=CONFIGS[:2], loo=False, jobs=2
    )
    for cid in one.config_runs:
        pd.testing.assert_frame_equal(
            one.config_runs[cid]["edge"].oos.trades, two.config_runs[cid]["edge"].oos.trades
        )


def test_loo_results_are_trades_only(small, tmp_path):
    settings, inp, windows = small
    res = run_research(settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS[:1], loo=True)
    cid = CONFIGS[0].id
    assert all(isinstance(t, pd.DataFrame) for t in res.config_loo[cid].values())
    assert all(isinstance(t, pd.DataFrame) for t in res.candidate_loo.values())


def test_refit_without_reuses_windows_without_the_asset(small):
    from luna.v2.walkforward import build_v2_tradelists, fit_config, refit_without

    settings, inp, windows = small
    tl = build_v2_tradelists(inp, settings, "spot", windows, cache_dir=None)
    cfg = CONFIGS[0]
    orig = fit_config(cfg, windows, tl, inp, settings)
    out = refit_without(cfg, orig, "ZZZ", tl, inp, settings)  # never in a universe: nothing refitted
    assert all(a is b for a, b in zip(out, orig, strict=True))
    out = refit_without(cfg, orig, "BTC", tl, inp, settings)
    assert all("BTC" not in cw.universe for cw in out)


def test_resume_refuses_a_different_run(small, tmp_path):
    settings, inp, windows = small
    run_research(settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS[:1], loo=False)
    with pytest.raises(runner_mod.ManifestMismatch, match="configs"):
        run_research(
            settings, "spot", inp, tmp_path, windows=windows, configs=CONFIGS[1:2], loo=False, resume=True
        )
    with pytest.raises(runner_mod.ManifestMismatch, match="no manifest"):
        run_research(
            settings,
            "spot",
            inp,
            tmp_path / "empty",
            windows=windows,
            configs=CONFIGS[:1],
            loo=False,
            resume=True,
        )


def _boom(x):
    if x == 1:
        raise RuntimeError("unit failed")
    return x


def _boom_synced(x, failing):
    """u1 signals that it is failing, then raises; u0 only finishes after that signal, so u0 is
    still running (not cancellable) when the failure starts and completes around the time the parent
    observes it -- the order that used to lose u0's checkpoint."""
    if x == 1:
        failing.set()
        raise RuntimeError("unit failed")
    if x == 0:
        assert failing.wait(60), "u1 never ran"
    return x


def test_pool_checkpoints_finished_units_and_cancels_on_failure(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    with multiprocessing.Manager() as mgr:
        failing = mgr.Event()
        units = [(f"u{i}", _boom_synced, (i, failing)) for i in (0, 1, 2, 3)]
        with ProcessPoolExecutor(2) as pool, pytest.raises(RuntimeError, match="unit failed"):
            runner_mod._execute(units, tmp_path, 2, False, pool)
    assert (tmp_path / "u0.pkl").exists()  # running when the failure hit: drained and kept
    assert not (tmp_path / "u1.pkl").exists()


def test_execute_drains_running_units_before_raising(tmp_path):
    from concurrent.futures import Future

    class RunningFuture(Future):
        def __init__(self, value):
            super().__init__()
            self.value = value
            self.set_running_or_notify_cancel()

        def cancel(self):
            ok = super().cancel()  # False: already running
            if not self.done():
                self.set_result(self.value)  # it finishes only after the failure was observed
            return ok

    class ScriptedExecutor:
        """Completion order is scripted, no processes or timing: 'fail' is done (raised) at submit,
        'running' completes only after the runner tried to cancel it (a unit still executing when
        another one failed), 'pending' is cancellable (never started)."""

        def __init__(self, script):
            self.script = script
            self.futures = {}

        def submit(self, fn, *args):
            kind = self.script[args[0]]
            if kind == "running":
                fut = RunningFuture(fn(*args))
            else:
                fut = Future()
                if kind == "fail":
                    fut.set_running_or_notify_cancel()
                    try:
                        fut.set_result(fn(*args))
                    except RuntimeError as e:
                        fut.set_exception(e)
            self.futures[args[0]] = fut
            return fut

    pool = ScriptedExecutor({0: "running", 1: "fail", 2: "running", 3: "pending"})
    units = [(f"u{i}", _boom, (i,)) for i in (0, 1, 2, 3)]
    with pytest.raises(RuntimeError, match="unit failed"):
        runner_mod._execute(units, tmp_path, 2, False, pool)
    assert (tmp_path / "u0.pkl").exists()
    assert (tmp_path / "u2.pkl").exists()
    assert pickle.loads((tmp_path / "u0.pkl").read_bytes()) == 0
    assert not (tmp_path / "u1.pkl").exists()
    assert pool.futures[3].cancelled()
    assert not (tmp_path / "u3.pkl").exists()
