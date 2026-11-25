"""Watch Mode's loop: cycles, scheduling with jitter, back-off, Alerts and
state. Everything runs on fakes: a fake clock whose sleep only advances
time, a fixed random source, an in-memory Arena Club and a recording
notifier."""

import json
import logging
import random
import signal
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_arena import sample_arena
from fake_values import FakeProvider

from arenaclub_optimizer.alerts import (
    STATE_FILENAME,
    AlertedPack,
    AlertKind,
    AlertState,
    HealthKind,
    load_state,
    save_state,
)
from arenaclub_optimizer.arena_client import ArenaApiError, ArenaBlockedError
from arenaclub_optimizer.config import Config, Override, Overrides, Watch
from arenaclub_optimizer.notify import NotifyError
from arenaclub_optimizer.valuation import ValueSources
from arenaclub_optimizer.values import CreditBudget, ValueCache
from arenaclub_optimizer.watch import (
    JITTER,
    MAX_BACKOFF_SECONDS,
    Backoff,
    Resources,
    SystemClock,
    next_delay,
    run_watch,
)

T0 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
INTERVAL = 600.0  # the default 10 minutes, in seconds
TOPIC = "AbCdEfGhIjKlMnOpQrStUv_-"

# Gold Pokemon: Buyback EV % about +43.7%, Gold Football +8%; Silver Pokemon
# stays below Value Coverage. The same values as test_cli's overrides file.
OVERRIDES = Overrides(
    by_cert={"11111111": Override(20000)},
    by_card_id={
        "card_gp_base": Override(60),
        "card_sp_1": Override(60),
        "card_gf_1": Override(120),
    },
)


# --- fakes ----------------------------------------------------------------------


class FakeClock:
    """Wall and monotonic time that only move when told to: `sleep` records
    the delay and advances both. `on_sleep(n)` runs after the n-th sleep,
    so a test can change the world between cycles."""

    def __init__(self, start: datetime = T0) -> None:
        self.wall = start
        self.mono = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


class FixedRandom:
    """`uniform(a, b)` always returns the point `u` of the way from a to b."""

    def __init__(self, u: float = 0.5) -> None:
        self.u = u
        self.calls: list[tuple[float, float]] = []

    def uniform(self, a: float, b: float) -> float:
        self.calls.append((a, b))
        return a + (b - a) * self.u


class Notifier:
    """Records every notification; raises NotifyError while `failing`."""

    def __init__(self) -> None:
        self.sent: list = []
        self.attempts: list = []
        self.failing = False
        self.built = 0
        self.closed = 0

    @contextmanager
    def __call__(self):
        self.built += 1
        try:
            yield self
        finally:
            self.closed += 1

    def send(self, notification) -> None:
        self.attempts.append(notification)
        if self.failing:
            raise NotifyError(f"could not reach ntfy at https://ntfy.sh/{TOPIC}")
        self.sent.append(notification)


