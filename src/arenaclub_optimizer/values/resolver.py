"""Resolve each card's Market Value from Manual Overrides, the cache and the
value providers, and collect the Needs-review List.

Makes no HTTP calls itself; providers do. Vocabulary follows GLOSSARY.md and
the rules are in ADR 0002 and docs/plan.md (Values).
"""

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from arenaclub_optimizer.arena_mapping import ARENA_CLUB_GRADER
from arenaclub_optimizer.config import Overrides
from arenaclub_optimizer.model import (
    CardIdentity,
    ChecklistCard,
    SlabPack,
    ValueConfidence,
)
from arenaclub_optimizer.values.budget import CreditBudget
from arenaclub_optimizer.values.cache import ValueCache
from arenaclub_optimizer.values.quote import (
    OVERRIDE_SOURCE,
    NotFound,
    ProviderUnavailable,
    QuoteResult,
    ValueProvider,
    ValueQuote,
    format_utc,
    require_aware,
)

UNMATCHED = "unmatched"
UNMATCHED_BY_ALL = "unmatched by all providers"
NO_PRICE_AT_GRADE = "no price at grade"
NO_RAW_PRICE = "no raw price"
NO_PROVIDERS = "no value providers"


class ValueOrigin(StrEnum):
    """Where a resolved value came from in this run."""

    OVERRIDE = "override"
    CACHE = "cache"
    FETCHED = "fetched"
    # A stale cached quote, kept because no provider could be asked for a
    # fresh one (budget exhausted or provider unavailable).
    STALE_CACHE = "stale_cache"


@dataclass(frozen=True)
class NeedsReview:
    """A card on the Needs-review List, with why it has no Market Value."""

    card_id: str
    cert_number: str | None
    label: str
    reason: str


@dataclass(frozen=True)
class ValueResolution:
    """The outcome of `resolve_values`.

    `values` maps card id to its Value Quote, and `origins` says where each
    came from. `needs_review` lists the cards left without a value, in card
    order. `unavailable` maps each provider that raised ProviderUnavailable
    during the run to its reason.
    """

    values: Mapping[str, ValueQuote]
    needs_review: tuple[NeedsReview, ...]
    origins: Mapping[str, ValueOrigin]
    unavailable: Mapping[str, str]


def resolve_values(
    cards: Iterable[ChecklistCard],
    providers: Sequence[ValueProvider],
    overrides: Overrides,
    cache: ValueCache,
    budget: CreditBudget,
    now: datetime | None = None,
) -> ValueResolution:
    """Find a Market Value for each card, in card order, each card id once.

    For each card, the first of these wins:
    1. its Manual Override (source "override", high confidence);
    2. a fresh cached quote from one of `providers`, in provider order;
    3. each provider in turn, skipping one with a fresh cached NotFound or
       Unmatched, one that raised ProviderUnavailable earlier in the run, and
       one whose `credit_cost` the budget can't cover. Every answer is cached.
    A card without a value then falls back to a stale cached quote if any
    provider was skipped for budget or unavailability, and otherwise goes on
    the Needs-review List with the reason.

    A card graded by Arena Club is looked up raw (grader and grade None) and
    its quote marked low confidence, since no provider prices Arena Club
    grades. `now` (time-zone aware) defaults to the current time.

    Raises ValueError if a provider returns a quote whose source isn't its name.
    """
    now = datetime.now(UTC) if now is None else require_aware(now, "now")
    run = _Run(providers, overrides, cache, budget, now)
    seen: set[str] = set()
    for card in cards:
        if card.card_id not in seen:
            seen.add(card.card_id)
            run.resolve(card)
    return ValueResolution(
        values=MappingProxyType(run.values),
        needs_review=tuple(run.needs_review),
        origins=MappingProxyType(run.origins),
        unavailable=MappingProxyType(
            {name: str(error) for name, error in run.unavailable.items()}
        ),
    )


def apply_values(pack: SlabPack, resolution: ValueResolution) -> SlabPack:
    """`pack` with each resolved card's Market Value and confidence set.

    Cards without a resolved value are left as they are.
    """
    cards = []
    for card in pack.checklist:
        quote = resolution.values.get(card.card_id)
        if quote is not None:
            card = dataclasses.replace(
                card,
                market_value=quote.market_value,
                value_confidence=quote.confidence,
            )
        cards.append(card)
    return dataclasses.replace(pack, checklist=tuple(cards))


def is_arena_club_graded(identity: CardIdentity) -> bool:
    grader = identity.grader
    return grader is not None and grader.casefold() == ARENA_CLUB_GRADER


