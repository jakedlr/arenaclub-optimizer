import json
import logging
import random
import signal
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from types import FrameType
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console, RenderableType
from rich.measure import Measurement
from rich.text import Text

from arenaclub_optimizer.arena_client import (
    ArenaApiError,
    ArenaBlockedError,
    ArenaClient,
)
from arenaclub_optimizer.config import (
    OVERRIDES_FILENAME,
    Config,
    ConfigError,
    Overrides,
    config_dir,
    load_config,
    load_overrides,
    override_config,
    state_dir,
)
from arenaclub_optimizer.fetch import ArenaSource, FetchResult, fetch_card_packs
from arenaclub_optimizer.notify import (
    Notifier,
    NotifyError,
    NtfyNotifier,
    PlainNotification,
    get_or_create_topic,
    subscribe_url,
)
from arenaclub_optimizer.output import (
    note_lines,
    overrides_snippet,
    report_json,
    results_table,
    review_json,
    review_summary,
    review_table,
    source_lines,
    summary_line,
    unavailable_lines,
)
from arenaclub_optimizer.ranking import rank as rank_packs
from arenaclub_optimizer.valuation import (
    ValueSources,
    open_value_sources,
    review_entries,
    value_packs,
)
from arenaclub_optimizer.values import ValueResolution
from arenaclub_optimizer.watch import Clock, Resources, SystemClock, run_watch

app = typer.Typer(help="Rank Arena Club Slab Packs by expected value.")

# Builds the Arena Club client. Tests replace it with a fake.
make_client: Callable[[], AbstractContextManager[ArenaSource]] = ArenaClient
# Opens the value cache, credit budget and value providers. Tests replace it
# with fakes.
make_value_sources: Callable[[], AbstractContextManager[ValueSources]] = (
    open_value_sources
)
# Builds the ntfy notifier for a server and Topic. Tests replace it with one on
# an httpx.MockTransport.
make_notifier: Callable[[str, str], AbstractContextManager[Notifier]] = NtfyNotifier
# Watch Mode's clock (with sleep) and the random source for its jitter. Tests
# replace them with fakes.
watch_clock: Clock = SystemClock()
watch_random: random.Random = random.Random()

TEST_NOTIFICATION = PlainNotification(
    title="arenaclub-optimizer test",
    message="Test notification from arena topic --test. If you can read this, "
    "Watch Mode Alerts will reach you.",
    tags=("white_check_mark",),
)

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
    "watch.interval_minutes": "--interval",
}

# Exit code for bad input or a block by Arena Club; typer uses 2 for usage errors.
EXIT_USAGE = 2

