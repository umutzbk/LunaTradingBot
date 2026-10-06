"""Symbol mapping and instrument construction for Kraken perps and spot."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx

from luna.config import CostConfig, Settings
from luna.types import AccountKind, Instrument


def _kraken_code(asset: str) -> str:
    return "XBT" if asset == "BTC" else asset


def perp_symbol(asset: str) -> str:
    return f"PF_{_kraken_code(asset)}USD"


SPOT_ALIAS = {"BTC": "XBT", "DOGE": "XDG"}  # Kraken spot altname codes that differ from the ticker
_SPOT_UNALIAS = {v: k for k, v in SPOT_ALIAS.items()}


def spot_pair(asset: str) -> str:
    return f"{SPOT_ALIAS.get(asset, asset)}USD"


def asset_from_spot(pair: str) -> str:
    code = pair.removesuffix("USD")
    return _SPOT_UNALIAS.get(code, code)


def asset_from_perp(symbol: str) -> str:
    code = symbol.removeprefix("PF_").removesuffix("USD")
    return "BTC" if code == "XBT" else code


def perp_instrument(meta: dict, costs: CostConfig) -> Instrument:
    """Build an Instrument from a /derivatives/api/v3/instruments entry."""
    step = 10.0 ** -int(meta["contractValueTradePrecision"])
    margin = (meta.get("marginLevels") or [{}])[0]
    return Instrument(
        asset=asset_from_perp(meta["symbol"]),
        symbol=meta["symbol"],
        venue="kraken_futures",
        kind="perp",
        qty_step=step,
        price_tick=float(meta["tickSize"]),
        min_qty=step,
        min_notional=0.0,
        maker_fee=costs.maker_fee,
        taker_fee=costs.taker_fee,
        initial_margin=float(margin.get("initialMargin", 0.0)),
        maintenance_margin=float(margin.get("maintenanceMargin", 0.0)),
    )


def _instrument_cache(settings: Settings, kind: AccountKind) -> Path:
    return Path(settings.var_dir) / "instruments" / f"{kind}.json"


def load_instruments(
    settings: Settings,
    kind: AccountKind,
    client: httpx.Client | None = None,
    assets: list[str] | None = None,
) -> dict[str, Instrument]:
    """Exchange instruments by asset from `var/instruments/{kind}.json`; if absent, fetch (perp:
    every listed perp; spot: `assets`, default the assets in the spot bar store) and write the cache."""
    path = _instrument_cache(settings, kind)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {a: Instrument(**d) for a, d in raw.items()}
    from luna.data.kraken_futures import list_perps
    from luna.data.kraken_spot import fetch_spot_instruments
    from luna.data.store import BarStore
    from luna.http import make_client

    own = client is None
    client = client if client is not None else make_client()
    try:
        if kind == "perp":
            found = {inst.asset: inst for inst, _ in list_perps(client, settings.costs["perp"])}
        else:
            wanted = assets if assets is not None else BarStore(settings.var_dir).assets("kraken_spot")
            found = fetch_spot_instruments(
                client, [spot_pair(a) for a in wanted], settings.costs["spot"], allow_missing=True
            )
    finally:
        if own:
            client.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {a: dataclasses.asdict(i) for a, i in found.items()}
    path.write_text(json.dumps(raw, indent=1), encoding="utf-8")
    return found
