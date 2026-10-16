"""Guarded ingestion into the raw store (spec §3 "Time units are normalized to ns at ingest", S-1.7 N1-N3,
S-2.2 DEC-33, S-2.6, L12; SC-01 part 1, DK-01; rulings R47 / R48; AMB-36, AMB-37).

Every entry point takes the run's ``fetch.Fetcher`` (the ONLY network path; this module never builds or
touches a transport; production drivers use ``with Fetcher.default(out_dir) as fetcher: ...`` so the pooled
HTTP client is closed when the run ends; the entry points never create or close a fetcher themselves, the
driver that creates it owns it) and runs under the fetcher's K9 log (``<out_dir>/k9``, ``k9.using``: the
previous process log is restored afterwards). One source file in memory at a time:

* ``ingest_bars(fetcher, store, table, symbol, month, *, monthly=True, daily=None)``: R3 / R5 / R4. For R3 /
  R5 the month's daily files come from ONE N2 listing of the month's daily prefix (``daily_days``; DK-01: a
  single page, else refused) unless named explicitly, so the monthly + daily selection is fixed before
  anything is written; R4 is monthly only (ruling R47). Each N1 zip is verified against its published
  ``.CHECKSUM`` by the fetch layer, must hold one non-empty CSV ("empty file" refused), is parsed by
  ``data.klines.parse_csv`` (header auto-detection) and ``klines.normalise`` with the one plausible time
  unit (ms / us / ns; L12), every daily row must lie inside its named day, and the files are united by
  ``klines.union`` (DEC-33: daily fills keys missing from monthly; a conflict is an ``IntegrityBlocker``).
  The decimals of the CSV strings give the stored scales (``RawStore.put_chunk(decimals=)``). The zip bytes
  never touch the disk; the manifest keeps each source's URL, name, size, sha256, published checksum and
  ``recv_ts``.
* ``ingest_funding_archive(fetcher, store, symbol, months=None)``: the monthly ``fundingRate`` files of the
  symbol's whole month run (from the N2 listing unless given), re-partitioned by event time (ruling R48,
  AMB-37): a row is stored in the chunk of its own month;
  rows of one month coming from several files are compared under the S-2.6 key rule (+-60 s): agreeing ->
  one row kept (the one from the month's own file, else from the earliest file), conflicting ->
  ``IntegrityBlocker`` for that month; a row at or after the store bound is dropped in memory and K9-logged
  (never stored). A source file that fails leaves its month and both neighbours as gaps (their spill-in is
  unknown).
* ``ingest_funding_rest``: N3 ``fundingRate`` for one month (``startTime`` = the month start, ``endTime`` =
  the month end - 1 ms, ``limit`` = ``N3_LIMIT``); a full page may be truncated: refused (fail closed); every
  row must carry the requested symbol.
* ``ingest_months(fetcher, store, table, symbol, months)``: the driver. A per-chunk failure of data
  integrity or fetching (``IntegrityBlocker``, ``FetchError``) is not fatal: it is ledgered as a gap in
  ``<out_dir>/INGEST_LEDGER.jsonl`` (append-only, fsynced) and the other chunks continue; a guard refusal
  (W_HOLD / M6 / allowlist / N2 truncation) is raised.
* Ingest completeness (red-team r2 MJ2, ruling R55, AMB-45): every ledger record that is not ``stored`` is
  also appended to the store's own ledger (``RawStore.append_ledger``; ``stored`` records are written there by
  ``put_chunk`` / ``resume_chunk`` themselves), and an ``attempt`` record is appended there before a chunk's
  sources are fetched, so a chunk that fails in any way (an exception that is not ledgered included) stays an
  unresolved key until a later successful ingest stores it; ``RawStore.check_ingest_complete`` refuses the
  data in use while one is unresolved. The fetcher's ledger keeps its format.
* ``list_archive`` / ``daily_days``: N2 single-page listings (truncation refused by the fetch layer, DK-01;
  M6 keys dropped there).

Resumable and idempotent: a chunk completed from exactly the same source URLs whose file verifies is
returned without a fetch (``RawStore.resume_chunk`` heals a stale table-manifest entry); otherwise the content
is rebuilt and ``put_chunk`` accepts only identical content. Before the opening nothing at or after
2025-07-01 is fetched (``netguard``) or stored (the store bound).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v7 import guard, k9
from luna.v7.constants import FROZEN
from luna.v7.data import funding as fund_mod
from luna.v7.data import klines
from luna.v7.data.schema import IntegrityBlocker, IntegrityConflict
from luna.v7.fetch import MAX_RESPONSE_BYTES, Fetched, Fetcher, FetchError, ListingEntry
from luna.v7.netguard import Request
from luna.v7.rawstore import TABLES, RawStore, StoreTable, _symbol, ledger_record, month_period, store_bound

__all__ = ["BVIS", "FAPI_FUNDING", "LEDGER", "N3_LIMIT", "RULE_DROPPED", "S3", "adopt_ledgers", "daily_days",
           "ingest_bars",
           "ingest_funding_archive", "ingest_funding_rest", "ingest_months", "list_archive", "listed_months",
           "n1_url"]

BVIS = "https://data.binance.vision/data/futures/um"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FAPI_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
N3_LIMIT = 1000  # the endpoint's maximum page; a full page fails closed (AMB-36 (f))
LEDGER = "INGEST_LEDGER.jsonl"
RULE_DROPPED = "S-1.7 W_HOLD seal: funding rows at / after the store bound dropped in memory (ruling R48)"
UNITS = ("ms", "us", "ns")
NONFATAL = (IntegrityBlocker, FetchError)  # per-chunk failures the driver ledgers (guard refusals raise)
_RAW_OF = {"o": "open", "h": "high", "l": "low", "c": "close", "vol_base": "volume",
           "vol_quote": "quote_volume", "taker_buy_quote": "taker_buy_quote_volume"}


def _n1_table(table: str) -> StoreTable:
    if table not in TABLES or TABLES[table].dataset is None:
        raise ValueError(f"{table!r} is not an N1 archive table")
    return TABLES[table]


def _day(period: str) -> tuple[int, int]:
    try:
        lo = pd.Timestamp(period, tz="UTC")
    except ValueError:
        raise ValueError(f"invalid day {period!r} (YYYY-MM-DD)") from None
    if lo.strftime("%Y-%m-%d") != period:
        raise ValueError(f"invalid day {period!r} (YYYY-MM-DD)")
    return int(lo.value), int((lo + pd.Timedelta(days=1)).value)


def _shift(month: str, k: int) -> str:
    lo, _ = month_period(month)
    return (pd.Timestamp(lo, unit="ns", tz="UTC") + pd.DateOffset(months=k)).strftime("%Y-%m")


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


def _listing(fetcher: Fetcher, prefix: str, freq: str, auth) -> list[tuple[str, ListingEntry]]:
    got = fetcher.fetch(Request(S3, {"delimiter": "/", "prefix": prefix}), auth=auth)
    assert got.listing is not None
    out: list[tuple[str, ListingEntry]] = []
    for e in got.listing.entries:
        name = e.key.rsplit("/", 1)[-1]
        if not name.endswith(".zip") or guard.file_period(name) is None:
            continue
        period = name[:-4].rsplit("-", 3 if freq == "daily" else 2)
        out.append(("-".join(period[1:]), e))
    return sorted(out, key=lambda x: (x[0], x[1].key))


def list_archive(fetcher: Fetcher, table: str, symbol: str, *, freq: str = "monthly",
                 auth: guard.HoldoutAuthorization | None = None) -> list[tuple[str, ListingEntry]]:
    """(period, entry) of the ``.zip`` keys of one N2 single-page listing, sorted by period (names, sizes and
    checksums only; a truncated listing is refused by the fetch layer)."""
    with k9.using(fetcher.k9):
        if freq not in ("monthly", "daily"):
            raise ValueError("freq must be monthly or daily")
        _symbol(symbol)
        return _listing(fetcher, _prefix(table, symbol, freq), freq, auth)


def daily_days(fetcher: Fetcher, table: str, symbol: str, month: str, *,
               auth: guard.HoldoutAuthorization | None = None) -> list[str]:
    """The days of ``month`` with a daily file (one N2 listing of the month's daily prefix, single page)."""
    with k9.using(fetcher.k9):
        t = _n1_table(table)
        month_period(month)
        stem = f"{symbol}-{t.interval}-" if t.interval else f"{symbol}-{t.dataset}-"
        got = _listing(fetcher, _prefix(table, symbol, "daily") + f"{stem}{month}-", "daily", auth)
        return sorted({p for p, _ in got if p.startswith(month + "-")})


def _source(f: Fetched) -> dict:
    body = f.body or b""
    return {"url": f.url, "name": f.url.rsplit("/", 1)[-1], "kind": f.kind, "bytes": len(body),
            "sha256": f.sha256, "checksum_sha256": f.checksum_sha256, "recv_ts": int(f.recv_ts),
            "md5": hashlib.md5(body, usedforsecurity=False).hexdigest()}  # R56: vs the pinned ETag


def _csv_text(body: bytes, url: str) -> str:
    """The one CSV member of a zip, refused when it has no data row ("empty file")."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise IntegrityBlocker(f"{url}: not a zip ({e})") from None
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) != 1 or not infos[0].filename.lower().endswith(".csv"):
            raise IntegrityBlocker(f"{url}: expected exactly one CSV member, got "
                                   f"{[i.filename for i in infos]}")
        if infos[0].file_size > MAX_RESPONSE_BYTES:
            raise IntegrityBlocker(f"{url}: CSV member larger than {MAX_RESPONSE_BYTES} bytes")
        text = z.read(infos[0]).decode("utf-8")
    lines = [x for x in text.splitlines() if x.strip()]
    first = lines[0].split(",", 1)[0].strip().lstrip("-") if lines else ""
    if not lines or (len(lines) == 1 and not first.isdigit()):
        raise IntegrityBlocker(f"{url}: empty file (no data rows)")
    return text


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


def _decimals(raw: pd.DataFrame, names: Sequence[str]) -> dict[str, int | None]:
    """Per stored column, the largest number of significant decimals in its CSV strings (None: exponent
    notation or not a plain decimal)."""
    out: dict[str, int | None] = {}
    for n in names:
        s = raw[_RAW_OF[n]].astype(str).str.strip()
        if not s.str.fullmatch(r"-?\d+(\.\d*)?").all():
            out[n] = None
            continue
        frac = s.str.partition(".")[2].str.rstrip("0")
        out[n] = int(frac.str.len().max()) if len(s) else 0
    return out


def _merge_decimals(parts: Sequence[Mapping[str, int | None]]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for n in parts[0]:
        ks = [p[n] for p in parts]
        out[n] = None if any(k is None for k in ks) else max(int(k) for k in ks)  # type: ignore[arg-type]
    return out


def _skip(store: RawStore, table: str, symbol: str, month: str, urls: Sequence[str]) -> dict | None:
    """The verified manifest entry when the chunk was completed from exactly these sources (no fetch); a
    stale table-manifest entry of the symbol is healed on the way (``RawStore.resume_chunk``)."""
    e = store.entry(table, symbol, month)
    if e is None or sorted(s.get("url") for s in e["sources"]) != sorted(urls):
        return None
    if not store.chunk_path(table, symbol, month).exists():
        return None  # lost file: rebuilt; put_chunk accepts only the identical content
    return store.resume_chunk(table, symbol, month)


def ingest_bars(fetcher: Fetcher, store: RawStore, table: str, symbol: str, month: str, *,
                monthly: bool = True, daily: Sequence[str] | None = None,
                auth: guard.HoldoutAuthorization | None = None) -> dict:
    """One bar chunk (R3 / R5 / R4): monthly union daily (DEC-33; module docstring). Returns its entry."""
    with k9.using(fetcher.k9):
        t = _n1_table(table)
        if t.ts_col != "open_ts":
            raise ValueError(f"{table} is not a bar table")
        lo, hi = month_period(month)
        if daily and not t.daily_union:
            raise ValueError(f"{table}: daily files are united with monthly files for R3 / R5 only (DEC-33; "
                             "R4 monthly only, ruling R47)")
        if daily is None:
            daily = daily_days(fetcher, table, symbol, month, auth=auth) if t.daily_union else ()
        days = sorted(set(daily))
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
        _attempt(store, table, symbol, month)
        bar_ns = int(t.known_at_ns)  # type: ignore[arg-type]
        sources: list[dict] = []
        frames: list[pd.DataFrame] = []
        decs: list[dict[str, int | None]] = []
        pairs: list[tuple[str, str | None]] = [(urls[0], None)] if monthly else []
        pairs += list(zip(urls[int(monthly):], days, strict=True))
        for url, day in pairs:
            f = fetcher.fetch(Request(url), auth=auth)
            text = _csv_text(f.body or b"", url)
            sources.append(_source(f))
            del f
            raw = klines.parse_csv(text)
            del text
            fr = _one_unit(lambda u, raw=raw: klines.normalise(raw, u, bar_ns), url)  # type: ignore[misc]
            if day is not None:
                dlo, dhi = _day(day)
                ts = fr["open_ts"].to_numpy(np.int64)
                if len(ts) and (ts.min() < dlo or ts.max() >= dhi):
                    raise IntegrityBlocker(f"{url}: rows outside the named day {day}")
            decs.append(_decimals(raw, t.scaled_names))
            frames.append(fr)
        m, extra = (frames[0], frames[1:]) if monthly else (frames[0].iloc[0:0], frames)
        d = pd.concat(extra, ignore_index=True) if extra else m.iloc[0:0]
        df = klines.union(m, d).reset_index(drop=True)
        return store.put_chunk(table, symbol, month, df, sources=sources, decimals=_merge_decimals(decs),
                               auth=auth)


# --------------------------------------------------------------------------------------------- funding


def _event_month(ts: np.ndarray) -> np.ndarray:
    return pd.to_datetime(ts, unit="ns", utc=True).strftime("%Y-%m").to_numpy()


def _dedupe_month(rows: pd.DataFrame, month: str) -> tuple[pd.DataFrame, list[str]]:
    """R48: one month's rows from several files. Rows of different files within the S-2.6 tolerance are one
    settlement: agreeing (rate within ``funding.RATE_ATOL``, same explicit interval) -> the row from the
    month's own file is kept (else the one from the earliest file); conflicting -> IntegrityBlocker. Rows of
    one file are left as they are (the store / merge checks apply). Returns (rows, contributing files)."""
    tol = int(FROZEN.funding.tol_ns)
    r = rows.assign(_other=(rows["_file"] != month)).sort_values(["funding_ts", "_file"],
                                                                  kind="stable").reset_index(drop=True)
    ts = r["funding_ts"].to_numpy(np.int64)
    cluster = np.concatenate([[0], np.cumsum(np.diff(ts) > tol)]) if len(ts) else np.empty(0, np.int64)
    keep = np.ones(len(r), bool)
    for c in np.unique(cluster):
        idx = np.flatnonzero(cluster == c)
        if len(idx) < 2 or r["_file"].iloc[idx].nunique() < 2:
            continue
        g = r.iloc[idx]
        rates = g["rate"].to_numpy(np.float64)
        iv = g["interval_h_explicit"]
        if not (np.abs(rates - rates[0]) <= fund_mod.RATE_ATOL).all() or iv.astype("object").nunique(
                dropna=False) > 1:
            files = sorted(set(g["_file"]))
            raise IntegrityConflict(f"funding {month}: conflicting records from files {files} "
                                   f"at {g['funding_ts'].tolist()} (S-2.6 key rule, ruling R48)")
        order = sorted(idx, key=lambda i: (bool(r["_other"].iat[i]), str(r["_file"].iat[i]), int(ts[i])))
        best = order[0]  # the row from the month's own file, else from the earliest file
        keep[idx] = False
        keep[best] = True
    kept = r[keep]
    return kept.drop(columns=["_other", "_file"]).reset_index(drop=True), sorted(set(kept["_file"]))


def _ledger(fetcher: Fetcher, rec: dict, store: RawStore | None = None) -> dict:
    """Append ``rec`` to the fetcher's ledger; with ``store``, a record that is not ``stored`` also to the
    store's ledger (ruling R55; ``stored`` records come from ``put_chunk`` / ``resume_chunk``)."""
    Path(fetcher.out_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(fetcher.out_dir) / LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if store is not None and rec.get("status") != "stored":
        store.append_ledger(rec)
    return rec


def adopt_ledgers(store: RawStore, paths: Sequence[str | Path]) -> int:
    """Append the records of earlier fetcher ledgers (``<out_dir>/INGEST_LEDGER.jsonl`` of runs from before
    the store had its own ledger, e.g. the C6 dry run's ``w<i>`` ledgers) to the store's ledger, as they are
    (ruling R55; the gate is order-independent, so adopting twice changes nothing). Metadata capture
    records (``kind`` = ``wayback_capture``, the MetaStore's) are skipped; an unreadable line becomes an
    ``unreadable`` record with no key, which blocks every use until governance repairs it (fail closed).
    Returns the number of records appended."""
    n = 0
    for path in paths:
        for i, line in enumerate(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                rec = {"status": "unreadable", "reason": f"{path}:{i}: unreadable legacy ledger line"}
            elif rec.get("kind") == "wayback_capture":
                continue
            store.append_ledger(rec)
            n += 1
    return n


def _attempt(store: RawStore, table: str, symbol: str, month: str) -> None:
    """Ruling R55: before a chunk's sources are fetched, an ``attempt`` record in the store's ledger (resolved
    only when the chunk is stored)."""
    store.append_ledger(ledger_record(table, symbol, month, "attempt", reason="ingest started"))


def _stored(table: str, symbol: str, month: str, e: Mapping) -> dict:
    return {"table": table, "symbol": symbol, "month": month, "status": "stored", "sha256": e["sha256"],
            "rows": e["rows"], "reason": None}


def _gap(table: str, symbol: str, month: str, reason: str, *, conflict: bool = False) -> dict:
    """A gap record; ``conflict`` (new field, ruling R55 round 1): a source conflict, never resolved."""
    return {"table": table, "symbol": symbol, "month": month, "status": "gap", "sha256": None, "rows": None,
            "reason": reason, "conflict": bool(conflict)}


def _err_gap(table: str, symbol: str, month: str, err: BaseException) -> dict:
    return _gap(table, symbol, month, f"{type(err).__name__}: {err}",
                conflict=isinstance(err, IntegrityConflict))


def listed_months(fetcher: Fetcher, table: str, symbol: str, *,
                  auth: guard.HoldoutAuthorization | None = None) -> list[str]:
    """The months with a monthly N1 file of ``table`` / ``symbol`` that the store may hold (one N2 listing;
    period end <= the store bound)."""
    bound = store_bound(auth)
    return [m for m, _ in list_archive(fetcher, table, symbol, auth=auth) if month_period(m)[1] <= bound]


def ingest_funding_archive(fetcher: Fetcher, store: RawStore, symbol: str,
                           months: Sequence[str] | None = None, *,
                           auth: guard.HoldoutAuthorization | None = None) -> list[dict]:
    """The archive ``fundingRate`` files of the symbol's whole month run, re-partitioned by event month
    (ruling R48; module docstring). ``months`` None (the driver's default): the run comes from the N2 listing
    (``listed_months``), so no spill-in from a neighbouring file is missed; an explicit ``months`` must be
    that whole contiguous run (spill from a file outside it is not seen, only ledgered). Returns the ledger
    records (one per month, plus one per spill month outside ``months``), also appended to the ledger."""
    with k9.using(fetcher.k9):
        _symbol(symbol)
        if months is None:
            months = listed_months(fetcher, "R2_archive", symbol, auth=auth)
        months = sorted(set(months))
        for m in months:
            month_period(m)
        table = "R2_archive"
        urls = {m: n1_url(table, symbol, m) for m in months}
        done = [store.resume_chunk(table, symbol, m)
                if store.entry(table, symbol, m) is not None and store.chunk_path(table, symbol, m).exists()
                else None for m in months]
        if months and all(e is not None for e in done):  # every month completed and verified: no fetch
            return [_ledger(fetcher, _stored(table, symbol, m, e)) for m, e in zip(months, done, strict=True)]
        for m in months:
            _attempt(store, table, symbol, m)
        bound = store_bound(auth)
        frames: list[pd.DataFrame] = []
        srcs: dict[str, dict] = {}
        failed: dict[str, str] = {}
        for m in months:
            try:
                f = fetcher.fetch(Request(urls[m]), auth=auth)
                text = _csv_text(f.body or b"", urls[m])
                src = _source(f)
                del f
                df = _one_unit(lambda u, text=text: fund_mod.parse_archive_csv(text, u), urls[m])  # type: ignore
            except NONFATAL as e:
                failed[m] = f"{type(e).__name__}: {e}"
                continue
            late = df["funding_ts"].to_numpy(np.int64) >= bound
            if late.any():
                k9.log_refusal(guard.HoldoutBlocked(f"{urls[m]}: {int(late.sum())} funding rows at / after "
                                                    f"{bound} dropped in memory (never stored)"),
                               request={"kind": "funding_rows_dropped", "url": urls[m],
                                        "rows": int(late.sum())}, rule=RULE_DROPPED)
                df = df[~late]
            srcs[m] = src
            frames.append(df.assign(_file=m))
        gaps: dict[str, str] = {}
        for m, why in failed.items():
            for n in (_shift(m, -1), m, _shift(m, 1)):
                if n in urls:
                    gaps.setdefault(n, f"source file {m} failed ({why})")
        allrows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["funding_ts", "rate", "interval_h_explicit", "mark_at_settle", "_file"])
        ev = _event_month(allrows["funding_ts"].to_numpy(np.int64)) if len(allrows) else np.array([])
        out: list[dict] = []
        for m in months:
            if m in gaps:
                out.append(_ledger(fetcher, _gap(table, symbol, m, gaps[m]), store))
                continue
            try:
                rows, files = _dedupe_month(allrows[ev == m], m)
                e = store.put_chunk(table, symbol, m, rows.sort_values("funding_ts", kind="stable"),
                                    sources=[srcs[x] for x in sorted(set(files) | {m})], auth=auth)
                out.append(_ledger(fetcher, _stored(table, symbol, m, e)))
            except IntegrityBlocker as err:
                out.append(_ledger(fetcher, _err_gap(table, symbol, m, err), store))
        for m in sorted(set(ev) - set(months)):
            out.append(_ledger(fetcher, {**_gap(table, symbol, str(m), f"{int((ev == m).sum())} rows of this "
                                                "month in the files of its neighbours; month not requested"),
                                         "status": "spill_not_requested"}, store))
        return out


def ingest_funding_rest(fetcher: Fetcher, store: RawStore, symbol: str, month: str, *,
                        auth: guard.HoldoutAuthorization | None = None) -> dict:
    """N3 ``fundingRate`` records of one month (S-2.6 REST source; module docstring)."""
    with k9.using(fetcher.k9):
        _symbol(symbol)
        lo, hi = month_period(month)
        params = {"symbol": symbol, "startTime": str(lo // 10**6), "endTime": str(hi // 10**6 - 1),
                  "limit": str(N3_LIMIT)}
        url_key = (f"{FAPI_FUNDING}?symbol={symbol}&startTime={params['startTime']}"
                   f"&endTime={params['endTime']}")
        done = _skip(store, "R2_rest", symbol, month, [url_key])
        if done is not None:
            return done
        _attempt(store, "R2_rest", symbol, month)
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


def ingest_months(fetcher: Fetcher, store: RawStore, table: str, symbol: str,
                  months: Sequence[str] | None = None, *,
                  auth: guard.HoldoutAuthorization | None = None) -> list[dict]:
    """The ingest driver of one (table, symbol) over ``months`` (None: the months of the N2 monthly listing,
    ``listed_months``; required for R2_rest, which has no listing): per-chunk integrity / fetch failures are
    ledgered gaps and the other chunks continue; guard refusals raise. Returns the ledger records."""
    with k9.using(fetcher.k9):
        if table == "R2_archive":
            return ingest_funding_archive(fetcher, store, symbol, months, auth=auth)
        if months is None:
            if table == "R2_rest":
                raise ValueError("R2_rest has no listing: pass the months")
            months = listed_months(fetcher, table, symbol, auth=auth)
        out: list[dict] = []
        for m in sorted(set(months)):
            try:
                if table == "R2_rest":
                    e = ingest_funding_rest(fetcher, store, symbol, m, auth=auth)
                else:
                    e = ingest_bars(fetcher, store, table, symbol, m, auth=auth)
                out.append(_ledger(fetcher, _stored(table, symbol, m, e)))
            except NONFATAL as err:
                out.append(_ledger(fetcher, _err_gap(table, symbol, m, err), store))
        return out
