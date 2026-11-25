"""Resolve each card's Market Value from Manual Overrides, the cache and the
value providers, and collect the Needs-review List.

Makes no HTTP calls itself; providers do. Vocabulary follows GLOSSARY.md and
the rules are in ADR 0002 and docs/plan.md (Values).
"""

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from arenaclub_optimizer.values.cache import CacheEntry, ValueCache
from arenaclub_optimizer.values.quote import (
    OVERRIDE_SOURCE,
    NotFound,
    ProviderUnavailable,
    QuoteResult,
    ValueProvider,
    ValueQuote,
    require_aware,
)

UNMATCHED = "unmatched"
UNMATCHED_BY_ALL = "unmatched by all providers"
NO_PRICE_AT_GRADE = "no price at grade"
NO_RAW_PRICE = "no raw price"
NO_PROVIDERS = "no value providers"
# The oldest stale quote the resolver falls back on.
DEFAULT_STALE_MAX_AGE = timedelta(days=30)


class ValueOrigin(StrEnum):
    """Where a resolved value came from in this run."""

    OVERRIDE = "override"
    CACHE = "cache"
    FETCHED = "fetched"
    # Low confidence; refresh pending. A stale cached quote from a provider
    # that couldn't be asked this run (budget exhausted or unavailable).
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
    *,
    stale_max_age: timedelta = DEFAULT_STALE_MAX_AGE,
) -> ValueResolution:
    """Find a Market Value for each card, in card order, each card id once.

    Call it once per run, with every card to value in priority order (the
    cards that matter most first). A provider that raises ProviderUnavailable
    is not asked again within the call, and a limited budget is spent on the
    cards that come first; calling once per pack would ask a provider that is
    down again for every pack.

    For each card, the first of these wins:
    1. its Manual Override (source "override", high confidence);
    2. a fresh cached quote from one of `providers`, in provider order;
    3. each provider in turn, skipping one with a fresh cached NotFound or
       Unmatched, one that raised ProviderUnavailable earlier in the call,
       and one whose cost the budget can't cover (its `estimated_cost` for
       the card if it has one, else its `credit_cost`). Every answer is
       cached;
    4. a stale cached quote, at most `stale_max_age` old, from a provider
       skipped in step 3 for budget or unavailability, returned at low
       confidence (the cached row is left as it is). A provider that
       answered NotFound or Unmatched has replaced its stale quote.
    A card still without a value goes on the Needs-review List with the
    reason.

    A card graded by Arena Club is looked up raw (grader and grade None) and
    its quote marked low confidence, since no provider prices Arena Club
    grades. `now` (time-zone aware) defaults to `budget`'s clock, which the
    providers' spending also uses; pass a different `now` only with a budget
    whose clock agrees.

    Raises ValueError if a provider returns a quote whose source isn't its name.
    """
    now = budget.now() if now is None else require_aware(now, "now")
    run = _Run(providers, overrides, cache, budget, now, stale_max_age)
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


@dataclass
class _Asked:
    """What asking the providers for one card came to."""

    quote: ValueQuote | None = None
    # Each provider's (name, cause) for not quoting, in provider order.
    causes: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    # Providers that couldn't be asked (budget exhausted or unavailable).
    skipped: list[str] = dataclasses.field(default_factory=list)


