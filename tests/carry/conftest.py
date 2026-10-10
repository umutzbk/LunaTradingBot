"""Carry tests never touch the network: any real HTTP request fails the test."""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(self, request, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError(f"carry tests must not use the network: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
