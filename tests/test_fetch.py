import pytest
from fake_arena import (
    FakeArena,
    level_json,
    sample_arena,
)

from arenaclub_optimizer.arena_client import ArenaApiError, ArenaBlockedError
from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.fetch import FetchResult, fetch_card_packs
from arenaclub_optimizer.model import SlabPack


def by_id(result: FetchResult) -> dict[str, tuple[SlabPack, PackRates]]:
    return {pack.pack_id: (pack, rates) for pack, rates in result.packs}


def test_fetches_every_card_slab_pack_and_its_cards():
    arena = sample_arena()

    result = fetch_card_packs(arena)

    assert sorted(by_id(result)) == ["gold-football", "gold-pokemon", "silver-pokemon"]
    assert arena.calls[0] == ("levels", None)
    assert sorted(arena.requested("card")) == sorted(arena.cards)
    assert result.skipped == ()


def test_returns_slab_pack_and_rates_pairs():
    result = fetch_card_packs(sample_arena())

    pack, rates = by_id(result)["gold-pokemon"]
    assert isinstance(pack, SlabPack)
    assert pack.price_level == "Gold"
    assert pack.category == "Pokemon"
    assert pack.price == 100.0
    assert pack.active is True
    assert [c.card_id for c in pack.checklist] == ["card_gp_hit", "card_gp_base"]
    assert rates == PackRates(buyback_rate=0.9, slab_safe_floor=0.8)


def test_non_card_price_levels_are_never_fetched():
    arena = sample_arena()

    fetch_card_packs(arena)

    assert "dusk-watch" not in arena.requested("pack")


def test_category_filter_applies_before_cards_are_fetched():
    arena = sample_arena()

    result = fetch_card_packs(arena, categories=["pokemon"])

    assert sorted(by_id(result)) == ["gold-pokemon", "silver-pokemon"]
    assert "card_gf_1" not in arena.requested("card")
    assert sorted(arena.requested("card")) == [
        "card_gp_base",
        "card_gp_hit",
        "card_sp_1",
        "card_sp_2",
    ]


def test_category_filter_accepts_several_categories_and_ignores_accents():
    arena = sample_arena()

    result = fetch_card_packs(arena, categories=["FOOTBALL", "Pokémon"])

    assert len(result.packs) == 3


def test_no_categories_means_all():
    assert len(fetch_card_packs(sample_arena(), categories=[]).packs) == 3
    assert len(fetch_card_packs(sample_arena(), categories=None).packs) == 3


def test_cards_are_mapped_from_identity_fields_only():
    arena = sample_arena()
    arena.cards["card_gp_hit"].update(
        status="sold", priceBadge="hot", clPriceBadge="bad"
    )

    pack, _ = by_id(fetch_card_packs(arena))["gold-pokemon"]

    hit = pack.checklist[0]
    assert hit.card_id == "card_gp_hit"
    assert hit.cert_number == "11111111"
    assert hit.market_value is None


def test_pack_status_decides_whether_it_is_active():
    arena = sample_arena()
    arena.packs["gold-pokemon"]["status"] = "sold_out"

    pack, _ = by_id(fetch_card_packs(arena))["gold-pokemon"]

    assert pack.active is False


def test_inactive_packs_cost_no_card_requests():
    arena = sample_arena()
    arena.packs["gold-pokemon"]["status"] = "inactive"

    result = fetch_card_packs(arena)

    assert "card_gp_hit" not in arena.requested("card")
    assert "card_gp_base" not in arena.requested("card")
    pack, _ = by_id(result)["gold-pokemon"]
    assert pack.active is False
    assert pack.checklist == ()


def test_buyback_disabled_packs_cost_no_card_requests():
    arena = sample_arena()
    arena.packs["silver-pokemon"]["isOfferEnabled"] = False

    result = fetch_card_packs(arena)

    assert "card_sp_1" not in arena.requested("card")
    _, rates = by_id(result)["silver-pokemon"]
    assert rates.buyback_rate is None


def test_per_pack_rates_come_from_the_pack():
    arena = sample_arena()
    arena.packs["gold-football"].update(
        offerPercentage=0.85, offerInsuredPercentage=0.7
    )

    _, rates = by_id(fetch_card_packs(arena))["gold-football"]

    assert rates == PackRates(buyback_rate=0.85, slab_safe_floor=0.7)


def test_a_pack_listed_under_two_price_levels_is_fetched_once():
    arena = sample_arena()
    arena.levels.append(level_json("lvl-dup", "Gold", 10000, ["gold-pokemon"]))

    result = fetch_card_packs(arena)

    assert arena.requested("pack").count("gold-pokemon") == 1
    assert [p.pack_id for p, _ in result.packs].count("gold-pokemon") == 1


def test_a_card_shared_by_two_packs_is_requested_through_the_client_each_time():
    # The client's on-disk cache makes the repeat free; fetch doesn't second-guess it.
    arena = sample_arena()
    arena.packs["silver-pokemon"]["tiers"][0]["itemIds"] = ["card_sp_1", "card_gp_hit"]

    fetch_card_packs(arena)

    assert arena.requested("card").count("card_gp_hit") == 2


@pytest.mark.parametrize(
    "failing",
    [("pack", "gold-football"), ("card", "card_gf_1"), ("levels", "")],
)
def test_a_block_stops_and_re_raises(failing):
    arena = sample_arena()
    arena.errors[failing] = ArenaBlockedError(429, retry_after=60)

    with pytest.raises(ArenaBlockedError):
        fetch_card_packs(arena)

    blocked_at = arena.calls.index(failing if failing[1] else ("levels", None))
    assert blocked_at == len(arena.calls) - 1


