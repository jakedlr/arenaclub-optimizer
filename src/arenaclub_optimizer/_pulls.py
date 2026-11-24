"""Pull arithmetic shared by `ev` and `risk`.

Pure arithmetic: no I/O. Vocabulary follows GLOSSARY.md. Both modules take their
defaults, parameter checks, Hit Rate normalisation and per-Pull Buyback proceeds
from here, so they accept the same inputs, reject the same Checklists with the
same error, and Buyback EV is built from exactly the proceeds risk works with.
"""

import math
from collections.abc import Iterable
from typing import NamedTuple

from arenaclub_optimizer.model import (
    ChecklistCard,
    NormalizationResult,
    NormalizationStatus,
    SlabPack,
    normalize_hit_rates,
)

DEFAULT_K = 1.0
DEFAULT_BUYBACK_RATE = 0.9


class Pull(NamedTuple):
    """One possible Pull of a Rip.

    `hit_rate` is the normalised Hit Rate, `market_value` is $0 when the card
    has none, and `proceeds` is its Buyback proceeds, buyback_rate x k x
    `market_value`.
    """

    hit_rate: float
    market_value: float
    proceeds: float


class HitRateRejectedError(ValueError):
    """A Slab Pack's Hit Rates are too far from summing to 1 to be used."""

    def __init__(self, pack_id: str, normalization: NormalizationResult) -> None:
        super().__init__(
            f"Slab Pack {pack_id!r}: Hit Rates sum to {normalization.raw_sum:.6f}, "
            f"drift {normalization.drift:.6f} is too large to normalise"
        )
        self.pack_id = pack_id
        self.normalization = normalization


def check_k_and_buyback_rate(k: float, buyback_rate: float) -> None:
    """Raise ValueError unless `k` is finite and > 0 and `buyback_rate` is in
    (0, 1]."""
    if not (math.isfinite(k) and k > 0):
        raise ValueError(f"k must be finite and > 0, got {k!r}")
    if not (math.isfinite(buyback_rate) and 0 < buyback_rate <= 1):
        raise ValueError(f"buyback_rate must be in (0, 1], got {buyback_rate!r}")


def normalise_checklist(pack: SlabPack) -> NormalizationResult:
    """Normalise `pack`'s Hit Rates, raising HitRateRejectedError on REJECT."""
    normalization = normalize_hit_rates(pack.checklist)
    if normalization.status is NormalizationStatus.REJECT:
        raise HitRateRejectedError(pack.pack_id, normalization)
    return normalization


def pulls(
    cards: Iterable[ChecklistCard], k: float, buyback_rate: float
) -> tuple[Pull, ...]:
    """Each card's Pull, in Checklist order. Pass normalised cards."""
    return tuple(_pull(card, k, buyback_rate) for card in cards)


def expected_proceeds(outcomes: Iterable[Pull]) -> float:
    """Expected Buyback proceeds of one Rip: the sum of hit_rate x proceeds."""
    return math.fsum(pull.hit_rate * pull.proceeds for pull in outcomes)


def _pull(card: ChecklistCard, k: float, buyback_rate: float) -> Pull:
    value = 0.0 if card.market_value is None else card.market_value
    return Pull(card.hit_rate, value, buyback_rate * k * value)
