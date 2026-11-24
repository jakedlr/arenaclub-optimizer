"""EV engine tests against a 3-card Slab Pack worked out by hand.

The pack (list price $100, wallet, no tax, so Cost = $100.00 and the Slab Safe
Cost = $110.00):

    card  tier    type     Hit Rate  Market Value  p*M
    G     Grail   grail    0.02      1500          30.0
    L1    Lineup  line_up  0.95        60          57.0
    L2    Lineup  line_up  0.03       100           3.0
                           ----                    ----
                           1.00                    90.0

The two Lineup cards have different Hit Rates so that one of them (L2) is rare
enough to leave Value Coverage above 95% when it has no Market Value. On Arena
Club a Tier shares one per-card Hit Rate; the EV engine doesn't rely on that.

Defaults: k = 1.0, buyback rate 0.9, Slab Safe floor 0.8 (so $80 here),
coverage threshold 0.95.
"""

import math

import pytest

from arenaclub_optimizer.cost import CostModel, PaymentMethod
from arenaclub_optimizer.ev import EVResult, HitRateRejectedError, evaluate_ev
from arenaclub_optimizer.model import (
    ChecklistCard,
    NormalizationStatus,
    SlabPack,
    TierType,
    ValueConfidence,
)

WALLET = CostModel()


def card(
    card_id: str,
    tier_name: str,
    tier_type: TierType,
    hit_rate: float,
    market_value: float | None,
) -> ChecklistCard:
    return ChecklistCard(
        card_id=card_id,
        label=f"Card {card_id}",
        tier_name=tier_name,
        tier_type=tier_type,
        hit_rate=hit_rate,
        grader="PSA",
        grade="10",
        market_value=market_value,
        value_confidence=None if market_value is None else ValueConfidence.HIGH,
    )


def make_pack(
    *,
    g: float | None = 1500.0,
    l1: float | None = 60.0,
    l2: float | None = 100.0,
    rates: tuple[float, float, float] = (0.02, 0.95, 0.03),
    l2_tier: tuple[str, TierType] = ("Lineup", TierType.LINE_UP),
    insurance_available: bool = True,
) -> SlabPack:
    rate_g, rate_l1, rate_l2 = rates
    return SlabPack(
        pack_id="pack-1",
        price_level="Gold",
        category="Pokemon",
        price=100.0,
        active=True,
        checklist=(
            card("G", "Grail", TierType.GRAIL, rate_g, g),
            card("L1", "Lineup", TierType.LINE_UP, rate_l1, l1),
            card("L2", *l2_tier, rate_l2, l2),
        ),
        insurance_available=insurance_available,
    )


# --- the full pack ---------------------------------------------------------


def test_full_pack_with_defaults():
    result = evaluate_ev(make_pack(), WALLET)

    assert isinstance(result, EVResult)
    assert result.cost == 100.00
    # Market: 0.02*1500 + 0.95*60 + 0.03*100 = 30 + 57 + 3 = 90; 90 - 100 = -10
    assert result.market_ev == pytest.approx(-10.0)
    assert result.market_ev_pct == pytest.approx(-0.10)  # -10 / 100
    # Buyback: 0.9 * 1.0 * 90 = 81; 81 - 100 = -19
    assert result.buyback_ev == pytest.approx(-19.0)
    assert result.buyback_ev_pct == pytest.approx(-0.19)  # -19 / 100
    # Slab Safe, floor 0.8*100 = 80:
    #   G:  max(80, 0.9*1500 = 1350) = 1350; * 0.02 = 27
    #   L1: max(80, 0.9*60   =   54) =   80; * 0.95 = 76
    #   L2: max(80, 0.9*100  =   90) =   90; * 0.03 =  2.7
    # 27 + 76 + 2.7 = 105.7; minus Slab Safe Cost 110 = -4.3
    assert result.slab_safe_ev == pytest.approx(-4.3)
    # Every card has a value: 0.02 + 0.95 + 0.03 = 1.0
    assert result.value_coverage == pytest.approx(1.0)
    assert result.rankable is True
    assert result.lower_bound is False
    assert result.missing_top_tiers == ()
    assert result.normalization_status is NormalizationStatus.OK


def test_k_other_than_one_scales_buyback_and_slab_safe_but_not_market():
    result = evaluate_ev(make_pack(), WALLET, k=1.5)

    # Market EV ignores k: still 90 - 100 = -10
    assert result.market_ev == pytest.approx(-10.0)
    assert result.market_ev_pct == pytest.approx(-0.10)
    # Buyback: 0.9 * 1.5 = 1.35 per $ of Market Value; 1.35 * 90 = 121.5
    # 121.5 - 100 = 21.5
    assert result.buyback_ev == pytest.approx(21.5)
    assert result.buyback_ev_pct == pytest.approx(0.215)  # 21.5 / 100
    # Slab Safe, floor 80:
    #   G:  max(80, 1.35*1500 = 2025) = 2025; * 0.02 = 40.5
    #   L1: max(80, 1.35*60   =   81) =   81; * 0.95 = 76.95  (floor no longer binds)
    #   L2: max(80, 1.35*100  =  135) =  135; * 0.03 =  4.05
    # 40.5 + 76.95 + 4.05 = 121.5; minus 110 = 11.5
    assert result.slab_safe_ev == pytest.approx(11.5)


