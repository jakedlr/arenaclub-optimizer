"""Risk metrics, worked out by hand.

Proceeds = buyback_rate x k x Market Value (0.9 x 1.0 x MV by default), with a
missing Market Value counted as $0. Every pack below is priced so that the Cost
(wallet, no tax) equals the list price.
"""

import math

import pytest

from arenaclub_optimizer.cost import CostModel, PaymentMethod
from arenaclub_optimizer.model import ChecklistCard, SlabPack, TierType
from arenaclub_optimizer.risk import RiskResult, evaluate_risk

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


def pack(price: float, *cards: ChecklistCard) -> SlabPack:
    return SlabPack(
        pack_id="pack-1",
        price_level="Gold",
        category="Pokemon",
        price=price,
        active=True,
        checklist=cards,
    )


# --- the 3-card pack ---------------------------------------------------------
#
# Cost $100.
#   card   Hit Rate   MV      proceeds (0.9 x MV)   p x proceeds
#   A      0.005      4000    3600                  18
#   B      0.195       150     135                  26.325
#   C      0.8          50      45                  36
# Expected proceeds mu = 18 + 26.325 + 36 = 80.325


def three_card_pack() -> SlabPack:
    # Listed out of rarity order so the code has to sort.
    return pack(100, card("C", 0.8, 50), card("A", 0.005, 4000), card("B", 0.195, 150))


def test_three_card_pack_p_profit():
    # Proceeds >= 100 for A (3600) and B (135): 0.005 + 0.195 = 0.2
    assert evaluate_risk(three_card_pack(), WALLET).p_profit == pytest.approx(0.2)


def test_three_card_pack_std_ratio():
    # E[x^2] = 0.005 x 3600^2 + 0.195 x 135^2 + 0.8 x 45^2
    #        = 64800 + 3553.875 + 1620 = 69973.875
    # mu^2   = 80.325^2 = 6452.105625
    # Var    = 69973.875 - 6452.105625 = 63521.769375
    # sd     = sqrt(63521.769375) ~ 252.035; / cost 100 ~ 2.52035
    result = evaluate_risk(three_card_pack(), WALLET)

    assert result.std_ratio == pytest.approx(math.sqrt(63521.769375) / 100)
    assert result.std_ratio == pytest.approx(2.52035, abs=1e-5)


def test_three_card_pack_median_ratio():
    # Ascending proceeds: 45 (P 0.8). P(proceeds <= 45) = 0.8 >= 0.5, so the
    # median is 45; / 100 = 0.45
    assert evaluate_risk(three_card_pack(), WALLET).median_ratio == pytest.approx(0.45)


def test_three_card_pack_top_heaviness():
    # Rarest first: A (0.005, all of it), then B, of which only 0.01 - 0.005 =
    # 0.005 is taken. Top mass proceeds = 0.005 x 3600 + 0.005 x 135
    #                                   = 18 + 0.675 = 18.675
    # Top-heaviness = 18.675 / 80.325 ~ 0.232493
    result = evaluate_risk(three_card_pack(), WALLET)

    assert result.top_heaviness == pytest.approx(18.675 / 80.325)
    assert result.top_heaviness == pytest.approx(0.232493, abs=1e-6)


def test_three_card_pack_is_high_variance_on_p_profit_only():
    # p_profit 0.2 < 0.25 fires; top-heaviness 0.2325 is not > 0.5.
    result = evaluate_risk(three_card_pack(), WALLET)

    assert result.high_variance is True
    assert result.note == (
        "Only 20% of pulls return at least your cost; "
        "a typical pull returns 0.45× your cost."
    )


@pytest.mark.parametrize(
    ("multiple", "expected"),
    [
        # >= 100: A and B -> 0.2 (the same as p_profit)
        (1.0, 0.2),
        # >= 135: B's 135 counts exactly at the boundary -> 0.2
        (1.35, 0.2),
        # >= 200: A only -> 0.005
        (2.0, 0.005),
        # >= 1000: A (3600) only -> 0.005
        (10.0, 0.005),
        # >= 4000: nothing
        (40.0, 0.0),
        # >= 0: everything
        (0.0, 1.0),
    ],
)
def test_three_card_pack_p_at_least(multiple, expected):
    result = evaluate_risk(three_card_pack(), WALLET)

    assert result.p_at_least(multiple) == pytest.approx(expected)


