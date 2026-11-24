"""Pull arithmetic shared by `ev` and `risk`.

Pure arithmetic: no I/O. Vocabulary follows GLOSSARY.md. Both modules take their
defaults, parameter checks and Hit Rate normalisation from here, so they accept
the same inputs and reject the same Checklists with the same error.
"""

import math

from arenaclub_optimizer.model import (
    ChecklistCard,
    NormalizationResult,
    NormalizationStatus,
    SlabPack,
    normalize_hit_rates,
)

DEFAULT_K = 1.0
DEFAULT_BUYBACK_RATE = 0.9


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


def market_value_or_zero(card: ChecklistCard) -> float:
    """A card's Market Value, or $0 when it has none."""
    return 0.0 if card.market_value is None else card.market_value
