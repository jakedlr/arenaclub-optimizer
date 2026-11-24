"""Risk metrics and the High-variance Note for one Slab Pack. Pure; no I/O."""

from dataclasses import dataclass

from arenaclub_optimizer.cost import CostModel
from arenaclub_optimizer.model import SlabPack


@dataclass(frozen=True)
class RiskResult:
    p_profit: float
    std_ratio: float
    median_ratio: float
    top_heaviness: float
    high_variance: bool
    note: str | None
    cost: float
    outcomes: tuple[tuple[float, float], ...]

    def p_at_least(self, multiple: float) -> float:
        raise NotImplementedError


def evaluate_risk(
    pack: SlabPack,
    cost_model: CostModel,
    k: float = 1.0,
    buyback_rate: float = 0.9,
    p_profit_threshold: float = 0.25,
    top_heaviness_threshold: float = 0.5,
) -> RiskResult:
    raise NotImplementedError
