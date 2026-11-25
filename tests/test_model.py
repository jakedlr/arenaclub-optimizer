import dataclasses
import math
import random

import pytest

from arenaclub_optimizer import model
from arenaclub_optimizer.model import (
    ChecklistCard,
    NormalizationStatus,
    SlabPack,
    TierType,
    ValueConfidence,
    normalize_hit_rates,
    percent_to_probability,
)


def make_card(card_id: str, hit_rate: float, **overrides) -> ChecklistCard:
    fields = {
        "card_id": card_id,
        "label": f"Card {card_id}",
        "tier_name": "Lineup",
        "tier_type": TierType.LINE_UP,
        "hit_rate": hit_rate,
        "grader": "PSA",
        "grade": "10",
        "market_value": 50.0,
        "value_confidence": ValueConfidence.HIGH,
    }
    fields.update(overrides)
    return ChecklistCard(**fields)


def cards_with_rates(*rates: float) -> tuple[ChecklistCard, ...]:
    return tuple(make_card(str(i), rate) for i, rate in enumerate(rates))


# --- model -----------------------------------------------------------------


def test_cert_number_defaults_to_none():
    assert make_card("a", 1.0).cert_number is None
    assert make_card("a", 1.0, cert_number="123").cert_number == "123"


def test_tier_type_values_match_arena_club_tier_type():
    assert TierType("grail") is TierType.GRAIL
    assert TierType("chase") is TierType.CHASE
    assert TierType("line_up") is TierType.LINE_UP
    assert {t.value for t in TierType} == {"grail", "chase", "line_up"}


def test_value_confidence_values():
    assert {c.value for c in ValueConfidence} == {"high", "low"}


def test_checklist_card_is_immutable():
    card = make_card("a", 0.5)

    with pytest.raises(dataclasses.FrozenInstanceError):
        card.hit_rate = 0.6


def test_checklist_card_allows_missing_market_value_and_confidence():
    card = make_card("a", 0.5, market_value=None, value_confidence=None)

    assert card.market_value is None
    assert card.value_confidence is None


@pytest.mark.parametrize("market_value", [0.0, 0.01, 1500.0])
def test_checklist_card_allows_zero_or_positive_market_value(market_value):
    assert make_card("a", 0.5, market_value=market_value).market_value == market_value


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf, -0.01, -100.0])
def test_checklist_card_rejects_invalid_market_value(bad_value):
    with pytest.raises(ValueError, match="Market Value"):
        make_card("a", 0.5, market_value=bad_value)


