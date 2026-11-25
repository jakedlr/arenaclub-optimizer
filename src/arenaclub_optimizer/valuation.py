"""Value the cards `rank` and `review` need, a Slab Pack at a time.

Picks the Slab Packs `ranking.rank` would evaluate and whose Hit Rates it
would accept, cheapest first, and lists each one's Checklist cards Grail
first, then Chase, then Lineup. All of them are resolved in one
`resolve_values` call, so a limited credit budget finishes one pack (getting
it to the Value Coverage threshold, and ranked) before starting the next,
and a provider that is down is asked only once. A pack already rankable
from Manual Overrides and fresh cached values goes last, so it doesn't hold
up the next one. `open_value_sources` opens the real value cache, credit
budget and providers. Vocabulary follows GLOSSARY.md; the value rules are in
ADR 0002 and docs/plan.md (Values).
"""

import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from arenaclub_optimizer._pulls import normalise_checklist
from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.config import Filters, Overrides
from arenaclub_optimizer.ev import DEFAULT_COVERAGE_THRESHOLD, meets_coverage
from arenaclub_optimizer.model import ChecklistCard, SlabPack, TierType
from arenaclub_optimizer.providers.cardvolume import CardVolumeProvider
from arenaclub_optimizer.providers.pokeprice import PokePriceProvider
from arenaclub_optimizer.ranking import matches_pack_filters, pack_name
from arenaclub_optimizer.values import (
    CreditBudget,
    ValueCache,
    ValueProvider,
    ValueResolution,
    is_valued_without_lookup,
    resolve_values,
)

__all__ = [
    "TIER_RANK",
    "ReviewEntry",
    "ValueSources",
    "card_priority",
    "cards_by_priority",
    "open_value_sources",
    "packs_by_price",
    "review_entries",
    "value_packs",
]

# The order tier types are valued in within a Slab Pack. Market Value is
# unknown before the lookup, so the tier is the best guess at which cards
# matter most; Grail and Chase first also keeps the lower-bound note away.
TIER_RANK: Mapping[TierType, int] = MappingProxyType(
    {TierType.GRAIL: 0, TierType.CHASE: 1, TierType.LINE_UP: 2}
)

type PackPairs = Iterable[tuple[SlabPack, PackRates]]


@dataclass(frozen=True)
class ValueSources:
    """What one run values cards with: the providers in the order they are
    asked, the value cache and the credit budget they share."""

    providers: tuple[ValueProvider, ...]
    cache: ValueCache
    budget: CreditBudget


@dataclass(frozen=True)
class ReviewEntry:
    """A card on the Needs-review List, with the Slab Packs it is in.
    `waiting` is True when a later run may value it without a Manual
    Override (see `values.NeedsReview`)."""

    card_id: str
    cert_number: str | None
    label: str
    reason: str
    slab_packs: tuple[str, ...]
    waiting: bool = False


def card_priority(card: ChecklistCard) -> tuple[int, float]:
    """Where `card` goes within its Slab Pack's valuation order: by tier
    type (`TIER_RANK`), then the higher Hit Rate first."""
    return (TIER_RANK[card.tier_type], -card.hit_rate)


def packs_by_price(packs_with_rates: PackPairs, filters: Filters) -> list[SlabPack]:
    """The Slab Packs to value, cheapest first, ties by pack id.

    These are the packs `rank` would evaluate (matching the Category and
    price filters, active, with Buyback enabled) whose Hit Rates it would
    accept: a pack whose Hit Rates are rejected or unusable is left out, so
    no credits go on it.
    """
    packs = [p for p in _evaluated(packs_with_rates, filters) if _usable(p)]
    return sorted(packs, key=lambda p: (p.price, p.pack_id))


