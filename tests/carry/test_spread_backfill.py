"""Spread backfill infrastructure (protocol §2.1 S0.2): resume-safe, cut before the holdout, honest
about the observed cadence. Fake HTTP only."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pandas as pd
import pytest

from luna.carry import spread_backfill as sb
from luna.v2.data import HoldoutLeak

CUT = pd.Timestamp("2026-07-01T00:00:00Z")


def _payload(ts: list[int], bid: float = 100.0, ask: float = 100.1, more: bool = False, hold: int = 1):
    """Analytics shape; ``hold`` > 1 repeats each value ``hold`` samples (forward-filled cadence)."""
    b = [str(bid + (i // hold) * 0.5) for i in range(len(ts))]
    a = [str(ask + (i // hold) * 0.5) for i in range(len(ts))]
    return {"result": {"timestamp": ts, "data": {"bid": {"best_price": b}, "ask": {"best_price": a}}, "more": more},
            "errors": []}


class FakeClient:
    """Serves [since, to] at ``interval`` with at most ``page`` points per response."""

    def __init__(self, page: int = 10_000, hold: int = 1, fail: int = 0, status: int = 503):
        self.page, self.hold, self.fail, self.status = page, hold, fail, status
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, **(params or {})})
        if self.fail > 0:
            self.fail -= 1
            return httpx.Response(self.status, request=httpx.Request("GET", url))
        lo, hi, iv = int(params["since"]), int(params["to"]), int(params["interval"])
        ts = list(range(lo - lo % iv, hi + 1, iv))
        ts = [t for t in ts if t >= lo]
        more = len(ts) > self.page
        body = _payload(ts[: self.page], more=more, hold=self.hold)
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def _no_sleep(_):
    return None


def test_parse_rejects_an_unknown_shape():
    with pytest.raises(ValueError):
        sb.parse_spreads({"result": {"timestamp": [1], "data": {}}})


def test_parse_gives_full_spread_and_nan_for_missing_quotes():
    df = sb.parse_spreads(_payload([1767225600, 1767225660]) | {})
    assert df["spread_full"].iloc[0] == pytest.approx(0.1 / 100.05)
    p = _payload([1767225600])
    p["result"]["data"]["bid"]["best_price"] = [None]
    assert np.isnan(sb.parse_spreads(p)["spread_full"].iloc[0])


def test_fetch_day_follows_pages_and_never_requests_past_the_cut(tmp_path):
    c = FakeClient(page=100)
    day = pd.Timestamp("2026-06-30T00:00Z")
    df, meta = sb.fetch_day(c, "BTC", day, 60, sleep=_no_sleep)
    assert len(df) == 1440 and df.index.max() == CUT - pd.Timedelta("60s")
    assert all(int(x["to"]) <= int(CUT.timestamp()) - 60 for x in c.calls)
    assert meta["pages"] == 15


def test_fetch_day_refuses_a_day_at_or_after_the_cut():
    with pytest.raises(HoldoutLeak):
        sb.fetch_day(FakeClient(), "BTC", CUT, 60, sleep=_no_sleep)


def test_rows_returned_at_or_after_the_cut_are_dropped_and_counted():
    class Leaky(FakeClient):
        def get(self, url, params=None, timeout=None):
            r = super().get(url, params, timeout)
            body = r.json()
            body["result"]["timestamp"][-1] = int(CUT.timestamp())
            return httpx.Response(200, json=body, request=r.request)

    df, meta = sb.fetch_day(Leaky(), "BTC", pd.Timestamp("2026-06-30T00:00Z"), 60, sleep=_no_sleep)
    assert df.index.max() < CUT
    assert meta["rows_dropped_at_or_after_cutoff"] == 1


def test_retry_recovers_from_transient_errors_with_bounded_backoff():
    waits = []
    c = FakeClient(fail=2)
    df, _ = sb.fetch_day(c, "BTC", pd.Timestamp("2026-06-29T00:00Z"), 300, sleep=waits.append)
    assert len(df) == 288
    assert waits[:2] == [1.0, 2.0]


def test_retry_gives_up_after_the_attempt_budget():
    with pytest.raises(sb.FetchFailed):
        sb.fetch_day(FakeClient(fail=99), "BTC", pd.Timestamp("2026-06-29T00:00Z"), 300, sleep=_no_sleep)


def test_backfill_is_resumable_and_idempotent(tmp_path):
    days = list(pd.date_range("2026-06-27", "2026-06-29", freq="D", tz="UTC"))
    c = FakeClient()
    sb.backfill(c, tmp_path, ["BTC"], days, 300, sleep=_no_sleep)
    n = len(c.calls)
    sb.backfill(c, tmp_path, ["BTC"], days, 300, sleep=_no_sleep)
    assert len(c.calls) == n  # every day already checkpointed
    parts = sorted((tmp_path / "parts" / "BTC").glob("*.parquet"))
    assert len(parts) == 3
    meta = json.loads(parts[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["sha256"] == sb.sha256_file(parts[0]) and meta["interval_s"] == 300


def test_a_corrupt_checkpoint_is_refetched(tmp_path):
    days = [pd.Timestamp("2026-06-29T00:00Z")]
    c = FakeClient()
    sb.backfill(c, tmp_path, ["BTC"], days, 300, sleep=_no_sleep)
    p = next((tmp_path / "parts" / "BTC").glob("*.parquet"))
    p.write_bytes(b"garbage")
    n = len(c.calls)
    sb.backfill(c, tmp_path, ["BTC"], days, 300, sleep=_no_sleep)
    assert len(c.calls) > n


def test_assemble_writes_one_file_with_provenance_and_observed_cadence(tmp_path):
    days = list(pd.date_range("2026-06-28", "2026-06-29", freq="D", tz="UTC"))
    sb.backfill(FakeClient(hold=5), tmp_path, ["BTC"], days, 60, sleep=_no_sleep)
    out = sb.assemble(tmp_path, "BTC", 60)
    df = pd.read_parquet(out)
    assert len(df) == 2 * 1440 and df.index.is_monotonic_increasing and not df.index.duplicated().any()
    prov = json.loads(out.with_name("BTC.provenance.json").read_text(encoding="utf-8"))
    assert prov["requested_interval_s"] == 60
    assert prov["observed_cadence_s"] == 300  # values change every 5 samples: not 1-minute data
    assert prov["real_resolution_matches_interval"] is False
    assert prov["sha256"] == sb.sha256_file(out)


def test_observed_cadence_of_true_1_minute_data():
    idx = pd.date_range("2026-06-01", periods=600, freq="1min", tz="UTC")
    df = pd.DataFrame({"spread_full": np.linspace(1e-4, 2e-4, 600)}, index=idx)
    assert sb.observed_cadence_s(df) == 60
