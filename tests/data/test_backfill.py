import json
import logging

import httpx
import pandas as pd
import pytest

from luna.config import load_settings
from luna.data import backfill as bf
from luna.data.store import BarStore
from luna.http import make_client
from luna.types import Instrument

END = pd.Timestamp("2026-10-06 08:30", tz="UTC")


def bars(start, n, close=100.0):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1.0, "trades": 1,
         "filled": False},
        index=idx,
    )


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s.var_dir = tmp_path
    return s


@pytest.fixture
def client():
    return make_client(httpx.MockTransport(lambda r: httpx.Response(404)))


def test_last_closed_boundary():
    now = pd.Timestamp("2026-10-06 08:44:59", tz="UTC")
    assert bf.last_closed_boundary(now) == pd.Timestamp("2026-10-06 08:30", tz="UTC")
    assert bf.last_closed_boundary(pd.Timestamp("2026-10-06 08:45", tz="UTC")) == pd.Timestamp(
        "2026-10-06 08:45", tz="UTC"
    )


def _cp(path, pair, start, end):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pair": pair, "start": int(start.value), "end": int(end.value), "since": 0}))


def test_checkpoint_end_reuses_matching(tmp_path):
    cp = tmp_path / "XBTUSD.json"
    start, old_end = pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-10-05", tz="UTC")
    _cp(cp, "XBTUSD", start, old_end)
    assert bf._checkpoint_end(cp, "XBTUSD", start, END) == old_end
    assert cp.exists()


def test_checkpoint_end_discards_mismatch(tmp_path):
    cp = tmp_path / "XBTUSD.json"
    _cp(cp, "XBTUSD", pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-10-05", tz="UTC"))
    assert bf._checkpoint_end(cp, "XBTUSD", pd.Timestamp("2026-07-02", tz="UTC"), END) == END
    assert not cp.exists()
    assert bf._checkpoint_end(cp, "XBTUSD", pd.Timestamp("2026-07-02", tz="UTC"), END) == END  # none at all


def _patch_spot(monkeypatch, archive, calls):
    monkeypatch.setattr(bf, "download_archive", lambda c, d, p: ["part"])
    monkeypatch.setattr(
        bf, "extract_15m", lambda parts, pairs: {p: archive[p] for p in pairs if p in archive}
    )
    monkeypatch.setattr(bf, "fetch_ohlc", lambda c, pair: bars("2026-10-06 06:00", 8, 5.0))

    def fake_trades(client, pair, start, end, checkpoint, on_bars, sleep_s=1.0):
        calls.append((pair, start, end))
        on_bars(bars(start, 3, 7.0))
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("{}")
        return 3

    monkeypatch.setattr(bf, "backfill_trades", fake_trades)


def test_spot_gapfill_merges_and_cleans_up(monkeypatch, settings, client):
    store, calls = BarStore(settings.var_dir), []
    _patch_spot(monkeypatch, {"XBTUSD": bars("2026-06-30 22:00", 8)}, calls)
    done = bf.backfill_spot(client, store, settings, "BTC", None, END)
    assert done == ["BTC"]
    pair, start, end = calls[0]
    assert pair == "XBTUSD" and start == bf.GAP_FILL_FLOOR and end == END
    df = store.read("kraken_spot", "BTC")
    assert df.index.is_unique and df.index.is_monotonic_increasing
    assert (df.index < END).all() and df.loc[bf.GAP_FILL_FLOOR, "close"] == 7.0
    assert not (settings.var_dir / "raw" / "gapfill" / "bars" / "kraken_spot" / "BTC.parquet").exists()
    assert not (settings.var_dir / "raw" / "checkpoints" / "XBTUSD.json").exists()


def test_spot_interrupted_run_merges_leftover_and_reuses_checkpoint(monkeypatch, settings, client):
    store, calls = BarStore(settings.var_dir), []
    _patch_spot(monkeypatch, {"XBTUSD": bars("2026-06-30 22:00", 8)}, calls)
    start = pd.Timestamp("2026-10-04", tz="UTC")
    old_end = pd.Timestamp("2026-10-06 08:00", tz="UTC")
    BarStore(settings.var_dir / "raw" / "gapfill").write("kraken_spot", "BTC", bars("2026-10-04", 4, 9.0))
    _cp(settings.var_dir / "raw" / "checkpoints" / "XBTUSD.json", "XBTUSD", start, old_end)
    bf.backfill_spot(client, store, settings, "BTC", start, END)
    assert calls[0] == ("XBTUSD", start, old_end)  # saved end reused for the resume
    df = store.read("kraken_spot", "BTC")
    assert df.loc[pd.Timestamp("2026-10-04 00:45", tz="UTC"), "close"] == 9.0  # leftover merged
    assert not (settings.var_dir / "raw" / "gapfill" / "bars" / "kraken_spot" / "BTC.parquet").exists()


def test_spot_skips_pair_missing_from_archive(monkeypatch, settings, client, caplog):
    store, calls = BarStore(settings.var_dir), []
    _patch_spot(monkeypatch, {"XBTUSD": bars("2026-06-30 22:00", 8)}, calls)
    with caplog.at_level(logging.WARNING, logger="luna.backfill"):
        done = bf.backfill_spot(client, store, settings, "BTC,NEWCOIN", None, END)
    assert done == ["BTC"] and store.assets("kraken_spot") == ["BTC"]
    assert "NEWCOIN" in caplog.text and "not in archive" in caplog.text


def test_spot_empty_perp_store_warns(monkeypatch, settings, client, caplog):
    store, calls = BarStore(settings.var_dir), []
    _patch_spot(monkeypatch, {}, calls)
    with caplog.at_level(logging.WARNING, logger="luna.backfill"):
        assert bf.backfill_spot(client, store, settings, None, None, END) == []
    assert "perp" in caplog.text and not calls


def _inst(asset):
    return Instrument(
        asset=asset, symbol=f"PF_{asset}USD", venue="kraken_futures", kind="perp", qty_step=1, price_tick=1,
        min_qty=1, min_notional=0, maker_fee=0, taker_fee=0, initial_margin=0, maintenance_margin=0,
    )


def test_perp_assets_start_and_prefilter(monkeypatch, settings, client):
    store, fetched = BarStore(settings.var_dir), []
    listing = [(_inst("BTC"), 5e7), (_inst("ETH"), 4e7), (_inst("TINY"), 1e3)]
    monkeypatch.setattr(bf, "list_perps", lambda c, costs: listing)

    def fake_candles(c, asset, start, end):
        fetched.append((asset, start, end))
        return bars(start, 4)

    monkeypatch.setattr(bf, "fetch_candles", fake_candles)
    funding = pd.DataFrame({"rate": [1e-6]}, index=pd.DatetimeIndex([END], tz="UTC"))
    monkeypatch.setattr(bf, "fetch_funding", lambda c, a: funding)

    # no --assets: prefilter by 24h volume; default start = history_start
    assert bf.backfill_perp(client, store, settings, None, None, END) == ["BTC", "ETH"]
    assert fetched[0][1] == pd.Timestamp(settings.universe.history_start, tz="UTC")
    assert fetched[0][2] == END - bf.BAR  # forming bar excluded

    # --assets restricts (and bypasses prefilter); --start overrides
    fetched.clear()
    start = pd.Timestamp("2026-09-25", tz="UTC")
    assert bf.backfill_perp(client, store, settings, "eth,tiny", start, END) == ["ETH", "TINY"]
    assert [f[0] for f in fetched] == ["ETH", "TINY"] and fetched[0][1] == start
    assert store.read_funding("ETH").index[0] == END
