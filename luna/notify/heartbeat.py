"""Dead-man's-switch heartbeat ping (healthchecks.io style)."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def ping(client: httpx.Client, url: str | None) -> None:
    """GET `url` with a short timeout. No-op when url is None; never raises."""
    if not url:
        return
    try:
        client.get(url, timeout=5.0)
    except Exception as e:  # noqa: BLE001 - heartbeat must never break the engine
        log.debug("heartbeat ping failed: %s", e)