# The Slab Pack filters, shared by `rank` and `review`.
CategoryOption = Annotated[
    list[str] | None,
    typer.Option(
        "--category",
        help="Category to include, e.g. Pokemon; case and accents are ignored. "
        "Repeat for several. Default: config file, else all.",
    ),
]
PriceMinOption = Annotated[
    float | None,
    typer.Option(
        "--price-min",
        help="Lowest list price, in $. Default: config file, else none.",
    ),
]
PriceMaxOption = Annotated[
    float | None,
    typer.Option(
        "--price-max",
        help="Highest list price, in $. Default: config file, else none.",
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print JSON instead of a table.")
]
# The metric filters and k, shared by `rank` and `watch`.
MinEvOption = Annotated[
    float | None,
    typer.Option(
        "--min-ev",
        help="Minimum Buyback EV %, in percent: 5 means 5%. May be negative. "
        "Default: config file, else none.",
    ),
]
MinPProfitOption = Annotated[
    float | None,
    typer.Option(
        "--min-p-profit",
        help="Minimum P(profit), in percent from 0 to 100: 20 means 20%. "
        "Default: config file, else none.",
    ),
]
HighVarianceOption = Annotated[
    bool | None,
    typer.Option(
        "--exclude-high-variance/--include-high-variance",
        help="Drop (or keep) Slab Packs with a High-variance Note. "
        "Default: config file, else keep.",
    ),
]
KOption = Annotated[
    float | None,
    typer.Option(
        "--k",
        help="Assumed AC Value / Market Value ratio. Default: config file, else 1.",
    ),
]


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
    category: CategoryOption = None,
    price_min: PriceMinOption = None,
    price_max: PriceMaxOption = None,
    min_ev: MinEvOption = None,
    min_p_profit: MinPProfitOption = None,
    exclude_high_variance: HighVarianceOption = None,
    top: Annotated[
        int | None,
        typer.Option(
            "--top",
            help="How many Top Results to show. Default: config file, else 10.",
        ),
    ] = None,
    k: KOption = None,
    wide: Annotated[
        bool,
        typer.Option(
            "--wide",
            help="Also show Market EV %, Slab Safe EV $ and Coverage. "
            "The default columns fit 80 columns.",
        ),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Print the Top Results: card Slab Packs ordered by Buyback EV %.

    Market Values come from Manual Overrides, then PokéPrice Tracker (with
    POKEPRICE_API_KEY set), then CardVolume, cached between runs. Every
    option defaults to the config file, config.toml in
    $XDG_CONFIG_HOME/arenaclub-optimizer (else ~/.config/arenaclub-optimizer),
    and to the built-in default where the file doesn't set it. Percentages are
    given in percent (5 = 5%); the config file writes the same settings as
    fractions (0.05).
    """
    errors = Console(stderr=True)
    config, overrides = _settings(
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
        }
    )
    fetched = _fetch(config, errors)
    values = _value(fetched, config, overrides, errors)
    report = rank_packs(fetched.packs, config, values, skipped=fetched.skipped)

    if json_output:
        _echo_json(report_json(report, config))
        typer.echo(summary_line(report), err=True)
        return

    console = Console()
    if report.results:
        _print_untruncated(console, results_table(report, config.k, wide=wide))
    else:
        console.print("No Slab Packs met the criteria.")
    # Text, not str: pack names, notes and reasons come from Arena Club and
    # may hold brackets that rich would otherwise parse as markup.
    notes = note_lines(report)
    if notes:
        console.print("Notes:")
    for note in notes:
        console.print(Text(f"- {note}"))
    for skipped in report.skipped:
        console.print(Text(f"Skipped {skipped.name}: {skipped.reason}"))
    for line in unavailable_lines(values):
        console.print(Text(line))
    sources = source_lines(report)
    if sources:
        console.print("Sources:")
    for citation in sources:
        console.print(Text(f"- {citation}"))
    console.print(Text(summary_line(report)))


@app.command()
def review(
    category: CategoryOption = None,
    price_min: PriceMinOption = None,
    price_max: PriceMaxOption = None,
    json_output: JsonOption = False,
) -> None:
    """Print the Needs-review List: cards in the matching Slab Packs that have
    no Market Value, with why, and an overrides.toml snippet to fill in.

    Values are looked up as `rank` does, so this can spend value credits too.
    The Slab Pack filters are `rank`'s and default to the config file the
    same way; `rank`'s EV and P(profit) filters don't apply, since a pack
    without values has no EV yet.
    """
    errors = Console(stderr=True)
    config, overrides = _settings(
        {
            "filters": {
                "categories": category or None,
                "price_min": price_min,
                "price_max": price_max,
            }
        }
    )
    fetched = _fetch(config, errors)
    values = _value(fetched, config, overrides, errors)
    entries = review_entries(fetched.packs, config.filters, values)

    if json_output:
        document = review_json(
            entries,
            config,
            unavailable=values.unavailable,
            generated_at=datetime.now(UTC),
        )
        _echo_json(document)
        typer.echo(review_summary(entries), err=True)
        return

    console = Console()
    to_review = [entry for entry in entries if not entry.waiting]
    waiting = [entry for entry in entries if entry.waiting]
    if to_review:
        console.print(review_table(to_review))
        path = config_dir() / OVERRIDES_FILENAME
        console.print()
        # soft_wrap: printed as is, so the path and snippet copy cleanly.
        console.print(
            Text(
                f"To value them, paste this into {path}, then fill in each "
                "market_value or delete the entry:"
            ),
            soft_wrap=True,
        )
        console.print()
        console.print(Text(overrides_snippet(to_review).rstrip("\n")), soft_wrap=True)
        console.print()
    else:
        console.print("No cards need review.")
    if waiting:
        console.print(review_table(waiting, title="Waiting"))
        console.print(
            Text(
                "These cards weren't looked up: a value source's budget ran out "
                "or it was unavailable. A later run will try them again, so they "
                "need no override."
            )
        )
        console.print()
    for line in unavailable_lines(values):
        console.print(Text(line))
    console.print(Text(review_summary(entries)))


@app.command()
def topic(
    test: Annotated[
        bool,
        typer.Option(
            "--test",
            help="Also send one test notification, to check your subscription.",
        ),
    ] = False,
) -> None:
    """Print the Topic Watch Mode sends Alerts to, and how to subscribe to it.

    The Topic is `watch.topic` from the config file if set, else a random one
    generated on first use and saved in $XDG_STATE_HOME/arenaclub-optimizer
    (else ~/.local/state/arenaclub-optimizer). Anyone who knows it can read
    your Alerts, so keep it private. The server is `watch.ntfy_server`,
    https://ntfy.sh by default.
    """
    try:
        config = load_config()
        name = get_or_create_topic(config, state_dir())
    except ConfigError as error:
        _fail(f"Error: {error}", EXIT_USAGE)
    except OSError as error:
        _fail(f"Error: could not save the Topic: {error}", 1)

    server = config.watch.ntfy_server
    url = subscribe_url(server, name)
    typer.echo(f"Topic: {name}")
    typer.echo(f"Subscribe URL: {url}")
    typer.echo(
        f"To get Alerts, subscribe to the Topic in the ntfy app (server {server}), "
        "or open the Subscribe URL in a browser."
    )
    if not test:
        return
    try:
        with make_notifier(server, name) as notifier:
            notifier.send(TEST_NOTIFICATION)
    except NotifyError as error:
        _fail(f"Error: the test notification was not sent: {error}", 1)
    typer.echo("Test notification sent.")


@app.command()
def watch(
    category: CategoryOption = None,
    price_min: PriceMinOption = None,
    price_max: PriceMaxOption = None,
    min_ev: MinEvOption = None,
    min_p_profit: MinPProfitOption = None,
    exclude_high_variance: HighVarianceOption = None,
    k: KOption = None,
    interval: Annotated[
        float | None,
        typer.Option(
            "--interval",
            metavar="MINUTES",
            help="Minutes between cycles, at least 5; each wait varies by up "
            "to 10% either way. Default: config file, else 10.",
        ),
    ] = None,
    once: Annotated[
        bool,
        typer.Option(
            "--once",
            help="Run one cycle, then exit: 0 if it went fine, 1 if it failed.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Print a cycle's summary line only when the cycle fails.",
        ),
    ] = False,
) -> None:
    """Run Watch Mode: rank on an interval and send Alerts to your Topic.

    Each cycle runs the same pipeline as `rank` with the same filters, then
    sends an Alert through ntfy when a Slab Pack newly meets the criteria
    or its Buyback EV % rises a step, and one when requests to Arena Club
    start failing and one when they recover. Every pack that meets the
    criteria counts, so there is no --top here. Each cycle prints a summary
    line. A failed cycle backs off: the interval, doubling up to two hours,
    or longer if Arena Club asks with Retry-After. Ctrl+C or SIGTERM stops
    it, saving the Alert state. Prints the Topic and its subscribe URL at
    start; see `arena topic`.
    """
    _configure_logging()
    config, overrides = _settings(
        {
            "k": k,
            "filters": {
                "categories": category or None,
                "price_min": price_min,
                "price_max": price_max,
                "min_ev_pct": _fraction(min_ev),
                "min_p_profit": _fraction(min_p_profit),
                "exclude_high_variance": exclude_high_variance,
            },
            "watch": {"interval_minutes": interval},
        }
    )
    try:
        name = get_or_create_topic(config, state_dir())
    except ConfigError as error:
        _fail(f"Error: {error}", EXIT_USAGE)
    except OSError as error:
        _fail(f"Error: could not save the Topic: {error}", 1)

    server = config.watch.ntfy_server
    typer.echo(f"Topic: {name}")
    typer.echo(f"Subscribe URL: {subscribe_url(server, name)}")
    resources = Resources(
        client=make_client,
        values=make_value_sources,
        notifier=lambda: make_notifier(server, name),
    )
    with _sigterm_as_interrupt():
        last = run_watch(
            config,
            overrides,
            resources,
            clock=watch_clock,
            rng=watch_random,
            max_cycles=1 if once else None,
            quiet=quiet,
            echo=typer.echo,
            topic=name,
        )
    if once and last is not None and not last.ok:
        raise typer.Exit(code=1)


def _settings(changes: Mapping[str, Any]) -> tuple[Config, Overrides]:
    """The config file with the command-line `changes`, and the Manual
    Overrides. Exits 2 on an invalid file or option."""
    try:
        config = override_config(load_config(), changes, names=OPTION_NAMES)
        return config, load_overrides()
    except ConfigError as error:
        _fail(f"Error: {error}", EXIT_USAGE)


def _fetch(config: Config, errors: Console) -> FetchResult:
    """The card Slab Packs matching the config's Category and price filters.
    Exits 2 if Arena Club blocks a request, 1 if it can't be read."""
    filters = config.filters
    try:
        with make_client() as client, _progress(errors) as progress:
            return fetch_card_packs(
                client,
                filters.categories,
                price_min=filters.price_min,
                price_max=filters.price_max,
                progress=progress,
            )
    except ArenaBlockedError as error:
        _fail(
            f"Error: {error}. Stopped at once without retrying; "
            "wait a while before running again.",
            EXIT_USAGE,
        )
    except ArenaApiError as error:
        _fail(f"Error: could not read Arena Club: {error}", 1)


def _value(
    fetched: FetchResult, config: Config, overrides: Overrides, errors: Console
) -> ValueResolution:
    """Resolve the Market Values of every card `rank` would evaluate, in one
    pass, a Slab Pack at a time. Exits 1 if the value cache can't be used."""
    try:
        with make_value_sources() as sources, _spinner(errors, "Valuing cards…"):
            return value_packs(
                fetched.packs,
                config.filters,
                overrides,
                sources,
                coverage_threshold=config.coverage_threshold,
            )
    except (OSError, sqlite3.Error) as error:
        _fail(f"Error: could not use the value cache: {error}", 1)


def _echo_json(document: dict[str, Any]) -> None:
    typer.echo(json.dumps(document, indent=2, allow_nan=False))


def _fraction(percent: float | None) -> float | None:
    return None if percent is None else percent / 100


def _fail(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _print_untruncated(console: Console, renderable: RenderableType) -> None:
    """Print `renderable` no narrower than it can go without truncating, so a
    figure is never cut short: on a terminal too narrow for it, such as
    `--wide` at 80 columns, it runs past the edge instead."""
    unbounded = console.options.update_width(10_000)
    needed = Measurement.get(console, unbounded, renderable).minimum
    width = console.width
    console.width = max(width, needed)
    try:
        console.print(renderable)
    finally:
        console.width = width


@contextmanager
def _progress(console: Console) -> Iterator[Callable[[int, int], None] | None]:
    """A spinner on stderr while Slab Packs are fetched, when it's a terminal."""
    if not console.is_terminal:
        yield None
        return
    with console.status("Fetching Slab Packs…") as status:
        yield lambda done, total: status.update(f"Fetching Slab Packs… {done}/{total}")


def _configure_logging() -> None:
    """Log to stderr with timestamps. The httpx and httpcore loggers are kept
    at WARNING or above: at INFO httpx logs every request URL, and the ntfy
    URL holds the Topic."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.setLevel(max(logger.level, logging.WARNING))


@contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    """While in the block, SIGTERM raises KeyboardInterrupt, so it stops Watch
    Mode as Ctrl+C does. The previous handler is restored afterwards."""

    def interrupt(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # not the main thread: leave SIGTERM alone
        yield
        return
    try:
        yield
    finally:
        signal.signal(
            signal.SIGTERM, previous if previous is not None else signal.SIG_DFL
        )


@contextmanager
def _spinner(console: Console, text: str) -> Iterator[None]:
    """A spinner showing `text` on stderr, when it's a terminal."""
    if not console.is_terminal:
        yield
        return
    with console.status(text):
        yield