def test_a_failed_card_request_skips_that_pack_and_continues():
    arena = sample_arena()
    arena.errors[("card", "card_gp_hit")] = ArenaApiError("GET: HTTP 500", 500)

    result = fetch_card_packs(arena)

    assert sorted(by_id(result)) == ["gold-football", "silver-pokemon"]
    [skipped] = result.skipped
    assert skipped.pack_id == "gold-pokemon"
    assert skipped.name == "Gold Pokemon"
    assert "500" in skipped.reason


def test_a_failed_pack_request_is_skipped_under_its_price_level():
    arena = sample_arena()
    arena.errors[("pack", "silver-pokemon")] = ArenaApiError("GET: timed out")

    result = fetch_card_packs(arena)

    [skipped] = result.skipped
    assert skipped.pack_id == "silver-pokemon"
    assert "Silver" in skipped.name
    assert "timed out" in skipped.reason
    assert len(result.packs) == 2


def test_unmappable_pack_json_is_skipped():
    arena = sample_arena()
    arena.packs["gold-football"]["tiers"][0]["type"] = "mystery"

    result = fetch_card_packs(arena)

    [skipped] = result.skipped
    assert skipped.pack_id == "gold-football"
    assert "mystery" in skipped.reason


def test_bad_offer_percentage_is_skipped():
    arena = sample_arena()
    arena.packs["gold-football"]["offerPercentage"] = 9

    result = fetch_card_packs(arena)

    [skipped] = result.skipped
    assert skipped.pack_id == "gold-football"
    assert "offerPercentage" in skipped.reason


def test_a_price_level_without_pack_ids_is_ignored():
    arena = FakeArena([level_json("lvl", "Gold", 100, [])], {}, {})
    del arena.levels[0]["repackSeriesIds"]

    result = fetch_card_packs(arena)

    assert result == FetchResult((), ())


def test_progress_is_reported_once_per_slab_pack():
    arena = sample_arena()
    seen: list[tuple[int, int]] = []

    fetch_card_packs(arena, progress=lambda done, total: seen.append((done, total)))

    assert seen == [(1, 3), (2, 3), (3, 3)]


# --- price pre-filter -----------------------------------------------------------
# sample_arena: Gold ($100) holds gold-pokemon and gold-football (3 cards),
# Silver ($50) holds silver-pokemon (2 cards).


def test_without_a_price_filter_every_card_pack_is_requested():
    arena = sample_arena()

    fetch_card_packs(arena)

    # 1 Price Level listing + 3 Slab Packs + 5 cards.
    assert len(arena.calls) == 9


def test_price_min_skips_price_levels_before_any_pack_or_card_request():
    arena = sample_arena()

    result = fetch_card_packs(arena, price_min=60)

    assert sorted(by_id(result)) == ["gold-football", "gold-pokemon"]
    assert "silver-pokemon" not in arena.requested("pack")
    assert not {"card_sp_1", "card_sp_2"} & set(arena.requested("card"))
    # 1 listing + 2 Slab Packs + 3 cards, instead of 9 requests.
    assert len(arena.calls) == 6


def test_price_max_skips_price_levels_before_any_pack_or_card_request():
    arena = sample_arena()

    result = fetch_card_packs(arena, price_max=75)

    assert sorted(by_id(result)) == ["silver-pokemon"]
    assert arena.requested("pack") == ["silver-pokemon"]
    assert sorted(arena.requested("card")) == ["card_sp_1", "card_sp_2"]
    assert len(arena.calls) == 4


def test_price_pre_filter_is_inclusive():
    arena = sample_arena()

    result = fetch_card_packs(arena, price_min=100, price_max=100)

    assert sorted(by_id(result)) == ["gold-football", "gold-pokemon"]
    assert len(arena.calls) == 6


def test_price_range_matching_no_price_level_makes_only_the_listing_request():
    arena = sample_arena()

    result = fetch_card_packs(arena, price_min=60, price_max=75)

    assert result == FetchResult((), ())
    assert arena.calls == [("levels", None)]


def test_price_and_category_pre_filters_combine():
    arena = sample_arena()

    result = fetch_card_packs(arena, ["football"], price_min=60)

    assert sorted(by_id(result)) == ["gold-football"]
    assert arena.requested("card") == ["card_gf_1"]


@pytest.mark.parametrize("price_cents", [None, "lots", True, float("nan")])
def test_a_price_level_without_a_usable_price_is_kept(price_cents):
    # The pre-filter must never drop a pack that `rank` would keep, so a
    # Price Level whose price can't be read is fetched and left to `rank`.
    arena = sample_arena()
    if price_cents is None:
        del arena.levels[1]["priceCents"]
    else:
        arena.levels[1]["priceCents"] = price_cents

    result = fetch_card_packs(arena, price_min=60)

    assert "silver-pokemon" in arena.requested("pack")
    assert "silver-pokemon" in by_id(result)


def test_a_pack_listed_under_a_kept_and_a_dropped_price_level_is_fetched():
    arena = sample_arena()
    arena.levels.append(level_json("lvl-promo", "Promo", 1000, ["gold-pokemon"]))

    result = fetch_card_packs(arena, price_min=60)

    assert arena.requested("pack").count("gold-pokemon") == 1
    assert "gold-pokemon" in by_id(result)


def test_progress_counts_only_the_packs_left_after_the_price_pre_filter():
    arena = sample_arena()
    seen: list[tuple[int, int]] = []

    fetch_card_packs(
        arena, price_max=75, progress=lambda done, total: seen.append((done, total))
    )

    assert seen == [(1, 1)]
