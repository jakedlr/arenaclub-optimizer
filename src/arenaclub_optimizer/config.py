"""User configuration and the Manual Overrides file.

Both files are TOML and live in `$XDG_CONFIG_HOME/arenaclub-optimizer/`, falling
back to `~/.config/arenaclub-optimizer/`. Loading never writes anything; a
missing file means defaults (or no Manual Overrides).
"""

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from arenaclub_optimizer.cost import PaymentMethod

APP_DIR_NAME = "arenaclub-optimizer"
CONFIG_FILENAME = "config.toml"
OVERRIDES_FILENAME = "overrides.toml"

MIN_INTERVAL_MINUTES = 5
CARD_ID_PREFIX = "card_"
# ntfy topics: letters, digits, `_` and `-`, at most 64 characters.
_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ConfigError(ValueError):
    """A config or overrides file is unreadable or holds an invalid entry."""


@dataclass(frozen=True)
class Filters:
    """Default Top Results filters. Fractions, not percentages: 0.05 is 5%."""

    categories: tuple[str, ...] = ()
    price_min: float | None = None
    price_max: float | None = None
    min_ev_pct: float | None = None
    min_p_profit: float | None = None
    exclude_high_variance: bool = False
    top: int = 10


@dataclass(frozen=True)
class Watch:
    """Watch Mode settings. Values only; Watch Mode itself is Sprint 4."""

    interval_minutes: float = 10.0
    ntfy_server: str = "https://ntfy.sh"
    topic: str | None = None


@dataclass(frozen=True)
class Config:
    k: float = 1.0
    payment_method: PaymentMethod = PaymentMethod.wallet
    tax_rate: float = 0.0
    coverage_threshold: float = 0.95
    p_profit_threshold: float = 0.25
    top_heaviness_threshold: float = 0.5
    filters: Filters = field(default_factory=Filters)
    watch: Watch = field(default_factory=Watch)


@dataclass(frozen=True)
class Override:
    """A Manual Override: a Market Value in dollars, with an optional note."""

    market_value: float
    note: str | None = None


def _frozen(mapping: Mapping[str, Override] | None = None) -> Mapping[str, Override]:
    return MappingProxyType(dict(mapping or {}))


@dataclass(frozen=True)
class Overrides:
    """Manual Overrides keyed by cert number and by card id."""

    by_cert: Mapping[str, Override] = field(default_factory=_frozen)
    by_card_id: Mapping[str, Override] = field(default_factory=_frozen)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_cert", _frozen(self.by_cert))
        object.__setattr__(self, "by_card_id", _frozen(self.by_card_id))

    def lookup(self, card_id: str, cert_number: str | None) -> float | None:
        """Return the overriding Market Value: by cert first, then by card id."""
        if cert_number is not None and cert_number in self.by_cert:
            return self.by_cert[cert_number].market_value
        if card_id in self.by_card_id:
            return self.by_card_id[card_id].market_value
        return None