class _Run:
    """The state of one `resolve_values` call."""

    def __init__(
        self,
        providers: Sequence[ValueProvider],
        overrides: Overrides,
        cache: ValueCache,
        budget: CreditBudget,
        now: datetime,
    ) -> None:
        self.providers = providers
        self.overrides = overrides
        self.cache = cache
        self.budget = budget
        self.now = now
        self.values: dict[str, ValueQuote] = {}
        self.origins: dict[str, ValueOrigin] = {}
        self.needs_review: list[NeedsReview] = []
        self.unavailable: dict[str, ProviderUnavailable] = {}

    def resolve(self, card: ChecklistCard) -> None:
        override = self.overrides.lookup(card.card_id, card.cert_number)
        if override is not None:
            quote = ValueQuote(
                market_value=override,
                source=OVERRIDE_SOURCE,
                as_of=self.now.date(),
                sales_count=None,
                confidence=ValueConfidence.HIGH,
            )
            self._found(card, quote, ValueOrigin.OVERRIDE)
            return

        entries = {
            p.name: self.cache.get(card.card_id, p.name, now=self.now)
            for p in self.providers
        }
        for entry in entries.values():
            if (
                entry is not None
                and entry.fresh
                and isinstance(entry.result, ValueQuote)
            ):
                self._found(card, entry.result, ValueOrigin.CACHE)
                return

        raw = is_arena_club_graded(card.identity)
        identity = (
            dataclasses.replace(card.identity, grader=None, grade=None)
            if raw
            else card.identity
        )
        causes: list[tuple[str, str]] = []
        could_not_ask = False
        for provider in self.providers:
            entry = entries[provider.name]
            if entry is not None and entry.fresh:
                causes.append((provider.name, _negative_cause(entry.result, raw)))
                continue
            cause = self._cannot_ask(provider)
            if cause is not None:
                causes.append((provider.name, cause))
                could_not_ask = True
                continue
            try:
                result = provider.quote(identity)
            except ProviderUnavailable as error:
                self.unavailable[provider.name] = error
                causes.append((provider.name, str(error)))
                could_not_ask = True
                continue
            if isinstance(result, ValueQuote) and raw:
                result = dataclasses.replace(result, confidence=ValueConfidence.LOW)
            self.cache.put(card.card_id, provider.name, result, fetched_at=self.now)
            if isinstance(result, ValueQuote):
                self._found(card, result, ValueOrigin.FETCHED)
                return
            causes.append((provider.name, _negative_cause(result, raw)))

        if could_not_ask:
            for entry in entries.values():
                if entry is not None and isinstance(entry.result, ValueQuote):
                    self._found(card, entry.result, ValueOrigin.STALE_CACHE)
                    return
        self.needs_review.append(
            NeedsReview(card.card_id, card.cert_number, card.label, _reason(causes))
        )

    def _cannot_ask(self, provider: ValueProvider) -> str | None:
        """Why `provider` can't be asked now, or None if it can."""
        stopped = self.unavailable.get(provider.name)
        if stopped is not None:
            return str(stopped)
        if not self.budget.can_spend(provider.name, provider.credit_cost, now=self.now):
            resets = format_utc(self.budget.resets_at(now=self.now))
            return f"budget exhausted, retry after {resets}"
        return None

    def _found(
        self, card: ChecklistCard, quote: ValueQuote, origin: ValueOrigin
    ) -> None:
        self.values[card.card_id] = quote
        self.origins[card.card_id] = origin


def _negative_cause(result: QuoteResult, raw: bool) -> str:
    if isinstance(result, NotFound):
        return NO_RAW_PRICE if raw else NO_PRICE_AT_GRADE
    return UNMATCHED


def _reason(causes: list[tuple[str, str]]) -> str:
    """One Needs-review reason from each provider's (name, cause).

    One cause shared by every provider stands alone ("unmatched" reads as
    "unmatched by all providers"); otherwise each distinct cause is followed
    by the providers it applies to, in provider order.
    """
    if not causes:
        return NO_PROVIDERS
    by_cause: dict[str, list[str]] = {}
    for name, cause in causes:
        by_cause.setdefault(cause, []).append(name)
    if len(by_cause) == 1:
        [cause] = by_cause
        return UNMATCHED_BY_ALL if cause == UNMATCHED else cause
    return "; ".join(
        f"{cause} ({', '.join(names)})" for cause, names in by_cause.items()
    )
