"""Frozen historical input copy (backtest protocol §2.2, R2): cut before the holdout, sha-pinned."""

from __future__ import annotations

import json
import os
import stat

import numpy as np
import pandas as pd
import pytest

from luna.carry import frozen
from luna.v2.data import HoldoutLeak

CUT = pd.Timestamp("2026-07-01T00:00:00Z")


def _bars(start: str, n: int) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    x = np.arange(n, dtype="float64") + 1.0
    return pd.DataFrame(
        {"open": x, "high": x, "low": x, "close": x, "volume": x, "trades": np.arange(n), "filled": False},
        index=idx,
    )


def _series(start: str, n: int, freq: str, col: str) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({col: np.linspace(0.1, 0.2, n)}, index=idx)


@pytest.fixture
def src(tmp_path):
    var = tmp_path / "var"
    (var / "bars" / "kraken_futures").mkdir(parents=True)
    (var / "bars" / "kraken_spot").mkdir(parents=True)
    # live bar files run past the cut (the paper bots append to them)
    _bars("2026-06-30T23:00Z", 8).to_parquet(var / "bars" / "kraken_futures" / "AAA.parquet")
    _bars("2026-06-30T22:00Z", 4).to_parquet(var / "bars" / "kraken_spot" / "AAA.parquet")
    for kind, col, freq in (
        ("funding", "funding_rel", "1h"),
        ("spreads", "spread_full", "1min"),
    ):
        d = var / "history_v4" / kind
        d.mkdir(parents=True)
        _series("2026-06-30T20:00Z", 3, freq, col).to_parquet(d / "AAA.parquet")
    for kind in ("mark", "index"):
        d = var / "history_v4" / kind
        d.mkdir(parents=True)
        _bars("2026-06-30T23:00Z", 4)[["open", "high", "low", "close"]].to_parquet(d / "AAA.parquet")
    inst = tmp_path / "v4_instruments.json"
    inst.write_text('{"assets": {}}', encoding="utf-8")
    return var, inst


def test_freeze_cuts_live_bars_before_holdout_and_writes_manifest(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    man = frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    perp = pd.read_parquet(dst / "bars" / "kraken_futures" / "AAA.parquet")
    assert perp.index.max() == CUT - pd.Timedelta("15min")  # last bar closes exactly at the cut
    assert len(perp) == 4
    e = man["files"]["bars/kraken_futures/AAA.parquet"]
    assert e["rows"] == 4
    assert e["first"] == "2026-06-30T23:00:00+00:00"
    assert e["last"] == "2026-06-30T23:45:00+00:00"
    assert e["rows_dropped_at_or_after_cutoff"] == 4
    assert e["sha256"] == frozen.sha256_file(dst / "bars" / "kraken_futures" / "AAA.parquet")
    on_disk = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["files"] == man["files"]
    assert on_disk["cutoff"] == CUT.isoformat()
    assert (dst / "config" / "v4_instruments.json").read_bytes() == inst.read_bytes()


def test_frozen_rows_are_bit_identical_to_the_source(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    a = pd.read_parquet(var / "bars" / "kraken_futures" / "AAA.parquet")
    b = pd.read_parquet(dst / "bars" / "kraken_futures" / "AAA.parquet")
    pd.testing.assert_frame_equal(a[a.index < CUT - pd.Timedelta("15min") + pd.Timedelta("1ns")], b)


def test_freeze_refuses_history_rows_at_or_after_the_cut(src, tmp_path):
    var, inst = src
    _series("2026-06-30T23:00Z", 2, "1h", "funding_rel").to_parquet(var / "history_v4" / "funding" / "AAA.parquet")
    with pytest.raises(HoldoutLeak):
        frozen.freeze_inputs(var, inst, tmp_path / "frozen", assets=["AAA"])


def test_freeze_refuses_a_cutoff_after_the_holdout_start(src, tmp_path):
    var, inst = src
    with pytest.raises(HoldoutLeak):
        frozen.freeze_inputs(var, inst, tmp_path / "frozen", assets=["AAA"], cutoff=CUT + pd.Timedelta("1s"))


def test_freeze_refuses_an_existing_destination(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    with pytest.raises(FileExistsError):
        frozen.freeze_inputs(var, inst, dst, assets=["AAA"])


def test_frozen_files_are_read_only(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    p = dst / "bars" / "kraken_futures" / "AAA.parquet"
    assert not os.stat(p).st_mode & stat.S_IWRITE


def test_verify_passes_on_an_untouched_copy(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    man = frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    assert frozen.verify_frozen(dst)["files"] == man["files"]


def _tamper(path):
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    return path


def test_verify_fails_on_a_modified_file(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    p = _tamper(dst / "history_v4" / "funding" / "AAA.parquet")
    _series("2026-06-30T19:00Z", 3, "1h", "funding_rel").to_parquet(p)
    with pytest.raises(frozen.FrozenInputError, match="sha256"):
        frozen.verify_frozen(dst)


def test_verify_fails_on_an_unlisted_file(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    _series("2026-06-30T19:00Z", 3, "1h", "funding_rel").to_parquet(dst / "history_v4" / "funding" / "BBB.parquet")
    with pytest.raises(frozen.FrozenInputError, match="not in the manifest"):
        frozen.verify_frozen(dst)


def test_verify_fails_on_a_missing_file(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    p = _tamper(dst / "history_v4" / "mark" / "AAA.parquet")
    p.unlink()
    with pytest.raises(frozen.FrozenInputError, match="missing"):
        frozen.verify_frozen(dst)


def test_verify_fails_on_a_holdout_row_even_with_a_matching_sha(src, tmp_path):
    """A copy rebuilt with a holdout row and a recomputed manifest still stops the run."""
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    p = _tamper(dst / "history_v4" / "spreads" / "AAA.parquet")
    _series("2026-06-30T23:59Z", 2, "1min", "spread_full").to_parquet(p)
    man_p = _tamper(dst / "manifest.json")
    man = json.loads(man_p.read_text(encoding="utf-8"))
    man["files"]["history_v4/spreads/AAA.parquet"]["sha256"] = frozen.sha256_file(p)
    man_p.write_text(json.dumps(man), encoding="utf-8")
    with pytest.raises(HoldoutLeak):
        frozen.verify_frozen(dst)


def test_verify_checks_the_expected_manifest_sha(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    good = frozen.sha256_file(dst / "manifest.json")
    frozen.verify_frozen(dst, expected_manifest_sha256=good)
    with pytest.raises(frozen.FrozenInputError, match="manifest"):
        frozen.verify_frozen(dst, expected_manifest_sha256="0" * 64)


def test_open_frozen_verifies_before_returning_a_path(src, tmp_path):
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    root = frozen.open_frozen(dst)
    assert root == dst
    p = _tamper(dst / "bars" / "kraken_spot" / "AAA.parquet")
    _bars("2026-06-30T20:00Z", 2).to_parquet(p)
    with pytest.raises(frozen.FrozenInputError):
        frozen.open_frozen(dst)


def test_bar_kinds_drop_bars_that_close_after_the_cut(src, tmp_path):
    """Bars are labelled by open_ts: the 23:45 bar closes at the cut and stays; nothing later."""
    var, inst = src
    dst = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, dst, assets=["AAA"])
    spot = pd.read_parquet(dst / "bars" / "kraken_spot" / "AAA.parquet")
    assert spot.index.max() == pd.Timestamp("2026-06-30T22:45Z")
    mark = pd.read_parquet(dst / "history_v4" / "mark" / "AAA.parquet")
    assert mark.index.max() == CUT - pd.Timedelta("15min")
