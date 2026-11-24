from importlib.metadata import version
from typing import Annotated

import typer

app = typer.Typer(help="Rank Arena Club Slab Packs by expected value.")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(version("arenaclub-optimizer"))
        raise typer.Exit()


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Rank Arena Club Slab Packs by expected value."""


def _not_implemented(command: str) -> None:
    typer.echo(f"arena {command}: not implemented yet", err=True)
    raise typer.Exit(code=1)


@app.command()
def rank() -> None:
    """Print the Top Results."""
    _not_implemented("rank")


@app.command()
def watch() -> None:
    """Run Watch Mode."""
    _not_implemented("watch")
