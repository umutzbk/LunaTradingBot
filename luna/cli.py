"""Luna command line interface."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer

if TYPE_CHECKING:
    import pandas as pd

app = typer.Typer(name="luna", help="Luna crypto research and paper-trading platform.", no_args_is_help=True)


@app.command()
def backfill(
    venue: Annotated[Literal["perp", "spot"], typer.Option(help="Venue to backfill")] = "perp",
    assets: Annotated[str | None, typer.Option(help="Comma-separated asset list")] = None,
    start: Annotated[datetime | None, typer.Option(help="Start date")] = None,
) -> None:
    """Backfill historical bars/funding."""
    import pandas as pd

    from luna.config import load_settings
    from luna.data.backfill import backfill_perp, backfill_spot, last_closed_boundary
    from luna.data.store import BarStore
    from luna.http import make_client

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    store = BarStore(settings.var_dir)
    start_ts = None
    if start is not None:
        start_ts = pd.Timestamp(start)
        start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
    end = last_closed_boundary()
    with make_client() as client:
        fn = backfill_perp if venue == "perp" else backfill_spot
        done = fn(client, store, settings, assets, start_ts, end)
    typer.echo(f"backfill {venue}: {len(done)} assets ({', '.join(done)})")


@app.command("data-report")
def data_report() -> None:
    """Report data coverage and quality."""
    from luna.config import load_settings
    from luna.data.report import build_data_report
    from luna.data.store import BarStore

    settings = load_settings()
    out = settings.var_dir / "reports" / "data" / "data_report.md"
    path = build_data_report(BarStore(settings.var_dir), out)
    typer.echo(f"data report: {path}")


def _utc(ts: datetime | None) -> pd.Timestamp | None:
    import pandas as pd

    if ts is None:
        return None
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


@app.command()
def baseline(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
    start: Annotated[datetime | None, typer.Option(help="Start (default history_start + 30d)")] = None,
    end: Annotated[datetime | None, typer.Option(help="End, exclusive (default last common bar)")] = None,
) -> None:
    """Run the naive baselines (combined + each alone) and write an HTML report."""
    import pandas as pd

    from luna.baseline import run_baseline
    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.instruments import load_instruments

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    store = BarStore(settings.var_dir)
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    out_dir = settings.var_dir / "reports" / f"baseline_{account}_{ts}"
    summ = run_baseline(
        settings, store, account, load_instruments(settings, account), _utc(start), _utc(end), out_dir
    )
    for name, m in summ.items():
        if name == "skipped_assets":
            if m:
                typer.echo(f"skipped assets (no instrument): {', '.join(m)}")
        elif "skipped" in m:
            typer.echo(f"{name}: skipped ({m['skipped']})")
        else:
            typer.echo(
                f"{name}: n={m['n_trades']:.0f} expR={m['expectancy_r']} "
                f"CI=[{m['ci_lo']}, {m['ci_hi']}] PF={m['profit_factor']} maxDD={m['max_dd']}"
            )
    typer.echo(f"report: {out_dir / 'report.html'}")


def _echo_verdict(rep, out_dir) -> None:
    g = rep.gonogo
    typer.echo(f"verdict: {g.verdict}")
    typer.echo(g.headline)
    for name, (ok, detail) in g.checks.items():
        typer.echo(f"  {'pass' if ok else 'FAIL'}  {name}: {detail}")
    for k, v in rep.setup.items():
        if k.startswith("runtime"):
            typer.echo(f"{k}: {v}")
    typer.echo(f"report: {out_dir / 'report.html'}")


@app.command()
def walkforward(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
    data_end: Annotated[
        datetime | None, typer.Option(help="Exclusive data end (default: all stored data); smoke runs")
    ] = None,
    assets: Annotated[str | None, typer.Option(help="Comma-separated asset subset (default: all)")] = None,
) -> None:
    """Walk-forward evaluation: main (realistic + edge), sensitivity, ablations, naive baselines and
    GO/NO-GO; writes var/reports/walkforward_<account>_<ts>/{report.html,summary.json}. Never runs
    the holdout."""
    import time

    import pandas as pd

    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.instruments import load_instruments
    from luna.report.walkforward import build_tradelists, load_inputs, walkforward_report, write_report

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    t0 = time.perf_counter()
    settings = load_settings()
    store = BarStore(settings.var_dir)
    end = _utc(data_end)
    subset = [a.strip() for a in assets.split(",")] if assets else None
    inp = load_inputs(
        settings, store, account, load_instruments(settings, account), data_end=end, assets=subset
    )
    t1 = time.perf_counter()
    tl = build_tradelists(inp, settings, account, use_regime=True, data_end=end)
    tl0 = build_tradelists(inp, settings, account, use_regime=False, data_end=end)
    t2 = time.perf_counter()
    rep = walkforward_report(inp, tl, tl0, settings, account, data_end=end)
    rep.setup["runtime trade lists"] = f"{t2 - t1:.1f}s"
    rep.setup["runtime total"] = f"{time.perf_counter() - t0:.1f}s"
    if end is not None:
        rep.setup["data end (option)"] = str(end)
    if subset:
        rep.setup["asset subset (option)"] = ", ".join(subset)
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    out_dir = settings.var_dir / "reports" / f"walkforward_{account}_{ts}"
    write_report(rep, out_dir, f"Luna walk-forward - {account}")
    _echo_verdict(rep, out_dir)


@app.command()
def holdout(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
) -> None:
    """The one-shot holdout (last holdout_months), refused if this account's holdout was already
    run; writes var/reports/holdout_<account>_<ts>/{report.html,summary.json}."""
    import time

    import pandas as pd

    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.engine.walkforward import HoldoutAlreadyRun, HoldoutRegistryCorrupt
    from luna.instruments import load_instruments
    from luna.report.walkforward import build_tradelists, holdout_report, load_inputs, write_report

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    t0 = time.perf_counter()
    settings = load_settings()
    store = BarStore(settings.var_dir)
    inp = load_inputs(settings, store, account, load_instruments(settings, account))
    tl = build_tradelists(inp, settings, account, use_regime=True)
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    out_dir = settings.var_dir / "reports" / f"holdout_{account}_{ts}"
    try:
        rep = holdout_report(inp, tl, settings, account, out_dir=out_dir)
    except (HoldoutAlreadyRun, HoldoutRegistryCorrupt) as e:
        typer.echo(f"holdout refused: {e}", err=True)
        raise typer.Exit(1) from e
    rep.setup["runtime total"] = f"{time.perf_counter() - t0:.1f}s"
    write_report(rep, out_dir, f"Luna holdout - {account}")
    _echo_verdict(rep, out_dir)


@app.command("research-v2")
def research_v2(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
    jobs: Annotated[int, typer.Option(help="Worker processes (each holds one copy of the inputs)")] = 2,
    assets: Annotated[str | None, typer.Option(help="Comma-separated asset subset (default: all)")] = None,
    resume: Annotated[
        Path | None, typer.Option(help="Report directory of an interrupted run to resume")
    ] = None,
) -> None:
    """Research V2 (frozen spec 2026-10-06 + SC-1): every configuration's development walk-forward
    OOS (2024-09 -> 2026-07), the account candidate, robustness and the GO/NO-GO report. Reads no
    bar at or after V2_DATA_END = 2026-07-01 and never touches the final generalization set."""
    import pandas as pd

    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.instruments import load_instruments
    from luna.v2.data import load_v2_inputs
    from luna.v2.report import build_v2_report, write_v2_report
    from luna.v2.runner import run_research

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    subset = [a.strip() for a in assets.split(",")] if assets else None
    inp = load_v2_inputs(
        settings, BarStore(settings.var_dir), account, load_instruments(settings, account), assets=subset
    )
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    out_dir = resume or settings.var_dir / "reports" / f"research_v2_{account}_{ts}"
    res = run_research(
        settings,
        account,
        inp,
        Path(out_dir),
        jobs=jobs,
        resume=resume is not None,
        tl_cache_dir=settings.var_dir / "research_v2",
    )
    rep = build_v2_report(res, inp, settings)
    path = write_v2_report(rep, Path(out_dir))
    typer.echo(
        f"research v2 {account}: {len(res.configs)} configurations, span {res.span[0]:%Y-%m-%d} .. "
        f"{res.span[1]:%Y-%m-%d}"
    )
    typer.echo(f"verdict: {rep.gonogo.verdict} ({rep.gonogo.headline}); candidate: {rep.candidate}")
    typer.echo(
        f"data: end {rep.data.get('data_end')}, last bar read {rep.data.get('last_bar')}, last funding "
        f"{rep.data.get('last_funding')}"
    )
    typer.echo(f"report: {path}")


@app.command("research-v3")
def research_v3(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
    jobs: Annotated[int, typer.Option(help="Worker processes (each holds one copy of the inputs)")] = 2,
    out: Annotated[Path | None, typer.Option(help="Report directory (default: timestamped)")] = None,
    resume: Annotated[bool, typer.Option(help="Resume the interrupted run in --out")] = False,
) -> None:
    """Research V3 (frozen spec 2026-10-09): premium / low-vol / vol-managed families on the V2
    engine, nested train-only selection, family null baselines and the GO/NO-GO report on the
    reused development walk-forward OOS (2024-09 -> 2026-07). Reads no bar at or after 2026-07-01."""
    import json

    import pandas as pd

    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.instruments import load_instruments
    from luna.v3.data import load_v3_inputs
    from luna.v3.report import write_v3_report
    from luna.v3.runner import run_v3

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if resume and out is None:
        raise typer.BadParameter("--resume needs --out (the interrupted run's directory)")
    settings = load_settings()
    other = "spot" if account == "perp" else "perp"
    inp = load_v3_inputs(
        settings,
        BarStore(settings.var_dir),
        account,
        load_instruments(settings, account),
        load_instruments(settings, other),
    )
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    out_dir = Path(out) if out is not None else settings.var_dir / "reports" / f"research_v3_{account}_{ts}"
    res, null = run_v3(settings, account, inp, out_dir, jobs=jobs, resume=resume)
    path = write_v3_report(res, null, inp, settings, out_dir)
    s = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    typer.echo(
        f"research v3 {account}: {len(res.configs)} configurations, span {res.span[0]:%Y-%m-%d} .. "
        f"{res.span[1]:%Y-%m-%d}"
    )
    typer.echo(f"verdict: {s['verdict']} ({s['headline']}); candidate: {s['candidate']}")
    typer.echo(f"null expectancy R: {s['v3']['null_expectancy_r']}; report: {path}")


@app.command()
def paper(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
    db: Annotated[Path | None, typer.Option(help="SQLite file (default var/luna_<account>.db)")] = None,
    once: Annotated[bool, typer.Option(help="Run a single tick + minute check, then exit")] = False,
) -> None:
    """Run the 24/7 paper-trading loop (public data, PaperBroker; never places real orders).
    Exits non-zero if another process holds the account's lock. SIGINT/SIGTERM stop it cleanly."""
    import signal

    import pandas as pd

    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.engine.feeds import KrakenFuturesLiveFeed, KrakenSpotLiveFeed
    from luna.engine.live import LIVE_BUFFER_BARS, EngineLocked, LiveEngine
    from luna.execution.costs import CostModel
    from luna.execution.paper import PaperBroker
    from luna.http import make_client
    from luna.instruments import load_instruments
    from luna.notify.telegram import make_notifier
    from luna.store.db import LEGACY_DB_NAME, Db, db_path, default_url

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs would leak the Telegram token
    settings = load_settings()
    legacy = settings.var_dir / LEGACY_DB_NAME
    if db is None and legacy.is_file() and not db_path(settings.var_dir, account).exists():
        # never start from an empty per-account DB while the old shared one holds the state
        typer.echo(
            f"paper {account}: legacy {legacy} found but no {db_path(settings.var_dir, account)}; "
            f"run `luna migrate-db --account <owner>` first (see docs/RUNBOOK.md)",
            err=True,
        )
        raise typer.Exit(1)
    instruments = load_instruments(settings, account)
    store = BarStore(settings.var_dir)
    venue = "kraken_futures" if account == "perp" else "kraken_spot"
    assets = [a for a in store.assets(venue) if a in instruments]
    client = make_client()
    feed_cls = KrakenFuturesLiveFeed if account == "perp" else KrakenSpotLiveFeed
    feed = feed_cls(assets, store, client, buffer_bars=LIVE_BUFFER_BARS)
    database = Db(f"sqlite:///{db.as_posix()}" if db is not None else default_url(settings.var_dir, account))
    broker = PaperBroker(
        account,
        CostModel(settings.costs[account]),
        feed,
        database,
        account,
        band=settings.accounts[account].max_price_band,
    )
    engine = LiveEngine(
        account,
        settings,
        feed,
        broker,
        database,
        make_notifier(settings, client),
        lambda: pd.Timestamp.now(tz="UTC"),
        "paper",
        instruments=instruments,
        http_client=client,
    )
    try:
        engine.start()
    except EngineLocked as e:
        typer.echo(f"paper {account}: {e}", err=True)
        database.close()
        client.close()
        raise typer.Exit(1) from e
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: engine.stop())
    try:
        if once:
            engine.tick(pd.Timestamp.now(tz="UTC"))
            engine.minute_check(pd.Timestamp.now(tz="UTC"))
            typer.echo(engine.status())
            typer.echo(engine.positions())
        else:
            engine.run_forever()
    finally:
        engine.close()
        database.close()
        client.close()
    if engine.exit_code:  # e.g. feed dead for hours: exit non-zero so Docker restarts the process
        typer.echo(f"paper {account}: exiting with code {engine.exit_code} ({engine.exit_reason})", err=True)
        raise typer.Exit(engine.exit_code)