def test_p_at_least_one_equals_p_profit():
    result = evaluate_risk(three_card_pack(), WALLET)

    assert result.p_at_least(1.0) == result.p_profit


@pytest.mark.parametrize("multiple", [-1.0, math.nan, math.inf])
def test_p_at_least_rejects_bad_multiple(multiple):
    result = evaluate_risk(three_card_pack(), WALLET)

    with pytest.raises(ValueError):
        result.p_at_least(multiple)


def test_result_is_a_risk_result():
    assert isinstance(evaluate_risk(three_card_pack(), WALLET), RiskResult)


# --- k, buyback_rate and Cost --------------------------------------------------


def test_k_scales_proceeds():
    # k = 0.5: proceeds A 0.9 x 0.5 x 4000 = 1800, B 67.5, C 22.5.
    # Only A is >= 100 -> p_profit 0.005. Median 22.5 / 100 = 0.225.
    result = evaluate_risk(three_card_pack(), WALLET, k=0.5)

    assert result.p_profit == pytest.approx(0.005)
    assert result.median_ratio == pytest.approx(0.225)


def test_buyback_rate_is_a_parameter():
    # buyback_rate 1.0: proceeds A 4000, B 150, C 50. Median 50 / 100 = 0.5.
    result = evaluate_risk(three_card_pack(), WALLET, buyback_rate=1.0)

    assert result.median_ratio == pytest.approx(0.5)


def test_cost_comes_from_the_cost_model():
    # Card payments: 100 x 1.029 + 0.30 = 103.20. B's 135 still clears it, so
    # p_profit stays 0.2, but the median is 45 / 103.20.
    card_model = CostModel(payment_method=PaymentMethod.card)

    result = evaluate_risk(three_card_pack(), card_model)

    assert result.cost == 103.20
    assert result.median_ratio == pytest.approx(45 / 103.20)


def test_proceeds_equal_to_cost_count_as_profit():
    # 0.9 x 1.0 x 100 = 90 against a Cost of 90: break-even counts.
    result = evaluate_risk(pack(90, card("a", 0.5, 100), card("b", 0.5, 10)), WALLET)

    assert result.p_profit == pytest.approx(0.5)


def test_break_even_survives_float_noise():
    # 0.9 x 0.95 x 14 = 11.97 exactly, but in floats it is 11.969999999999999,
    # one ulp short of the 11.97 Cost. It should still count as break-even.
    result = evaluate_risk(
        pack(11.97, card("a", 0.5, 14), card("b", 0.5, 1)), WALLET, k=0.95
    )

    assert result.p_profit == pytest.approx(0.5)


# --- Hit Rate normalisation ----------------------------------------------------


def test_hit_rates_are_normalised_first():
    # Raw 0.49 + 0.49 = 0.98 (a warn, not a reject) normalises to 0.5 / 0.5.
    # Proceeds 180 and 45 against Cost 100: p_profit = 0.5.
    result = evaluate_risk(pack(100, card("a", 0.49, 200), card("b", 0.49, 50)), WALLET)

    assert result.p_profit == pytest.approx(0.5)


def test_rejected_checklist_raises():
    # Raw sum 1.2 is more than 5% off.
    with pytest.raises(ValueError):
        evaluate_risk(pack(100, card("a", 0.6, 200), card("b", 0.6, 50)), WALLET)


# --- top-heaviness --------------------------------------------------------------


