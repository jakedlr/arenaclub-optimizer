import copy
import json
import math
from collections import Counter
from pathlib import Path

import pytest

from arenaclub_optimizer.arena_mapping import (
    ARENA_CLUB_GRADER,
    DEFAULT_BUYBACK_RATE,
    DEFAULT_SLAB_SAFE_FLOOR,
    ArenaMappingError,
    PackRates,
    build_label,
    is_card_product,
    map_slab_pack,
    pack_rates,
)
from arenaclub_optimizer.model import SlabPack, TierType

SNAPSHOT = Path(__file__).parent / "fixtures" / "arena" / "snapshot"


def load(name: str):
    return json.loads((SNAPSHOT / name).read_text(encoding="utf-8"))


@pytest.fixture
def pack_json() -> dict:
    return load("gold_pokemon_pack.json")


@pytest.fixture
def cards_json() -> dict:
    return load("gold_pokemon_items.json")


def small_pack(**overrides) -> dict:
    pack = {
        "id": "pack-1",
        "name": "Pokemon",
        "status": "active",
        "isInsuranceAvailable": False,
        "isOfferEnabled": True,
        "offerPercentage": 0.85,
        "offerInsuredPercentage": 0.75,
        "category": {"name": "Gold", "priceCents": 12345, "category": "card"},
        "tiers": [
            {
                "name": "Grail",
                "type": "grail",
                "formattedHitRate": "10",
                "itemIds": ["card_a"],
            },
            {
                "name": "Lineup",
                "type": "line_up",
                "formattedHitRate": "90",
                "itemIds": ["card_b"],
            },
        ],
    }
    pack.update(overrides)
    return pack


def card(card_id: str, **overrides) -> dict:
    fields = {
        "id": card_id,
        "year": 2021,
        "setName": "2021 Pokemon SWSH",
        "insert": "Evolving Skies",
        "playerName": "Umbreon VMAX",
        "setNumber": "215",
        "gradingCompany": "psa",
        "overall": 10,
    }
    fields.update(overrides)
    return fields


SMALL_CARDS = {"card_a": card("card_a"), "card_b": card("card_b", overall=9)}


# --- map_slab_pack: the Gold Pokemon fixture ---------------------------------


def test_fixture_maps_to_62_cards_in_the_published_tiers(pack_json, cards_json):
    pack = map_slab_pack(pack_json, cards_json)

    assert isinstance(pack, SlabPack)
    assert len(pack.checklist) == 62
    tiers = Counter(c.tier_name for c in pack.checklist)
    assert tiers == {"Super Grail": 1, "Grail": 3, "Chase": 8, "Lineup": 50}


def test_fixture_raw_hit_rate_sum_is_about_1_0006(pack_json, cards_json):
    pack = map_slab_pack(pack_json, cards_json)

    raw_sum = math.fsum(c.hit_rate for c in pack.checklist)
    assert raw_sum == pytest.approx(1.000602, abs=1e-9)


def test_fixture_pack_fields(pack_json, cards_json):
    pack = map_slab_pack(pack_json, cards_json)

    assert pack.pack_id == "c43ab013-f7d9-4930-b2fd-f06eb8191de3"
    assert pack.category == "Pokemon"
    assert pack.price_level == "Gold"
    assert pack.price == 100.0
    assert pack.active is True
    assert pack.insurance_available is True


def test_fixture_cards_keep_tier_types_and_rates(pack_json, cards_json):
    pack = map_slab_pack(pack_json, cards_json)

    by_tier = {c.tier_name: c for c in pack.checklist}
    assert by_tier["Super Grail"].tier_type is TierType.GRAIL
    assert by_tier["Super Grail"].hit_rate == pytest.approx(0.000002)
    assert by_tier["Grail"].tier_type is TierType.GRAIL
    assert by_tier["Chase"].tier_type is TierType.CHASE
    assert by_tier["Chase"].hit_rate == pytest.approx(0.0025)
    assert by_tier["Lineup"].tier_type is TierType.LINE_UP
    assert by_tier["Lineup"].hit_rate == pytest.approx(0.0196)


def test_fixture_card_identity(pack_json, cards_json):
    pack = map_slab_pack(pack_json, cards_json)

    umbreon = next(
        c
        for c in pack.checklist
        if c.card_id == "card_80747173-c08f-4080-bdf6-623fedc5cef6"
    )
    assert umbreon.tier_name == "Super Grail"
    assert umbreon.grader == "psa"
    assert umbreon.grade == "10"
    assert umbreon.market_value is None
    assert umbreon.value_confidence is None
    assert umbreon.label == "2003 Pokemon E-CARD Skyridge Umbreon #H30 PSA 10"


