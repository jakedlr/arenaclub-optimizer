import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from importlib.metadata import version
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.text import Text

from arenaclub_optimizer.arena_client import (
    ArenaApiError,
    ArenaBlockedError,
    ArenaClient,
)
from arenaclub_optimizer.config import (
    ConfigError,
    load_config,
    load_overrides,
    override_config,
)
from arenaclub_optimizer.fetch import ArenaSource, fetch_card_packs
from arenaclub_optimizer.output import report_json, results_table, summary_line
from arenaclub_optimizer.ranking import rank as rank_packs

app = typer.Typer(help="Rank Arena Club Slab Packs by expected value.")

# Builds the Arena Club client for `rank`. Tests replace it with a fake.
make_client: Callable[[], AbstractContextManager[ArenaSource]] = ArenaClient

# How CLI-set config keys appear in error messages. Percent options are
# converted to fractions before they are checked, so their messages say so.
OPTION_NAMES = {
    "k": "--k",
    "filters.categories": "--category",
    "filters.price_min": "--price-min",
    "filters.price_max": "--price-max",
    "filters.min_ev_pct": "--min-ev (as a fraction)",
    "filters.min_p_profit": "--min-p-profit (as a fraction)",
    "filters.top": "--top",
}

# Exit code for bad input or a block by Arena Club; typer uses 2 for usage errors.
EXIT_USAGE = 2


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


@app.command()
def rank(
    category: Annotated[
        list[str] | None,
        typer.Option(
            "--category",
            help="Category to include, e.g. Pokemon (case-insensitive). "
            "Repeat for several. Default: all.",
        ),
    ] = None,
    price_min: Annotated[
        float | None, typer.Option("--price-min", help="Lowest list price, in $.")
    ] = None,
    price_max: Annotated[
        float | None, typer.Option("--price-max", help="Highest list price, in $.")
    ] = None,
    min_ev: Annotated[
        float | None,
        typer.Option(
            "--min-ev",
            help="Minimum Buyback EV %, in percent: 5 means 5%. May be negative.",
        ),
    ] = None,
    min_p_profit: Annotated[
        float | None,
        typer.Option(
            "--min-p-profit",
            help="Minimum P(profit), in percent from 0 to 100: 20 means 20%.",
        ),
    ] = None,
    exclude_high_variance: Annotated[
        bool | None,
        typer.Option(
            "--exclude-high-variance/--include-high-variance",
            help="Drop (or keep) Slab Packs with a High-variance Note.",
        ),
    ] = None,
    top: Annotated[
        int | None,
        typer.Option("--top", help="How many Top Results to show. Default: 10."),
    ] = None,
    k: Annotated[
        float | None,
        typer.Option("--k", help="Assumed AC Value / Market Value ratio. Default: 1."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Print the Top Results: card Slab Packs ordered by Buyback EV %.

    Options override the config file. Percentages are given in percent (5 = 5%);
    the config file writes the same settings as fractions (0.05).
    """
    errors = Console(stderr=True)
    try:
        config = override_config(
            load_config(),
            {
                "k": k,
                "filters": {
                    "categories": category or None,
                    "price_min": price_min,
                    "price_max": price_max,
                    "min_ev_pct": _fraction(min_ev),
                    "min_p_profit": _fraction(min_p_profit),
                    "exclude_high_variance": exclude_high_variance,
                    "top": top,
                },
            },
            names=OPTION_NAMES,
        )
        overrides = load_overrides()
    except ConfigError as error:
        _fail(f"Error: {error}", EXIT_USAGE)

    try:
        with make_client() as client, _progress(errors) as progress:
            fetched = fetch_card_packs(
                client, config.filters.categories, progress=progress
            )
    except ArenaBlockedError as error:
        _fail(
            f"Error: {error}. Stopped at once without retrying; "
            "wait a while before running again.",
            EXIT_USAGE,
        )
    except ArenaApiError as error:
        _fail(f"Error: could not read Arena Club: {error}", 1)

    report = rank_packs(fetched.packs, config, overrides, skipped=fetched.skipped)

    if json_output:
        document = report_json(report, config)
        typer.echo(json.dumps(document, indent=2, allow_nan=False))
        typer.echo(summary_line(report), err=True)
        return

    console = Console()
    if report.results:
        console.print(results_table(report, config.k))
    else:
        console.print("No Slab Packs met the criteria.")
    # Text, not str: pack names and reasons come from Arena Club and may hold
    # brackets that rich would otherwise parse as markup.
    for skipped in report.skipped:
        console.print(Text(f"Skipped {skipped.name}: {skipped.reason}"))
    console.print(Text(summary_line(report)))


@app.command()
def watch() -> None:
    """Run Watch Mode."""
    typer.echo("arena watch: not implemented yet", err=True)
    raise typer.Exit(code=1)


def _fraction(percent: float | None) -> float | None:
    return None if percent is None else percent / 100


def _fail(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


@contextmanager
def _progress(console: Console) -> Iterator[Callable[[int, int], None] | None]:
    """A spinner on stderr while Slab Packs are fetched, when it's a terminal."""
    if not console.is_terminal:
        yield None
        return
    with console.status("Fetching Slab Packs…") as status:
        yield lambda done, total: status.update(f"Fetching Slab Packs… {done}/{total}")