def test_top_heaviness_takes_a_fraction_of_the_card_crossing_one_percent():
    #   card   Hit Rate   MV     proceeds   p x proceeds
    #   X      0.004      1000   900        3.6
    #   Y      0.016       500   450        7.2
    #   Z      0.98         10     9        8.82
    # Total = 3.6 + 7.2 + 8.82 = 19.62
    # Rarest 1%: all of X (0.004) and 0.006 of Y's 0.016.
    #   3.6 + 0.006 x 450 = 3.6 + 2.7 = 6.3
    # Top-heaviness = 6.3 / 19.62 ~ 0.3211
    # (All of Y would give 10.8 / 19.62 ~ 0.55, wrongly over 0.5; none of Y
    # would give 3.6 / 19.62 ~ 0.18.)
    result = evaluate_risk(
        pack(100, card("Z", 0.98, 10), card("Y", 0.016, 500), card("X", 0.004, 1000)),
        WALLET,
    )

    assert result.top_heaviness == pytest.approx(6.3 / 19.62)
    assert result.top_heaviness == pytest.approx(0.3211, abs=1e-4)


def test_top_heaviness_breaks_hit_rate_ties_by_higher_proceeds_first():
    #   card   Hit Rate   MV     proceeds   p x proceeds
    #   L      0.008       100    90        0.72
    #   H      0.008      1000   900        7.2
    #   Z      0.984        10     9        8.856
    # Total = 0.72 + 7.2 + 8.856 = 16.776
    # L and H tie on Hit Rate, so H (higher proceeds) goes first: all 0.008 of
    # H, then 0.002 of L.  7.2 + 0.002 x 90 = 7.2 + 0.18 = 7.38
    # Top-heaviness = 7.38 / 16.776 ~ 0.4399
    # (L first would give 0.72 + 1.8 = 2.52 / 16.776 ~ 0.15.)
    result = evaluate_risk(
        pack(100, card("L", 0.008, 100), card("H", 0.008, 1000), card("Z", 0.984, 10)),
        WALLET,
    )

    assert result.top_heaviness == pytest.approx(7.38 / 16.776)
    assert result.top_heaviness == pytest.approx(0.4399, abs=1e-4)


def test_normalisation_residue_does_not_break_a_hit_rate_tie():
    # A uniform Checklist: 7 cards at 0.996/7 each (raw sum 0.996, drift 0.004,
    # ok), so each normalises to 1/7. MVs 100..700 give proceeds 90..630.
    # Normalisation puts its float residue on one card, which can leave it an
    # ulp rarer than the rest; that must not stop the tie going to the higher
    # proceeds.
    #   Total = (90 + 180 + ... + 630) / 7 = 2520 / 7 = 360
    #   Rarest 1%: all 0.01 from the 630 card (1/7 > 0.01): 0.01 x 630 = 6.3
    #   Top-heaviness = 6.3 / 360 = 0.0175
    # (Ordering the residue card first gives 0.01 x 90 / 360 = 0.0025.)
    cards = [card(str(mv), 0.996 / 7, mv) for mv in range(100, 800, 100)]

    result = evaluate_risk(pack(100, *cards), WALLET)

    assert result.top_heaviness == pytest.approx(0.0175)


def test_a_real_near_miss_is_not_profit():
    # 0.9 x 111.1 = 99.99 against a Cost of 100: a cent short, which is a
    # relative miss of 1e-4, far outside the float tolerance.
    result = evaluate_risk(pack(100, card("a", 0.5, 111.1), card("b", 0.5, 10)), WALLET)

    assert result.outcomes[0].proceeds == pytest.approx(99.99)
    assert result.p_profit == 0.0
    assert result.p_at_least(1.0) == 0.0


# --- median -----------------------------------------------------------------


def test_median_at_an_exact_half_takes_the_lower_value():
    # Proceeds 18 (MV 20) and 90 (MV 100), each with P 0.5.
    # P(proceeds <= 18) = 0.5, which is >= 0.5, so the median is 18, not the
    # midpoint 54.  18 / 100 = 0.18
    result = evaluate_risk(pack(100, card("hi", 0.5, 100), card("lo", 0.5, 20)), WALLET)

    assert result.median_ratio == pytest.approx(0.18)


def test_median_reaches_half_by_accumulating_cards():
    # Proceeds 90 (P 0.5), 9 (P 0.25), 18 (P 0.25). Ascending:
    #   9 -> cumulative 0.25;  18 -> 0.5 (>= 0.5, stop).  Median 18 -> 0.18
    result = evaluate_risk(
        pack(100, card("c", 0.5, 100), card("a", 0.25, 10), card("b", 0.25, 20)),
        WALLET,
    )

    assert result.median_ratio == pytest.approx(0.18)