def test_fixture_cards_without_an_insert_use_the_subset_and_parallel(
    pack_json, cards_json
):
    pack = map_slab_pack(pack_json, cards_json)

    charizard = next(
        c
        for c in pack.checklist
        if c.card_id == "card_664742a8-d5b2-4944-bbea-46a7913e7a17"
    )
    assert charizard.label == (
        "2019 Pokemon S&M Promo Charizard GX Hidden Fates Tins #SM211 PSA 10"
    )


def test_fixture_checklist_follows_tier_then_item_order(pack_json, cards_json):
    pack = map_slab_pack(pack_json, cards_json)

    expected = [i for tier in pack_json["tiers"] for i in tier["itemIds"]]
    assert [c.card_id for c in pack.checklist] == expected


# --- map_slab_pack: field mapping ---------------------------------------------


def test_price_is_price_cents_over_100():
    pack = map_slab_pack(small_pack(), SMALL_CARDS)

    assert pack.price == pytest.approx(123.45)


def test_status_other_than_active_is_inactive():
    pack = map_slab_pack(small_pack(status="sold_out"), SMALL_CARDS)

    assert pack.active is False


def test_insurance_unavailable_is_mapped_explicitly():
    pack = map_slab_pack(small_pack(isInsuranceAvailable=False), SMALL_CARDS)

    assert pack.insurance_available is False


def test_grade_with_a_half_point():
    cards = {**SMALL_CARDS, "card_a": card("card_a", overall=9.5)}

    pack = map_slab_pack(small_pack(), cards)

    assert pack.checklist[0].grade == "9.5"


def test_arena_club_grader_is_kept_raw_and_readable_in_the_label():
    cards = {**SMALL_CARDS, "card_a": card("card_a", gradingCompany="arena_club")}

    pack = map_slab_pack(small_pack(), cards)

    assert ARENA_CLUB_GRADER == "arena_club"
    assert pack.checklist[0].grader == ARENA_CLUB_GRADER
    assert pack.checklist[0].label.endswith("Arena Club 10")


# --- map_slab_pack: errors ------------------------------------------------------


def test_unknown_tier_type_names_the_pack_and_tier():
    pack = small_pack()
    pack["tiers"][1]["type"] = "mystery"

    with pytest.raises(ArenaMappingError, match=r"pack-1.*Lineup.*mystery"):
        map_slab_pack(pack, SMALL_CARDS)


def test_missing_card_names_the_pack_and_card():
    cards = {"card_a": SMALL_CARDS["card_a"]}

    with pytest.raises(ArenaMappingError, match=r"pack-1.*Lineup.*card_b"):
        map_slab_pack(small_pack(), cards)


def test_missing_required_pack_field_is_a_mapping_error():
    pack = small_pack()
    del pack["category"]

    with pytest.raises(ArenaMappingError, match="pack-1"):
        map_slab_pack(pack, SMALL_CARDS)


def test_malformed_hit_rate_is_a_mapping_error():
    pack = small_pack()
    pack["tiers"][0]["formattedHitRate"] = "lots"

    with pytest.raises(ArenaMappingError, match=r"pack-1.*Grail"):
        map_slab_pack(pack, SMALL_CARDS)


def test_mapping_error_is_a_value_error():
    assert issubclass(ArenaMappingError, ValueError)


def test_mapping_does_not_mutate_its_inputs(pack_json, cards_json):
    pack_before = copy.deepcopy(pack_json)
    cards_before = copy.deepcopy(cards_json)

    map_slab_pack(pack_json, cards_json)

    assert pack_json == pack_before
    assert cards_json == cards_before


# --- build_label ----------------------------------------------------------------


def test_label_matches_the_issue_example():
    assert build_label(card("x")) == (
        "2021 Pokemon SWSH Evolving Skies Umbreon VMAX #215 PSA 10"
    )


def test_label_adds_the_year_when_the_set_name_lacks_it():
    label = build_label(card("x", setName="Pokemon SWSH"))

    assert label == "2021 Pokemon SWSH Evolving Skies Umbreon VMAX #215 PSA 10"


def test_label_skips_missing_and_empty_parts():
    label = build_label(
        card("x", year=None, insert=None, setNumber="", setName="Pokemon  ")
    )

    assert label == "Pokemon Umbreon VMAX PSA 10"


def test_label_collapses_whitespace():
    label = build_label(
        card("x", year=2019, setName="2019 Pokemon S&M  ", insert=" Team Up ")
    )

    assert label == "2019 Pokemon S&M Team Up Umbreon VMAX #215 PSA 10"


def test_label_without_grader_or_grade():
    label = build_label(card("x", gradingCompany=None, overall=None))

    assert label == "2021 Pokemon SWSH Evolving Skies Umbreon VMAX #215"


def test_label_uses_the_subset_when_the_insert_is_missing():
    label = build_label(card("x", insert=None, subsetName="Promo"))

    assert label == "2021 Pokemon SWSH Promo Umbreon VMAX #215 PSA 10"


