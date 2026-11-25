"""Alerts for Watch Mode: which Slab Packs and health changes to notify about.

`detect` and `detect_health` are pure: they compare one cycle against the
previous AlertState and return the Alerts to send with the new state. An Alert
goes out only when a Slab Pack newly becomes a Top Result, when its Buyback
EV % rises a step above the value last alerted, or when the requests start or
stop failing; never every cycle. `load_state` and `save_state` keep the state
in `alerts.json` in the state directory (see `config.state_dir`). Vocabulary
follows GLOSSARY.md; the rules are in docs/plan.md (Watch Mode).
"""

import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from arenaclub_optimizer.config import state_dir
from arenaclub_optimizer.fetch import FetchResult
from arenaclub_optimizer.model import FLOAT_TOLERANCE
from arenaclub_optimizer.ranking import RankedPack, RankingReport, pack_name

__all__ = [
    "DEFAULT_EV_STEP_PCT",
    "MAX_MESSAGE_BYTES",
    "STATE_FILENAME",
    "Alert",
    "AlertKind",
    "AlertState",
    "AlertedPack",
    "HealthKind",
    "HealthOutcome",
    "Priority",
    "detect",
    "detect_health",
    "load_state",
    "save_state",
]

logger = logging.getLogger(__name__)

STATE_FILENAME = "alerts.json"
STATE_VERSION = 1

# A rise in Buyback EV % that earns a new Alert: 5 percentage points.
DEFAULT_EV_STEP_PCT = 0.05

# ntfy takes messages up to 4096 bytes; stay clear of the limit.
MAX_MESSAGE_BYTES = 4000

# Over this share of the Slab Packs in scope skipped, a cycle is degraded.
DEGRADED_SKIP_SHARE = 0.5

_ELLIPSIS = "…"
_HIGH_VARIANCE_TAG = "high-variance"
_BACKING_OFF = (
    "Watch Mode is backing off and will send one Alert when requests work again."
)


class AlertKind(StrEnum):
    ENTERED = "entered"
    IMPROVED = "improved"
    HEALTH_FAILED = "health_failed"
    HEALTH_RECOVERED = "health_recovered"


class Priority(StrEnum):
    """ntfy message priorities, by their ntfy names."""

    DEFAULT = "default"
    HIGH = "high"


class HealthKind(StrEnum):
    """How a Watch Mode cycle went: fine, or how it failed."""

    OK = "ok"
    BLOCKED = "blocked"
    API_ERROR = "api_error"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Alert:
    """A notification to send.

    `message` is plain text of at most MAX_MESSAGE_BYTES in UTF-8. `tags` are
    ntfy tags; one that names an emoji shows as that emoji. `result` is the
    Top Result an Entered or Improved Alert is about, None for a health Alert.
    """

    kind: AlertKind
    title: str
    message: str
    priority: Priority
    tags: tuple[str, ...]
    result: RankedPack | None = None


@dataclass(frozen=True)
class AlertedPack:
    """A Slab Pack's last Alert: its Buyback EV % then, and when it was sent."""

    buyback_ev_pct: float
    alerted_at: datetime


def _frozen(
    packs: Mapping[str, AlertedPack] | None = None,
) -> Mapping[str, AlertedPack]:
    return MappingProxyType(dict(packs or {}))


@dataclass(frozen=True)
class AlertState:
    """What has been alerted: each Slab Pack with a live Alert, by pack id,
    and the health of the last cycle. The default is the empty state."""

    packs: Mapping[str, AlertedPack] = field(default_factory=_frozen)
    health: HealthKind = HealthKind.OK

    def __post_init__(self) -> None:
        object.__setattr__(self, "packs", _frozen(self.packs))


@dataclass(frozen=True)
class HealthOutcome:
    """How one Watch Mode cycle went. Build it with `ok`, `blocked`,
    `api_error`, `degraded` or `from_fetch`; only the kind's own fields are
    set."""

    kind: HealthKind
    status: int | None = None
    retry_after: float | None = None
    error: str | None = None
    skipped: int | None = None
    total: int | None = None

    @classmethod
    def ok(cls) -> HealthOutcome:
        return cls(HealthKind.OK)

    @classmethod
    def blocked(cls, status: int, retry_after: float | None) -> HealthOutcome:
        """Arena Club answered `status` (403 or 429), asking to wait
        `retry_after` seconds, if it said."""
        return cls(HealthKind.BLOCKED, status=status, retry_after=retry_after)

    @classmethod
    def api_error(cls, error: str) -> HealthOutcome:
        """A request failed for another reason, described by `error`."""
        return cls(HealthKind.API_ERROR, error=error)

    @classmethod
    def degraded(cls, skipped: int, total: int) -> HealthOutcome:
        """`skipped` of the `total` Slab Packs in scope failed to fetch."""
        return cls(HealthKind.DEGRADED, skipped=skipped, total=total)

    @classmethod
    def from_fetch(cls, result: FetchResult) -> HealthOutcome:
        """Degraded when more than half of the fetched Slab Packs were
        skipped, which is how a network loss mid-run shows up; else ok."""
        skipped = len(result.skipped)
        total = len(result.packs) + skipped
        if total and skipped / total > DEGRADED_SKIP_SHARE:
            return cls.degraded(skipped, total)
        return cls.ok()


