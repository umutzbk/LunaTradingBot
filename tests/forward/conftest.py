"""Shared fakes for the forward-collector tests: a fake clock/sleep and a Kraken mock transport."""

from __future__ import annotations

import httpx
import pytest

S = 1_000_000_000


class FakeClock:
    """``clock()`` returns ns; ``sleep(s)`` advances it. No real time passes."""

    def __init__(self, ns: int) -> None:
        self.ns = ns
        self.sleeps: list[float] = []

    def __call__(self) -> int:
        return self.ns

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.ns += round(seconds * S)


def kraken_payload(path: str):
    if path.endswith("/derivatives/api/v3/tickers"):
        return {"result": "success", "serverTime": "2026-10-09T12:00:00.000Z",
                "tickers": [{"symbol": "PF_XBTUSD", "bid": 1.0, "ask": 2.0,
                             "lastTime": "2026-10-09T12:00:00Z"}]}
    if path.endswith("/0/public/Ticker"):
        return {"error": [], "result": {"XXBTZUSD": {"a": ["2.0", "1", "1.0"], "b": ["1.0", "1", "1.0"]}}}
    if path.endswith("/0/public/Time"):
        return {"error": [], "result": {"unixtime": 1791547200, "rfc1123": "x"}}
    return {"result": "success", "path": path}


class Kraken:
    """MockTransport handler: records (path, params, clock ns) and advances the clock by latency."""

    def __init__(self, clock: FakeClock, latency_s: float = 0.05) -> None:
        self.clock = clock
        self.latency_ns = int(latency_s * S)
        self.calls: list[tuple[str, dict, int]] = []
        self.fail: set[str] = set()  # path suffixes answering HTTP 500

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((path, dict(request.url.params), self.clock.ns))
        self.clock.ns += self.latency_ns
        if any(path.endswith(f) for f in self.fail):
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=kraken_payload(path))

    def count(self, suffix: str) -> int:
        return sum(1 for p, _, _ in self.calls if suffix in p)

    def times(self, suffix: str) -> list[int]:
        return [t for p, _, t in self.calls if suffix in p]


@pytest.fixture
def fake_kraken():
    def make(start_ns: int, latency_s: float = 0.05):
        clock = FakeClock(start_ns)
        return clock, Kraken(clock, latency_s)

    return make