def test_median_just_short_of_half_moves_to_the_next_value():
    # Proceeds 9 (P 0.25), 18 (P 0.24), 90 (P 0.51). Ascending:
    #   9 -> 0.25;  18 -> 0.49 (< 0.5);  90 -> 1.0.  Median 90 -> 0.9
    result = evaluate_risk(
        pack(100, card("a", 0.25, 10), card("b", 0.24, 20), card("c", 0.51, 100)),
        WALLET,
    )

    assert result.median_ratio == pytest.approx(0.9)


def test_median_survives_float_noise_at_the_half():
    # Proceeds 9 (P 0.03), 18 (P 0.29), 27 (P 0.18), 90 (P 0.5). Ascending:
    #   9 -> 0.03;  18 -> 0.32;  27 -> 0.5 exactly (>= 0.5, stop).
    # Median 27 -> 0.27. In floats 0.03 + 0.29 + 0.18 is 0.49999999999999994,
    # which must still count as reaching 0.5 (otherwise the median would be 90).
    result = evaluate_risk(
        pack(
            100,
            card("d", 0.5, 100),
            card("a", 0.03, 10),
            card("b", 0.29, 20),
            card("c", 0.18, 30),
        ),
        WALLET,
    )

    assert result.median_ratio == pytest.approx(0.27)


# --- the High-variance Note ---------------------------------------------------


def test_top_heaviness_alone_fires():
    #   card   Hit Rate   MV      proceeds   p x proceeds
    #   G      0.01       20000   18000      180
    #   M      0.99         120     108      106.92
    # Cost 100: both clear it, so p_profit = 1.0 (does not fire).
    # Rarest 1% is exactly G: 180 / (180 + 106.92) = 180 / 286.92 ~ 0.6274
    # (fires, > 0.5). Median 108 -> 1.08x.
    result = evaluate_risk(
        pack(100, card("M", 0.99, 120), card("G", 0.01, 20000)), WALLET
    )

    assert result.p_profit == pytest.approx(1.0)
    assert result.top_heaviness == pytest.approx(180 / 286.92)
    assert result.high_variance is True
    assert result.note == (
        "63% of expected value comes from the rarest 1% of pulls; "
        "a typical pull returns 1.08× your cost."
    )


def test_both_conditions_fire():
    #   card   Hit Rate   MV      proceeds   p x proceeds
    #   G      0.01       20000   18000      180
    #   M      0.99          50      45      44.55
    # p_profit = 0.01 (< 0.25, fires).
    # Top-heaviness = 180 / 224.55 ~ 0.8016 (> 0.5, fires). Median 45 -> 0.45x.
    result = evaluate_risk(
        pack(100, card("M", 0.99, 50), card("G", 0.01, 20000)), WALLET
    )

    assert result.p_profit == pytest.approx(0.01)
    assert result.top_heaviness == pytest.approx(180 / 224.55)
    assert result.high_variance is True
    assert result.note == (
        "Only 1% of pulls return at least your cost; "
        "80% of expected value comes from the rarest 1% of pulls; "
        "a typical pull returns 0.45× your cost."
    )


def balanced_pack() -> SlabPack:
    #   card   Hit Rate   MV    proceeds   p x proceeds
    #   a      0.5        200   180        90
    #   b      0.5         50    45        22.5
    # p_profit = 0.5. Hit Rates tie, so a (higher proceeds) is "rarest":
    # top-heaviness = 0.01 x 180 / 112.5 = 1.8 / 112.5 = 0.016.
    return pack(100, card("b", 0.5, 50), card("a", 0.5, 200))


def test_neither_condition_fires():
    result = evaluate_risk(balanced_pack(), WALLET)

    assert result.p_profit == pytest.approx(0.5)
    assert result.top_heaviness == pytest.approx(0.016)
    assert result.high_variance is False
    assert result.note is None