def config_dir(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Return the app's config directory, honouring `XDG_CONFIG_HOME`."""
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (home if home is not None else Path.home()) / ".config"
    return base / APP_DIR_NAME


def load_config(path: Path | None = None, *, directory: Path | None = None) -> Config:
    """Read the config file. `path` wins over `directory`; both default sensibly."""
    path = _resolve(path, directory, CONFIG_FILENAME)
    data = _read_toml(path)
    if data is None:
        return Config()
    return _parse_config(_Reader(path), data)


def override_config(
    config: Config,
    changes: Mapping[str, Any],
    *,
    source: str = "command line",
    names: Mapping[str, str] | None = None,
) -> Config:
    """Return `config` with `changes` applied, checked by the config file's rules.

    `changes` has the config file's layout: top-level keys, plus `filters` and
    `watch` tables. A None value leaves that setting as it is. Error messages
    start with `source` and show each key as `names` gives it (e.g.
    "filters.top" as "--top"), else as the file would.

    Raises ConfigError for an unknown key or an invalid value, including a
    price range whose minimum (from either side) is above its maximum.
    """
    data = _as_toml(config)
    for key, value in changes.items():
        if isinstance(value, Mapping):
            table = data.setdefault(key, {})
            if isinstance(table, dict):
                table.update({k: v for k, v in value.items() if v is not None})
                continue
        if value is not None:
            data[key] = value
    return _parse_config(_Reader(source, names), data)


def _parse_config(reader: _Reader, data: dict[str, Any]) -> Config:
    reader.reject_unknown(data, Config, "")
    filters = reader.table(data, "filters")
    watch = reader.table(data, "watch")
    reader.reject_unknown(filters, Filters, "filters.")
    reader.reject_unknown(watch, Watch, "watch.")

    defaults = Config()
    return Config(
        k=reader.number(data, "k", defaults.k, low=0.0, low_open=True),
        payment_method=reader.payment_method(data, "payment_method"),
        tax_rate=reader.number(
            data, "tax_rate", defaults.tax_rate, low=0.0, high=1.0, high_open=True
        ),
        coverage_threshold=reader.fraction(
            data, "coverage_threshold", defaults.coverage_threshold
        ),
        p_profit_threshold=reader.fraction(
            data, "p_profit_threshold", defaults.p_profit_threshold
        ),
        top_heaviness_threshold=reader.fraction(
            data, "top_heaviness_threshold", defaults.top_heaviness_threshold
        ),
        filters=_filters(reader, filters),
        watch=_watch(reader, watch),
    )


def _as_toml(value: Any) -> Any:
    """A Config (or part of one) in the parsed-TOML shape `_parse_config` reads:
    tables as dicts, tuples as lists, enums as strings, and None left out."""
    if is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _as_toml(v) for k, v in value.items() if v is not None}
    if isinstance(value, tuple):
        return [_as_toml(v) for v in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def load_overrides(
    path: Path | None = None, *, directory: Path | None = None
) -> Overrides:
    """Read the Manual Overrides file; a missing file means no overrides.

    Each top-level table is one entry. A key with the `card_` prefix is a card
    id; any other key is a cert number.
    """
    path = _resolve(path, directory, OVERRIDES_FILENAME)
    data = _read_toml(path)
    if data is None:
        return Overrides()
    reader = _Reader(path)
    by_cert: dict[str, Override] = {}
    by_card_id: dict[str, Override] = {}
    for key in data:
        entry = reader.table(data, key)
        if key == CARD_ID_PREFIX:
            raise reader.error(key, "has the card_ prefix but no card id after it")
        prefix = f"{key}."
        reader.reject_unknown(entry, Override, prefix)
        override = Override(
            market_value=reader.required_number(
                entry, "market_value", low=0.0, at=prefix
            ),
            note=reader.optional_str(entry, "note", at=prefix),
        )
        target = by_card_id if key.startswith(CARD_ID_PREFIX) else by_cert
        target[key] = override
    return Overrides(by_cert=by_cert, by_card_id=by_card_id)


def _filters(reader: _Reader, data: dict[str, Any]) -> Filters:
    at = "filters."
    defaults = Filters()
    price_min = reader.optional_number(data, "price_min", low=0.0, at=at)
    price_max = reader.optional_number(data, "price_max", low=0.0, at=at)
    if price_min is not None and price_max is not None and price_min > price_max:
        raise reader.error(
            f"{at}price_min",
            f"({price_min}) is above {reader.name(f'{at}price_max')} ({price_max})",
        )
    return Filters(
        categories=reader.str_list(data, "categories", at=at),
        price_min=price_min,
        price_max=price_max,
        min_ev_pct=reader.optional_number(data, "min_ev_pct", at=at),
        min_p_profit=reader.optional_number(
            data, "min_p_profit", low=0.0, high=1.0, at=at
        ),
        exclude_high_variance=reader.boolean(
            data, "exclude_high_variance", defaults.exclude_high_variance, at=at
        ),
        top=reader.integer(data, "top", defaults.top, low=1, at=at),
    )


def _watch(reader: _Reader, data: dict[str, Any]) -> Watch:
    at = "watch."
    defaults = Watch()
    interval = reader.number(
        data,
        "interval_minutes",
        defaults.interval_minutes,
        low=MIN_INTERVAL_MINUTES,
        at=at,
    )

    server = reader.string(data, "ntfy_server", defaults.ntfy_server, at=at)
    url = urlsplit(server)
    if url.scheme not in ("https", "http") or not url.netloc:
        raise reader.error(
            f"{at}ntfy_server", f"must be an http(s) URL with a host, got {server!r}"
        )

    topic = reader.optional_str(data, "topic", at=at)
    if topic is not None and not _TOPIC_RE.fullmatch(topic):
        raise reader.error(
            f"{at}topic", "must be 1-64 letters, digits, '_' or '-' characters"
        )
    return Watch(interval_minutes=interval, ntfy_server=server, topic=topic)


def _resolve(path: Path | None, directory: Path | None, filename: str) -> Path:
    if path is not None:
        return Path(path)
    return (directory if directory is not None else config_dir()) / filename


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e}") from e


