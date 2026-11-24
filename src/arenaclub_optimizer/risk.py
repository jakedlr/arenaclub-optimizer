"""Risk metrics and the High-variance Note for one Slab Pack.

Pure arithmetic: no I/O. Vocabulary follows GLOSSARY.md. "Proceeds" means a
Pull's Buyback proceeds, buyback_rate x k x Market Value, with a missing
Market Value counted as $0.
"""

import math
from dataclasses import dataclass
from typing import NamedTuple

from arenaclub_optimizer.cost import CostModel
from arenaclub_optimizer.model import (
    ChecklistCard,
    NormalizationStatus,
    SlabPack,
    normalize_hit_rates,
)

DEFAULT_BUYBACK_RATE = 0.9
DEFAULT_P_PROFIT_THRESHOLD = 0.25
DEFAULT_TOP_HEAVINESS_THRESHOLD = 0.5

# Top-heaviness looks at the rarest 1% of probability mass.
RAREST_MASS = 0.01

# Float noise allowed at a boundary, so that proceeds such as 0.9 x 0.95 x 14
# (11.969999999999999 in floats) meet a Cost of 11.97, and a cumulative
# probability of 0.1 summed five times counts as reaching 0.5.
_TOLERANCE = 1e-9

# Most decimal places a note uses for a percentage.
_MAX_PERCENT_DECIMALS = 4


class PullOutcome(NamedTuple):
    """One Pull's probability (its normalised Hit Rate) and its proceeds."""

    probability: float
    proceeds: float


@dataclass(frozen=True)
class RiskResult:
    """Risk metrics for one Rip of a Slab Pack.

    `std_ratio` and `median_ratio` are multiples of `cost`. `outcomes` holds
    every Pull's probability and proceeds, which `p_at_least` reads.
    """

    p_profit: float
    std_ratio: float
    median_ratio: float
    top_heaviness: float
    high_variance: bool
    note: str | None
    cost: float
    outcomes: tuple[PullOutcome, ...]

    def p_at_least(self, multiple: float) -> float:
        """P(proceeds >= multiple x Cost), e.g. 1.0, 2.0 or 10.0.

        Raises ValueError if `multiple` is negative or not finite.
        """
        if not (math.isfinite(multiple) and multiple >= 0):
            raise ValueError(f"multiple must be finite and >= 0, got {multiple!r}")
        return _probability_at_least(self.outcomes, multiple * self.cost)


def evaluate_risk(
    pack: SlabPack,
    cost_model: CostModel,
    k: float = 1.0,
    buyback_rate: float = DEFAULT_BUYBACK_RATE,
    p_profit_threshold: float = DEFAULT_P_PROFIT_THRESHOLD,
    top_heaviness_threshold: float = DEFAULT_TOP_HEAVINESS_THRESHOLD,
) -> RiskResult:
    """Compute the risk metrics and High-variance Note for one Rip of `pack`.

    Hit Rates are normalised first. Cost is `cost_model.cost(pack.price)`
    (without Slab Safe). A Slab Pack is high-variance when P(profit) is below
    `p_profit_threshold` or Top-heaviness is above `top_heaviness_threshold`.

    Raises ValueError if the Checklist can't be normalised, or if `k` or
    `buyback_rate` is out of range.
    """
    if not (math.isfinite(k) and k >= 0):
        raise ValueError(f"k must be finite and >= 0, got {k!r}")
    if not (math.isfinite(buyback_rate) and 0 <= buyback_rate <= 1):
        raise ValueError(f"buyback_rate must be in [0, 1], got {buyback_rate!r}")

    normalised = normalize_hit_rates(pack.checklist)
    if normalised.status is NormalizationStatus.REJECT:
        raise ValueError(
            f"Slab Pack {pack.pack_id!r} Hit Rates sum to {normalised.raw_sum}, "
            f"too far from 1 to use"
        )

    cost = cost_model.cost(pack.price)
    outcomes = tuple(
        PullOutcome(card.hit_rate, _buyback_proceeds(card, k, buyback_rate))
        for card in normalised.cards
    )

    p_profit = _probability_at_least(outcomes, cost)
    median_ratio = _weighted_median(outcomes) / cost
    top_heaviness = _top_heaviness(outcomes)

    # One clause per High-variance condition that fired.
    fired = []
    if p_profit < p_profit_threshold:
        fired.append(_p_profit_clause(p_profit, p_profit_threshold))
    if top_heaviness > top_heaviness_threshold:
        fired.append(_top_heaviness_clause(top_heaviness, top_heaviness_threshold))

    return RiskResult(
        p_profit=p_profit,
        std_ratio=_weighted_std(outcomes) / cost,
        median_ratio=median_ratio,
        top_heaviness=top_heaviness,
        high_variance=bool(fired),
        note=_note(fired, median_ratio) if fired else None,
        cost=cost,
        outcomes=outcomes,
    )


