"""Guarded ingestion into the raw store (spec §3 "Time units are normalized to ns at ingest", S-1.7 N1-N3,
S-2.2 DEC-33, S-2.6, L12; SC-01 part 1, DK-01; AMB-36).

Every entry point takes the run's ``fetch.Fetcher`` (the ONLY network path; this module never builds a
transport) and first configures the process K9 log to the fetcher's (``<out_dir>/k9``). One (table, symbol,
month) chunk at a time, one source file in memory at a time:

* ``ingest_bars(fetcher, store, table, symbol, month, *, monthly=True, daily=())``: R3 / R5 / R4. The monthly
  N1 zip and the named daily N1 zips (DK-01: exact URLs) are fetched (each verified against its published
  ``.CHECKSUM`` by the fetch layer), parsed by ``data.klines.parse_csv`` (header auto-detection) and
  ``klines.normalise`` with the one plausible time unit (ms / us / ns; ambiguous or implausible: refused,
  L12), united by ``klines.union`` (DEC-33: daily fills keys missing from monthly; a conflict is an
  ``IntegrityBlocker``; R3 / R5 only, R4 takes no daily files, AMB-36 (e)) and written once by
  ``RawStore.put_chunk``. The zip bytes never touch the disk: they are dropped after the verified parse; the
  manifest keeps each source's URL, name, size, sha256, published checksum and ``recv_ts``.
* ``ingest_funding_archive``: the monthly ``fundingRate`` N1 file (``data.funding.parse_archive_csv``).
* ``ingest_funding_rest``: N3 ``fundingRate`` for one month (``startTime`` = the month start, ``endTime`` =
  the month end - 1 ms, ``limit`` = ``N3_LIMIT``); a full page may be truncated: refused (fail closed); every
  row must carry the requested symbol (``data.funding.normalise_rest``).
* ``list_archive``: the N2 single-page listing of a symbol's monthly (or daily) files (truncation refused by
  the fetch layer, DK-01; M6 keys dropped there).

Resumable and idempotent: a chunk already in the manifest with the same source URLs and a file whose sha256
matches is returned without any fetch; otherwise the content is rebuilt and ``put_chunk`` accepts only
identical content (a lost file is re-created, a different content refused and K9-logged). Before the
opening nothing at or after 2025-07-01 is fetched (``netguard``) or stored (the store bound).
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Sequence

import pandas as pd

from luna.v7 import guard, k9
from luna.v7.data import funding as fund_mod
from luna.v7.data import klines
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.fetch import MAX_RESPONSE_BYTES, Fetched, Fetcher, ListingEntry
from luna.v7.netguard import Request
from luna.v7.rawstore import TABLES, RawStore, StoreTable, _symbol, month_period

__all__ = ["BVIS", "FAPI_FUNDING", "N3_LIMIT", "S3", "ingest_bars", "ingest_funding_archive",
           "ingest_funding_rest", "list_archive", "n1_url"]

BVIS = "https://data.binance.vision/data/futures/um"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FAPI_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
N3_LIMIT = 1000  # the endpoint's maximum page; a full page fails closed (AMB-36 (f))
UNITS = ("ms", "us", "ns")


def _n1_table(table: str) -> StoreTable:
    if table not in TABLES or TABLES[table].dataset is None:
        raise ValueError(f"{table!r} is not an N1 archive table")
    return TABLES[table]


def _day(period: str) -> tuple[int, int]:
    lo = pd.Timestamp(period, tz="UTC")
    if lo.strftime("%Y-%m-%d") != period:
        raise ValueError(f"invalid day {period!r} (YYYY-MM-DD)")
    return int(lo.value), int((lo + pd.Timedelta(days=1)).value)


def n1_url(table: str, symbol: str, period: str) -> str:
    """The data.binance.vision URL of a table's monthly (``YYYY-MM``) or daily (``YYYY-MM-DD``) file."""
    t = _n1_table(table)
    _symbol(symbol)
    if len(period) == 7:
        month_period(period)
        freq = "monthly"
    else:
        _day(period)
        freq = "daily"
    if t.interval is None:
        return f"{BVIS}/{freq}/{t.dataset}/{symbol}/{symbol}-{t.dataset}-{period}.zip"
    return f"{BVIS}/{freq}/{t.dataset}/{symbol}/{t.interval}/{symbol}-{t.interval}-{period}.zip"


