"""Test-only network fixtures for the S-1.7 fetch layer (Task 25): a recording fake ``Transport`` (no
network), and N6 pairs obtained the only legitimate way, through an N5 response fetched by a ``Fetcher`` (the
registry is private to the fetcher, Task 25 review m-1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from luna.v7.fetch import Fetcher, Response
from luna.v7.netguard import CdxPairs, Request

CDX = "https://web.archive.org/cdx/search/cdx"


@dataclass
class FakeTransport:
    """Serves fixed responses by URL and records every call (a refused request must never get here)."""

    routes: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def get(self, url, params, *, timeout, max_bytes):
        self.calls.append((url, dict(params), timeout))
        if url not in self.routes:
            raise AssertionError(f"unexpected transport call {url}")
        r = self.routes[url]
        return r(url, params) if callable(r) else r


def ok(body: bytes, url: str = "", headers: dict | None = None) -> Response:
    return Response(200, dict(headers or {}), body, url)


def cdx_req(to: str = "20250630235959", **extra) -> Request:
    return Request(CDX, {"url": "binance.com/en/support/announcement/*", "fl": "timestamp,original", "to": to,
                         **extra})


def pairs_via_n5(fetcher: Fetcher, rows, *, to: str = "20250630235959", auth=None) -> CdxPairs:
    """Register ``rows`` [(ts, original)] in ``fetcher``'s private pairs by fetching an N5 response."""
    body = "".join(f"{ts} {orig}\n" for ts, orig in rows).encode()
    tr = fetcher.transport
    assert isinstance(tr, FakeTransport)
    tr.routes[CDX] = ok(body)
    fetcher.fetch(cdx_req(to=to), auth=auth)
    return fetcher.cdx_pairs


def pairs(tmp: Path, rows) -> CdxPairs:
    """A fresh fetcher's pairs holding ``rows`` (pre-opening bound)."""
    return pairs_via_n5(Fetcher(FakeTransport(), Path(tmp)), rows)