def test_buyback_rate_and_slab_safe_floor_are_parameters():
    result = evaluate_ev(make_pack(), WALLET, buyback_rate=0.5, slab_safe_floor=0.7)

    # Buyback: 0.5 * 90 = 45; 45 - 100 = -55
    assert result.buyback_ev == pytest.approx(-55.0)
    # Slab Safe, floor 0.7*100 = 70:
    #   G:  max(70, 0.5*1500 = 750) = 750; * 0.02 = 15
    #   L1: max(70, 0.5*60   =  30) =  70; * 0.95 = 66.5
    #   L2: max(70, 0.5*100  =  50) =  70; * 0.03 =  2.1
    # 15 + 66.5 + 2.1 = 83.6; minus 110 = -26.4
    assert result.slab_safe_ev == pytest.approx(-26.4)


def test_cost_comes_from_the_cost_model():
    card_payment = CostModel(payment_method=PaymentMethod.card)

    result = evaluate_ev(make_pack(), card_payment)

    # Cost: 100 + 2.9% + $0.30 = 100 + 2.90 + 0.30 = 103.20
    assert result.cost == 103.20
    # Market: 90 - 103.20 = -13.20
    assert result.market_ev == pytest.approx(-13.2)
    assert result.market_ev_pct == pytest.approx(-13.2 / 103.2)
    # Buyback: 81 - 103.20 = -22.20
    assert result.buyback_ev == pytest.approx(-22.2)
    assert result.buyback_ev_pct == pytest.approx(-22.2 / 103.2)
    # Slab Safe Cost: 110 + 110*0.029 + 0.30 = 110 + 3.19 + 0.30 = 113.49
    # 105.7 - 113.49 = -7.79
    assert result.slab_safe_ev == pytest.approx(-7.79)


# --- missing Market Values -------------------------------------------------


def test_missing_lineup_value_lowers_coverage_but_stays_rankable():
    result = evaluate_ev(make_pack(l2=None), WALLET)

    # L2 counts as $0. Market: 30 + 57 + 0 = 87; 87 - 100 = -13
    assert result.market_ev == pytest.approx(-13.0)
    assert result.market_ev_pct == pytest.approx(-0.13)
    # Buyback: 0.9 * 87 = 78.3; 78.3 - 100 = -21.7
    assert result.buyback_ev == pytest.approx(-21.7)
    # Slab Safe: G 27, L1 76, L2 max(80, 0) = 80 * 0.03 = 2.4
    # 27 + 76 + 2.4 = 105.4; minus 110 = -4.6
    assert result.slab_safe_ev == pytest.approx(-4.6)
    # Coverage: 0.02 + 0.95 = 0.97 >= 0.95
    assert result.value_coverage == pytest.approx(0.97)
    assert result.rankable is True
    assert result.lower_bound is True
    # L2 is a Lineup card, so no top Tier is missing a value
    assert result.missing_top_tiers == ()


def test_missing_grail_value_names_the_tier():
    result = evaluate_ev(make_pack(g=None), WALLET)

    # G counts as $0. Market: 0 + 57 + 3 = 60; 60 - 100 = -40
    assert result.market_ev == pytest.approx(-40.0)
    assert result.market_ev_pct == pytest.approx(-0.40)
    # Buyback: 0.9 * 60 = 54; 54 - 100 = -46
    assert result.buyback_ev == pytest.approx(-46.0)
    assert result.buyback_ev_pct == pytest.approx(-0.46)
    # Slab Safe: G max(80, 0) = 80 * 0.02 = 1.6, L1 76, L2 2.7
    # 1.6 + 76 + 2.7 = 80.3; minus 110 = -29.7
    assert result.slab_safe_ev == pytest.approx(-29.7)
    # Coverage: 0.95 + 0.03 = 0.98
    assert result.value_coverage == pytest.approx(0.98)
    assert result.rankable is True
    assert result.lower_bound is True
    assert result.missing_top_tiers == ("Grail",)


def test_missing_chase_value_names_the_tier():
    pack = make_pack(l2=None, l2_tier=("Chase", TierType.CHASE))

    result = evaluate_ev(pack, WALLET)

    # Same numbers as the missing-Lineup case; only the Tier differs.
    assert result.value_coverage == pytest.approx(0.97)
    assert result.rankable is True
    assert result.missing_top_tiers == ("Chase",)


def test_missing_top_tiers_lists_each_tier_once_in_checklist_order():
    pack = SlabPack(
        pack_id="pack-2",
        price_level="Gold",
        category="Pokemon",
        price=100.0,
        active=True,
        checklist=(
            card("S", "Super Grail", TierType.GRAIL, 0.01, None),
            card("C1", "Chase", TierType.CHASE, 0.01, None),
            card("C2", "Chase", TierType.CHASE, 0.01, None),
            card("L", "Lineup", TierType.LINE_UP, 0.97, 50.0),
        ),
    )

    result = evaluate_ev(pack, WALLET)

    assert result.missing_top_tiers == ("Super Grail", "Chase")


