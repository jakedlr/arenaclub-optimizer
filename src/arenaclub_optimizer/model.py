"""Domain model for Slab Packs and their Checklists, and Hit Rate normalisation.

Pure data and arithmetic: no I/O and no HTTP. Vocabulary follows GLOSSARY.md.
"""

import dataclasses
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

DEFAULT_WARN_DRIFT = 0.005
DEFAULT_REJECT_DRIFT = 0.05

# Float noise allowed when comparing drift with a threshold, so that a raw sum
# such as 0.5 + 0.505 counts as a drift of exactly 0.005.
_DRIFT_TOLERANCE = 1e-9

_PERCENT_PATTERN = re.compile(r"\d+(\.\d+)?%?")


class TierType(StrEnum):
    """Arena Club's tier `type`."""

    GRAIL = "grail"
    CHASE = "chase"
    LINE_UP = "line_up"


class ValueConfidence(StrEnum):
    """How far a card's Market Value can be trusted."""

    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class ChecklistCard:
    """One card on a Slab Pack's Checklist.

    `hit_rate` is a probability from 0 to 1, not a percentage. `market_value` is
    in dollars, or None when no Market Value is known.
    """

    card_id: str
    label: str
    tier_name: str
    tier_type: TierType
    hit_rate: float
    grader: str
    grade: str
    market_value: float | None
    value_confidence: ValueConfidence | None


@dataclass(frozen=True)
class SlabPack:
    """A Slab Pack: one Price Level in one Category. `price` is the list price."""

    pack_id: str
    price_level: str
    category: str
    price: float
    active: bool
    checklist: tuple[ChecklistCard, ...]


class NormalizationStatus(StrEnum):
    """How far a Checklist's raw Hit Rates were from summing to 1."""

    OK = "ok"
    WARN = "warn"
    REJECT = "reject"


@dataclass(frozen=True)
class NormalizationResult:
    """The outcome of `normalize_hit_rates`.

    `cards` holds the normalised cards, whose Hit Rates sum to exactly 1. It is
    empty when `status` is REJECT, because those cards are not usable.
    """

    cards: tuple[ChecklistCard, ...]
    raw_sum: float
    drift: float
    status: NormalizationStatus


def normalize_hit_rates(
    cards: Iterable[ChecklistCard],
    *,
    warn_drift: float = DEFAULT_WARN_DRIFT,
    reject_drift: float = DEFAULT_REJECT_DRIFT,
) -> NormalizationResult:
    """Scale the cards' Hit Rates so they sum to exactly 1.

    The status is OK when drift = |raw sum - 1| is at most `warn_drift`, WARN
    when it is at most `reject_drift`, and REJECT otherwise.

    Raises ValueError for an empty Checklist or a Hit Rate that is zero,
    negative or not finite.
    """
    cards = tuple(cards)
    if not cards:
        raise ValueError("cannot normalise an empty Checklist")
    for card in cards:
        if not math.isfinite(card.hit_rate) or card.hit_rate <= 0:
            raise ValueError(
                f"card {card.card_id!r} has an invalid Hit Rate: {card.hit_rate!r}"
            )

    raw_sum = math.fsum(card.hit_rate for card in cards)
    drift = abs(raw_sum - 1)
    status = _status_for(drift, warn_drift, reject_drift)
    if status is NormalizationStatus.REJECT:
        return NormalizationResult((), raw_sum, drift, status)

    rates = _scale_to_one([card.hit_rate for card in cards], raw_sum)
    normalised = tuple(
        dataclasses.replace(card, hit_rate=rate)
        for card, rate in zip(cards, rates, strict=True)
    )
    return NormalizationResult(normalised, raw_sum, drift, status)


def _status_for(
    drift: float, warn_drift: float, reject_drift: float
) -> NormalizationStatus:
    if drift <= warn_drift + _DRIFT_TOLERANCE:
        return NormalizationStatus.OK
    if drift <= reject_drift + _DRIFT_TOLERANCE:
        return NormalizationStatus.WARN
    return NormalizationStatus.REJECT


def _scale_to_one(rates: list[float], raw_sum: float) -> list[float]:
    """Divide by `raw_sum`, then put the rounding residue on the largest rate.

    Dividing alone leaves the floats summing to 1 only approximately. Nudging
    the largest rate makes both the exact sum (`math.fsum`) and the plain
    left-to-right `sum` come out as 1.0.
    """
    scaled = [rate / raw_sum for rate in rates]
    largest = max(range(len(scaled)), key=scaled.__getitem__)
    for _ in range(8):
        exact = math.fsum(scaled)
        naive = sum(scaled)
        if exact == 1.0 and naive == 1.0:
            break
        residue = 1.0 - exact
        if residue == 0.0:
            # Exact sum is right but `sum` rounds away from it: step one ulp.
            residue = math.copysign(math.ulp(scaled[largest]), 1.0 - naive)
        scaled[largest] += residue
    return scaled


def percent_to_probability(text: str) -> float:
    """Parse Arena Club's `formattedHitRate` (a per-card percentage).

    "1.96" becomes 0.0196. Surrounding whitespace and a trailing "%" are
    allowed. Raises ValueError for anything else, or for a percentage above 100.
    """
    stripped = text.strip()
    if not _PERCENT_PATTERN.fullmatch(stripped):
        raise ValueError(f"malformed Hit Rate percentage: {text!r}")
    percent = Decimal(stripped.removesuffix("%"))
    if percent > 100:
        raise ValueError(f"Hit Rate percentage above 100: {text!r}")
    return float(percent / 100)
