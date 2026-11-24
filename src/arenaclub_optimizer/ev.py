"""EV of one Rip of a Slab Pack: Market EV, Buyback EV, Slab Safe EV, Value Coverage.

Pure arithmetic: no I/O. Vocabulary follows GLOSSARY.md. A card with no Market
Value counts as $0, so every EV figure is a lower bound when any value is missing.
"""

import math
from dataclasses import dataclass

from arenaclub_optimizer.cost import CostModel
from arenaclub_optimizer.model import (
    FLOAT_TOLERANCE,
    ChecklistCard,
    NormalizationResult,
    NormalizationStatus,
    SlabPack,
    TierType,
    normalize_hit_rates,
)

DEFAULT_K = 1.0
DEFAULT_BUYBACK_RATE = 0.9
DEFAULT_SLAB_SAFE_FLOOR = 0.8
DEFAULT_COVERAGE_THRESHOLD = 0.95

# Tier types whose missing values are called out in `missing_top_tiers`.
TOP_TIER_TYPES = frozenset({TierType.GRAIL, TierType.CHASE})


class HitRateRejectedError(ValueError):
    """A Slab Pack's Hit Rates are too far from summing to 1 to be used."""

    def __init__(self, pack_id: str, normalization: NormalizationResult) -> None:
        super().__init__(
            f"Slab Pack {pack_id!r}: Hit Rates sum to {normalization.raw_sum:.6f}, "
            f"drift {normalization.drift:.6f} is too large to normalise"
        )
        self.pack_id = pack_id
        self.normalization = normalization


@dataclass(frozen=True)
class EVResult:
    """The EV figures for one Rip of a Slab Pack.

    Amounts are in dollars; `*_pct` values are fractions of `cost` (0.1 = 10%).
    `slab_safe_ev` is None when the pack doesn't offer Slab Safe.
    """

    cost: float
    market_ev: float
    market_ev_pct: float
    buyback_ev: float
    buyback_ev_pct: float
    slab_safe_ev: float | None
    value_coverage: float
    rankable: bool
    lower_bound: bool
    missing_top_tiers: tuple[str, ...]
    normalization_status: NormalizationStatus


def evaluate_ev(
    pack: SlabPack,
    cost_model: CostModel,
    k: float = DEFAULT_K,
    buyback_rate: float = DEFAULT_BUYBACK_RATE,
    slab_safe_floor: float = DEFAULT_SLAB_SAFE_FLOOR,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> EVResult:
    """Compute the EV figures for one Rip of `pack`.

    Hit Rates are normalised first. AC Value is estimated as `k` x Market Value,
    Buyback pays `buyback_rate` of it, and Slab Safe guarantees at least
    `slab_safe_floor` of the list price.

    Raises HitRateRejectedError if normalisation rejects the Checklist, and
    ValueError for an out-of-range parameter.
    """
    _check_parameters(k, buyback_rate, slab_safe_floor, coverage_threshold)

    normalization = normalize_hit_rates(pack.checklist)
    if normalization.status is NormalizationStatus.REJECT:
        raise HitRateRejectedError(pack.pack_id, normalization)
    cards = normalization.cards

    cost = cost_model.cost(pack.price)
    buyback_per_dollar = buyback_rate * k

    market_proceeds = math.fsum(c.hit_rate * _value(c) for c in cards)
    buyback_proceeds = buyback_per_dollar * market_proceeds
    market_ev = market_proceeds - cost
    buyback_ev = buyback_proceeds - cost

    slab_safe_ev = None
    if pack.insurance_available:
        floor = slab_safe_floor * pack.price
        slab_safe_proceeds = math.fsum(
            c.hit_rate * max(floor, buyback_per_dollar * _value(c)) for c in cards
        )
        slab_safe_ev = slab_safe_proceeds - cost_model.cost(pack.price, slab_safe=True)

    unvalued = [c for c in cards if c.market_value is None]
    value_coverage = math.fsum(c.hit_rate for c in cards if c.market_value is not None)

    return EVResult(
        cost=cost,
        market_ev=market_ev,
        market_ev_pct=market_ev / cost,
        buyback_ev=buyback_ev,
        buyback_ev_pct=buyback_ev / cost,
        slab_safe_ev=slab_safe_ev,
        value_coverage=value_coverage,
        # FLOAT_TOLERANCE lets normalised Hit Rates adding up to e.g.
        # 0.9499999999999998 still count as 95%.
        rankable=value_coverage >= coverage_threshold - FLOAT_TOLERANCE,
        lower_bound=bool(unvalued),
        missing_top_tiers=_missing_top_tiers(unvalued),
        normalization_status=normalization.status,
    )


def _value(card: ChecklistCard) -> float:
    """A card's Market Value, or $0 when it has none."""
    return 0.0 if card.market_value is None else card.market_value


def _missing_top_tiers(unvalued: list[ChecklistCard]) -> tuple[str, ...]:
    """Names of Grail- and Chase-type Tiers with an unvalued card, each once."""
    names = (c.tier_name for c in unvalued if c.tier_type in TOP_TIER_TYPES)
    return tuple(dict.fromkeys(names))


def _check_parameters(
    k: float, buyback_rate: float, slab_safe_floor: float, coverage_threshold: float
) -> None:
    if not (math.isfinite(k) and k > 0):
        raise ValueError(f"k must be finite and > 0, got {k}")
    if not 0 < buyback_rate <= 1:
        raise ValueError(f"buyback_rate must be in (0, 1], got {buyback_rate}")
    if not 0 <= slab_safe_floor <= 1:
        raise ValueError(f"slab_safe_floor must be in [0, 1], got {slab_safe_floor}")
    if not 0 <= coverage_threshold <= 1:
        raise ValueError(
            f"coverage_threshold must be in [0, 1], got {coverage_threshold}"
        )