def test_coverage_below_threshold_is_not_rankable():
    result = evaluate_ev(make_pack(l1=None), WALLET)

    # Coverage: 0.02 + 0.03 = 0.05 < 0.95
    assert result.value_coverage == pytest.approx(0.05)
    assert result.rankable is False
    assert result.lower_bound is True
    assert result.missing_top_tiers == ()
    # The figures are still reported, as lower bounds. Market: 30 + 0 + 3 = 33
    assert result.market_ev == pytest.approx(-67.0)  # 33 - 100


def test_coverage_exactly_at_threshold_is_rankable():
    result = evaluate_ev(make_pack(g=None, l2=None), WALLET)

    # Coverage: only L1, 0.95 == 0.95
    assert result.value_coverage == pytest.approx(0.95)
    assert result.rankable is True


def test_coverage_threshold_is_a_parameter():
    result = evaluate_ev(make_pack(l2=None), WALLET, coverage_threshold=0.98)

    # Coverage 0.97 < 0.98
    assert result.rankable is False


def test_coverage_a_float_ulp_under_threshold_is_still_rankable():
    # 27 valued cards of 0.95/27 and one unvalued card of 0.05, all scaled by
    # 1.01 so normalisation has work to do. Exactly 95% coverage, but the float
    # sum of the normalised rates comes out as 0.9499999999999998.
    valued = [
        card(str(i), "Lineup", TierType.LINE_UP, 0.95 / 27 * 1.01, 10.0)
        for i in range(27)
    ]
    unvalued = card("x", "Lineup", TierType.LINE_UP, 0.05 * 1.01, None)
    pack = SlabPack(
        pack_id="pack-3",
        price_level="Gold",
        category="Pokemon",
        price=100.0,
        active=True,
        checklist=(unvalued, *valued),
    )

    result = evaluate_ev(pack, WALLET)

    assert result.value_coverage == pytest.approx(0.95)
    assert result.rankable is True


# --- Slab Safe availability ------------------------------------------------


def test_pack_without_slab_safe_has_no_slab_safe_ev():
    result = evaluate_ev(make_pack(insurance_available=False), WALLET)

    assert result.slab_safe_ev is None
    # Everything else is unchanged from the full pack.
    assert result.market_ev == pytest.approx(-10.0)
    assert result.buyback_ev == pytest.approx(-19.0)
    assert result.value_coverage == pytest.approx(1.0)


def test_slab_pack_offers_slab_safe_by_default():
    pack = SlabPack(
        pack_id="pack-1",
        price_level="Gold",
        category="Pokemon",
        price=100.0,
        active=True,
        checklist=make_pack().checklist,
    )

    assert pack.insurance_available is True


# --- Hit Rate normalisation ------------------------------------------------


def test_warn_drift_is_normalised_before_computing():
    # Every rate is 98% of the base pack's: 0.0196 + 0.931 + 0.0294 = 0.98,
    # drift 0.02 -> warn. Normalising divides by 0.98 and gives back
    # 0.02, 0.95, 0.03, so every figure matches the full pack.
    result = evaluate_ev(make_pack(rates=(0.0196, 0.931, 0.0294)), WALLET)

    assert result.normalization_status is NormalizationStatus.WARN
    assert result.market_ev == pytest.approx(-10.0)
    assert result.buyback_ev == pytest.approx(-19.0)
    assert result.slab_safe_ev == pytest.approx(-4.3)
    assert result.value_coverage == pytest.approx(1.0)


def test_rejected_hit_rates_raise_a_domain_error():
    # 0.02 + 0.85 + 0.03 = 0.90, drift 0.10 > 0.05 -> reject
    pack = make_pack(rates=(0.02, 0.85, 0.03))

    with pytest.raises(HitRateRejectedError) as excinfo:
        evaluate_ev(pack, WALLET)

    assert excinfo.value.normalization.status is NormalizationStatus.REJECT
    assert excinfo.value.normalization.raw_sum == pytest.approx(0.90)
    assert "pack-1" in str(excinfo.value)


def test_domain_error_is_a_value_error():
    assert issubclass(HitRateRejectedError, ValueError)


# --- parameter validation --------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"k": 0.0},
        {"k": -1.0},
        {"k": math.inf},
        {"k": math.nan},
        {"buyback_rate": 0.0},
        {"buyback_rate": 1.01},
        {"slab_safe_floor": -0.1},
        {"slab_safe_floor": 1.01},
        {"coverage_threshold": -0.1},
        {"coverage_threshold": 1.01},
    ],
)
def test_out_of_range_parameters_raise(kwargs):
    with pytest.raises(ValueError):
        evaluate_ev(make_pack(), WALLET, **kwargs)
