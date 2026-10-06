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


@app.command()
def walkforward() -> None:
    """Run walk-forward evaluation."""
    _not_implemented("walkforward")


@app.command()
def holdout() -> None:
    """Run the one-shot holdout evaluation."""
    _not_implemented("holdout")


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
