"""One HTTP call -> one envelope row (spec §4). Failures become ``ok=false`` rows, never raise."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from luna.http import get_json

NS = 1_000_000_000


@dataclass(frozen=True)
class Request:
    source: str
    url: str
    params: dict[str, Any] | None
    transform: Callable[[Any], Any] | None = None  # only the fut_tickers universe filter


def iso_ns(ns: int) -> str:
    """UTC ISO-8601 with nanoseconds, e.g. ``2026-10-09T12:00:00.123456789Z``."""
    sec, frac = divmod(int(ns), NS)
    return datetime.fromtimestamp(sec, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{frac:09d}Z"


def ns_from_iso(text: str) -> int:
    """Inverse of :func:`iso_ns`; also accepts ms/us fractions or none (``...Z`` / ``+00:00``)."""
    s = text.strip().removesuffix("Z").removesuffix("+00:00")
    head, _, frac = s.partition(".")
    sec = int(datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp())
    return sec * NS + int((frac or "0")[:9].ljust(9, "0"))


def _api_error(payload: Any) -> str | None:
    """Exchange-level error inside a 200 response (spot ``error`` list, futures ``result=error``)."""
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, list) and err:
        return "; ".join(map(str, err))
    if payload.get("result") == "error":
        return str(err or "result=error")
    return None


def fetch(
    client: httpx.Client,
    req: Request,
    seq: int,
    pair_seq: int | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
    retries: int = 3,
    backoff: float = 1.0,
) -> dict[str, Any]:
    """GET ``req`` via ``luna.http.get_json`` and wrap the raw JSON in an envelope."""
    req_ns = clock_ns()
    payload: Any = None
    error: str | None = None
    try:
        payload = get_json(client, req.url, req.params, retries=retries, backoff=backoff)
        error = _api_error(payload)
        if error is None and req.transform is not None:
            payload = req.transform(payload)
    except Exception as e:  # noqa: BLE001 - every failure must become a row, never a crash
        error = f"{type(e).__name__}: {e}"[:500]
    recv_ns = clock_ns()
    if error is not None:
        payload = None
    env: dict[str, Any] = {"source": req.source, "seq": seq}
    if pair_seq is not None:
        env["pair_seq"] = pair_seq
    env.update(
        req_ts=iso_ns(req_ns),
        recv_ts=iso_ns(recv_ns),
        latency_ms=round((recv_ns - req_ns) / 1e6, 3),
        ok=error is None,
        error=error,
        url=req.url,
        params=req.params,
        filtered_to_universe=req.transform is not None,
        payload=payload,
    )
    return env
