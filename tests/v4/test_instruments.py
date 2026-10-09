"""Freeze-time instrument metadata snapshot (spec §3 minimum order, §7.1) built only from the collector's
raw `fut_instruments` / `spot_assetpairs` envelopes (exchange metadata, not outcomes)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from luna.v4 import instruments as ins
from luna.v4.accounting import LegMeta
from tests.v4.synth import write_env, write_meta

T0 = pd.Timestamp("2026-10-09T11:52:50Z")


def _fut(symbols=("PF_XBTUSD", "PF_ETHUSD"), prec=4, tick=1.0):
    return {
        "result": "success",
        "instruments": [
            {
                "symbol": s,
                "contractSize": 1,
                "contractValueTradePrecision": prec,
                "tickSize": tick,
                "openingDate": "2022-03-21T00:00:00.000Z",
            }
            for s in symbols
        ],
    }


def _spot(pairs=(("XXBTZUSD", "XBTUSD"), ("XETHZUSD", "ETHUSD"))):
    return {
        "error": [],
        "result": {
            k: {
                "altname": alt,
                "lot_decimals": 8,
                "pair_decimals": 1,
                "tick_size": "0.1",
                "ordermin": "0.00005",
                "costmin": "0.5",
            }
            for k, alt in pairs
        },
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "forward"
    write_meta(root)
    write_env(root, "fut_instruments", T0 + pd.Timedelta("1h"), _fut(prec=2))  # a later snapshot
    write_env(root, "fut_instruments", T0, _fut())
    write_env(root, "fut_instruments", T0 - pd.Timedelta("1min"), _fut(prec=1))  # before collector start
    write_env(root, "spot_assetpairs", T0, _spot())
    return root


def test_snapshot_uses_the_earliest_ok_envelopes_after_collector_start(tmp_path: Path):
    snap = ins.build_snapshot(_root(tmp_path), ["BTC", "ETH"])
    btc = snap["assets"]["BTC"]
    assert btc["perp"] == {"step": 1e-4, "min_qty": 1e-4, "min_cost": 0.0, "tick": 1.0}
    assert btc["spot"] == {"step": 1e-8, "min_qty": 0.00005, "min_cost": 0.5, "tick": 0.1}
    assert snap["source"]["fut_instruments"]["recv_ts"].startswith("2026-10-09T11:52:50")
    assert LegMeta(**btc["perp"]).step == 1e-4


def test_snapshot_fails_loudly_on_a_missing_asset_or_field(tmp_path: Path):
    root = _root(tmp_path)
    with pytest.raises(ValueError, match="SOL"):
        ins.build_snapshot(root, ["BTC", "SOL"])
    bad = tmp_path / "bad"
    write_meta(bad)
    fut = _fut()
    del fut["instruments"][0]["tickSize"]
    write_env(bad, "fut_instruments", T0, fut)
    write_env(bad, "spot_assetpairs", T0, _spot())
    with pytest.raises(ValueError, match="tickSize"):
        ins.build_snapshot(bad, ["BTC"])
    spot_bad = tmp_path / "spot_bad"
    write_meta(spot_bad)
    write_env(spot_bad, "fut_instruments", T0, _fut())
    sp = _spot()
    del sp["result"]["XXBTZUSD"]["costmin"]
    write_env(spot_bad, "spot_assetpairs", T0, sp)
    with pytest.raises(ValueError, match="costmin"):
        ins.build_snapshot(spot_bad, ["BTC"])


def test_snapshot_rejects_a_non_linear_contract(tmp_path: Path):
    root = tmp_path / "f"
    write_meta(root)
    fut = _fut()
    fut["instruments"][0]["contractSize"] = 10
    write_env(root, "fut_instruments", T0, fut)
    write_env(root, "spot_assetpairs", T0, _spot())
    with pytest.raises(ValueError, match="contractSize"):
        ins.build_snapshot(root, ["BTC"])


def test_cli_writes_the_snapshot_that_the_runner_loads(tmp_path: Path, monkeypatch):
    from luna.cli import app
    from luna.v4 import runner as rn

    root = _root(tmp_path)
    monkeypatch.setattr(ins, "DEFAULT_ASSETS", ("BTC", "ETH"))
    out = tmp_path / "repo" / "config" / "v4_instruments.json"
    res = CliRunner().invoke(app, ["v4-instruments", "--forward-dir", str(root), "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "sha256" in res.output
    meta, sha = rn.load_instrument_meta(tmp_path / "repo")
    assert meta["ETH"]["spot"].min_cost == 0.5 and len(sha) == 64
    assert json.loads(out.read_text(encoding="utf-8"))["universe"] == ["BTC", "ETH"]