def test_label_skips_a_base_subset_and_a_subset_equal_to_the_insert():
    assert build_label(card("x", insert=None, subsetName="Base")) == (
        "2021 Pokemon SWSH Umbreon VMAX #215 PSA 10"
    )
    assert build_label(card("x", subsetName="Evolving Skies")) == (
        "2021 Pokemon SWSH Evolving Skies Umbreon VMAX #215 PSA 10"
    )


def test_label_adds_a_non_base_parallel_after_the_player_name():
    label = build_label(card("x", parallelName="Master Ball"))

    assert (
        label == "2021 Pokemon SWSH Evolving Skies Umbreon VMAX Master Ball #215 PSA 10"
    )


def test_label_strips_the_player_name_from_the_front_of_the_parallel():
    label = build_label(
        card(
            "x",
            playerName="Bulbasaur",
            parallelName="Bulbasaur Pokemon GO Pin Collection",
        )
    )

    assert label == (
        "2021 Pokemon SWSH Evolving Skies Bulbasaur Pokemon GO Pin Collection "
        "#215 PSA 10"
    )


def test_label_keeps_a_parallel_that_only_shares_a_word_prefix():
    label = build_label(
        card("x", playerName="Mew", parallelName="Mewtwo Collection Box")
    )

    assert "Mew Mewtwo Collection Box #215" in label


def test_label_drops_a_parallel_equal_to_the_player_name():
    label = build_label(card("x", playerName="Bulbasaur", parallelName="Bulbasaur"))

    assert label == "2021 Pokemon SWSH Evolving Skies Bulbasaur #215 PSA 10"


def test_label_skips_a_base_parallel():
    label = build_label(card("x", parallelName="Base"))

    assert label == "2021 Pokemon SWSH Evolving Skies Umbreon VMAX #215 PSA 10"


def test_label_of_a_card_with_nothing_falls_back_to_its_id():
    assert build_label({"id": "card_z"}) == "card_z"


# --- pack_rates -------------------------------------------------------------------


def test_pack_rates_from_the_fixture(pack_json):
    assert pack_rates(pack_json) == PackRates(0.9, 0.8)


def test_pack_rates_read_offer_percentages():
    rates = pack_rates(small_pack())

    assert rates.buyback_rate == 0.85
    assert rates.slab_safe_floor == 0.75


def test_pack_rates_default_when_missing():
    pack = small_pack()
    del pack["offerPercentage"]
    del pack["offerInsuredPercentage"]

    assert pack_rates(pack) == PackRates(DEFAULT_BUYBACK_RATE, DEFAULT_SLAB_SAFE_FLOOR)
    assert (DEFAULT_BUYBACK_RATE, DEFAULT_SLAB_SAFE_FLOOR) == (0.9, 0.8)


def test_pack_rates_default_when_null():
    pack = small_pack(offerPercentage=None, offerInsuredPercentage=None)

    assert pack_rates(pack) == PackRates(0.9, 0.8)


def test_offer_disabled_gives_no_buyback_rate():
    rates = pack_rates(small_pack(isOfferEnabled=False))

    assert rates.buyback_rate is None
    assert rates.slab_safe_floor == 0.75


def test_missing_offer_enabled_counts_as_enabled():
    pack = small_pack()
    del pack["isOfferEnabled"]

    assert pack_rates(pack).buyback_rate == 0.85


@pytest.mark.parametrize("key", ["offerPercentage", "offerInsuredPercentage"])
@pytest.mark.parametrize("bad", ["lots", True, [0.9], 0, -0.1, 1.5, float("nan")])
def test_bad_offer_percentage_is_a_mapping_error_naming_the_pack(key, bad):
    pack = small_pack(**{key: bad})

    with pytest.raises(ArenaMappingError, match=rf"pack-1.*{key}"):
        pack_rates(pack)


def test_offer_percentage_of_one_is_allowed():
    rates = pack_rates(small_pack(offerPercentage=1, offerInsuredPercentage=1.0))

    assert rates == PackRates(1.0, 1.0)


def test_pack_rates_unpack_as_a_pair():
    buyback_rate, slab_safe_floor = pack_rates(small_pack())

    assert (buyback_rate, slab_safe_floor) == (0.85, 0.75)


# --- is_card_product ----------------------------------------------------------


def test_card_price_level_is_a_card_product():
    level = {"id": "level-1", "name": "Gold", "category": "card", "priceCents": 1}

    assert is_card_product(level) is True


@pytest.mark.parametrize("product_type", ["watch", "coin", "wine", None])
def test_other_product_types_are_not_card_products(product_type):
    assert is_card_product({"category": product_type}) is False


def test_price_level_without_a_category_is_not_a_card_product():
    assert is_card_product({}) is False
