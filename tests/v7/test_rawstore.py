"""Task 26a (SC-01 part 1, SC-03 read-only loaders): the immutable raw store (manifests, write-once chunks),
the read-only lazy loaders with a bounded LRU chunk cache, the persisted BookRuns and the loader-backed
research universe. Synthetic data in tmp_path only; never var/, never the network."""

from __future__ import annotations

import hashlib
import json
import time
import tracemalloc

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from luna.v7 import guard, k9, rawstore, runner, windows
from luna.v7.data import funding as fund_mod
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.rawstore import ChunkCache, FundingLoader, RawStore, TableLoader
from luna.v7.runner import InMemorySource, ResearchUniverse
from luna.v7.timeutil import MIN, D, to_ns
from tests.v7 import fixtures_v7 as fx

JAN = to_ns("2024-01-01")
FEB = to_ns("2024-02-01")
MAR = to_ns("2024-03-01")
HOLD = windows.W_HOLD[0]


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


def bars(lo: int, n: int, step: int = 15 * MIN, seed: int = 0) -> pd.DataFrame:
    """A normalised bar frame (``klines.normalise`` columns, known_at = open_ts + step)."""
    g = np.random.default_rng(seed)
    c = np.round(100 * np.exp(np.cumsum(g.normal(0, 1e-3, n))), 2)
    ts = lo + np.arange(n, dtype=np.int64) * step
    vb = np.round(g.gamma(2.0, 5.0, n), 3)
    return pd.DataFrame(dict(open_ts=ts, o=c, h=c + 0.01, l=c - 0.01, c=c, vol_base=vb, vol_quote=vb * c,
                             n_trades=g.poisson(50, n).astype(np.int64), taker_buy_quote=vb * c / 2,
                             known_at=ts + step))


def month_of(df: pd.DataFrame, lo: int, hi: int) -> pd.DataFrame:
    t = df["open_ts"].to_numpy()
    return df[(t >= lo) & (t < hi)].reset_index(drop=True)


def put_months(store: RawStore, table: str, sym: str, df: pd.DataFrame, months) -> None:
    for m in months:
        lo, hi = rawstore.month_period(m)
        store.put_chunk(table, sym, m, month_of(df, lo, hi), sources=[{"url": f"synthetic://{m}"}])


# ---------------------------------------------------------------------------------------------- store