def test_p_profit_threshold_is_configurable():
    # p_profit 0.5 < 0.6 fires. Median 45 -> 0.45x.
    result = evaluate_risk(balanced_pack(), WALLET, p_profit_threshold=0.6)

    assert result.high_variance is True
    assert result.note == (
        "Only 50% of pulls return at least your cost; "
        "a typical pull returns 0.45× your cost."
    )


def test_top_heaviness_threshold_is_configurable():
    # Top-heaviness 0.016 > 0.01 fires; 0.016 rounds to 2%, which already
    # reads as over the 1% threshold.
    result = evaluate_risk(balanced_pack(), WALLET, top_heaviness_threshold=0.01)

    assert result.high_variance is True
    assert result.note == (
        "2% of expected value comes from the rarest 1% of pulls; "
        "a typical pull returns 0.45× your cost."
    )


def test_conditions_at_exactly_the_thresholds_do_not_fire():
    #   card   Hit Rate   MV    proceeds   p x proceeds
    #   a      0.25       200   180        45
    #   b      0.75        50    45        33.75
    # p_profit = 0.25, which is not < 0.25.
    # Top-heaviness = 0.01 x 180 / 78.75 = 1.8 / 78.75 ~ 0.0229; with the
    # threshold set to exactly the computed value, it is not > the threshold.
    at_threshold = pack(100, card("a", 0.25, 200), card("b", 0.75, 50))
    first = evaluate_risk(at_threshold, WALLET)
    assert first.top_heaviness == pytest.approx(1.8 / 78.75)

    result = evaluate_risk(
        at_threshold, WALLET, top_heaviness_threshold=first.top_heaviness
    )

    assert result.p_profit == 0.25
    assert result.high_variance is False
    assert result.note is None


def test_note_shows_enough_digits_not_to_contradict_the_threshold():
    #   card   Hit Rate   MV    proceeds
    #   a      0.2496     200   180
    #   b      0.7504      50    45
    # p_profit = 0.2496 < 0.25 fires. Rounded to a whole percent it would read
    # "25%", which looks like it meets the threshold, so it shows 24.96%.
    result = evaluate_risk(
        pack(100, card("a", 0.2496, 200), card("b", 0.7504, 50)), WALLET
    )

    assert result.note == (
        "Only 24.96% of pulls return at least your cost; "
        "a typical pull returns 0.45× your cost."
    )


# --- missing Market Values ----------------------------------------------------


def test_missing_value_counts_as_zero():
    # Proceeds 0 (missing, P 0.5) and 180 (P 0.5).
    # P(proceeds <= 0) = 0.5 -> median 0. p_profit = 0.5.
    # mu = 90; Var = 0.5 x 90^2 + 0.5 x 90^2 = 8100; sd 90 -> 0.9
    result = evaluate_risk(pack(100, card("a", 0.5, None), card("b", 0.5, 200)), WALLET)

    assert result.median_ratio == 0.0
    assert result.p_profit == pytest.approx(0.5)
    assert result.std_ratio == pytest.approx(0.9)


def test_all_missing_values_do_not_divide_by_zero():
    # Every Pull's proceeds are $0: p_profit 0, sd 0, median 0, and total
    # expected proceeds are 0, so top-heaviness is defined as 0.
    # p_profit 0 < 0.25 fires.
    result = evaluate_risk(
        pack(100, card("a", 0.005, None), card("b", 0.995, None)), WALLET
    )

    assert result.p_profit == 0.0
    assert result.std_ratio == 0.0
    assert result.median_ratio == 0.0
    assert result.top_heaviness == 0.0
    assert result.p_at_least(2.0) == 0.0
    assert result.high_variance is True
    assert result.note == (
        "No pull returns at least your cost; a typical pull returns 0.00× your cost."
    )


# --- argument checks ----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"k": -0.1},
        {"k": math.nan},
        {"k": math.inf},
        {"buyback_rate": -0.1},
        {"buyback_rate": 1.1},
        {"buyback_rate": math.nan},
    ],
)
def test_bad_k_or_buyback_rate_raises(kwargs):
    with pytest.raises(ValueError):
        evaluate_risk(three_card_pack(), WALLET, **kwargs)