def _buyback_proceeds(card: ChecklistCard, k: float, buyback_rate: float) -> float:
    """A Pull's Buyback proceeds: buyback_rate x k x Market Value, or 0."""
    if card.market_value is None:
        return 0.0
    return buyback_rate * k * card.market_value


def _probability_at_least(outcomes: tuple[PullOutcome, ...], threshold: float) -> float:
    """P(proceeds >= threshold), counting a near-tie as meeting it."""
    total = math.fsum(
        outcome.probability
        for outcome in outcomes
        if outcome.proceeds >= threshold
        or math.isclose(outcome.proceeds, threshold, rel_tol=_TOLERANCE)
    )
    return min(total, 1.0)


def _weighted_std(outcomes: tuple[PullOutcome, ...]) -> float:
    """Probability-weighted (population) standard deviation of proceeds."""
    mean = _expected_proceeds(outcomes)
    variance = math.fsum(
        outcome.probability * (outcome.proceeds - mean) ** 2 for outcome in outcomes
    )
    return math.sqrt(variance)


def _weighted_median(outcomes: tuple[PullOutcome, ...]) -> float:
    """The smallest v such that P(proceeds <= v) >= 0.5.

    At an exact 0.5 boundary this is the lower of the two middle values, not
    their midpoint.
    """
    ordered = sorted(outcomes, key=lambda outcome: outcome.proceeds)
    cumulative = 0.0
    for outcome in ordered:
        cumulative += outcome.probability
        if cumulative >= 0.5 - _TOLERANCE:
            return outcome.proceeds
    return ordered[-1].proceeds


def _top_heaviness(outcomes: tuple[PullOutcome, ...]) -> float:
    """Share of expected proceeds from the rarest `RAREST_MASS` of probability.

    Pulls are taken rarest first, ties broken by higher proceeds first, and the
    Pull that crosses the boundary contributes only the part of its probability
    that fits. Returns 0 when expected proceeds are 0.
    """
    total = _expected_proceeds(outcomes)
    if total == 0:
        return 0.0
    ordered = sorted(
        outcomes, key=lambda outcome: (outcome.probability, -outcome.proceeds)
    )
    remaining = RAREST_MASS
    top = []
    for outcome in ordered:
        if remaining <= 0:
            break
        taken = min(outcome.probability, remaining)
        top.append(taken * outcome.proceeds)
        remaining -= taken
    return math.fsum(top) / total


def _expected_proceeds(outcomes: tuple[PullOutcome, ...]) -> float:
    return math.fsum(outcome.probability * outcome.proceeds for outcome in outcomes)


def _p_profit_clause(p_profit: float, threshold: float) -> str:
    if p_profit == 0:
        return "no pull returns at least your cost"
    return f"only {_percent(p_profit, threshold)} of pulls return at least your cost"


def _top_heaviness_clause(top_heaviness: float, threshold: float) -> str:
    return (
        f"{_percent(top_heaviness, threshold)} of expected value comes from "
        f"the rarest {RAREST_MASS:.0%} of pulls"
    )


def _note(fired: list[str], median_ratio: float) -> str:
    """The High-variance Note: the conditions that fired, then the median."""
    clauses = [*fired, f"a typical pull returns {median_ratio:.2f}× your cost"]
    text = "; ".join(clauses) + "."
    return text[0].upper() + text[1:]


def _percent(value: float, threshold: float) -> str:
    """Format `value` as a percentage with the fewest decimals that keep it
    distinct from both `threshold` and zero, so 0.2496 against a 25% threshold
    shows as "24.96%", not "25%"."""
    for decimals in range(_MAX_PERCENT_DECIMALS + 1):
        shown = f"{value:.{decimals}%}"
        if shown != f"{threshold:.{decimals}%}" and shown != f"{0:.{decimals}%}":
            return shown
    return shown
