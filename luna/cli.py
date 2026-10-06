"""Luna command line interface. Commands are stubs until their owning task lands."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, NoReturn

import typer

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
    _not_implemented("backfill")


@app.command("data-report")
def data_report() -> None:
    """Report data coverage and quality."""
    _not_implemented("data-report")


@app.command()
def baseline() -> None:
    """Run baseline strategies."""
    _not_implemented("baseline")


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
