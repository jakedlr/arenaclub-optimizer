"""Top Results: evaluate, filter and order Slab Packs.

Pure: no I/O and no HTTP. Vocabulary follows GLOSSARY.md; the rules are in
docs/plan.md (Ranking, Filters, Value Coverage, Hit Rates).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from arenaclub_optimizer._pulls import HitRateRejectedError
from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.config import Config, Filters
from arenaclub_optimizer.cost import CostModel
from arenaclub_optimizer.ev import EVResult, evaluate_ev
from arenaclub_optimizer.model import (
    DEFAULT_REJECT_DRIFT,
    DEFAULT_WARN_DRIFT,
    NormalizationStatus,
    SkippedPack,
    SlabPack,
    ValueConfidence,
    category_key,
)
from arenaclub_optimizer.risk import RiskResult, evaluate_risk
from arenaclub_optimizer.values import ValueResolution, apply_values

# SkippedPack and category_key live in `model`, so that `fetch` can use them
# without depending on ranking; they are re-exported here for callers.
__all__ = [
    "RankedPack",
    "RankingReport",
    "SkippedPack",
    "UnrankablePack",
    "category_key",
    "matches_pack_filters",
    "pack_name",
    "rank",
]

BUYBACK_DISABLED = "Buyback offer disabled, so there is no Buyback EV"


@dataclass(frozen=True)
class RankedPack:
    """One Top Result. `pack` has the resolved Market Values applied.

    `p_1x`, `p_2x` and `p_10x` are the probabilities that a Rip's Buyback
    proceeds reach 1x, 2x and 10x its Cost.
    """

    pack: SlabPack
    ev: EVResult
    risk: RiskResult
    p_1x: float
    p_2x: float
    p_10x: float
    notes: tuple[str, ...]


@dataclass(frozen=True)
class UnrankablePack:
    """A Slab Pack below the Value Coverage threshold, so not ranked."""

    pack: SlabPack
    value_coverage: float


@dataclass(frozen=True)
class RankingReport:
    """The Top Results, best first, plus the packs left out and why, and the
    value resolution the packs were ranked with."""

    results: tuple[RankedPack, ...]
    unrankable: tuple[UnrankablePack, ...]
    skipped: tuple[SkippedPack, ...]
    generated_at: datetime
    values: ValueResolution


def pack_name(pack: SlabPack) -> str:
    """How a Slab Pack reads to the user: Price Level, then Category."""
    return f"{pack.price_level} {pack.category}"


def rank(
    packs_with_rates: Iterable[tuple[SlabPack, PackRates]],
    config: Config,
    values: ValueResolution,
    *,
    skipped: Iterable[SkippedPack] = (),
    generated_at: datetime | None = None,
) -> RankingReport:
    """Evaluate each Slab Pack with its own rates and return the Top Results.

    Each card's Market Value and confidence come from `values` (see
    `values.resolve_values`, which also applies the Manual Overrides); a card
    it has no value for keeps its own. Packs outside the Category or price
    filters are dropped first, then
    inactive (sold-out) packs. A pack with Buyback disabled, or whose Hit Rates
    are rejected or unusable, goes under `skipped`; one below Value Coverage
    goes under `unrankable`. The rest are filtered on EV %, P(profit) and the
    High-variance Note, sorted by Buyback EV % descending, and cut to the top
    N. `skipped` carries packs already skipped upstream (e.g. while fetching)
    into the report, ahead of the ones skipped here. `generated_at` defaults
    to now, in UTC.
    """
    filters = config.filters
    cost_model = CostModel(config.payment_method, config.tax_rate)
    results: list[RankedPack] = []
    unrankable: list[UnrankablePack] = []
    skips = list(skipped)

    for pack, rates in packs_with_rates:
        if not matches_pack_filters(pack, filters) or not pack.active:
            continue
        if rates.buyback_rate is None:
            skips.append(_skip(pack, BUYBACK_DISABLED))
            continue
        pack = apply_values(pack, values)
        try:
            ev = evaluate_ev(
                pack,
                cost_model,
                k=config.k,
                buyback_rate=rates.buyback_rate,
                slab_safe_floor=rates.slab_safe_floor,
                coverage_threshold=config.coverage_threshold,
            )
        except HitRateRejectedError as error:
            skips.append(_skip(pack, _rejected_reason(error)))
            continue
        except ValueError as error:
            skips.append(_skip(pack, str(error)))
            continue
        if not ev.rankable:
            unrankable.append(UnrankablePack(pack, ev.value_coverage))
            continue
        risk = evaluate_risk(
            pack,
            cost_model,
            k=config.k,
            buyback_rate=rates.buyback_rate,
            p_profit_threshold=config.p_profit_threshold,
            top_heaviness_threshold=config.top_heaviness_threshold,
        )
        if _matches_metric_filters(ev, risk, filters):
            results.append(_ranked(pack, ev, risk))

    results.sort(key=_order)
    return RankingReport(
        results=tuple(results[: filters.top]),
        unrankable=tuple(unrankable),
        skipped=tuple(skips),
        generated_at=generated_at or datetime.now(UTC),
        values=values,
    )


def matches_pack_filters(pack: SlabPack, filters: Filters) -> bool:
    """Whether `pack` passes the Category and price filters."""
    if filters.categories:
        wanted = {category_key(c) for c in filters.categories}
        if category_key(pack.category) not in wanted:
            return False
    if filters.price_min is not None and pack.price < filters.price_min:
        return False
    return filters.price_max is None or pack.price <= filters.price_max


def _matches_metric_filters(ev: EVResult, risk: RiskResult, filters: Filters) -> bool:
    if filters.min_ev_pct is not None and ev.buyback_ev_pct < filters.min_ev_pct:
        return False
    if filters.min_p_profit is not None and risk.p_profit < filters.min_p_profit:
        return False
    return not (filters.exclude_high_variance and risk.high_variance)


def _ranked(pack: SlabPack, ev: EVResult, risk: RiskResult) -> RankedPack:
    return RankedPack(
        pack=pack,
        ev=ev,
        risk=risk,
        p_1x=risk.p_at_least(1.0),
        p_2x=risk.p_at_least(2.0),
        p_10x=risk.p_at_least(10.0),
        notes=_notes(pack, ev, risk),
    )


def _notes(pack: SlabPack, ev: EVResult, risk: RiskResult) -> tuple[str, ...]:
    notes = []
    if risk.note is not None:
        notes.append(risk.note)
    if ev.missing_top_tiers:
        tiers = ", ".join(ev.missing_top_tiers)
        notes.append(f"Lower bound: some {tiers} cards have no Market Value.")
    if ev.normalization_status is NormalizationStatus.WARN:
        notes.append(
            f"Hit Rates were more than {DEFAULT_WARN_DRIFT:.1%} off 100% "
            "and were rescaled."
        )
    low = sum(c.value_confidence is ValueConfidence.LOW for c in pack.checklist)
    if low:
        noun = "card" if low == 1 else "cards"
        notes.append(f"{low} {noun} valued at low confidence.")
    return tuple(notes)


def _order(result: RankedPack) -> tuple[float, str, str, str]:
    """Buyback EV % descending; ties by name, then id, so output is stable."""
    pack = result.pack
    return (-result.ev.buyback_ev_pct, pack.category, pack.price_level, pack.pack_id)


def _skip(pack: SlabPack, reason: str) -> SkippedPack:
    return SkippedPack(pack.pack_id, pack_name(pack), reason)


def _rejected_reason(error: HitRateRejectedError) -> str:
    raw_sum = error.normalization.raw_sum
    return (
        f"Hit Rates rejected: they sum to {raw_sum:.4%}, "
        f"more than {DEFAULT_REJECT_DRIFT:.0%} off 100%"
    )