def test_round_trip_exact_columns_and_dtypes(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    df = bars(JAN, 2 * 31 * 96)
    put_months(st, "R3", "BTCUSDT", df, ["2024-01", "2024-02"])
    ld = TableLoader(st, "R3")
    got = ld.frame("BTCUSDT", JAN, MAR)
    want = month_of(df, JAN, MAR)
    pd.testing.assert_frame_equal(got, want)
    assert list(got.columns) == ["open_ts", "o", "h", "l", "c", "vol_base", "vol_quote", "n_trades",
                                 "taker_buy_quote", "known_at"]
    a = ld.arrays("BTCUSDT", JAN + 5 * D, JAN + 6 * D)
    assert a["open_ts"].dtype == np.int64 and a["c"].dtype == np.float64 and a["n_trades"].dtype == np.int64
    assert len(a["open_ts"]) == 96 and a["open_ts"][0] == JAN + 5 * D
    assert (a["known_at"] == a["open_ts"] + 15 * MIN).all()
    # R4: only o, h, l, c are stored (the R4 columns)
    put_months(st, "R4_mark", "BTCUSDT", df[["open_ts", "o", "h", "l", "c"]], ["2024-01"])
    f4 = TableLoader(st, "R4_mark").frame("BTCUSDT", JAN, FEB)
    assert list(f4.columns) == ["open_ts", "o", "h", "l", "c", "known_at"]
    assert log.records() == []


def test_manifests_hash_chain(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    tman = st.table_dir("R3") / "MANIFEST.json"
    sman = st.table_dir("R3") / "BTCUSDT" / "MANIFEST.json"
    chunk = st.table_dir("R3") / "BTCUSDT" / "BTCUSDT-2024-01.parquet"
    t = json.loads(tman.read_text(encoding="utf-8"))
    s = json.loads(sman.read_text(encoding="utf-8"))
    e = s["chunks"]["2024-01"]
    assert e["sha256"] == hashlib.sha256(chunk.read_bytes()).hexdigest()
    assert e["rows"] == 31 * 96 and e["period"] == [JAN, FEB] and e["sources"] == [{"url": "synthetic://2024-01"}]
    assert t["symbols"]["BTCUSDT"]["manifest_sha256"] == hashlib.sha256(sman.read_bytes()).hexdigest()
    assert st.manifest_sha256("R3") == hashlib.sha256(tman.read_bytes()).hexdigest()
    assert st.manifest_sha256s() == {"R3": st.manifest_sha256("R3")}  # DT4 data_manifests
    ld = TableLoader(st, "R3", manifest_sha256=st.manifest_sha256("R3"))
    assert ld.manifest_sha256 == st.manifest_sha256("R3")
    with pytest.raises(IntegrityBlocker):
        TableLoader(st, "R3", manifest_sha256="0" * 64)
    # a symbol manifest that no longer matches the table manifest is refused when the symbol is opened
    sman.write_text(sman.read_text(encoding="utf-8").replace('"rows":2976', '"rows":2975'), encoding="utf-8")
    with pytest.raises(IntegrityBlocker):
        TableLoader(st, "R3").frame("BTCUSDT", JAN, FEB)


def test_written_once_identical_rewrite_is_a_no_op_different_content_is_refused(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    df = month_of(bars(JAN, 31 * 96), JAN, FEB)
    e1 = st.put_chunk("R3", "BTCUSDT", "2024-01", df, sources=[{"url": "a"}])
    chunk = st.chunk_path("R3", "BTCUSDT", "2024-01")
    mtime = chunk.stat().st_mtime_ns
    assert st.put_chunk("R3", "BTCUSDT", "2024-01", df, sources=[{"url": "b"}]) == e1  # entry kept as is
    assert chunk.stat().st_mtime_ns == mtime
    changed = df.copy()
    changed.loc[5, "c"] += 0.01
    with pytest.raises(rawstore.ImmutabilityViolation):
        st.put_chunk("R3", "BTCUSDT", "2024-01", changed, sources=[{"url": "a"}])
    assert chunk.stat().st_mtime_ns == mtime and st.entry("R3", "BTCUSDT", "2024-01") == e1
    (r,) = log.records()
    assert r["exception"] == "ImmutabilityViolation" and r["rule"].startswith("§3")
    assert r["request"] == {"kind": "raw_store_write", "table": "R3", "symbol": "BTCUSDT", "month": "2024-01"}
    # an orphan file (written, manifest not yet updated: a crash) is adopted only when identical
    st2 = RawStore(tmp_path / "raw2")
    st2.chunk_path("R3", "BTCUSDT", "2024-01").parent.mkdir(parents=True)
    st2.chunk_path("R3", "BTCUSDT", "2024-01").write_bytes(chunk.read_bytes())
    assert st2.put_chunk("R3", "BTCUSDT", "2024-01", df, sources=[{"url": "a"}])["sha256"] == e1["sha256"]
    st3 = RawStore(tmp_path / "raw3")
    st3.chunk_path("R3", "BTCUSDT", "2024-01").parent.mkdir(parents=True)
    st3.chunk_path("R3", "BTCUSDT", "2024-01").write_bytes(chunk.read_bytes())
    with pytest.raises(rawstore.ImmutabilityViolation):
        st3.put_chunk("R3", "BTCUSDT", "2024-01", changed, sources=[{"url": "a"}])


def test_crash_between_symbol_and_table_manifest_is_refused_then_healed(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    tman = st.table_dir("R3") / "MANIFEST.json"
    stale = tman.read_bytes()
    df = month_of(bars(FEB, 29 * 96, seed=1), FEB, MAR)
    st.put_chunk("R3", "BTCUSDT", "2024-02", df, sources=[])
    tman.write_bytes(stale)  # the table manifest replace never happened
    with pytest.raises(IntegrityBlocker):
        TableLoader(st, "R3").frame("BTCUSDT", JAN, MAR)
    st.put_chunk("R3", "BTCUSDT", "2024-02", df, sources=[])  # resumed ingest: identical content, healed
    assert len(TableLoader(st, "R3").frame("BTCUSDT", JAN, MAR)) == 60 * 96


def test_chunk_validation_fails_closed(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    df = month_of(bars(JAN, 31 * 96), JAN, FEB)
    for bad, why in ((df.iloc[::-1], "sorted"), (pd.concat([df, df.iloc[[0]]]), "sorted"),
                     (month_of(bars(JAN, 40 * 96), JAN, FEB + D).assign(open_ts=lambda x: x.open_ts + D,
                                                                    known_at=lambda x: x.known_at + D),
                      "period"),
                     (df.drop(columns="n_trades"), "missing"),
                     (df.assign(known_at=df.known_at + 1), "known_at")):
        with pytest.raises((IntegrityBlocker, ValueError), match=why):
            st.put_chunk("R3", "BTCUSDT", "2024-01", bad, sources=[])
    for sym, month in (("btc/../x", "2024-01"), ("BTCUSDT", "2024-1"), ("BTCUSDT", "2024-13")):
        with pytest.raises(ValueError):
            st.put_chunk("R3", sym, month, df, sources=[])
    with pytest.raises(ValueError):
        st.put_chunk("R9", "BTCUSDT", "2024-01", df, sources=[])
    assert not st.chunk_path("R3", "BTCUSDT", "2024-01").exists()


def test_tampered_chunk_is_refused_and_logged(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    p = st.chunk_path("R3", "BTCUSDT", "2024-01")
    p.write_bytes(p.read_bytes()[:-3] + b"xyz")
    with pytest.raises(IntegrityBlocker, match="sha256"):
        TableLoader(st, "R3").arrays("BTCUSDT", JAN, FEB)
    (r,) = log.records()
    assert r["rule"].startswith("§3") and r["request"]["path"].endswith("BTCUSDT-2024-01.parquet")
    with pytest.raises(IntegrityBlocker):
        st.verify_chunk("R3", "BTCUSDT", "2024-01")


def test_w_hold_is_neither_stored_nor_loaded_before_the_opening(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    hold = bars(HOLD, 96)
    with pytest.raises(guard.HoldoutBlocked):
        st.put_chunk("R3", "BTCUSDT", "2025-07", hold, sources=[])
    assert not st.chunk_path("R3", "BTCUSDT", "2025-07").exists()
    # a month after W_HOLD outside every seal window is still beyond the pre-opening store bound
    with pytest.raises(guard.HoldoutBlocked):
        st.put_chunk("R3", "BTCUSDT", "2026-09", bars(to_ns("2026-09-01"), 96), sources=[])
    put_months(st, "R3", "BTCUSDT", bars(to_ns("2025-06-01"), 30 * 96), ["2025-06"])
    ld = TableLoader(st, "R3")
    n = len(log.records())
    with pytest.raises(guard.HoldoutBlocked):
        ld.arrays("BTCUSDT", to_ns("2025-06-30"), HOLD + D)  # the requested span reaches W_HOLD
    recs = log.records()[n:]
    assert len(recs) == 1 and recs[0]["request"]["kind"] == "data_span"
    assert len(ld.arrays("BTCUSDT", to_ns("2025-06-30"), HOLD)["open_ts"]) == 96


def test_loaders_need_a_k9_log(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    ld = TableLoader(st, "R3")
    with k9.using(None):
        for call in (lambda: TableLoader(st, "R3"), lambda: ld.arrays("BTCUSDT", JAN, FEB),
                     lambda: st.put_chunk("R3", "BTCUSDT", "2024-02", bars(FEB, 10), sources=[])):
            with pytest.raises(k9.K9NotConfigured):
                call()


def test_unknown_symbol_or_empty_window(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    ld = TableLoader(st, "R3")
    assert ld.symbols() == ("BTCUSDT",)
    none = ld.frame("ETHUSDT", JAN, FEB)
    assert len(none) == 0 and list(none.columns)[-1] == "known_at"
    assert len(ld.arrays("BTCUSDT", MAR, MAR + D)["open_ts"]) == 0
    with pytest.raises(ValueError):
        ld.arrays("BTCUSDT", FEB, JAN)
    assert len(TableLoader(st, "R5").frame("BTCUSDT", JAN, FEB)) == 0  # a table never written is empty


# ---------------------------------------------------------------------------------------------- cache


def test_lru_cache_byte_budget_and_deterministic_eviction():
    c = ChunkCache(100)
    a, b, d = np.zeros(5), np.zeros(5), np.zeros(5)  # 40 bytes each
    c.put(("t", "A", "1"), {"x": a}, 40)
    c.put(("t", "B", "1"), {"x": b}, 40)
    assert c.get(("t", "A", "1")) is not None  # A is now the most recently used
    c.put(("t", "D", "1"), {"x": d}, 40)  # 120 > 100: evict the least recently used (B)
    assert c.keys() == (("t", "A", "1"), ("t", "D", "1"))
    assert c.bytes == 80 and c.stats["evictions"] == 1
    c.put(("t", "E", "1"), {"x": np.zeros(20)}, 160)  # larger than the whole budget: never cached
    assert c.get(("t", "E", "1")) is None and c.bytes == 80
    with pytest.raises(ValueError):
        ChunkCache(0)


def test_window_load_touches_only_overlapping_chunks(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    df = bars(JAN, 6 * 31 * 96)
    months = ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"]
    put_months(st, "R3", "BTCUSDT", df, months)
    cache = ChunkCache(10 * 2**20)
    ld = TableLoader(st, "R3", cache=cache)
    a = ld.arrays("BTCUSDT", FEB - D, FEB + D)  # straddles two chunks
    assert ld.stats["decoded_chunks"] == 2 and len(a["open_ts"]) == 2 * 96
    assert (np.diff(a["open_ts"]) == 15 * MIN).all()
    ld.arrays("BTCUSDT", FEB + D, FEB + 2 * D)
    assert ld.stats["decoded_chunks"] == 2 and cache.stats["hits"] >= 1  # served from the cache
    # a budget of one chunk: deterministic LRU, never above the budget
    one = TableLoader(st, "R3", budget_bytes=31 * 96 * 9 * 8 + 1)
    for m in months:
        lo, _ = rawstore.month_period(m)
        one.arrays("BTCUSDT", lo, lo + D)
        assert one.cache.bytes <= one.cache.budget
    assert one.stats["decoded_chunks"] == 6 and len({k[:4] for k in one.cache.keys()}) == 1


def test_column_projection(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    put_months(st, "R5", "BTCUSDT", bars(JAN, 31 * 1440, step=MIN), ["2024-01"])
    a = TableLoader(st, "R5").arrays("BTCUSDT", JAN, JAN + D, columns=("o", "vol_base"))
    assert set(a) == {"open_ts", "o", "vol_base", "known_at"} and (a["known_at"] == a["open_ts"] + MIN).all()
    with pytest.raises(ValueError):
        TableLoader(st, "R5").arrays("BTCUSDT", JAN, JAN + D, columns=("nope",))


# -------------------------------------------------------------------------------------------- funding


def _funding_frames():
    ts = JAN + np.arange(0, 31 * 3, dtype=np.int64) * 8 * 3600 * 10**9
    rate = np.linspace(1e-4, 2e-4, len(ts))
    arch = pd.DataFrame(dict(calc_time=ts[:-2] // 10**6, funding_interval_hours=8,
                             last_funding_rate=rate[:-2]))
    rest = pd.DataFrame(dict(fundingTime=ts[-5:] // 10**6 + 3, fundingRate=rate[-5:],
                             markPrice=["100.5"] * 5))
    return fund_mod.normalise_archive(arch, "ms"), fund_mod.normalise_rest(rest, "ms")


def test_funding_loader_merges_archive_and_rest(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    a, r = _funding_frames()
    st.put_chunk("R2_archive", "BTCUSDT", "2024-01", a, sources=[])
    st.put_chunk("R2_rest", "BTCUSDT", "2024-01", r, sources=[])
    fl = FundingLoader(st)
    got = fl.frame("BTCUSDT", JAN, FEB)
    pd.testing.assert_frame_equal(got, fund_mod.merge(a, r))
    assert got["interval_h_explicit"].dtype == "Int64" and got["interval_h_explicit"].isna().sum() == 2
    # a window is the full merge restricted to [lo, hi) (archive / REST pairs across the edge are matched)
    lo, hi = JAN + 10 * D, JAN + 20 * D
    full = fund_mod.merge(a, r)
    t = full["funding_ts"].to_numpy()
    want = full[(t >= lo) & (t < hi)].reset_index(drop=True)
    pd.testing.assert_frame_equal(fl.frame("BTCUSDT", lo, hi), want)
    empty = fl.frame("ETHUSDT", JAN, FEB)
    assert len(empty) == 0 and list(empty.columns) == list(fund_mod.OUT_COLUMNS)


# --------------------------------------------------------------------------------- universe sources


@pytest.fixture(scope="module")
def fx_inputs():
    return fx.build_inputs(7, dense=(fx.BOOK,))


SUB = (fx.BTC, "AAAUSDT", "EEEUSDT")


def test_in_memory_source_is_the_window_of_the_inputs(fx_inputs, log):
    src = fx.LedgeredSource({s: fx_inputs[s] for s in SUB})
    full = ResearchUniverse({s: fx_inputs[s] for s in SUB})
    lo, hi = full.data_span()
    same = ResearchUniverse.from_source(src, lo, hi)
    for s in SUB:
        pd.testing.assert_frame_equal(same.inputs[s].bars1m.reset_index(drop=True),
                                      fx_inputs[s].bars1m.reset_index(drop=True))
    p_full = runner.DecisionProvider(full, with_features=True)
    p_same = runner.DecisionProvider(same, with_features=True)
    t = fx.BOOK[0] + D + 5 * MIN
    pd.testing.assert_frame_equal(p_full(t).cands, p_same(t).cands)
    w_lo, w_hi = fx.BOOK[0] - 40 * D, fx.BOOK[1]
    win = ResearchUniverse.from_source(src, w_lo, w_hi)
    for s in SUB:
        b = win.inputs[s].bars15["open_ts"].to_numpy()
        assert b.min() >= w_lo and b.max() < w_hi
        ref = fx_inputs[s].bars15
        assert len(b) == int(((ref.open_ts >= w_lo) & (ref.open_ts < w_hi)).sum())
        f = win.inputs[s].funding["funding_ts"].to_numpy()
        assert f.min() >= w_lo and f.max() < w_hi
    with pytest.raises(ValueError):
        InMemorySource({fx.BTC: fx_inputs[fx.BTC]}).load(fx.BTC, w_hi, w_lo)


def complete(table: str, df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in rawstore.TABLES[table].names:
        if c not in out.columns:
            out[c] = (out["c"] if c in ("o", "h", "l") else np.zeros(len(out), np.int64) if c == "n_trades"
                      else 0.0)
    return out


def test_store_source_round_trips_a_symbol(tmp_path, fx_inputs, log):
    st = RawStore(tmp_path / "raw")
    x = fx_inputs["AAAUSDT"]
    lo, hi = fx.START, fx.END
    months = rawstore.months_between(lo, hi)
    for table, df in (("R3", x.bars15), ("R5", x.bars1m), ("R4_mark", x.r4.mark), ("R4_index", x.r4.index),
                      ("R4_premium", x.r4.premium)):
        df = complete(table, df)  # the fixture's R4 / 1m frames lack some stored columns
        for m in months:
            mlo, mhi = rawstore.month_period(m)
            part = df[(df.open_ts >= mlo) & (df.open_ts < mhi)].reset_index(drop=True)
            if len(part):
                st.put_chunk(table, "AAAUSDT", m, part, sources=[])
    arch = x.funding[x.funding.source == "archive"][["funding_ts", "rate", "interval_h_explicit",
                                                     "mark_at_settle"]]
    for m in months:
        mlo, mhi = rawstore.month_period(m)
        part = arch[(arch.funding_ts >= mlo) & (arch.funding_ts < mhi)].reset_index(drop=True)
        if len(part):
            st.put_chunk("R2_archive", "AAAUSDT", m, part, sources=[])
    meta = rawstore.SymbolMeta(x.insts, x.identity, x.notices, x.terminals, None)
    src = rawstore.StoreSource(st, {"AAAUSDT": meta})
    got = src.load("AAAUSDT", lo, hi)
    for name in ("bars15", "bars1m"):
        ref = getattr(x, name).reset_index(drop=True)
        pd.testing.assert_frame_equal(getattr(got, name)[list(ref.columns)], ref, check_dtype=False)
    pd.testing.assert_series_equal(got.r4.mark["c"], x.r4.mark["c"].reset_index(drop=True))
    want_f = fund_mod.merge(arch.reset_index(drop=True), fund_mod.normalise_rest(
        pd.DataFrame(dict(fundingTime=[], fundingRate=[])), "ms"))
    pd.testing.assert_frame_equal(got.funding, want_f)
    assert got.insts == x.insts and got.notices == x.notices and src.symbols() == ("AAAUSDT",)


# ------------------------------------------------------------------------------------------- budgets


@pytest.mark.v7_scale
def test_representative_scale_memory_and_window_cost(tmp_path, log):
    """3 symbols x 1M 1-minute rows on disk (23 monthly chunks each); window loads keep the traced peak
    and the cache under their bounds, and a window load decodes only the chunks it overlaps."""
    st = RawStore(tmp_path / "raw")
    syms = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    lo0 = to_ns("2020-01-01")
    n_rows = 1_000_000
    months = rawstore.months_between(lo0, lo0 + n_rows * MIN)
    t0 = time.perf_counter()
    for i, s in enumerate(syms):
        for m in months:
            mlo, mhi = rawstore.month_period(m)
            n = int((min(mhi, lo0 + n_rows * MIN) - mlo) // MIN)
            st.put_chunk("R5", s, m, bars(mlo, n, step=MIN, seed=i), sources=[])
    write_s = time.perf_counter() - t0
    total = sum(TableLoader(st, "R5").rows(s) for s in syms)
    assert total == 3 * n_rows
    budget = 32 * 2**20
    try:  # RSS as well when psutil is installed (it is not a project dependency: traced memory otherwise)
        import psutil
        proc = psutil.Process()
        rss0 = proc.memory_info().rss
    except ImportError:
        proc = None
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        ld = TableLoader(st, "R5", budget_bytes=budget)
        g = np.random.default_rng(0)
        for _ in range(60):
            s = syms[int(g.integers(3))]
            lo = lo0 + int(g.integers(0, n_rows - 1440)) * MIN
            a = ld.arrays(s, lo, lo + D)
            assert len(a["open_ts"]) == 1440 and a["open_ts"][0] == lo
            assert ld.cache.bytes <= budget
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert peak < budget + 16 * 2**20  # cache budget + one decoded chunk and the window
    if proc is not None:
        assert proc.memory_info().rss - rss0 < budget + 128 * 2**20
    # O(window): a one-day window decodes at most the two chunks it overlaps, whatever the history
    cold = TableLoader(st, "R5", budget_bytes=budget)
    cold.arrays("AAAUSDT", lo0 + 400 * D, lo0 + 401 * D)
    assert cold.stats["decoded_chunks"] <= 2
    assert write_s < 60


# ------------------------------------------------------------------------------- Task 26a fix round 1


def btc_like_month(seed: int = 1) -> pd.DataFrame:
    """The reviewer's BTC-like 1m month (tick 0.1, clustered volatility / activity, volumes to 3 / 5
    decimals)."""
    g = np.random.default_rng(seed)
    n = 44640
    ts = JAN + np.arange(n, dtype=np.int64) * MIN
    sig = 4e-4 * np.exp(np.convolve(g.normal(0, .5, n), np.ones(60) / 60, "same") * 3)
    c = np.round(np.exp(np.log(42000) + np.cumsum(g.normal(0, 1, n) * sig)), 1)
    o = np.r_[c[0], c[:-1]]
    h = np.round(np.maximum(o, c) + np.abs(g.normal(0, 1, n)) * sig * c, 1)
    lo_ = np.round(np.minimum(o, c) - np.abs(g.normal(0, 1, n)) * sig * c, 1)
    act = np.exp(np.convolve(g.normal(0, 1, n), np.ones(30) / 30, "same") * 4)
    vb = np.round(g.gamma(2, 60, n) * act, 3)
    vq = np.round(vb * (lo_ + (h - lo_) * g.uniform(0, 1, n)), 5)
    return pd.DataFrame(dict(open_ts=ts, o=o, h=h, l=lo_, c=c, vol_base=vb, vol_quote=vq,
                             n_trades=g.poisson(800 * act).astype(np.int64),
                             taker_buy_quote=np.round(vq * g.uniform(.3, .7, n), 5)))


DEC = {"o": 1, "h": 1, "l": 1, "c": 1, "vol_base": 3, "vol_quote": 5, "taker_buy_quote": 5}


def test_scaled_int_delta_layout_is_lossless_and_compact(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    df = btc_like_month()
    e = st.put_chunk("R5", "BTCUSDT", "2024-01", df, sources=[], decimals=DEC)
    assert e["scales"] == DEC
    bpr = e["bytes"] / e["rows"]
    assert bpr < 22, bpr  # about 20 B/row (float64 + zstd: about 34)
    p = st.chunk_path("R5", "BTCUSDT", "2024-01")
    schema = pq.read_schema(p)
    assert json.loads(schema.metadata[rawstore.SCALES_KEY]) == DEC
    assert {str(schema.field(n).type) for n in DEC} == {"int64"}
    got = TableLoader(st, "R5").frame("BTCUSDT", JAN, FEB)
    pd.testing.assert_frame_equal(got.drop(columns="known_at"), df)  # exact float64 values
    t0 = time.perf_counter()
    for _ in range(5):
        TableLoader(st, "R5").arrays("BTCUSDT", JAN, FEB)
    assert (time.perf_counter() - t0) / 5 < 0.5
    # the content identity is the float64 content, whatever the encoding
    assert e["content_sha256"] == rawstore.content_sha256(
        rawstore.TABLES["R5"], {c: df[c].to_numpy() for c in rawstore.TABLES["R5"].names})


def test_lossy_scale_falls_back_to_float64_per_column(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    df = btc_like_month().iloc[:1000].copy()
    df["vol_quote"] = df["vol_quote"] * (1 + 1e-13)  # no longer a short decimal
    e = st.put_chunk("R5", "BTCUSDT", "2024-01", df, sources=[], decimals={**DEC, "c": 0})
    assert e["scales"]["vol_quote"] is None and e["scales"]["c"] is None and e["scales"]["o"] == 1
    got = TableLoader(st, "R5").frame("BTCUSDT", JAN, FEB)
    pd.testing.assert_frame_equal(got.drop(columns="known_at"), df.reset_index(drop=True))
    # without source decimals the smallest exact scale is inferred
    e2 = st.put_chunk("R5", "ETHUSDT", "2024-01", btc_like_month().iloc[:1000], sources=[])
    assert all(k is not None and k <= DEC[n] for n, k in e2["scales"].items())


def test_decode_reads_only_the_requested_columns(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    st.put_chunk("R5", "BTCUSDT", "2024-01", btc_like_month().iloc[:5000], sources=[])
    ld = TableLoader(st, "R5")
    ld.arrays("BTCUSDT", JAN, FEB, columns=("c",))
    assert sorted(k[-1] for k in ld.cache.keys()) == ["c", "open_ts"]
    ld.arrays("BTCUSDT", JAN, FEB, columns=("c", "vol_base"))
    assert sorted(k[-1] for k in ld.cache.keys()) == ["c", "open_ts", "vol_base"]
    assert ld.stats["decoded_chunks"] == 2  # the second read decoded vol_base only


def test_funding_window_ending_at_the_w_hold_start_is_not_refused(tmp_path, log):
    """Review I-1: the +-60 s merge pad must not turn a W_DEV window ending at W_HOLD[0] into a refusal."""
    st = RawStore(tmp_path / "raw")
    ts = to_ns("2025-06-01") + np.arange(90, dtype=np.int64) * 8 * 3600 * 10**9
    a = fund_mod.normalise_archive(pd.DataFrame(dict(calc_time=ts // 10**6, funding_interval_hours=8,
                                                     last_funding_rate=1e-4)), "ms")
    st.put_chunk("R2_archive", "BTCUSDT", "2025-06", a, sources=[])
    f = FundingLoader(st).frame("BTCUSDT", to_ns("2025-06-01"), HOLD)
    assert len(f) == 90 and log.records() == []
    with pytest.raises(guard.HoldoutBlocked):  # a window reaching into W_HOLD is still refused
        FundingLoader(st).frame("BTCUSDT", to_ns("2025-06-01"), HOLD + 1)


def test_unlisted_symbol_manifest_is_refused_not_empty(tmp_path, log):
    """Review I-2: a symbol without its table-manifest entry is an integrity failure, never 0 rows."""
    st = RawStore(tmp_path / "raw")
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    (st.table_dir("R3") / "MANIFEST.json").unlink()
    with pytest.raises(IntegrityBlocker, match="not listed"):
        TableLoader(st, "R3").frame("BTCUSDT", JAN, FEB)
    st.resume_chunk("R3", "BTCUSDT", "2024-01")  # heals
    assert len(TableLoader(st, "R3").frame("BTCUSDT", JAN, FEB)) == 31 * 96


def test_create_once_never_overwrites_and_cleans_up(tmp_path):
    p = tmp_path / "x.parquet"
    rawstore._create_once(p, b"one")
    with pytest.raises(rawstore.ImmutabilityViolation):
        rawstore._create_once(p, b"two")
    assert p.read_bytes() == b"one" and sorted(x.name for x in tmp_path.iterdir()) == ["x.parquet"]


def test_one_writer_per_store(tmp_path, log):
    st = RawStore(tmp_path / "raw")
    with rawstore._writer_lock(st.root), pytest.raises(rawstore.StoreBusy):
        put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])
    put_months(st, "R3", "BTCUSDT", bars(JAN, 31 * 96), ["2024-01"])  # released


def test_negative_zero_is_exact(tmp_path, log):
    """Review round 2, minor A: -0.0 cannot come back from a scaled int; that column stays float64, the
    content sha256 survives the round trip and an orphan of the same content is adopted."""
    st = RawStore(tmp_path / "raw")
    ts = JAN + np.arange(6, dtype=np.int64) * 15 * MIN
    c = np.array([-0.0, -0.00012345, 0.5, 1.25, -3.0, 2.0])
    df = pd.DataFrame(dict(open_ts=ts, o=c, h=c, l=c, c=c))
    e = st.put_chunk("R4_mark", "BTCUSDT", "2024-01", df, sources=[])
    assert e["scales"] == {"o": None, "h": None, "l": None, "c": None}
    t = rawstore.TABLES["R4_mark"]
    got = TableLoader(st, "R4_mark").arrays("BTCUSDT", JAN, FEB)
    assert bool(np.signbit(got["c"][0]))
    assert rawstore.content_sha256(t, {k: got[k] for k in t.names}) == e["content_sha256"]
    plus = df.assign(c=np.abs(c) * np.sign(c) + 0.0)  # +0.0 instead of -0.0: different content
    assert rawstore.content_sha256(t, {k: plus[k].to_numpy() for k in t.names}) != e["content_sha256"]
    st2 = RawStore(tmp_path / "raw2")
    q = st2.chunk_path("R4_mark", "BTCUSDT", "2024-01")
    q.parent.mkdir(parents=True)
    q.write_bytes(st.chunk_path("R4_mark", "BTCUSDT", "2024-01").read_bytes())
    assert st2.put_chunk("R4_mark", "BTCUSDT", "2024-01", df, sources=[])["sha256"] == e["sha256"]
    with pytest.raises(rawstore.ImmutabilityViolation):
        RawStore(tmp_path / "raw2").put_chunk("R4_mark", "BTCUSDT", "2024-01", plus, sources=[])