# --- detection ----------------------------------------------------------------


def detect(
    previous: AlertState,
    report: RankingReport,
    *,
    now: datetime,
    ev_step_pct: float = DEFAULT_EV_STEP_PCT,
) -> tuple[list[Alert], AlertState]:
    """The Alerts for this cycle's Top Results, and the new state.

    A Top Result with no entry in `previous` is Entered. One with an entry is
    Improved when its Buyback EV % is at least `ev_step_pct` (a fraction:
    0.05 is 5 percentage points) above the value last alerted; the entry
    keeps that value until the next Alert, so a dip and a recovery to the
    same level send nothing. Alerted packs are recorded as of `now`.

    A pack no longer among the Top Results is dropped silently, so it is
    Entered again if it returns. A pack in `report.skipped` couldn't be
    evaluated this cycle, so its entry is kept as it is.

    Alerts are sorted by Buyback EV % descending, ties in Top Results order.

    Raises ValueError if `ev_step_pct` is not positive and finite.
    """
    if not (math.isfinite(ev_step_pct) and ev_step_pct > 0):
        raise ValueError(f"ev_step_pct must be positive, got {ev_step_pct}")

    unknown = {skip.pack_id for skip in report.skipped}
    packs = {
        pack_id: entry
        for pack_id, entry in previous.packs.items()
        if pack_id in unknown
    }
    alerts: list[Alert] = []
    # A stable sort, so the Alerts come out in order whatever the report's.
    for result in sorted(report.results, key=lambda r: -r.ev.buyback_ev_pct):
        pack_id = result.pack.pack_id
        ev_pct = result.ev.buyback_ev_pct
        entry = previous.packs.get(pack_id)
        if entry is not None and not _risen(entry, ev_pct, ev_step_pct):
            packs[pack_id] = entry
            continue
        alerts.append(_pack_alert(result, entry))
        packs[pack_id] = AlertedPack(ev_pct, now)
    return alerts, replace(previous, packs=packs)


def _risen(entry: AlertedPack, ev_pct: float, step: float) -> bool:
    """Whether `ev_pct` is at least `step` above the value last alerted,
    allowing float noise: 0.10 to 0.15 is a rise of 0.04999999999999999."""
    return ev_pct - entry.buyback_ev_pct >= step - FLOAT_TOLERANCE


def detect_health(
    previous: AlertState, outcome: HealthOutcome
) -> tuple[Alert | None, AlertState]:
    """The health Alert for this cycle, if any, and the new state.

    One high-priority Alert when the outcome goes from ok to a failure, and
    one recovery Alert when it returns to ok. Anything else, including a
    different failure while already failing, sends nothing.
    """
    state = replace(previous, health=outcome.kind)
    was_ok = previous.health is HealthKind.OK
    is_ok = outcome.kind is HealthKind.OK
    if was_ok and not is_ok:
        return _failed(outcome), state
    if is_ok and not was_ok:
        return _recovered(), state
    return None, state


# --- messages -------------------------------------------------------------------


def _pack_alert(result: RankedPack, entry: AlertedPack | None) -> Alert:
    """Entered when there is no previous `entry`, else Improved. The message
    gives the figures, the price, the rise for an Improved Alert, then the
    Notes, the High-variance Note named as such."""
    name = pack_name(result.pack)
    ev, risk = result.ev, result.risk
    lines = [
        f"{name}: Buyback EV {ev.buyback_ev_pct:+.1%} "
        f"({_money(ev.buyback_ev)}), P(profit) {_percent(risk.p_profit)}",
        f"Price {_money(result.pack.price)}.",
    ]
    if entry is None:
        kind, title, tags = AlertKind.ENTERED, f"New Top Result: {name}", ["new"]
    else:
        kind, title = AlertKind.IMPROVED, f"Buyback EV up: {name}"
        tags = ["chart_with_upwards_trend"]
        lines.append(
            f"Buyback EV % up from {entry.buyback_ev_pct:+.1%} when last alerted."
        )
    lines += (
        f"High-variance Note: {note}" if note == risk.note else note
        for note in result.notes
    )
    if risk.high_variance:
        tags.append(_HIGH_VARIANCE_TAG)
    message = _fit("\n".join(lines))
    return Alert(kind, title, message, Priority.DEFAULT, tuple(tags), result)


