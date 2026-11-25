"""Value Quotes and the interface every value provider implements.

No I/O. Vocabulary follows GLOSSARY.md; the value rules are in ADR 0002.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from arenaclub_optimizer.model import CardIdentity, ValueConfidence

# The `source` of a Value Quote that comes from a Manual Override.
OVERRIDE_SOURCE = "override"


@dataclass(frozen=True)
class ValueQuote:
    """A card's Market Value as one source states it.

    `market_value` is in dollars. `as_of` is the date the source gives for the
    figure. `sales_count` is the number of sales behind it, when known.
    `citation` is the attribution text the source asks for, if any.

    Raises ValueError if `market_value` is not finite or is negative, if
    `source` is empty, or if `sales_count` is negative.
    """

    market_value: float
    source: str
    as_of: date
    sales_count: int | None
    confidence: ValueConfidence
    citation: str | None = None

    def __post_init__(self) -> None:
        value = self.market_value
        if isinstance(value, bool) or not (math.isfinite(value) and value >= 0):
            raise ValueError(f"invalid Market Value in a Value Quote: {value!r}")
        if not self.source.strip():
            raise ValueError("a Value Quote needs a non-empty source")
        if self.sales_count is not None and self.sales_count < 0:
            raise ValueError(f"sales_count must be >= 0, got {self.sales_count!r}")
        object.__setattr__(self, "confidence", ValueConfidence(self.confidence))


@dataclass(frozen=True)
class NotFound:
    """The provider knows the card but has no price at the asked grade."""


@dataclass(frozen=True)
class Unmatched:
    """The provider couldn't identify the card."""


type QuoteResult = ValueQuote | NotFound | Unmatched


class ProviderUnavailable(Exception):
    """A provider can't answer now: a limit is reached, it is down, or it
    can't run at all (e.g. no API key). The resolver stops asking it for the
    rest of the run.

    `retry_after` is when it is worth asking again, if known (time-zone
    aware). `reason` says why, e.g. "daily limit reached".
    """

    def __init__(
        self, retry_after: datetime | None = None, *, reason: str = "unavailable"
    ) -> None:
        self.retry_after = retry_after
        self.reason = reason
        text = reason
        if retry_after is not None:
            text += f", retry after {format_utc(retry_after)}"
        super().__init__(text)


class ValueProvider(Protocol):
    """A source of Market Values.

    `name` identifies the provider; its quotes carry it as their `source`, and
    the cache and credit budget are keyed by it. `credit_cost` is what one
    `quote` can cost in credits at most (0 means free). The resolver only
    asks a provider when the budget can cover that; the provider spends its
    actual credits itself, with `CreditBudget.try_spend`, before each request.

    `quote` returns a ValueQuote, NotFound or Unmatched. A grade of None in
    the identity asks for the raw (ungraded) value. It raises
    ProviderUnavailable when a limit is reached or the provider is down.
    """

    name: str
    credit_cost: int

    def quote(self, identity: CardIdentity) -> QuoteResult: ...


def format_utc(when: datetime) -> str:
    """`when` in UTC to the minute, e.g. "2026-11-25 00:00 UTC"."""
    return f"{when.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def require_aware(when: datetime, name: str) -> datetime:
    """`when`, or ValueError if it has no time zone."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"{name} must have a time zone, got {when!r}")
    return when
