"""Luna command line interface. Commands are stubs until their owning task lands."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn

import typer

if TYPE_CHECKING:
    import pandas as pd

app = typer.Typer(name="luna", help="Luna crypto research and paper-trading platform.", no_args_is_help=True)


def _not_implemented(name: str) -> NoReturn:
    typer.echo(f"{name}: not implemented", err=True)
    raise typer.Exit(1)


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
    inp = load_inputs(settings, store, account, load_instruments(settings, account), data_end=end,
                      assets=subset)
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


@app.command()
def paper(
    account: Annotated[Literal["perp", "spot"], typer.Option(help="Account to run")] = "perp",
) -> None:
    """Run the paper-trading loop."""
    _not_implemented("paper")


@app.command()
def label() -> None:
    """Label trades / regimes."""
    _not_implemented("label")


if __name__ == "__main__":
    app()
