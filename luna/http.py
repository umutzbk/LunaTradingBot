"""HTTP helper: shared client factory and a retrying JSON GET."""

from __future__ import annotations

import time

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=httpx.Timeout(20.0), follow_redirects=True)


def get_json(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    retries: int = 3,
    backoff: float = 1.0,
) -> dict:
    """GET and decode JSON. Retries 429/5xx/transport errors up to `retries` times with
    exponential backoff (backoff * 2**attempt); other 4xx fail immediately. Raises httpx.HTTPError."""
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in RETRY_STATUS or attempt == retries:
                raise
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == retries:
                raise
        time.sleep(backoff * 2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover
