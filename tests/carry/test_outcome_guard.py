"""Outcome guard (frozen protocol §12, R1, R9): the single historical outcome run is refused unless
every pre-run check passes, the user's explicit approval is given and no outcome run was started
before. Temporary git repos and a tiny fabricated frozen copy only."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pandas as pd
import pytest

from luna.carry import backtest as bt
from luna.carry import frozen
from luna.carry import outcome_guard as og
from tests.carry.backtest_fixtures import T, base_cfg, instruments, make_data
from tests.carry.test_frozen import _bars, _series

PROTO = "docs/protocol.md"
TAG = "test-protocol-frozen"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "docs").mkdir()
    (repo / PROTO).write_text("frozen protocol\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "freeze")
    _git(repo, "tag", "-a", TAG, "-m", "frozen")
    var = tmp_path / "var"
    (var / "bars" / "kraken_futures").mkdir(parents=True)
    _bars("2026-06-30T22:00Z", 4).to_parquet(var / "bars" / "kraken_futures" / "AAA.parquet")
    d = var / "history_v4" / "funding"
    d.mkdir(parents=True)
    _series("2026-06-30T20:00Z", 3, "1h", "funding_rel").to_parquet(d / "AAA.parquet")
    inst = tmp_path / "inst.json"
    inst.write_text('{"assets": {}}', encoding="utf-8")
    fz = tmp_path / "frozen"
    frozen.freeze_inputs(var, inst, fz, assets=["AAA"])
    kw = {"protocol_rel": PROTO, "tag": TAG,
          "protocol_sha": hashlib.sha256(b"frozen protocol\n").hexdigest(),
          "manifest_sha": frozen.sha256_file(fz / "manifest.json")}
    return repo, fz, tmp_path / "ops", kw


def test_preflight_passes_on_a_clean_tagged_tree(env):
    repo, fz, ops, kw = env
    rec = og.preflight(repo, fz, **kw)
    assert rec["head"] == _git(repo, "rev-parse", "HEAD").strip() and rec["manifest_sha256"] == kw["manifest_sha"]


@pytest.mark.parametrize("breakage", ["dirty", "protocol_edited", "no_tag", "wrong_protocol_sha", "wrong_manifest"])
def test_preflight_refuses_every_broken_condition(env, breakage):
    repo, fz, ops, kw = env
    if breakage == "dirty":
        (repo / "x.txt").write_text("x", encoding="utf-8")
    elif breakage == "protocol_edited":
        (repo / PROTO).write_text("changed after the freeze\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "edit")
    elif breakage == "no_tag":
        kw = {**kw, "tag": "missing-tag"}
    elif breakage == "wrong_protocol_sha":
        kw = {**kw, "protocol_sha": "0" * 64}
    else:
        kw = {**kw, "manifest_sha": "0" * 64}
    with pytest.raises(bt.OutcomeBlocked):
        og.preflight(repo, fz, **kw)


def test_tag_must_be_an_ancestor_of_head(env):
    repo, fz, ops, kw = env
    _git(repo, "checkout", "-q", "--orphan", "other")
    _git(repo, "commit", "-q", "-m", "unrelated")
    with pytest.raises(bt.OutcomeBlocked):
        og.preflight(repo, fz, **kw)


def test_authorize_needs_the_explicit_approval_phrase(env):
    repo, fz, ops, kw = env
    for bad in (None, "", "yes", og.approval_phrase("0" * 64)):
        with pytest.raises(bt.OutcomeBlocked):
            og.authorize(repo, fz, ops, bad, **kw)
    assert not (ops / og.MARKER).exists()


def test_authorize_is_single_use_and_its_token_unlocks_real_inputs(env):
    repo, fz, ops, kw = env
    auth = og.authorize(repo, fz, ops, og.approval_phrase(kw["protocol_sha"]), **kw)
    rec = json.loads((ops / og.MARKER).read_text(encoding="utf-8"))
    assert rec["head"] == auth.record["head"] and rec["approved"] is True
    with pytest.raises(bt.OutcomeBlocked):  # R9: a second outcome run is never automatic
        og.authorize(repo, fz, ops, og.approval_phrase(kw["protocol_sha"]), **kw)
    data = make_data(2)
    data.real = True  # fabricated stand-in: only the authorization path is exercised here
    r = bt.run_arm(data, bt.arm_config(base_cfg(), "W"), instruments(), start=T, end=T + pd.Timedelta("1D"),
                   auth=auth)
    assert len(r.fills) == 4


def test_the_real_cli_constants_are_the_frozen_ones():
    assert og.TAG == "v5-backtest-protocol-frozen"
    assert og.PROTOCOL_SHA == "4bd90f5a169025e0483d13c636964a937d01f10fce3355ced1c81058dfcaba54"
    assert og.FROZEN_MANIFEST_SHA == "eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14"
    assert og.PROTOCOL == "docs/superpowers/specs/luna-v5-historical-backtest-protocol.md"
