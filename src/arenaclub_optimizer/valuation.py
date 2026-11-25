"""Value the cards that `rank` and `review` need, most relevant first.

Picks the Checklist cards of the Slab Packs `ranking.rank` would evaluate,
orders them by Hit Rate × tier weight, and resolves them all in one
`resolve_values` call, so a limited credit budget is spent where it matters
most and a provider that is down is asked only once. `open_value_sources`
opens the real value cache, credit budget and providers. Vocabulary follows
GLOSSARY.md; the value rules are in ADR 0002.
"""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.config import Filters, Overrides
from arenaclub_optimizer.model import ChecklistCard, SlabPack, TierType
from arenaclub_optimizer.providers.cardvolume import CardVolumeProvider
from arenaclub_optimizer.providers.pokeprice import PokePriceProvider
from arenaclub_optimizer.ranking import matches_pack_filters, pack_name
from arenaclub_optimizer.values import (
    CreditBudget,
    ValueCache,
    ValueProvider,
    ValueResolution,
    resolve_values,
)

__all__ = [
    "TIER_WEIGHTS",
    "ReviewEntry",
    "ValueSources",
    "card_priority",
    "cards_by_priority",
    "open_value_sources",
    "review_entries",
    "value_packs",
]

# Roughly how much more a card in each tier is worth than a Lineup card, so
# Hit Rate × weight approximates a card's share of a pack's EV before any
# value is known. In practice this puts Grail and Chase cards first: a Grail
# card at 0.0002% (2 × 10⁻⁶ × 10⁶ = 2) still comes before a Lineup card at
# 2% (0.02).
TIER_WEIGHTS: Mapping[TierType, float] = MappingProxyType(
    {TierType.GRAIL: 1_000_000.0, TierType.CHASE: 1_000.0, TierType.LINE_UP: 1.0}
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


def card_priority(card: ChecklistCard) -> float:
    """How much valuing `card` matters: its Hit Rate × its tier's weight."""
    return card.hit_rate * TIER_WEIGHTS[card.tier_type]


def cards_by_priority(
    packs_with_rates: PackPairs, filters: Filters
) -> list[ChecklistCard]:
    """The Checklist cards of the Slab Packs `rank` would evaluate, highest
    `card_priority` first.

    Those packs match the Category and price filters, are active and have
    Buyback enabled. Ties keep pack order, then Checklist order.
    """
    packs = _evaluated(packs_with_rates, filters)
    cards = [card for pack in packs for card in pack.checklist]
    return sorted(cards, key=card_priority, reverse=True)


def value_packs(
    packs_with_rates: PackPairs,
    filters: Filters,
    overrides: Overrides,
    sources: ValueSources,
) -> ValueResolution:
    """Resolve every card `rank` would need, in one `resolve_values` call:
    Manual Overrides first, then each provider in turn (see
    `values.resolve_values`), in `cards_by_priority` order."""
    return resolve_values(
        cards_by_priority(packs_with_rates, filters),
        sources.providers,
        overrides,
        sources.cache,
        sources.budget,
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