class _Reader:
    """Typed, range-checked access to a parsed TOML table from one source.

    `source` (a file path, or e.g. "command line") starts every error message,
    and `names` maps a key such as "filters.top" to how the message shows it.
    """

    def __init__(
        self, source: Path | str, names: Mapping[str, str] | None = None
    ) -> None:
        self.source = source
        self.names = names or {}

    def name(self, key: str) -> str:
        return self.names.get(key, key)

    def error(self, key: str, problem: str) -> ConfigError:
        return ConfigError(f"{self.source}: {self.name(key)} {problem}")

    def reject_unknown(self, data: dict[str, Any], schema: type, at: str) -> None:
        known = set(schema.__dataclass_fields__)
        for key in data:
            if key not in known:
                raise self.error(f"{at}{key}", "is not a known key")

    def table(self, data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key, {})
        if not isinstance(value, dict):
            raise self.error(key, "must be a table")
        return value

    def number(
        self,
        data: dict[str, Any],
        key: str,
        default: float,
        *,
        low: float | None = None,
        high: float | None = None,
        high_open: bool = False,
        low_open: bool = False,
        at: str = "",
    ) -> float:
        value = self.optional_number(
            data,
            key,
            low=low,
            high=high,
            high_open=high_open,
            low_open=low_open,
            at=at,
        )
        return default if value is None else value

    def required_number(
        self, data: dict[str, Any], key: str, *, low: float, at: str = ""
    ) -> float:
        value = self.optional_number(data, key, low=low, at=at)
        if value is None:
            raise self.error(f"{at}{key}", "is required")
        return value

    def optional_number(
        self,
        data: dict[str, Any],
        key: str,
        *,
        low: float | None = None,
        high: float | None = None,
        high_open: bool = False,
        low_open: bool = False,
        at: str = "",
    ) -> float | None:
        if key not in data:
            return None
        value = data[key]
        name = f"{at}{key}"
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self.error(name, f"must be a number, got {value!r}")
        try:
            value = float(value)
        except OverflowError:
            raise self.error(name, "is too large to be a number") from None
        if not math.isfinite(value):
            raise self.error(name, f"must be finite, got {value}")
        if low is not None and (value <= low if low_open else value < low):
            bound = "more than" if low_open else "at least"
            raise self.error(name, f"must be {bound} {low}, got {value}")
        if high is not None and (value >= high if high_open else value > high):
            bound = "below" if high_open else "at most"
            raise self.error(name, f"must be {bound} {high}, got {value}")
        return value

    def fraction(self, data: dict[str, Any], key: str, default: float) -> float:
        return self.number(data, key, default, low=0.0, high=1.0)

    def integer(
        self, data: dict[str, Any], key: str, default: int, *, low: int, at: str = ""
    ) -> int:
        value = data.get(key, default)
        name = f"{at}{key}"
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.error(name, f"must be an integer, got {value!r}")
        if value < low:
            raise self.error(name, f"must be at least {low}, got {value}")
        return value

    def boolean(
        self, data: dict[str, Any], key: str, default: bool, *, at: str = ""
    ) -> bool:
        value = data.get(key, default)
        if not isinstance(value, bool):
            raise self.error(f"{at}{key}", f"must be true or false, got {value!r}")
        return value

    def string(
        self, data: dict[str, Any], key: str, default: str, *, at: str = ""
    ) -> str:
        value = self.optional_str(data, key, at=at)
        return default if value is None else value

    def optional_str(
        self, data: dict[str, Any], key: str, *, at: str = ""
    ) -> str | None:
        if key not in data:
            return None
        value = data[key]
        if not isinstance(value, str):
            raise self.error(f"{at}{key}", f"must be a string, got {value!r}")
        return value

    def str_list(
        self, data: dict[str, Any], key: str, *, at: str = ""
    ) -> tuple[str, ...]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.error(f"{at}{key}", f"must be a list of strings, got {value!r}")
        return tuple(value)

    def payment_method(self, data: dict[str, Any], key: str) -> PaymentMethod:
        value = data.get(key, PaymentMethod.wallet.value)
        try:
            return PaymentMethod(value)
        except ValueError:
            choices = ", ".join(m.value for m in PaymentMethod)
            raise self.error(key, f"must be one of {choices}, got {value!r}") from None