@app.command()
def health(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to check")] = "perp",
    db: Annotated[Path | None, typer.Option(help="SQLite file (default var/luna_<account>.db)")] = None,
    max_age_min: Annotated[float, typer.Option(help="Max age of the latest equity row, minutes")] = 20,
) -> None:
    """Liveness probe (Docker healthcheck): exit 0 iff the account's latest equity row (written by
    every completed paper tick) is younger than --max-age-min, else 1. Read-only."""
    import sqlite3

    import pandas as pd

    from luna.store.models import ts_from_str

    if db is None:
        from luna.config import load_settings
        from luna.store.db import db_path

        db = db_path(load_settings().var_dir, account)
    if not db.is_file():
        typer.echo(f"health {account}: database missing ({db})")
        raise typer.Exit(1)
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute("SELECT max(ts) FROM equity WHERE account = ?", (account,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        typer.echo(f"health {account}: database error ({e})")
        raise typer.Exit(1) from e
    if row is None or row[0] is None:
        typer.echo(f"health {account}: no equity rows")
        raise typer.Exit(1)
    age = (pd.Timestamp.now(tz="UTC") - ts_from_str(row[0])).total_seconds() / 60
    if age >= max_age_min:
        typer.echo(f"health {account}: stale, last equity {row[0]} ({age:.1f} min old)")
        raise typer.Exit(1)
    typer.echo(f"health {account}: ok, last equity {row[0]} ({age:.1f} min old)")


def _utc_ns(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    from datetime import UTC

    return int((ts if ts.tzinfo else ts.replace(tzinfo=UTC)).timestamp()) * 1_000_000_000


@app.command()
def collect() -> None:
    """Run the V4 forward data collector forever (raw Kraken public snapshots into var/forward;
    spec docs/superpowers/specs/luna-v4-forward-collector-design.md). Exit 1 if another collector
    holds var/forward/collector.lock; exit 3 after 30 min without any ticker success.
    Backup target: LUNA_FORWARD_BACKUP_DIR (unset/empty = no backup, reported as a warning)."""
    import signal

    from luna.config import load_settings
    from luna.forward import collector as coll
    from luna.forward.backup import resolve_backup_dir
    from luna.forward.lock import CollectorLocked
    from luna.http import make_client

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    client = make_client()
    c = coll.Collector(Path(settings.var_dir) / "forward", client,
                       backup_dir=resolve_backup_dir(settings.forward_backup_dir))
    try:
        c.start()
    except (CollectorLocked, coll.CollectorStartError) as e:
        typer.echo(f"collect: {e}", err=True)
        client.close()
        raise typer.Exit(1) from e
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: c.stop())
    try:
        c.run_forever()
    finally:
        c.close()
        client.close()
    if c.exit_code:
        typer.echo(f"collect: exiting with code {c.exit_code} ({c.exit_reason})", err=True)
        raise typer.Exit(c.exit_code)


@app.command("collector-health")
def collector_health(
    strict: Annotated[bool, typer.Option(help="Exit 2 when there is a backup warning")] = False,
) -> None:
    """Collector liveness probe (Docker healthcheck), reads var/forward/status.json only: exit 0 iff
    fut_tickers and spot_ticker succeeded < 3 min ago and fut_funding < 2 h ago, else 1. Backup and
    stale daily reference data are printed as WARNING lines; --strict turns a backup warning into 2."""
    import time

    from luna.config import load_settings
    from luna.forward.status import check_health, read_status

    code, lines = check_health(read_status(Path(load_settings().var_dir) / "forward"), time.time_ns(), strict)
    for line in lines:
        typer.echo(line)
    if code:
        raise typer.Exit(code)


@app.command("forward-dq")
def forward_dq_cmd(
    since: Annotated[datetime | None, typer.Option(help="UTC start (default collector_start_ts)")] = None,
    until: Annotated[datetime | None, typer.Option(help="UTC end, exclusive (default now)")] = None,
) -> None:
    """Data-quality report of the forward data as JSON: paired/clock skew, coverage, gaps, asset
    completeness, quote-sanity counts. Never prints prices, basis, spreads or returns."""
    import json

    from luna.config import load_settings
    from luna.forward.dq import forward_dq

    try:
        rep = forward_dq(Path(load_settings().var_dir) / "forward", _utc_ns(since), _utc_ns(until))
    except ValueError as e:
        typer.echo(f"forward-dq: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(json.dumps(rep, indent=1))


@app.command()
def label(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to label")] = "perp",
    db: Annotated[Path | None, typer.Option(help="SQLite file (default var/luna_<account>.db)")] = None,
) -> None:
    """Counterfactual labels: net R of every matured candidate (selected or rejected) simulated with
    its own stop/exit rules; idempotent."""
    import pandas as pd
    from sqlalchemy import func, select

    from luna.config import load_settings
    from luna.data.store import BarStore
    from luna.execution.costs import CostModel
    from luna.labeling import label_candidates
    from luna.store import models as m
    from luna.store.db import Db, default_url

    settings = load_settings()
    database = Db(f"sqlite:///{db.as_posix()}" if db is not None else default_url(settings.var_dir, account))
    try:
        n = label_candidates(
            database,
            BarStore(settings.var_dir),
            CostModel(settings.costs[account]),
            account,
            pd.Timestamp.now(tz="UTC"),
            band=settings.accounts[account].max_price_band,
        )
        o, c = m.candidate_outcomes, m.candidates
        with database.engine.connect() as conn:
            total, mean_r = conn.execute(
                select(func.count(), func.avg(o.c.outcome_r))
                .select_from(o.join(c, c.c.id == o.c.candidate_id))
                .where(c.c.account == account)
            ).one()
            cands = conn.execute(
                select(func.count()).select_from(c).where(c.c.account == account)
            ).scalar_one()
    finally:
        database.close()
    mean = "n/a" if mean_r is None else f"{mean_r:.3f}"
    typer.echo(f"label {account}: {n} new outcomes; {total}/{cands} candidates labeled, mean R {mean}")


@app.command("migrate-db")
def migrate_db(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Owner of the legacy DB")] = "perp",
) -> None:
    """One-time move of the legacy shared var/luna.db to var/luna_<account>.db (one DB file per
    account). Stop the bot first: refused while an account lock in it is fresh, or if the target
    already exists. Checkpoints the WAL before the rename."""
    from luna.config import load_settings
    from luna.store.db import migrate_legacy_db

    try:
        typer.echo(migrate_legacy_db(load_settings().var_dir, account))
    except RuntimeError as e:
        typer.echo(f"migrate-db refused: {e}", err=True)
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
