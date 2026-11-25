"""Watch Mode: run the ranking on an interval and send Alerts.

Each cycle runs `rank`'s pipeline (fetch, then values, then ranking), then
`alerts.detect` and `alerts.detect_health`, and sends each Alert. Every cycle
opens its own Arena Club client, value sources (cache handle, credit budget,
providers) and notifier, and closes them before the next, so nothing
long-lived goes stale; the caches persist on disk, so this is cheap.

The next cycle starts the interval ± 10% jitter after this one started, or at
once if this one ran longer; cycles never overlap. A cycle that fails (Arena
Club blocks a request, another request error, more than half of the Slab
Packs skipped, or an unusable value cache) backs off instead: the interval,
doubling with each failure up to two hours, or Retry-After if longer, and
reset by a successful cycle.

Sleep, the clock and randomness are passed in, as are the factories for the
client, value sources and notifier, so tests run the loop on fakes.
Vocabulary follows GLOSSARY.md; the rules are in docs/plan.md (Watch Mode).
"""

import itertools
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from arenaclub_optimizer.alerts import (
    Alert,
    AlertState,
    HealthKind,
    HealthOutcome,
    detect,
    detect_health,
    load_state,
    save_state,
)
from arenaclub_optimizer.arena_client import ArenaApiError, ArenaBlockedError
from arenaclub_optimizer.config import Config, Overrides
from arenaclub_optimizer.fetch import ArenaSource, FetchResult, fetch_card_packs
from arenaclub_optimizer.notify import Notifier, NotifyError
from arenaclub_optimizer.ranking import RankingReport, rank
from arenaclub_optimizer.valuation import ValueSources, value_packs

__all__ = [
    "JITTER",
    "MAX_BACKOFF_SECONDS",
    "Backoff",
    "Clock",
    "Cycle",
    "Resources",
    "SystemClock",
    "next_delay",
    "run_cycle",
    "run_watch",
    "summary_line",
]

logger = logging.getLogger(__name__)

# The wait between cycles varies by up to this share of the interval.
JITTER = 0.10
# The longest a failing cycle backs off for, unless Retry-After asks longer.
MAX_BACKOFF_SECONDS = 2 * 60 * 60.0

_REDACTED = "<topic>"