def cards_by_priority(
    packs_with_rates: PackPairs,
    filters: Filters,
    *,
    valued: Callable[[ChecklistCard], bool] = lambda _: False,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> list[ChecklistCard]:
    """The cards to value, in the order a limited budget should go to them:
    a Slab Pack at a time, in `packs_by_price` order, each pack's cards in
    `card_priority` order (ties keep Checklist order).

    A pack whose cards `valued` (those valued without spending a credit)
    already reach `coverage_threshold` of its rescaled Hit Rates is already
    rankable: it moves behind every pack that isn't, so its remaining cards
    only get credits that are left over. A card in several packs is listed
    once, at its earliest position.
    """
    to_value: list[SlabPack] = []
    rankable: list[SlabPack] = []
    for pack in packs_by_price(packs_with_rates, filters):
        covered = meets_coverage(_known_coverage(pack, valued), coverage_threshold)
        (rankable if covered else to_value).append(pack)
    cards: dict[str, ChecklistCard] = {}
    for pack in [*to_value, *rankable]:
        for card in sorted(pack.checklist, key=card_priority):
            cards.setdefault(card.card_id, card)
    return list(cards.values())


def value_packs(
    packs_with_rates: PackPairs,
    filters: Filters,
    overrides: Overrides,
    sources: ValueSources,
    *,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> ValueResolution:
    """Resolve every card `rank` would need, in one `resolve_values` call:
    Manual Overrides first, then each provider in turn (see
    `values.resolve_values`), in `cards_by_priority` order. A card counts as
    valued for free when it has a Manual Override or a fresh cached quote
    (`values.is_valued_without_lookup`). The resolver checks the budget card
    by card, so a run stops spending once it runs out, and the next run
    carries on from the cache."""
    now = sources.budget.now()

    def valued(card: ChecklistCard) -> bool:
        return is_valued_without_lookup(
            card, sources.providers, overrides, sources.cache, now
        )

    cards = cards_by_priority(
        packs_with_rates,
        filters,
        valued=valued,
        coverage_threshold=coverage_threshold,
    )
    return resolve_values(
        cards, sources.providers, overrides, sources.cache, sources.budget, now
    )


def review_entries(
    packs_with_rates: PackPairs, filters: Filters, resolution: ValueResolution
) -> tuple[ReviewEntry, ...]:
    """The Needs-review List of `resolution`, in its order, each card with the
    names of the Slab Packs it is in among those `rank` would evaluate."""
    packs_of: dict[str, list[str]] = {}
    for pack in _evaluated(packs_with_rates, filters):
        for card in pack.checklist:
            names = packs_of.setdefault(card.card_id, [])
            if pack_name(pack) not in names:
                names.append(pack_name(pack))
    return tuple(
        ReviewEntry(
            card_id=item.card_id,
            cert_number=item.cert_number,
            label=item.label,
            reason=item.reason,
            slab_packs=tuple(packs_of.get(item.card_id, ())),
            waiting=item.waiting,
        )
        for item in resolution.needs_review
    )


@contextmanager
def open_value_sources(cache_dir: Path | None = None) -> Iterator[ValueSources]:
    """The value cache (in `cache_dir`, else the app's cache directory), a
    credit budget with the default daily limits, and the providers in ADR
    0002's order: PokéPrice Tracker (key from `POKEPRICE_API_KEY` or `.env`;
    without one it reports itself unavailable), then CardVolume, whose card
    index sits beside the value cache. Paid providers come after these once
    they exist. Everything is closed on exit."""
    with ValueCache(cache_dir) as cache:
        budget = CreditBudget(cache)
        with (
            PokePriceProvider.from_environment(cache, budget) as pokeprice,
            CardVolumeProvider(cache_dir=cache.directory, budget=budget) as cardvolume,
        ):
            yield ValueSources((pokeprice, cardvolume), cache, budget)


def _evaluated(packs_with_rates: PackPairs, filters: Filters) -> Iterator[SlabPack]:
    """Each pack `rank` would evaluate: matching the Category and price
    filters, active, and with Buyback enabled."""
    for pack, rates in packs_with_rates:
        if (
            matches_pack_filters(pack, filters)
            and pack.active
            and rates.buyback_rate is not None
        ):
            yield pack


def _usable(pack: SlabPack) -> bool:
    """Whether `rank` would accept `pack`'s Hit Rates."""
    try:
        normalise_checklist(pack)
    except ValueError:  # includes HitRateRejectedError
        return False
    return True


def _known_coverage(pack: SlabPack, valued: Callable[[ChecklistCard], bool]) -> float:
    """The share of `pack`'s rescaled Hit Rates whose cards are `valued`,
    as `ev.evaluate_ev` would compute Value Coverage. Call on usable packs."""
    cards = normalise_checklist(pack).cards
    return math.fsum(card.hit_rate for card in cards if valued(card))
