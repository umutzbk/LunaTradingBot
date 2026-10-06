"""Backfill orchestration for perp and spot history (raw traded bars; no gap filling)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pandas as pd

from luna.config import Settings
from luna.data.kraken_futures import fetch_candles, fetch_funding, list_perps
from luna.data.kraken_spot import backfill_trades, fetch_ohlc
from luna.data.kraken_spot_archive import PARTS, download_archive, extract_15m
from luna.data.store import BarStore
from luna.instruments import spot_pair

log = logging.getLogger("luna.backfill")
BAR = pd.Timedelta("15min")
GAP_FILL_FLOOR = pd.Timestamp("2026-07-01", tz="UTC")


def last_closed_boundary(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """Exclusive upper bound for fetches: the start of the currently forming 15m bar."""
    return (now if now is not None else pd.Timestamp.now(tz="UTC")).floor("15min")


def _parse_assets(assets: str | None) -> list[str] | None:
    return [a.strip().upper() for a in assets.split(",") if a.strip()] if assets else None


def backfill_perp(
    client: httpx.Client,
    store: BarStore,
    settings: Settings,
    assets: str | None,
    start: pd.Timestamp | None,
    end: pd.Timestamp,
) -> list[str]:
    cfg = settings.universe
    start = start if start is not None else pd.Timestamp(cfg.history_start, tz="UTC")
    wanted = _parse_assets(assets)
    perps = list_perps(client, settings.costs["perp"])
    chosen = [
        inst.asset
        for inst, vol in perps
        if (inst.asset in wanted if wanted else vol >= cfg.prefilter_volume_usd)
    ]
    for asset in sorted(chosen):
        bars = fetch_candles(client, asset, start, end - BAR)  # inclusive upper bound = last closed bar
        added = store.append("kraken_futures", asset, bars) if not bars.empty else 0
        funding = fetch_funding(client, asset)
        old = store.read_funding(asset)
        merged = pd.concat([old, funding])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        if len(merged):
            store.write_funding(asset, merged)
        log.info("perp %s: +%d bars, %d funding rows", asset, added, len(merged))
    if wanted:
        for a in sorted(set(wanted) - set(chosen)):
            log.warning("perp %s: not listed on Kraken Futures, skipped", a)
    return sorted(chosen)


def _checkpoint_end(cp: Path, pair: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Timestamp:
    """Reuse a resumable checkpoint's end (same pair+start); otherwise discard it."""
    if cp.exists():
        saved = json.loads(cp.read_text())
        if saved.get("pair") == pair and saved.get("start") == int(start.value):
            return pd.Timestamp(saved["end"], tz="UTC")
        cp.unlink()
    return end


def backfill_spot(
    client: httpx.Client,
    store: BarStore,
    settings: Settings,
    assets: str | None,
    start: pd.Timestamp | None,
    end: pd.Timestamp,
) -> list[str]:
    var = Path(settings.var_dir)
    asset_list = _parse_assets(assets) or store.assets("kraken_futures")
    if not asset_list:
        log.warning("spot: no assets (perp store empty, no --assets); run the perp backfill first")
        return []
    parts = download_archive(client, var / "raw", PARTS)
    # Gap-fill bars land in a small staging store (appending to the full-history parquet every
    # page is far too slow). Everything for one asset is merged into the main store in a single
    # read-merge-write, and the staging file is only deleted after that, so an interrupted run
    # loses nothing.
    staging = BarStore(var / "raw" / "gapfill")
    done = []
    for asset in asset_list:
        pair = spot_pair(asset)
        # one pair at a time keeps memory flat; archive frames are dropped after each asset
        archive = extract_15m(parts, [pair]).get(pair)
        if archive is None:
            log.warning("spot %s: pair %s not in archive, skipped", asset, pair)
            continue
        if start is not None:
            gap_start = start
        else:
            after_archive = archive.index[-1] + BAR if not archive.empty else GAP_FILL_FLOOR
            gap_start = max(after_archive, GAP_FILL_FLOOR)
        added = 0
        cp = var / "raw" / "checkpoints" / f"{pair}.json"
        if gap_start < end:
            gap_end = _checkpoint_end(cp, pair, gap_start, end)
            added = backfill_trades(
                client, pair, gap_start, gap_end, cp,
                on_bars=lambda df, a=asset: staging.append("kraken_spot", a, df),
            )
        top = fetch_ohlc(client, pair)
        top = top[top.index < end]
        pieces = [archive, staging.read("kraken_spot", asset), top]  # later pieces win
        store.append("kraken_spot", asset, pd.concat([p for p in pieces if not p.empty]))
        staging.delete("kraken_spot", asset)
        cp.unlink(missing_ok=True)
        log.info("spot %s: archive %d, gap-fill %d, top-up %d", asset, len(archive), added, len(top))
        done.append(asset)
        del archive, pieces
    return done
