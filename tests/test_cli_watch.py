"""`arena watch`: options, the startup banner, one cycle end to end, signals
and logging. The loop itself is covered in test_watch."""

import json
import logging
import os
import re
import signal
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fake_arena import sample_arena
from typer.testing import CliRunner

from arenaclub_optimizer import cli
from arenaclub_optimizer.alerts import STATE_FILENAME
from arenaclub_optimizer.arena_client import ArenaApiError, ArenaBlockedError
from arenaclub_optimizer.cli import app
from arenaclub_optimizer.notify import NtfyNotifier
from arenaclub_optimizer.valuation import ValueSources
from arenaclub_optimizer.values import CreditBudget, ValueCache
from arenaclub_optimizer.watch import SystemClock

runner = CliRunner()

TOPIC = "AbCdEfGhIjKlMnOpQrStUv_-"
T0 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)

OVERRIDES = """\
["11111111"]
market_value = 20000

[card_gp_base]
market_value = 60

[card_sp_1]
market_value = 60

[card_gf_1]
market_value = 120
"""

SUMMARY = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d UTC\] ")
# rich colours help text where it detects CI, e.g. with GITHUB_ACTIONS set.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class FakeClock:
    """Time moves only when `sleep` is called; `on_sleep` then runs."""

    def __init__(self) -> None:
        self.wall = T0
        self.mono = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)
        if self.on_sleep is not None:
            self.on_sleep()


class MidRandom:
    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


class Ntfy:
    """`make_notifier` on an httpx.MockTransport that records each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.built: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})

    def __call__(self, server: str, topic: str) -> NtfyNotifier:
        self.built.append((server, topic))
        return NtfyNotifier(server, topic, transport=httpx.MockTransport(self.handle))

    def titles(self) -> list[str]:
        return [r.headers["Title"] for r in self.requests]


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    directory = tmp_path / "xdg-config" / "arenaclub-optimizer"
    directory.mkdir(parents=True)
    (directory / "overrides.toml").write_text(OVERRIDES, encoding="utf-8")
    (directory / "config.toml").write_text(
        f'[watch]\ntopic = "{TOPIC}"\n', encoding="utf-8"
    )
    return directory


@pytest.fixture
def state_dir(config_dir) -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "arenaclub-optimizer"


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(cli, "watch_clock", fake)
    monkeypatch.setattr(cli, "watch_random", MidRandom())
    return fake


@pytest.fixture
def ntfy(monkeypatch) -> Ntfy:
    fake = Ntfy()
    monkeypatch.setattr(cli, "make_notifier", fake)
    return fake


class Sources:
    """A fresh value cache handle per cycle, counted."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.opened = 0

    @contextmanager
    def __call__(self):
        self.opened += 1
        with ValueCache(self.directory) as cache:
            yield ValueSources((), cache, CreditBudget(cache, clock=lambda: T0))


@pytest.fixture
def world(config_dir, state_dir, clock, ntfy, tmp_path, monkeypatch):
    arena = sample_arena()
    clients = []

    def make_client():
        clients.append(arena)
        return arena

    sources = Sources(tmp_path / "values")
    monkeypatch.setattr(cli, "make_client", make_client)
    monkeypatch.setattr(cli, "make_value_sources", sources)
    return {"arena": arena, "clients": clients, "sources": sources}