def _prefix(table: str, symbol: str, freq: str) -> str:
    t = _n1_table(table)
    tail = f"{symbol}/" if t.interval is None else f"{symbol}/{t.interval}/"
    return f"data/futures/um/{freq}/{t.dataset}/{tail}"


def list_archive(fetcher: Fetcher, table: str, symbol: str, *, freq: str = "monthly",
                 auth: guard.HoldoutAuthorization | None = None) -> list[tuple[str, ListingEntry]]:
    """(period, entry) of the ``.zip`` keys of one N2 single-page listing, sorted by period (names, sizes and
    checksums only; a truncated listing is refused by the fetch layer)."""
    k9.configure(fetcher.k9)
    if freq not in ("monthly", "daily"):
        raise ValueError("freq must be monthly or daily")
    _symbol(symbol)
    got = fetcher.fetch(Request(S3, {"delimiter": "/", "prefix": _prefix(table, symbol, freq)}), auth=auth)
    assert got.listing is not None
    out: list[tuple[str, ListingEntry]] = []
    for e in got.listing.entries:
        name = e.key.rsplit("/", 1)[-1]
        if not name.endswith(".zip") or guard.file_period(name) is None:
            continue
        period = name[:-4].rsplit("-", 3 if freq == "daily" else 2)
        out.append(("-".join(period[1:]), e))
    return sorted(out, key=lambda x: (x[0], x[1].key))


def _source(f: Fetched) -> dict:
    return {"url": f.url, "name": f.url.rsplit("/", 1)[-1], "kind": f.kind, "bytes": len(f.body or b""),
            "sha256": f.sha256, "checksum_sha256": f.checksum_sha256, "recv_ts": int(f.recv_ts)}


def _csv_text(body: bytes, url: str) -> str:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) != 1 or not infos[0].filename.lower().endswith(".csv"):
            raise IntegrityBlocker(f"{url}: expected exactly one CSV member, got "
                                   f"{[i.filename for i in infos]}")
        if infos[0].file_size > MAX_RESPONSE_BYTES:
            raise IntegrityBlocker(f"{url}: CSV member larger than {MAX_RESPONSE_BYTES} bytes")
        return z.read(infos[0]).decode("utf-8")


def _one_unit(parse: Callable[[str], pd.DataFrame], what: str) -> pd.DataFrame:
    """L12: the frame parsed with the ONE time unit whose values are plausible; else refused."""
    ok: list[tuple[str, pd.DataFrame]] = []
    errs: list[str] = []
    for u in UNITS:
        try:
            ok.append((u, parse(u)))
        except ValueError as e:
            errs.append(f"{u}: {e}")
    if len(ok) != 1:
        raise IntegrityBlocker(f"{what}: time unit not identifiable (plausible: {[u for u, _ in ok]}; "
                               f"{'; '.join(errs)})")
    return ok[0][1]


def _skip(store: RawStore, table: str, symbol: str, month: str, urls: Sequence[str]) -> dict | None:
    """The verified manifest entry when the chunk was completed from exactly these sources (no fetch)."""
    e = store.entry(table, symbol, month)
    if e is None or sorted(s.get("url") for s in e["sources"]) != sorted(urls):
        return None
    if not store.chunk_path(table, symbol, month).exists():
        return None  # lost file: rebuilt; put_chunk accepts only the identical content
    return store.verify_chunk(table, symbol, month)


