"""`ev` and `risk` agree: same defaults, same checks, same reject error, and the
same per-Pull Buyback proceeds."""

import inspect
import math
import random

import pytest

from arenaclub_optimizer import ev, risk
from arenaclub_optimizer.cost import CostModel
from arenaclub_optimizer.ev import HitRateRejectedError, evaluate_ev
from arenaclub_optimizer.model import ChecklistCard, SlabPack, TierType
from arenaclub_optimizer.risk import evaluate_risk

WALLET = CostModel()


def card(card_id: str, hit_rate: float, market_value: float | None) -> ChecklistCard:
    return ChecklistCard(
        card_id=card_id,
        label=f"Card {card_id}",
        tier_name="Lineup",
        tier_type=TierType.LINE_UP,
        hit_rate=hit_rate,
        grader="PSA",
        grade="10",
        market_value=market_value,
        value_confidence=None,
    )


def pack(*cards: ChecklistCard, price: float = 100.0) -> SlabPack:
    return SlabPack(
        pack_id="pack-1",
        price_level="Gold",
        category="Pokemon",
        price=price,
        active=True,
        checklist=cards,
    )


def three_card_pack() -> SlabPack:
    return pack(card("G", 0.02, 1500), card("L1", 0.95, 60), card("L2", 0.03, 100))


# --- defaults --------------------------------------------------------------


def test_defaults_are_defined_once_and_shared():
    assert ev.DEFAULT_K == 1.0
    assert ev.DEFAULT_BUYBACK_RATE == 0.9
    assert risk.DEFAULT_K == ev.DEFAULT_K
    assert risk.DEFAULT_BUYBACK_RATE == ev.DEFAULT_BUYBACK_RATE


@pytest.mark.parametrize("evaluate", [evaluate_ev, evaluate_risk])
def test_signatures_use_the_shared_defaults(evaluate):
    parameters = inspect.signature(evaluate).parameters

    assert parameters["k"].default == ev.DEFAULT_K
    assert parameters["buyback_rate"].default == ev.DEFAULT_BUYBACK_RATE


# --- parameter checks ------------------------------------------------------


@pytest.mark.parametrize("evaluate", [evaluate_ev, evaluate_risk])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"k": 0.0},
        {"k": -1.0},
        {"k": math.inf},
        {"k": math.nan},
        {"buyback_rate": 0.0},
        {"buyback_rate": -0.1},
        {"buyback_rate": 1.01},
        {"buyback_rate": math.inf},
        {"buyback_rate": math.nan},
    ],
)
def test_both_modules_reject_the_same_k_and_buyback_rate(evaluate, kwargs):
    with pytest.raises(ValueError):
        evaluate(three_card_pack(), WALLET, **kwargs)


@pytest.mark.parametrize("evaluate", [evaluate_ev, evaluate_risk])
@pytest.mark.parametrize(
    "kwargs",
    [{"k": 1e-9}, {"k": 1e9}, {"buyback_rate": 1.0}, {"buyback_rate": 1e-9}],
)
def test_both_modules_accept_the_same_boundaries(evaluate, kwargs):
    evaluate(three_card_pack(), WALLET, **kwargs)


# --- rejected Hit Rates ----------------------------------------------------


def rejected_pack() -> SlabPack:
    # 0.6 + 0.6 = 1.2, drift 0.2 > 0.05
    return pack(card("a", 0.6, 200), card("b", 0.6, 50))


def test_both_modules_raise_the_same_error_with_the_same_message():
    with pytest.raises(HitRateRejectedError) as from_ev:
        evaluate_ev(rejected_pack(), WALLET)
    with pytest.raises(HitRateRejectedError) as from_risk:
        evaluate_risk(rejected_pack(), WALLET)

    assert str(from_risk.value) == str(from_ev.value)
    assert str(from_risk.value) == (
        "Slab Pack 'pack-1': Hit Rates sum to 1.200000, "
        "drift 0.200000 is too large to normalise"
    )
    assert from_risk.value.pack_id == "pack-1"
    assert from_risk.value.normalization.raw_sum == pytest.approx(1.2)


def test_risk_exports_the_same_error_class():
    assert risk.HitRateRejectedError is ev.HitRateRejectedError


# --- shared proceeds -------------------------------------------------------


def random_pack(rng: random.Random) -> SlabPack:
    size = rng.randint(1, 30)
    rates = [rng.uniform(1e-4, 1.0) for _ in range(size)]
    total = math.fsum(rates)
    cards = [
        card(
            str(i),
            rate / total * rng.uniform(0.999, 1.001),
            None if rng.random() < 0.1 else round(rng.uniform(0, 5000), 2),
        )
        for i, rate in enumerate(rates)
    ]
    return pack(*cards, price=round(rng.uniform(10, 1000), 2))


@pytest.mark.parametrize("seed", range(50))
def test_buyback_ev_comes_from_the_same_proceeds_as_risk(seed):
    # Buyback EV must be the expected value of exactly the per-Pull proceeds
    # risk works with, minus Cost: equal to the last bit, not just approximately.
    rng = random.Random(seed)
    slab_pack = random_pack(rng)
    k = rng.choice([1.0, 0.85, 0.95, 1.1, 1.3])
    buyback_rate = rng.choice([0.9, 0.8, 1.0])

    ev_result = evaluate_ev(slab_pack, WALLET, k=k, buyback_rate=buyback_rate)
    risk_result = evaluate_risk(slab_pack, WALLET, k=k, buyback_rate=buyback_rate)
    expected_proceeds = math.fsum(
        o.probability * o.proceeds for o in risk_result.outcomes
    )

    assert ev_result.buyback_ev == expected_proceeds - ev_result.cost


def test_proceeds_are_buyback_rate_times_k_times_market_value_per_pull():
    result = evaluate_risk(three_card_pack(), WALLET, k=1.3, buyback_rate=0.85)

    assert [o.proceeds for o in result.outcomes] == [
        0.85 * 1.3 * 1500,
        0.85 * 1.3 * 60,
        0.85 * 1.3 * 100,
    ]