class Values:
    """Opens a fresh value cache handle, budget and provider tuple on every
    call, like `open_value_sources`, and records each one and its closing."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.opened: list[ValueSources] = []
        self.closed: list[ValueSources] = []
        self.error: Exception | None = None

    @contextmanager
    def __call__(self):
        if self.error is not None:
            raise self.error
        cache = ValueCache(self.directory)
        budget = CreditBudget(cache, clock=lambda: T0)
        sources = ValueSources((FakeProvider("fake"),), cache, budget)
        self.opened.append(sources)
        try:
            yield sources
        finally:
            cache.close()
            self.closed.append(sources)


class World:
    """Everything a watch run needs, wired to fakes."""

    def __init__(self, tmp_path: Path) -> None:
        self.arena = sample_arena()
        self.clients = 0
        self.client_error: BaseException | None = None
        self.cycle_seconds = 0.0
        self.clock = FakeClock()
        self.rng = FixedRandom()
        self.notifier = Notifier()
        self.values = Values(tmp_path / "values")
        self.state_dir = tmp_path / "state"
        self.lines: list[str] = []
        self.config = Config()
        self.overrides = OVERRIDES

    @contextmanager
    def client(self):
        self.clients += 1
        # A cycle takes `cycle_seconds`, spent fetching.
        self.clock.advance(self.cycle_seconds)
        if self.client_error is not None:
            raise self.client_error
        with self.arena as arena:
            yield arena

    def run(self, cycles: int | None = None, **kwargs):
        resources = Resources(
            client=self.client, values=self.values, notifier=self.notifier
        )
        return run_watch(
            self.config,
            self.overrides,
            resources,
            clock=self.clock,
            rng=self.rng,
            state_directory=self.state_dir,
            max_cycles=cycles,
            echo=self.lines.append,
            topic=TOPIC,
            **kwargs,
        )

    def kinds(self) -> list[AlertKind]:
        return [n.kind for n in self.notifier.sent]

    def titles(self) -> list[str]:
        return [n.title for n in self.notifier.sent]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def run_or_fail(world: World):
    """`world.run()`, failing the test (rather than stopping pytest) if a
    KeyboardInterrupt escapes."""
    try:
        return world.run()
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped run_watch")


def blocked(retry_after: float | None = None) -> ArenaBlockedError:
    return ArenaBlockedError(429, retry_after)


# --- scheduling: jitter and overlong cycles -------------------------------------


def test_jitter_is_ten_percent():
    assert JITTER == 0.10


@pytest.mark.parametrize(("u", "expected"), [(0.0, 540.0), (0.5, 600.0), (1.0, 660.0)])
def test_the_delay_is_the_interval_plus_or_minus_ten_percent(u, expected):
    rng = FixedRandom(u)

    assert next_delay(INTERVAL, 0.0, rng) == pytest.approx(expected)
    assert rng.calls == [(0.9, 1.1)]


def test_jittered_delays_stay_within_bounds_and_spread_out():
    rng = random.Random(38)

    delays = [next_delay(INTERVAL, 0.0, rng) for _ in range(2000)]

    assert all(540.0 <= d <= 660.0 for d in delays)
    # Real jitter, not a constant: both ends of the range are reached.
    assert min(delays) < 550.0
    assert max(delays) > 650.0


def test_the_delay_is_measured_from_the_cycle_start():
    assert next_delay(INTERVAL, 100.0, FixedRandom(0.5)) == pytest.approx(500.0)


def test_an_overlong_cycle_starts_the_next_one_at_once():
    assert next_delay(INTERVAL, 700.0, FixedRandom(1.0)) == 0.0


def test_the_loop_waits_the_jittered_interval_less_the_cycle_time(world):
    world.cycle_seconds = 120.0
    world.rng = FixedRandom(1.0)

    world.run(cycles=3)

    assert world.clock.sleeps == pytest.approx([540.0, 540.0])


def test_the_loop_runs_an_overlong_cycle_back_to_back(world):
    world.cycle_seconds = 900.0

    world.run(cycles=3)

    assert world.clock.sleeps == [0.0, 0.0]
    assert world.clients == 3


def test_the_interval_comes_from_the_config(world):
    world.config = replace(world.config, watch=Watch(interval_minutes=7))

    world.run(cycles=2)

    assert world.clock.sleeps == pytest.approx([420.0])


# --- back-off ---------------------------------------------------------------------


def test_backoff_starts_at_the_interval_and_doubles_up_to_two_hours():
    backoff = Backoff(INTERVAL)

    waits = [backoff.next() for _ in range(7)]

    assert MAX_BACKOFF_SECONDS == 2 * 60 * 60
    assert waits == [600.0, 1200.0, 2400.0, 4800.0, 7200.0, 7200.0, 7200.0]


def test_retry_after_wins_when_it_is_longer():
    backoff = Backoff(INTERVAL)

    assert backoff.next(retry_after=3600.0) == 3600.0
    # The back-off itself still doubled underneath.
    assert backoff.next(retry_after=5.0) == 1200.0


def test_backoff_resets_after_a_success():
    backoff = Backoff(INTERVAL)
    backoff.next()
    backoff.next()

    backoff.reset()

    assert backoff.next() == 600.0


def test_an_interval_above_the_cap_is_never_cut():
    assert Backoff(3 * 60 * 60.0).next() == 3 * 60 * 60.0


def test_the_loop_backs_off_on_repeated_blocks(world):
    world.client_error = blocked()

    world.run(cycles=7)

    assert world.clock.sleeps == [600.0, 1200.0, 2400.0, 4800.0, 7200.0, 7200.0]


def test_the_loop_honours_a_longer_retry_after(world):
    world.client_error = blocked(retry_after=5400.0)

    world.run(cycles=2)

    assert world.clock.sleeps == [5400.0]


def test_the_loop_backs_off_on_other_api_errors(world):
    world.client_error = ArenaApiError("GET /v4/repacks: HTTP 503", 503)

    world.run(cycles=3)

    assert world.clock.sleeps == [600.0, 1200.0]


def test_the_backoff_resets_after_a_successful_cycle(world):
    world.client_error = blocked()

    def on_sleep(n):
        # Cycles 1-2 blocked, 3 fine, 4 blocked again.
        world.client_error = None if n == 2 else blocked()

    world.clock.on_sleep = on_sleep

    world.run(cycles=4)

    assert world.clock.sleeps == [600.0, 1200.0, 600.0]


# --- cycles and Alerts -------------------------------------------------------------


def test_one_cycle_sends_an_entered_alert_per_new_top_result(world):
    cycle = world.run(cycles=1)

    assert cycle.ok
    assert world.kinds() == [AlertKind.ENTERED, AlertKind.ENTERED]
    assert world.titles() == [
        "New Top Result: Gold Pokemon",
        "New Top Result: Gold Football",
    ]
    assert world.clock.sleeps == []  # no sleep after the last cycle


def test_the_summary_line_gives_the_time_counts_and_alerts(world):
    world.run(cycles=1)

    assert world.lines == [
        "[2026-11-26 10:00 UTC] 2 ranked, 1 unrankable, 0 skipped; 2 alerts sent"
    ]


def test_an_unchanged_cycle_sends_nothing(world):
    world.run(cycles=2)

    assert len(world.notifier.sent) == 2
    assert world.lines[1].endswith("; 0 alerts sent")


def test_the_second_cycle_sends_an_entered_alert_for_a_new_top_result(world):
    world.arena.packs["gold-football"]["status"] = "sold_out"

    def on_sleep(n):
        world.arena.packs["gold-football"]["status"] = "active"

    world.clock.on_sleep = on_sleep

    world.run(cycles=2)

    assert world.titles() == [
        "New Top Result: Gold Pokemon",
        "New Top Result: Gold Football",
    ]
    assert world.lines[0].endswith("1 ranked, 1 unrankable, 0 skipped; 1 alert sent")
    assert world.lines[1].endswith("2 ranked, 1 unrankable, 0 skipped; 1 alert sent")


def test_alerts_cover_packs_below_the_top_n_cut(world):
    world.config = replace(world.config, filters=replace(world.config.filters, top=1))

    world.run(cycles=1)

    assert len(world.notifier.sent) == 2


def test_the_state_is_saved_after_each_cycle(world):
    world.run(cycles=1)

    state = load_state(world.state_dir)
    assert set(state.packs) == {"gold-pokemon", "gold-football"}
    assert state.packs["gold-football"].alerted_at == T0


def test_the_saved_state_is_loaded_at_start(world):
    seen = AlertedPack(0.08, T0 - timedelta(hours=1))
    save_state(
        AlertState(packs={"gold-pokemon": seen, "gold-football": seen}),
        world.state_dir,
    )

    world.run(cycles=1)

    # Gold Pokemon rose far past the step; Gold Football is unchanged.
    assert world.kinds() == [AlertKind.IMPROVED]


# --- health -------------------------------------------------------------------


def test_a_degraded_cycle_sends_the_health_alert_and_backs_off(world):
    world.arena.errors[("pack", "gold-pokemon")] = ArenaApiError("timeout")
    world.arena.errors[("pack", "silver-pokemon")] = ArenaApiError("timeout")

    world.run(cycles=2)

    titles = world.titles()
    assert titles[0] == "Most Slab Packs could not be fetched"
    assert "2 of 3 Slab Packs were skipped" in world.notifier.sent[0].message
    # The pack that was fetched is still alerted, once.
    assert titles.count("New Top Result: Gold Football") == 1
    assert world.clock.sleeps == [600.0]
    assert load_state(world.state_dir).health is HealthKind.DEGRADED
    assert "degraded" in world.lines[0]
    assert "backing off 10 min" in world.lines[0]


def test_a_blocked_cycle_alerts_once_then_recovers_once(world):
    world.client_error = blocked(retry_after=60.0)

    def on_sleep(n):
        world.client_error = None if n >= 2 else blocked()

    world.clock.on_sleep = on_sleep

    world.run(cycles=3)

    assert world.kinds() == [
        AlertKind.HEALTH_FAILED,
        AlertKind.HEALTH_RECOVERED,
        AlertKind.ENTERED,
        AlertKind.ENTERED,
    ]
    assert "HTTP 429" in world.lines[0]
    assert "backing off 10 min" in world.lines[0]


def test_a_blocked_cycle_keeps_the_pack_entries(world):
    world.run(cycles=1)
    world.client_error = blocked()

    world.run(cycles=1)

    state = load_state(world.state_dir)
    assert set(state.packs) == {"gold-pokemon", "gold-football"}
    assert state.health is HealthKind.BLOCKED


def test_a_value_cache_error_is_logged_and_backed_off(world, caplog):
    world.values.error = sqlite3.OperationalError("database is locked")

    world.run(cycles=2)

    assert world.clock.sleeps == [600.0]
    assert "database is locked" in caplog.text
    assert "database is locked" in world.lines[0]
    assert load_state(world.state_dir).health is HealthKind.VALUE_CACHE


def test_a_value_cache_outage_sends_one_health_alert_and_one_recovery(world):
    world.values.error = sqlite3.OperationalError("database is locked")

    def on_sleep(n):
        # Cycles 1-3 can't use the cache, cycle 4 can.
        if n == 3:
            world.values.error = None

    world.clock.on_sleep = on_sleep

    world.run(cycles=4)

    assert world.kinds() == [
        AlertKind.HEALTH_FAILED,
        AlertKind.HEALTH_RECOVERED,
        AlertKind.ENTERED,
        AlertKind.ENTERED,
    ]
    failed = world.notifier.sent[0]
    assert failed.title == "Watch Mode can't use its value cache"
    assert "database is locked" in failed.message


def test_a_value_cache_error_keeps_the_pack_entries(world):
    world.run(cycles=1)
    world.values.error = OSError("disk full")

    world.run(cycles=1)

    assert set(load_state(world.state_dir).packs) == {"gold-pokemon", "gold-football"}


# --- fresh resources ----------------------------------------------------------------


def test_every_cycle_builds_and_closes_fresh_resources(world):
    world.run(cycles=3)

    assert world.clients == 3
    assert len(world.values.opened) == 3
    assert world.values.closed == world.values.opened
    # A new cache handle and new providers each time, not one reused.
    caches = {id(s.cache) for s in world.values.opened}
    providers = {id(s.providers[0]) for s in world.values.opened}
    assert len(caches) == len(providers) == 3
    assert world.notifier.closed == world.notifier.built


def test_the_client_is_closed_before_values_are_looked_up(world):
    closed_when_valuing = []
    values = world.values

    @contextmanager
    def check():
        closed_when_valuing.append(world.arena.closed)
        with values() as sources:
            yield sources

    world.values = check

    world.run(cycles=1)

    assert closed_when_valuing == [True]


# --- failures that must not stop the loop ---------------------------------------


def test_a_failed_notification_is_logged_and_the_loop_goes_on(world, caplog):
    world.notifier.failing = True

    world.run(cycles=2)

    assert world.clients == 2
    assert "New Top Result: Gold Pokemon" in caplog.text
    assert world.lines[0].endswith("; 0 alerts sent, 2 failed")


def test_a_failed_alert_is_retried_next_cycle(world):
    world.notifier.failing = True

    def on_sleep(n):
        world.notifier.failing = False

    world.clock.on_sleep = on_sleep

    world.run(cycles=2)

    assert world.titles() == [
        "New Top Result: Gold Pokemon",
        "New Top Result: Gold Football",
    ]


def test_a_failed_improved_alert_is_retried_next_cycle(world):
    seen = AlertedPack(0.08, T0 - timedelta(hours=1))
    save_state(
        AlertState(packs={"gold-pokemon": seen, "gold-football": seen}),
        world.state_dir,
    )
    world.notifier.failing = True

    def on_sleep(n):
        world.notifier.failing = False

    world.clock.on_sleep = on_sleep

    world.run(cycles=2)

    assert world.kinds() == [AlertKind.IMPROVED]
    assert load_state(world.state_dir).packs["gold-pokemon"].buyback_ev_pct > 0.4


def test_a_failed_health_alert_is_retried_next_cycle(world):
    world.client_error = blocked()
    world.notifier.failing = True

    def on_sleep(n):
        world.notifier.failing = False

    world.clock.on_sleep = on_sleep

    world.run(cycles=2)

    assert world.kinds() == [AlertKind.HEALTH_FAILED]


def test_the_topic_is_redacted_from_a_logged_notification_failure(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.notifier.failing = True

    world.run(cycles=1)

    assert "could not reach ntfy" in caplog.text
    assert TOPIC not in caplog.text


def test_a_state_save_failure_is_logged_and_the_loop_goes_on(world, caplog):
    world.state_dir.parent.mkdir(parents=True, exist_ok=True)
    world.state_dir.write_text("not a directory")

    cycle = world.run(cycles=2)

    assert cycle.ok
    assert world.clients == 2
    assert "could not save the Alert state" in caplog.text


def test_a_state_save_failure_on_shutdown_is_logged(world, caplog, monkeypatch):
    def fail(state, directory=None):
        raise PermissionError("read-only file system")

    monkeypatch.setattr("arenaclub_optimizer.watch.save_state", fail)

    def stop(n):
        raise KeyboardInterrupt

    world.clock.on_sleep = stop

    world.run()

    assert caplog.text.count("read-only file system") == 2


# --- shutdown -------------------------------------------------------------------


def test_an_interrupt_while_sleeping_saves_the_state_and_returns(world):
    def stop(n):
        raise KeyboardInterrupt

    world.clock.on_sleep = stop

    cycle = world.run()

    assert cycle.ok
    assert set(load_state(world.state_dir).packs) == {"gold-pokemon", "gold-football"}
    assert world.lines[-1] == "Stopped; Alert state saved."


def test_an_interrupt_mid_cycle_abandons_it_and_keeps_the_last_state(world):
    def interrupt_next_cycle(n):
        world.arena.closed = False
        world.arena.errors[("pack", "gold-pokemon")] = KeyboardInterrupt()

    world.clock.on_sleep = interrupt_next_cycle

    world.run()

    assert world.clients == 2
    assert world.arena.closed  # the client was closed on the way out
    data = json.loads((world.state_dir / STATE_FILENAME).read_text())
    assert set(data["packs"]) == {"gold-pokemon", "gold-football"}
    assert world.lines[-1] == "Stopped; Alert state saved."


def test_a_second_interrupt_during_the_shutdown_save_is_ignored(world, monkeypatch):
    real_save = save_state
    calls = []

    def save_then_interrupt(state, directory=None):
        calls.append(state)
        if len(calls) == 2:  # the save on shutdown: Ctrl+C again
            signal.raise_signal(signal.SIGINT)
        real_save(state, directory)

    monkeypatch.setattr("arenaclub_optimizer.watch.save_state", save_then_interrupt)

    def stop(n):
        raise KeyboardInterrupt

    world.clock.on_sleep = stop

    run_or_fail(world)

    assert len(calls) == 2
    assert world.lines[-1] == "Stopped; Alert state saved."
    # Ctrl+C works as usual again afterwards.
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_an_interrupt_while_loading_the_state_saves_nothing(world, monkeypatch):
    seen = AlertedPack(0.08, T0 - timedelta(hours=1))
    save_state(AlertState(packs={"gold-pokemon": seen}), world.state_dir)
    before = (world.state_dir / STATE_FILENAME).read_bytes()

    def interrupted(directory=None):
        raise KeyboardInterrupt

    monkeypatch.setattr("arenaclub_optimizer.watch.load_state", interrupted)

    cycle = run_or_fail(world)

    assert cycle is None
    assert world.clients == 0
    assert (world.state_dir / STATE_FILENAME).read_bytes() == before
    assert world.lines == ["Stopped."]


# --- quiet ----------------------------------------------------------------------


def test_quiet_prints_only_failed_cycles(world):
    world.client_error = blocked()

    def on_sleep(n):
        world.client_error = None

    world.clock.on_sleep = on_sleep

    world.run(cycles=3, quiet=True)

    assert len(world.lines) == 1
    assert "HTTP 429" in world.lines[0]


# --- the real clock ---------------------------------------------------------------


def test_the_system_clock_is_aware_utc_and_monotonic(monkeypatch):
    slept = []
    monkeypatch.setattr("arenaclub_optimizer.watch.time.sleep", slept.append)
    clock = SystemClock()

    now = clock.now()
    first = clock.monotonic()
    clock.sleep(1.5)

    assert now.tzinfo is UTC
    assert clock.monotonic() >= first
    assert slept == [1.5]