def _failed(outcome: HealthOutcome) -> Alert:
    match outcome.kind:
        case HealthKind.BLOCKED:
            title = "Arena Club is blocking requests"
            problem = f"Arena Club answered HTTP {outcome.status}"
            if outcome.retry_after is not None:
                problem += f" and asked to retry after {outcome.retry_after:g}s"
            problem += "."
        case HealthKind.DEGRADED:
            title = "Most Slab Packs could not be fetched"
            problem = (
                f"{outcome.skipped} of {outcome.total} Slab Packs were skipped "
                "this cycle, which usually means the network dropped mid-run."
            )
        case _:
            title = "Arena Club requests are failing"
            problem = outcome.error or "A request to Arena Club failed."
    # A long error is cut so that the line about backing off always fits.
    room = MAX_MESSAGE_BYTES - len(f"\n{_BACKING_OFF}".encode())
    message = f"{_fit(problem, room)}\n{_BACKING_OFF}"
    return Alert(AlertKind.HEALTH_FAILED, title, message, Priority.HIGH, ("warning",))


def _recovered() -> Alert:
    return Alert(
        AlertKind.HEALTH_RECOVERED,
        "Watch Mode recovered",
        "Requests to Arena Club are working again.",
        Priority.DEFAULT,
        ("white_check_mark",),
    )


def _money(amount: float) -> str:
    """E.g. "$4.20", or "-$3.00" for a loss."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _percent(p: float) -> str:
    """A probability as a whole percentage, without rounding a small one to 0%."""
    if 0 < p < 0.005:
        return "<1%"
    return f"{p:.0%}"


def _fit(text: str, limit: int = MAX_MESSAGE_BYTES) -> str:
    """`text`, cut to `limit` bytes of UTF-8 with an ellipsis if longer. A
    character split by the cut is dropped whole."""
    data = text.encode()
    if len(data) <= limit:
        return text
    room = limit - len(_ELLIPSIS.encode())
    return data[:room].decode(errors="ignore") + _ELLIPSIS


# --- state storage --------------------------------------------------------------


def load_state(directory: Path | None = None) -> AlertState:
    """Read the AlertState from `alerts.json` in `directory` (default: the
    state directory). A missing file is the empty state; an unreadable or
    corrupt one is logged and treated as the empty state."""
    path = _state_path(directory)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AlertState()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("ignoring unreadable Alert state %s: %s", path, exc)
        return AlertState()
    try:
        return _parse_state(json.loads(text))
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("ignoring corrupt Alert state %s: %s", path, exc)
        return AlertState()


def save_state(state: AlertState, directory: Path | None = None) -> None:
    """Write `state` to `alerts.json` in `directory` (default: the state
    directory), creating it if needed.

    The write is atomic: the state goes to a temp file beside it, which then
    replaces the old file, so a crash never leaves a half-written file.
    Raises OSError if the write fails; the old file is then left as it was.
    """
    path = _state_path(directory)
    text = json.dumps(_state_json(state), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


def _state_path(directory: Path | None) -> Path:
    return (directory if directory is not None else state_dir()) / STATE_FILENAME


def _state_json(state: AlertState) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "health": state.health.value,
        "packs": {
            pack_id: {
                "buyback_ev_pct": entry.buyback_ev_pct,
                "alerted_at": entry.alerted_at.isoformat(),
            }
            for pack_id, entry in state.packs.items()
        },
    }


def _parse_state(data: Any) -> AlertState:
    """The AlertState in parsed `alerts.json`. Raises ValueError (or
    TypeError, KeyError) for anything out of shape."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if data.get("version") != STATE_VERSION:
        raise ValueError(f"unknown version {data.get('version')!r}")
    packs = data["packs"]
    if not isinstance(packs, dict):
        raise ValueError("packs is not an object")
    return AlertState(
        packs={str(k): _parse_entry(v) for k, v in packs.items()},
        health=HealthKind(data["health"]),
    )


def _parse_entry(data: Any) -> AlertedPack:
    if not isinstance(data, dict):
        raise ValueError("a pack entry is not an object")
    ev_pct = data["buyback_ev_pct"]
    if isinstance(ev_pct, bool) or not isinstance(ev_pct, int | float):
        raise ValueError(f"buyback_ev_pct is not a number: {ev_pct!r}")
    if not math.isfinite(ev_pct):
        raise ValueError(f"buyback_ev_pct is not finite: {ev_pct!r}")
    alerted_at = data["alerted_at"]
    if not isinstance(alerted_at, str):
        raise ValueError(f"alerted_at is not a string: {alerted_at!r}")
    return AlertedPack(float(ev_pct), datetime.fromisoformat(alerted_at))