def ingest_bars(fetcher: Fetcher, store: RawStore, table: str, symbol: str, month: str, *,
                monthly: bool = True, daily: Sequence[str] = (),
                auth: guard.HoldoutAuthorization | None = None) -> dict:
    """One bar chunk (R3 / R5 / R4): monthly union daily (DEC-33; module docstring). Returns its entry."""
    k9.configure(fetcher.k9)
    t = _n1_table(table)
    if t.ts_col != "open_ts":
        raise ValueError(f"{table} is not a bar table")
    lo, hi = month_period(month)
    days = sorted(set(daily))
    if days and not t.daily_union:
        raise ValueError(f"{table}: daily files are united with monthly files for R3 / R5 only (DEC-33)")
    for d in days:
        dlo, _ = _day(d)
        if not lo <= dlo < hi:
            raise ValueError(f"daily file {d} is outside {month}")
    if not monthly and not days:
        raise ValueError("no source file: monthly=False and no daily file")
    urls = ([n1_url(table, symbol, month)] if monthly else []) + [n1_url(table, symbol, d) for d in days]
    done = _skip(store, table, symbol, month, urls)
    if done is not None:
        return done
    bar_ns = int(t.known_at_ns)  # type: ignore[arg-type]
    sources: list[dict] = []
    frames: list[pd.DataFrame] = []
    for url in urls:
        f = fetcher.fetch(Request(url), auth=auth)
        text = _csv_text(f.body or b"", url)
        sources.append(_source(f))
        del f
        raw = klines.parse_csv(text)
        del text
        frames.append(_one_unit(lambda u, raw=raw: klines.normalise(raw, u, bar_ns), url))  # type: ignore[misc]
    m, extra = (frames[0], frames[1:]) if monthly else (frames[0].iloc[0:0], frames)
    d = pd.concat(extra, ignore_index=True) if extra else m.iloc[0:0]
    df = klines.union(m, d).reset_index(drop=True)
    return store.put_chunk(table, symbol, month, df, sources=sources, auth=auth)


def ingest_funding_archive(fetcher: Fetcher, store: RawStore, symbol: str, month: str, *,
                           auth: guard.HoldoutAuthorization | None = None) -> dict:
    """The monthly archive ``fundingRate`` file of ``symbol`` (S-2.6 archive source)."""
    k9.configure(fetcher.k9)
    month_period(month)
    url = n1_url("R2_archive", symbol, month)
    done = _skip(store, "R2_archive", symbol, month, [url])
    if done is not None:
        return done
    f = fetcher.fetch(Request(url), auth=auth)
    text = _csv_text(f.body or b"", url)
    src = _source(f)
    del f
    df = _one_unit(lambda u: fund_mod.parse_archive_csv(text, u), url)  # type: ignore[arg-type]
    df = df.sort_values("funding_ts", kind="stable").reset_index(drop=True)
    return store.put_chunk("R2_archive", symbol, month, df, sources=[src], auth=auth)


def ingest_funding_rest(fetcher: Fetcher, store: RawStore, symbol: str, month: str, *,
                        auth: guard.HoldoutAuthorization | None = None) -> dict:
    """N3 ``fundingRate`` records of one month (S-2.6 REST source; module docstring)."""
    k9.configure(fetcher.k9)
    _symbol(symbol)
    lo, hi = month_period(month)
    params = {"symbol": symbol, "startTime": str(lo // 10**6), "endTime": str(hi // 10**6 - 1),
              "limit": str(N3_LIMIT)}
    url_key = f"{FAPI_FUNDING}?symbol={symbol}&startTime={params['startTime']}&endTime={params['endTime']}"
    done = _skip(store, "R2_rest", symbol, month, [url_key])
    if done is not None:
        return done
    f = fetcher.fetch(Request(FAPI_FUNDING, params), auth=auth)
    try:
        rows = json.loads((f.body or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise IntegrityBlocker(f"N3 {symbol} {month}: response is not JSON ({e})") from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise IntegrityBlocker(f"N3 {symbol} {month}: response is not a list of records")
    if len(rows) >= N3_LIMIT:
        raise IntegrityBlocker(f"N3 {symbol} {month}: {len(rows)} records = the page limit {N3_LIMIT}; "
                               "may be truncated (fails closed)")
    if any(r.get("symbol") != symbol for r in rows):
        raise IntegrityBlocker(f"N3 {symbol} {month}: a record for another symbol")
    src = {**_source(f), "url": url_key, "params": dict(f.params)}
    raw = pd.DataFrame(rows, columns=["fundingTime", "fundingRate", "markPrice"]) if rows else \
        pd.DataFrame({"fundingTime": pd.Series(dtype="int64"), "fundingRate": pd.Series(dtype="float64")})
    if rows and raw[["fundingTime", "fundingRate"]].isna().any().any():
        raise IntegrityBlocker(f"N3 {symbol} {month}: a record without fundingTime / fundingRate")
    df = _one_unit(lambda u: fund_mod.normalise_rest(raw, u), f"N3 {symbol} {month}") if rows else \
        fund_mod.normalise_rest(raw, "ms")  # type: ignore[arg-type]
    df = df.sort_values("funding_ts", kind="stable").reset_index(drop=True)
    return store.put_chunk("R2_rest", symbol, month, df, sources=[src], auth=auth)