class Clock(Protocol):
    """Wall time for timestamps, monotonic time for measuring, and sleep."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock: UTC wall time, `time.monotonic` and `time.sleep`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Random(Protocol):
    """What the jitter needs from `random.Random`."""

    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True)
class Resources:
    """Factories for what a cycle opens. Each is called afresh every cycle
    (the notifier only when there is something to send) and closed at its
    end."""

    client: Callable[[], AbstractContextManager[ArenaSource]]
    values: Callable[[], AbstractContextManager[ValueSources]]
    notifier: Callable[[], AbstractContextManager[Notifier]]


def next_delay(interval: float, elapsed: float, rng: Random) -> float:
    """Seconds to wait before the next cycle: `interval` ± JITTER, measured
    from the start of a cycle that took `elapsed` seconds. Never negative,
    so an overlong cycle is followed at once."""
    target = interval * rng.uniform(1 - JITTER, 1 + JITTER)
    return max(0.0, target - elapsed)


class Backoff:
    """How long to wait after a failed cycle.

    The first failure waits `interval`; each further one doubles the wait,
    up to `maximum` (but never below `interval`). A longer Retry-After wins
    for that wait. `reset` after a successful cycle starts over.
    """

    def __init__(self, interval: float, maximum: float = MAX_BACKOFF_SECONDS) -> None:
        self.interval = interval
        self.maximum = max(maximum, interval)
        self._current: float | None = None

    def next(self, retry_after: float | None = None) -> float:
        if self._current is None:
            self._current = self.interval
        else:
            self._current = min(self._current * 2, self.maximum)
        return max(self._current, retry_after or 0.0)

    def reset(self) -> None:
        self._current = None


@dataclass(frozen=True)
class Cycle:
    """How one cycle went.

    `health` is the outcome Arena Club gave, or None when the cycle failed
    on this side (an unusable value cache) before it was known. `report` is
    None when nothing was ranked. `problem` describes a failure. `sent` and
    `failed` count the Alerts sent and those that couldn't be.
    """

    started_at: datetime
    state: AlertState
    health: HealthOutcome | None
    report: RankingReport | None
    sent: int = 0
    failed: int = 0
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.health is not None and self.health.kind is HealthKind.OK

    @property
    def retry_after(self) -> float | None:
        return None if self.health is None else self.health.retry_after


def run_cycle(
    config: Config,
    overrides: Overrides,
    state: AlertState,
    resources: Resources,
    *,
    now: datetime,
    topic: str | None = None,
) -> Cycle:
    """Run one cycle from `state` at `now` and send its Alerts.

    The client is closed before values are looked up, as in `rank`. A
    blocked or failed request, or a degraded fetch, gives a health outcome
    for `detect_health`; Slab Packs are still alerted on in a degraded
    cycle, whose skipped packs keep their entries. A failed notification is
    logged (with `topic` redacted) and left out of the returned state, so
    the next cycle tries it again.
    """
    report: RankingReport | None = None
    problem: str | None = None
    try:
        fetched = _fetch(config, resources)
    except ArenaBlockedError as error:
        health = HealthOutcome.blocked(error.status or 0, error.retry_after)
        problem = str(error)
    except ArenaApiError as error:
        health = HealthOutcome.api_error(str(error))
        problem = f"could not read Arena Club: {error}"
    else:
        try:
            with resources.values() as sources:
                values = value_packs(
                    fetched.packs,
                    config.filters,
                    overrides,
                    sources,
                    coverage_threshold=config.coverage_threshold,
                )
        except (OSError, sqlite3.Error) as error:
            problem = f"could not use the value cache: {error}"
            logger.error("%s", problem)
            return Cycle(now, state, None, None, problem=problem)
        report = rank(
            fetched.packs, config, values, skipped=fetched.skipped, generated_at=now
        )
        health = HealthOutcome.from_fetch(fetched)

    alerts: list[Alert] = []
    new_state = state
    if report is not None:
        alerts, new_state = detect(new_state, report, now=now)
    health_alert, new_state = detect_health(new_state, health)
    if health_alert is not None:
        alerts.insert(0, health_alert)

    failed = _send(alerts, resources, topic)
    for alert in failed:
        new_state = _forget(alert, new_state, state)
    return Cycle(
        now, new_state, health, report, len(alerts) - len(failed), len(failed), problem
    )


def run_watch(
    config: Config,
    overrides: Overrides,
    resources: Resources,
    *,
    clock: Clock,
    rng: Random,
    state_directory: Path | None = None,
    max_cycles: int | None = None,
    quiet: bool = False,
    echo: Callable[[str], None] = print,
    topic: str | None = None,
) -> Cycle | None:
    """Run cycles every `config.watch.interval_minutes` until interrupted,
    or for `max_cycles`. Returns the last cycle that finished, if any.

    The Alert state is loaded from `state_directory` (default: the state
    directory) at the start and saved after every cycle; a failed save is
    logged and the loop goes on. Each cycle's `summary_line` goes to
    `echo`, only for a failed cycle when `quiet`.

    KeyboardInterrupt (Ctrl+C, or SIGTERM as the CLI maps it) abandons the
    cycle in progress, whose client and value sources are closed on the way
    out, saves the state of the last finished cycle and returns.
    """
    interval = config.watch.interval_minutes * 60
    backoff = Backoff(interval)
    state = load_state(state_directory)
    last: Cycle | None = None

    def show(cycle: Cycle, backing_off: float | None = None) -> None:
        if not (quiet and cycle.ok):
            echo(summary_line(cycle, backing_off=backing_off))

    try:
        for count in itertools.count(1):
            start = clock.monotonic()
            last = run_cycle(
                config, overrides, state, resources, now=clock.now(), topic=topic
            )
            state = last.state
            _save(state, state_directory)
            if max_cycles is not None and count >= max_cycles:
                show(last)
                return last
            if last.ok:
                backoff.reset()
                show(last)
                clock.sleep(next_delay(interval, clock.monotonic() - start, rng))
            else:
                wait = backoff.next(last.retry_after)
                show(last, backing_off=wait)
                clock.sleep(wait)
    except KeyboardInterrupt:
        if _save(state, state_directory):
            echo("Stopped; Alert state saved.")
        else:
            echo("Stopped; the Alert state could not be saved.")
        return last


def summary_line(cycle: Cycle, *, backing_off: float | None = None) -> str:
    """E.g. "[2026-11-26 10:00 UTC] 2 ranked, 5 unrankable, 0 skipped; 1 alert
    sent", with the problem and the back-off (`backing_off` seconds) when
    the cycle failed. "ranked" counts every Slab Pack that met the
    criteria."""
    stamp = cycle.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = []
    if cycle.report is not None:
        report = cycle.report
        parts.append(
            f"{len(report.matching)} ranked, {len(report.unrankable)} unrankable, "
            f"{len(report.skipped)} skipped"
        )
    if cycle.problem is not None:
        parts.append(cycle.problem)
    noun = "alert" if cycle.sent == 1 else "alerts"
    sent = f"{cycle.sent} {noun} sent"
    if cycle.failed:
        sent += f", {cycle.failed} failed"
    parts.append(sent)
    health = cycle.health
    if health is not None and health.kind is HealthKind.DEGRADED:
        parts.append(
            f"degraded ({health.skipped} of {health.total} Slab Packs skipped)"
        )
    if backing_off is not None:
        parts.append(f"backing off {_minutes(backing_off)}")
    return f"[{stamp}] " + "; ".join(parts)


def _fetch(config: Config, resources: Resources) -> FetchResult:
    filters = config.filters
    with resources.client() as client:
        return fetch_card_packs(
            client,
            filters.categories,
            price_min=filters.price_min,
            price_max=filters.price_max,
        )


def _send(alerts: list[Alert], resources: Resources, topic: str | None) -> list[Alert]:
    """Send each Alert in turn; return those that failed, each logged."""
    if not alerts:
        return []
    failed: list[Alert] = []
    with resources.notifier() as notifier:
        for alert in alerts:
            try:
                notifier.send(alert)
            except NotifyError as error:
                failed.append(alert)
                logger.warning(
                    "could not send the Alert %r: %s",
                    alert.title,
                    _redact(str(error), topic),
                )
    return failed


def _forget(alert: Alert, state: AlertState, previous: AlertState) -> AlertState:
    """`state` as if `alert` had never been decided on, so the next cycle
    sends it again: a health Alert's health, or a pack Alert's entry, goes
    back to what it was in `previous`."""
    if alert.result is None:
        return replace(state, health=previous.health)
    pack_id = alert.result.pack.pack_id
    packs = dict(state.packs)
    old = previous.packs.get(pack_id)
    if old is None:
        packs.pop(pack_id, None)
    else:
        packs[pack_id] = replace(old, last_seen_at=packs[pack_id].last_seen_at)
    return replace(state, packs=packs)


def _save(state: AlertState, directory: Path | None) -> bool:
    """Save `state`; on failure log it and carry on. Whether it was saved."""
    try:
        save_state(state, directory)
    except OSError as error:
        logger.error("could not save the Alert state: %s", error)
        return False
    return True


def _redact(text: str, topic: str | None) -> str:
    """`text` with the Topic hidden: on ntfy it works like a password."""
    return text.replace(topic, _REDACTED) if topic else text


def _minutes(seconds: float) -> str:
    return f"{round(seconds / 60, 1):g} min"