class _Run:
    """The state of one `resolve_values` call."""

    def __init__(
        self,
        providers: Sequence[ValueProvider],
        overrides: Overrides,
        cache: ValueCache,
        budget: CreditBudget,
        now: datetime,
        stale_max_age: timedelta,
    ) -> None:
        self.providers = providers
        self.overrides = overrides
        self.cache = cache
        self.budget = budget
        self.now = now
        self.stale_max_age = stale_max_age
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
            if entry is not None and entry.fresh and _is_quote(entry):
                self._found(card, entry.result, ValueOrigin.CACHE)
                return

        asked = self._ask_providers(card, entries)
        if asked.quote is not None:
            self._found(card, asked.quote, ValueOrigin.FETCHED)
            return

        stale = self._stale_fallback(entries[name] for name in asked.skipped)
        if stale is not None:
            low = dataclasses.replace(stale, confidence=ValueConfidence.LOW)
            self._found(card, low, ValueOrigin.STALE_CACHE)
            return
        self.needs_review.append(
            NeedsReview(
                card.card_id, card.cert_number, card.label, _reason(asked.causes)
            )
        )

    def _ask_providers(
        self, card: ChecklistCard, entries: Mapping[str, CacheEntry | None]
    ) -> _Asked:
        """Ask each provider in turn until one quotes, caching every answer."""
        raw = is_arena_club_graded(card.identity)
        identity = (
            dataclasses.replace(card.identity, grader=None, grade=None)
            if raw
            else card.identity
        )
        asked = _Asked()
        for provider in self.providers:
            entry = entries[provider.name]
            if entry is not None and entry.fresh:
                asked.causes.append((provider.name, _negative_cause(entry.result, raw)))
                continue
            skip = self._cannot_ask(provider, identity)
            if skip is None:
                try:
                    result = provider.quote(identity)
                except ProviderUnavailable as error:
                    self.unavailable[provider.name] = error
                    skip = str(error)
            if skip is not None:
                asked.causes.append((provider.name, skip))
                asked.skipped.append(provider.name)
                continue
            if isinstance(result, ValueQuote) and raw:
                result = dataclasses.replace(result, confidence=ValueConfidence.LOW)
            self.cache.put(card.card_id, provider.name, result, fetched_at=self.now)
            if isinstance(result, ValueQuote):
                asked.quote = result
                return asked
            asked.causes.append((provider.name, _negative_cause(result, raw)))
        return asked

    def _cannot_ask(
        self, provider: ValueProvider, identity: CardIdentity
    ) -> str | None:
        """Why `provider` can't be asked about `identity` now, or None if it
        can. The budget must cover the provider's `estimated_cost` for the
        identity if it has one, else its `credit_cost`."""
        stopped = self.unavailable.get(provider.name)
        if stopped is not None:
            return str(stopped)
        estimate = getattr(provider, "estimated_cost", None)
        cost = provider.credit_cost if estimate is None else estimate(identity)
        if not self.budget.can_spend(provider.name, cost, now=self.now):
            return str(self.budget.exhausted_error(now=self.now))
        return None

    def _stale_fallback(
        self, entries: Iterable[CacheEntry | None]
    ) -> ValueQuote | None:
        """The first cached quote among `entries` no older than the limit."""
        for entry in entries:
            if entry is None or not _is_quote(entry):
                continue
            if self.now - entry.fetched_at <= self.stale_max_age:
                return entry.result
        return None

    def _found(
        self, card: ChecklistCard, quote: ValueQuote, origin: ValueOrigin
    ) -> None:
        self.values[card.card_id] = quote
        self.origins[card.card_id] = origin


def _is_quote(entry: CacheEntry) -> bool:
    return isinstance(entry.result, ValueQuote)


def _negative_cause(result: QuoteResult, raw: bool) -> str:
    if isinstance(result, NotFound):
        return NO_RAW_PRICE if raw else NO_PRICE_AT_GRADE
    return UNMATCHED


def _reason(causes: list[tuple[str, str]]) -> str:
    """One Needs-review reason from each provider's (name, cause).

    One cause shared by every provider stands alone, with "unmatched" read
    as "unmatched by <provider>" for a single provider and "unmatched by all
    providers" otherwise. Mixed causes are each followed by the providers
    they apply to, in provider order.
    """
    if not causes:
        return NO_PROVIDERS
    by_cause: dict[str, list[str]] = {}
    for name, cause in causes:
        by_cause.setdefault(cause, []).append(name)
    if len(by_cause) == 1:
        [(cause, names)] = by_cause.items()
        if cause != UNMATCHED:
            return cause
        return f"unmatched by {names[0]}" if len(names) == 1 else UNMATCHED_BY_ALL
    return "; ".join(
        f"{cause} ({', '.join(names)})" for cause, names in by_cause.items()
    )