def test_slab_pack_is_immutable_and_holds_a_tuple_checklist():
    pack = SlabPack(
        pack_id="pack-1",
        price_level="Gold",
        category="Pokemon",
        price=100.0,
        active=True,
        checklist=cards_with_rates(0.5, 0.5),
    )

    assert isinstance(pack.checklist, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pack.price = 50.0


# --- normalize_hit_rates ---------------------------------------------------


def test_exact_sum_is_ok_with_zero_drift():
    result = normalize_hit_rates(cards_with_rates(0.25, 0.25, 0.5))

    assert result.status is NormalizationStatus.OK
    assert result.raw_sum == 1.0
    assert result.drift == 0.0
    assert [c.hit_rate for c in result.cards] == [0.25, 0.25, 0.5]


def test_small_drift_is_ok_and_normalised():
    result = normalize_hit_rates(cards_with_rates(0.3, 0.3, 0.402))

    assert result.status is NormalizationStatus.OK
    assert result.raw_sum == pytest.approx(1.002)
    assert result.drift == pytest.approx(0.002)
    assert sum(c.hit_rate for c in result.cards) == 1.0


def test_warn_case_is_still_normalised():
    cards = cards_with_rates(0.2, 0.3, 0.48)  # raw sum 0.98

    result = normalize_hit_rates(cards)

    assert result.status is NormalizationStatus.WARN
    assert result.raw_sum == pytest.approx(0.98)
    assert result.drift == pytest.approx(0.02)
    assert sum(c.hit_rate for c in result.cards) == 1.0
    assert [c.hit_rate for c in result.cards] == pytest.approx(
        [0.2 / 0.98, 0.3 / 0.98, 0.48 / 0.98]
    )


def test_normalisation_keeps_every_other_field():
    cards = cards_with_rates(0.2, 0.3, 0.48)

    result = normalize_hit_rates(cards)

    for before, after in zip(cards, result.cards, strict=True):
        assert dataclasses.replace(after, hit_rate=before.hit_rate) == before


def test_reject_case_reports_drift_and_no_usable_cards():
    result = normalize_hit_rates(cards_with_rates(0.5, 0.6))  # raw sum 1.1

    assert result.status is NormalizationStatus.REJECT
    assert result.raw_sum == pytest.approx(1.1)
    assert result.drift == pytest.approx(0.1)
    assert result.cards == ()


@pytest.mark.parametrize(
    ("rates", "expected"),
    [
        # drift of exactly 0.005 is still ok
        ((0.5, 0.505), NormalizationStatus.OK),
        ((0.5, 0.495), NormalizationStatus.OK),
        # just past 0.005 is warn
        ((0.5, 0.5051), NormalizationStatus.WARN),
        ((0.5, 0.4949), NormalizationStatus.WARN),
        # drift of exactly 0.05 is still warn
        ((0.5, 0.55), NormalizationStatus.WARN),
        ((0.5, 0.45), NormalizationStatus.WARN),
        # just past 0.05 is reject
        ((0.5, 0.5501), NormalizationStatus.REJECT),
        ((0.5, 0.4499), NormalizationStatus.REJECT),
    ],
)
def test_threshold_boundaries(rates, expected):
    assert normalize_hit_rates(cards_with_rates(*rates)).status is expected


def test_thresholds_are_parameters():
    cards = cards_with_rates(0.5, 0.52)  # drift 0.02

    loose = normalize_hit_rates(cards, warn_drift=0.03)
    strict = normalize_hit_rates(cards, reject_drift=0.01)

    assert loose.status is NormalizationStatus.OK
    assert strict.status is NormalizationStatus.REJECT


def test_realistic_checklist_sums_to_exactly_one():
    # Gold Pokemon example from docs/research/arenaclub-api.md.
    rates = (
        [percent_to_probability("0.0002")]
        + [percent_to_probability("0.02")] * 3
        + [percent_to_probability("0.25")] * 8
        + [percent_to_probability("1.96")] * 50
    )

    result = normalize_hit_rates(cards_with_rates(*rates))

    assert result.raw_sum == pytest.approx(1.000602)
    assert result.status is NormalizationStatus.OK
    assert sum(c.hit_rate for c in result.cards) == 1.0
    assert math.fsum(c.hit_rate for c in result.cards) == 1.0


@pytest.mark.parametrize("seed", range(200))
def test_normalised_hit_rates_always_sum_to_exactly_one(seed):
    rng = random.Random(seed)
    rates = [rng.uniform(1e-6, 1.0) for _ in range(rng.randint(1, 80))]
    target = rng.uniform(0.96, 1.04)
    total = math.fsum(rates)
    rates = [rate / total * target for rate in rates]

    result = normalize_hit_rates(cards_with_rates(*rates))

    assert sum(c.hit_rate for c in result.cards) == 1.0
    assert math.fsum(c.hit_rate for c in result.cards) == 1.0
    assert all(c.hit_rate > 0 for c in result.cards)


def test_raises_if_exact_sum_correction_does_not_converge(monkeypatch):
    # Ten rates of 0.1 plus 0.001 scale to a float sum of 0.9999999999999999, so
    # they need at least one correction step; allowing none forces the
    # non-convergence path.
    cards = cards_with_rates(*[0.1] * 10, 0.001)
    monkeypatch.setattr(model, "_MAX_CORRECTION_STEPS", 0)

    with pytest.raises(RuntimeError):
        normalize_hit_rates(cards)


def test_accepts_any_iterable_of_cards():
    result = normalize_hit_rates(iter(cards_with_rates(0.5, 0.5)))

    assert result.status is NormalizationStatus.OK
    assert len(result.cards) == 2


def test_empty_checklist_raises():
    with pytest.raises(ValueError):
        normalize_hit_rates(())


@pytest.mark.parametrize("bad_rate", [0.0, -0.1])
def test_zero_or_negative_hit_rate_raises(bad_rate):
    with pytest.raises(ValueError):
        normalize_hit_rates(cards_with_rates(0.5, 0.5, bad_rate))


@pytest.mark.parametrize("bad_rate", [math.nan, math.inf])
def test_non_finite_hit_rate_raises(bad_rate):
    with pytest.raises(ValueError):
        normalize_hit_rates(cards_with_rates(0.5, bad_rate))


# --- percent_to_probability ------------------------------------------------


def test_percent_to_probability_parses_formatted_hit_rate():
    assert percent_to_probability("1.96") == 0.0196


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.02", 0.0002),
        ("0.0002", 0.000002),
        ("0.25", 0.0025),
        ("100", 1.0),
        (" 1.96 ", 0.0196),
        ("1.96%", 0.0196),
    ],
)
def test_percent_to_probability_examples(text, expected):
    assert percent_to_probability(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "1.96.1",
        "1,96",
        "%",
        "-1",
        "100.01",
        "nan",
        "inf",
        "1e2",
        "١.٩٦",  # Arabic-Indic digits for "1.96"
        "１.９６",  # full-width digits for "1.96"
    ],
)
def test_percent_to_probability_rejects_malformed_input(text):
    with pytest.raises(ValueError):
        percent_to_probability(text)