@pytest.fixture
def http_loggers():
    """Put the httpx and httpcore loggers back as they were after the test."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    yield loggers
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def watch(*args: str):
    return runner.invoke(app, ["watch", *args])


def stop_after(clock: FakeClock, sleeps: int, action=None) -> None:
    """Interrupt the loop on its `sleeps`-th sleep, by `action` (default:
    raise KeyboardInterrupt, as Ctrl+C does)."""

    def on_sleep():
        if len(clock.sleeps) >= sleeps:
            if action is None:
                raise KeyboardInterrupt
            action()

    clock.on_sleep = on_sleep


# --- the defaults ------------------------------------------------------------------


def test_the_default_clock_and_random_are_the_real_ones():
    assert isinstance(cli.watch_clock, SystemClock)
    assert callable(cli.watch_random.uniform)


# --- --once ----------------------------------------------------------------------


def test_once_runs_one_cycle_prints_the_banner_and_summary_and_exits_0(
    world, ntfy, clock, state_dir
):
    result = watch("--once")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"Topic: {TOPIC}"
    assert lines[1] == f"Subscribe URL: https://ntfy.sh/{TOPIC}"
    assert SUMMARY.match(lines[-1])
    assert lines[-1].endswith("2 ranked, 1 unrankable, 0 skipped; 2 alerts sent")
    assert ntfy.titles() == [
        "New Top Result: Gold Pokemon",
        "New Top Result: Gold Football",
    ]
    assert ntfy.built == [("https://ntfy.sh", TOPIC)]
    assert clock.sleeps == []
    assert len(world["clients"]) == 1
    data = json.loads((state_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    assert set(data["packs"]) == {"gold-pokemon", "gold-football"}


def test_once_applies_the_filter_options(world, ntfy):
    result = watch("--once", "--category", "football", "--price-max", "150")

    assert result.exit_code == 0, result.output
    assert ntfy.titles() == ["New Top Result: Gold Football"]


@pytest.mark.parametrize(
    "args",
    [
        ["--min-ev", "10"],
        ["--min-p-profit", "1"],
        ["--exclude-high-variance"],
        # k 0.5: Gold Pokemon about -28%, Gold Football -46%.
        ["--k", "0.5", "--min-ev", "-30"],
    ],
)
def test_the_metric_options_drop_gold_pokemon_or_football(world, ntfy, args):
    result = watch("--once", *args)

    assert result.exit_code == 0, result.output
    assert len(ntfy.titles()) == 1


def test_a_blocked_cycle_with_once_exits_2_like_rank(world, ntfy):
    world["arena"].errors[("levels", "")] = ArenaBlockedError(429, 30.0)

    result = watch("--once")

    assert result.exit_code == 2
    assert ntfy.titles() == ["Arena Club is blocking requests"]
    assert "HTTP 429" in result.stdout.splitlines()[-1]


def test_another_failed_cycle_with_once_exits_1(world, ntfy):
    world["arena"].errors[("levels", "")] = ArenaApiError("HTTP 503", 503)

    result = watch("--once")

    assert result.exit_code == 1
    assert ntfy.titles() == ["Arena Club requests are failing"]


def test_a_second_once_run_sends_nothing_new(world, ntfy):
    watch("--once")
    second = watch("--once")

    assert second.exit_code == 0
    assert len(ntfy.requests) == 2
    assert second.stdout.splitlines()[-1].endswith("; 0 alerts sent")


# --- options ----------------------------------------------------------------------


@pytest.mark.parametrize("interval", ["4", "0", "-5"])
def test_an_interval_below_five_minutes_is_rejected(world, interval):
    result = watch("--once", "--interval", interval)

    assert result.exit_code == 2
    assert "--interval" in result.stderr
    assert "at least 5" in result.stderr


def test_the_interval_option_sets_the_wait(world, clock):
    stop_after(clock, 1)

    result = watch("--interval", "6")

    assert result.exit_code == 0, result.output
    assert clock.sleeps == [360.0]


def test_the_interval_defaults_to_the_config_file(world, clock, config_dir):
    (config_dir / "config.toml").write_text(
        f'[watch]\ntopic = "{TOPIC}"\ninterval_minutes = 8\n', encoding="utf-8"
    )
    stop_after(clock, 1)

    result = watch()

    assert result.exit_code == 0, result.output
    assert clock.sleeps == [480.0]


def test_the_interval_defaults_to_ten_minutes(world, clock, config_dir):
    (config_dir / "config.toml").unlink()
    stop_after(clock, 1)

    result = watch()

    assert result.exit_code == 0, result.output
    assert clock.sleeps == [600.0]


def test_an_invalid_config_exits_2(world, config_dir):
    (config_dir / "config.toml").write_text("k = -1\n", encoding="utf-8")

    result = watch("--once")

    assert result.exit_code == 2
    assert "k" in result.stderr


def test_quiet_prints_the_banner_but_no_ok_summary(world):
    result = watch("--once", "--quiet")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"Topic: {TOPIC}",
        f"Subscribe URL: https://ntfy.sh/{TOPIC}",
    ]


def test_a_generated_topic_is_saved_and_printed(world, config_dir, state_dir):
    (config_dir / "config.toml").unlink()

    result = watch("--once")

    assert result.exit_code == 0, result.output
    topic = (state_dir / "topic").read_text(encoding="utf-8").strip()
    assert result.stdout.splitlines()[0] == f"Topic: {topic}"


def test_watch_help_lists_the_options():
    result = runner.invoke(app, ["watch", "--help"], env={"COLUMNS": "200"})
    text = ANSI_ESCAPE.sub("", result.stdout)

    for option in (
        "--category",
        "--price-min",
        "--price-max",
        "--min-ev",
        "--min-p-profit",
        "--exclude-high-variance",
        "--k",
        "--interval",
        "--once",
        "--quiet",
    ):
        assert option in text


def test_watch_has_no_top_option(config_dir):
    # Alerts cover every Slab Pack that meets the criteria, and watch
    # prints no table, so a top-N cut would do nothing. The other options
    # come first, so the error is about --top, not an unknown --once.
    result = watch("--once", "--category", "pokemon", "--top", "3")

    assert result.exit_code == 2
    assert "No such option: --top" in ANSI_ESCAPE.sub("", result.stderr)


# --- fresh resources ---------------------------------------------------------------


def test_every_cycle_builds_a_new_client_and_value_sources(world, clock):
    stop_after(clock, 3)

    result = watch()

    assert result.exit_code == 0, result.output
    assert len(world["clients"]) == 3
    assert world["sources"].opened == 3


# --- signals ----------------------------------------------------------------------


def test_ctrl_c_saves_the_state_and_exits_0(world, clock, state_dir):
    stop_after(clock, 1)

    result = watch()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "Stopped; Alert state saved."
    data = json.loads((state_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    assert set(data["packs"]) == {"gold-pokemon", "gold-football"}


def test_sigterm_stops_the_loop_like_ctrl_c(world, clock, state_dir):
    before = signal.getsignal(signal.SIGTERM)
    stop_after(clock, 1, lambda: signal.raise_signal(signal.SIGTERM))

    result = watch()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "Stopped; Alert state saved."
    assert (state_dir / STATE_FILENAME).exists()
    # The previous handler is back once watch returns.
    assert signal.getsignal(signal.SIGTERM) is before


def test_sigterm_during_the_shutdown_save_is_ignored(world, clock, monkeypatch):
    import arenaclub_optimizer.watch as watch_module

    real_save = watch_module.save_state
    calls = []

    def save_then_sigterm(state, directory=None):
        calls.append(state)
        if len(calls) == 2:  # the save on shutdown, as a second pkill lands
            signal.raise_signal(signal.SIGTERM)
        real_save(state, directory)

    monkeypatch.setattr(watch_module, "save_state", save_then_sigterm)
    stop_after(clock, 1)

    result = watch()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "Stopped; Alert state saved."


def test_ctrl_c_mid_cycle_exits_0(world, clock):
    world["arena"].errors[("pack", "gold-football")] = KeyboardInterrupt()

    result = watch()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "Stopped; Alert state saved."


@pytest.fixture
def los_angeles(monkeypatch):
    """Local time 8 hours behind UTC in November."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_log_timestamps_are_utc_like_the_summary_lines(los_angeles):
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "msg", (), None)
    record.created = T0.timestamp()

    line = cli.log_formatter().format(record)

    assert line.startswith("2026-11-26 10:00:00 UTC WARNING")


# --- logging ----------------------------------------------------------------------


def test_http_request_logs_never_show_the_topic(world, ntfy, caplog, http_loggers):
    # As if something had turned HTTP request logging on: httpx logs every
    # request URL at INFO, and the Topic is part of the ntfy URL.
    for logger in http_loggers:
        logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    result = watch("--once")

    assert result.exit_code == 0, result.output
    assert len(ntfy.requests) == 2
    for logger in http_loggers:
        assert logger.getEffectiveLevel() >= logging.WARNING
    assert TOPIC not in caplog.text


def test_watch_keeps_a_stricter_http_log_level(world, http_loggers):
    http_loggers[0].setLevel(logging.ERROR)

    watch("--once")

    assert http_loggers[0].level == logging.ERROR
