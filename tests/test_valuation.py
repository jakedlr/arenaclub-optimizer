"""Valuing the cards `rank` and `review` need: pack-at-a-time order, one
resolver call per run, and the real value sources. Fake providers only; no
network."""

from datetime import timedelta

import httpx
import pytest
from fake_values import NOW, Clock, FakeProvider, checklist_card, quote

from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.config import Filters, Override, Overrides, cache_dir
from arenaclub_optimizer.model import SlabPack, TierType, ValueConfidence
from arenaclub_optimizer.valuation import (
    TIER_RANK,
    ReviewEntry,
    ValueSources,
    card_priority,
    cards_by_priority,
    open_value_sources,
    packs_by_price,
    review_entries,
    value_packs,
)
from arenaclub_optimizer.values import (
    CreditBudget,
    ProviderUnavailable,
    ValueCache,
    ValueOrigin,
    ValueQuote,
)

RATES = PackRates(buyback_rate=0.9, slab_safe_floor=0.8)
NO_BUYBACK = PackRates(buyback_rate=None, slab_safe_floor=0.8)
GRAIL, CHASE, LINE_UP = TierType.GRAIL, TierType.CHASE, TierType.LINE_UP


def pack(
    pack_id: str,
    cards,
    *,
    category: str = "Pokemon",
    price: float = 100.0,
    active: bool = True,
    level: str = "Gold",
) -> SlabPack:
    return SlabPack(
        pack_id=pack_id,
        price_level=level,
        category=category,
        price=price,
        active=active,
        checklist=tuple(cards),
    )


def card(card_id: str, tier_type: TierType = LINE_UP, hit_rate: float = 1.0, **kw):
    return checklist_card(card_id, tier_type=tier_type, hit_rate=hit_rate, **kw)


def lineup(pack_id: str, *card_ids: str, **kw) -> SlabPack:
    """A Slab Pack of Lineup cards at equal Hit Rates that sum to 100%."""
    rate = 1 / len(card_ids)
    return pack(pack_id, [card(c, LINE_UP, rate) for c in card_ids], **kw)


def twenty(pack_id: str = "cheap", *, price: float = 10.0) -> SlabPack:
    """Twenty Lineup cards c0..c19 at 5% each."""
    return lineup(pack_id, *(f"c{i}" for i in range(20)), price=price)


def priority_ids(pairs, filters: Filters | None = None, **kw) -> list[str]:
    return [c.card_id for c in cards_by_priority(pairs, filters or Filters(), **kw)]


# The Gold Pokemon snapshot's per-card Hit Rates: Super Grail 0.0002%,
# Grail 0.02%, Chase 0.25%; the Lineup card takes the rest.
SNAPSHOT_PACK = pack(
    "gold",
    [
        card("lineup", LINE_UP, 0.997298),
        card("chase", CHASE, 0.0025),
        card("super-grail", GRAIL, 0.000002),
        card("grail", GRAIL, 0.0002),
    ],
)


# --- card order within a pack -----------------------------------------------------


def test_tier_rank_puts_grail_then_chase_then_lineup():
    assert TIER_RANK == {GRAIL: 0, CHASE: 1, LINE_UP: 2}


def test_priority_is_tier_rank_then_the_higher_hit_rate():
    for c in SNAPSHOT_PACK.checklist:
        assert card_priority(c) == (TIER_RANK[c.tier_type], -c.hit_rate)


def test_within_a_pack_grail_cards_come_before_chase_before_lineup():
    order = priority_ids([(SNAPSHOT_PACK, RATES)])

    assert order == ["grail", "super-grail", "chase", "lineup"]


def test_a_super_grail_comes_before_a_chase_card_with_a_far_higher_hit_rate():
    p = pack(
        "p",
        [
            card("chase", CHASE, 0.4),
            card("super", GRAIL, 1e-9),
            card("lineup", LINE_UP, 0.6 - 1e-9),
        ],
    )

    assert priority_ids([(p, RATES)]) == ["super", "chase", "lineup"]


def test_within_a_tier_the_higher_hit_rate_comes_first():
    p = pack(
        "p",
        [
            card("low", CHASE, 0.1),
            card("high", CHASE, 0.4),
            card("mid", CHASE, 0.2),
            card("rest", LINE_UP, 0.3),
        ],
    )

    assert priority_ids([(p, RATES)]) == ["high", "mid", "low", "rest"]


def test_ties_keep_checklist_order():
    order = priority_ids([(lineup("p", "c1", "c2", "c3"), RATES)])

    assert order == ["c1", "c2", "c3"]


# --- pack order ---------------------------------------------------------------------


def test_packs_are_valued_cheapest_first():
    dear = lineup("dear", "d1", "d2", price=250.0)
    cheap = lineup("cheap", "c1", "c2", price=25.0)
    mid = lineup("mid", "m1", price=100.0)
    pairs = [(dear, RATES), (cheap, RATES), (mid, RATES)]

    assert priority_ids(pairs) == ["c1", "c2", "m1", "d1", "d2"]
    ordered = [p.pack_id for p in packs_by_price(pairs, Filters())]
    assert ordered == ["cheap", "mid", "dear"]


def test_a_whole_pack_is_ordered_before_the_next_one_starts():
    # The cheap pack's Lineup card comes before the dear pack's Grail card.
    cheap = pack("cheap", [card("c-grail", GRAIL, 0.01), card("c-line", LINE_UP, 0.99)])
    dear = pack(
        "dear",
        [card("d-grail", GRAIL, 0.01), card("d-line", LINE_UP, 0.99)],
        price=500.0,
    )

    order = priority_ids([(dear, RATES), (cheap, RATES)])

    assert order == ["c-grail", "c-line", "d-grail", "d-line"]


def test_packs_at_the_same_price_are_ordered_by_pack_id():
    b = lineup("b-pack", "b1", category="Baseball")
    a = lineup("a-pack", "a1", category="Zebra")

    assert priority_ids([(b, RATES), (a, RATES)]) == ["a1", "b1"]


def test_a_card_shared_between_packs_is_listed_once_at_its_earliest_position():
    shared_cheap = card("shared", LINE_UP, 0.5)
    shared_dear = card("shared", GRAIL, 0.5)
    cheap = pack("cheap", [card("c1", LINE_UP, 0.5), shared_cheap], price=10.0)
    dear = pack("dear", [shared_dear, card("d1", LINE_UP, 0.5)], price=20.0)

    assert priority_ids([(dear, RATES), (cheap, RATES)]) == ["c1", "shared", "d1"]


def test_only_packs_rank_would_evaluate_are_valued():
    pairs = [
        (lineup("kept", "kept"), RATES),
        (lineup("football", "football", category="Football"), RATES),
        (lineup("cheap", "cheap", price=20.0), RATES),
        (lineup("dear", "dear", price=500.0), RATES),
        (lineup("sold-out", "sold-out", active=False), RATES),
        (lineup("no-buyback", "no-buyback"), NO_BUYBACK),
    ]
    filters = Filters(categories=("pokémon",), price_min=50.0, price_max=150.0)

    assert priority_ids(pairs, filters) == ["kept"]


def test_packs_whose_hit_rates_would_be_rejected_are_skipped():
    # 50% + 40% = 90%: more than 5% off 100%, so `rank` would skip the pack.
    rejected = pack("rejected", [card("r1", hit_rate=0.5), card("r2", hit_rate=0.4)])
    # 98%: rescaled with a warning, so still ranked.
    rescaled = pack(
        "rescaled",
        [card("w1", hit_rate=0.5), card("w2", hit_rate=0.48)],
        price=200.0,
    )

    assert priority_ids([(rejected, RATES), (rescaled, RATES)]) == ["w1", "w2"]
    assert [p.pack_id for p in packs_by_price([(rejected, RATES)], Filters())] == []


def test_packs_with_unusable_hit_rates_are_skipped():
    zero = pack("zero", [card("z1", hit_rate=0.0), card("z2", hit_rate=1.0)])
    empty = pack("empty", [])
    kept = lineup("kept", "k1", price=200.0)

    assert priority_ids([(zero, RATES), (empty, RATES), (kept, RATES)]) == ["k1"]


def test_a_pack_already_rankable_goes_after_the_packs_still_to_value():
    cheap = twenty()
    dear = lineup("dear", "d1", "d2", price=500.0)
    known = {f"c{i}" for i in range(19)}  # 95% of the cheap pack

    order = priority_ids(
        [(cheap, RATES), (dear, RATES)],
        valued=lambda c: c.card_id in known,
        coverage_threshold=0.95,
    )

    assert order[:2] == ["d1", "d2"]
    assert order[2:] == [c.card_id for c in cheap.checklist]


def test_a_pack_just_below_the_threshold_keeps_its_place():
    cheap = twenty()
    dear = lineup("dear", "d1", price=500.0)
    known = {f"c{i}" for i in range(18)}  # 90%

    order = priority_ids(
        [(cheap, RATES), (dear, RATES)],
        valued=lambda c: c.card_id in known,
        coverage_threshold=0.95,
    )

    assert order[-1] == "d1"


def test_rankability_uses_rescaled_hit_rates():
    # Raw rates sum to 98%, so the valued card's 93.1% rescales to 95%.
    p = pack("p", [card("big", hit_rate=0.931), card("small", hit_rate=0.049)])
    later = lineup("later", "l1", price=500.0)

    order = priority_ids(
        [(p, RATES), (later, RATES)],
        valued=lambda c: c.card_id == "big",
        coverage_threshold=0.95,
    )

    assert order == ["l1", "big", "small"]


# --- one resolver call per run ------------------------------------------------------


@pytest.fixture
def cache(tmp_path):
    with ValueCache(tmp_path / "values") as c:
        yield c


def sources(cache, *providers, limits=None) -> ValueSources:
    budget = CreditBudget(cache, limits or {}, clock=Clock(NOW))
    return ValueSources(tuple(providers), cache, budget)


def budgeted(cache, credits: int, *, cost: int = 1, clock: Clock | None = None):
    """A PokéPrice that quotes $10 for any card at `cost` credits a card,
    with `credits` to spend today, and the sources it is the one provider of."""
    budget = CreditBudget(cache, {"pokeprice": credits}, clock=clock or Clock(NOW))
    provider = FakeProvider(
        "pokeprice",
        lambda _: quote(10.0, "pokeprice"),
        credit_cost=cost,
        budget=budget,
    )
    return provider, ValueSources((provider,), cache, budget)


def cache_quotes(cache, *card_ids: str, fetched_at=NOW) -> None:
    for card_id in card_ids:
        cache.put(card_id, "pokeprice", quote(5.0, "pokeprice"), fetched_at=fetched_at)


def test_cards_are_asked_for_in_priority_order(cache):
    provider = FakeProvider("pokeprice")

    value_packs(
        [(SNAPSHOT_PACK, RATES)], Filters(), Overrides(), sources(cache, provider)
    )

    assert provider.called_names == ["grail", "super-grail", "chase", "lineup"]


def test_with_budget_for_one_pack_the_cheaper_one_is_valued_and_not_the_other(cache):
    cheap = lineup("cheap", "c1", "c2", "c3", price=50.0)
    dear = lineup("dear", "d1", "d2", "d3", price=100.0)
    provider, run = budgeted(cache, 6, cost=2)

    resolution = value_packs(
        [(dear, RATES), (cheap, RATES)], Filters(), Overrides(), run
    )

    assert set(resolution.values) == {"c1", "c2", "c3"}
    assert provider.called_names == ["c1", "c2", "c3"]
    assert [e.card_id for e in resolution.needs_review] == ["d1", "d2", "d3"]
    assert all(e.waiting for e in resolution.needs_review)
    assert all("budget exhausted" in e.reason for e in resolution.needs_review)


def test_the_next_run_picks_up_where_the_last_one_stopped(cache):
    pairs = [
        (lineup("cheap", "c1", "c2", price=50.0), RATES),
        (lineup("dear", "d1", "d2", price=100.0), RATES),
    ]
    today, first = budgeted(cache, 3)
    value_packs(pairs, Filters(), Overrides(), first)

    tomorrow, second = budgeted(cache, 3, clock=Clock(NOW + timedelta(days=1)))
    resolution = value_packs(pairs, Filters(), Overrides(), second)

    assert today.called_names == ["c1", "c2", "d1"]
    assert tomorrow.called_names == ["d2"]
    assert resolution.origins == {
        "c1": ValueOrigin.CACHE,
        "c2": ValueOrigin.CACHE,
        "d1": ValueOrigin.CACHE,
        "d2": ValueOrigin.FETCHED,
    }


def test_a_pack_already_rankable_from_the_cache_spends_no_budget(cache):
    cache_quotes(cache, *(f"c{i}" for i in range(19)))
    dear = lineup("dear", "d1", "d2", price=500.0)
    provider, run = budgeted(cache, 2)

    resolution = value_packs(
        [(twenty(), RATES), (dear, RATES)],
        Filters(),
        Overrides(),
        run,
        coverage_threshold=0.95,
    )

    assert provider.called_names == ["d1", "d2"]
    assert {"d1", "d2"} <= set(resolution.values)
    assert [e.card_id for e in resolution.needs_review] == ["c19"]


def test_a_pack_already_rankable_from_overrides_spends_no_budget(cache):
    cheap = lineup("cheap", "c1", "c2", price=10.0)
    dear = lineup("dear", "d1", price=500.0)
    overrides = Overrides(by_card_id={"c1": Override(5.0), "c2": Override(6.0)})
    provider, run = budgeted(cache, 1)

    resolution = value_packs([(cheap, RATES), (dear, RATES)], Filters(), overrides, run)

    assert provider.called_names == ["d1"]
    assert resolution.origins["c1"] is ValueOrigin.OVERRIDE


def test_leftover_budget_still_values_the_rest_of_an_already_rankable_pack(cache):
    cache_quotes(cache, *(f"c{i}" for i in range(19)))
    dear = lineup("dear", "d1", price=500.0)
    provider, run = budgeted(cache, 5)

    value_packs([(twenty(), RATES), (dear, RATES)], Filters(), Overrides(), run)

    assert provider.called_names == ["d1", "c19"]


def test_the_coverage_threshold_defaults_to_95_percent(cache):
    cache_quotes(cache, *(f"c{i}" for i in range(19)))
    dear = lineup("dear", "d1", price=500.0)
    provider, run = budgeted(cache, 1)

    value_packs([(twenty(), RATES), (dear, RATES)], Filters(), Overrides(), run)

    assert provider.called_names == ["d1"]


def test_a_higher_coverage_threshold_keeps_the_pack_first(cache):
    cache_quotes(cache, *(f"c{i}" for i in range(19)))
    dear = lineup("dear", "d1", price=500.0)
    provider, run = budgeted(cache, 1)

    value_packs(
        [(twenty(), RATES), (dear, RATES)],
        Filters(),
        Overrides(),
        run,
        coverage_threshold=1.0,
    )

    assert provider.called_names == ["c19"]


def test_a_stale_cached_value_does_not_make_a_pack_rankable(cache):
    cache_quotes(cache, "c1", fetched_at=NOW - timedelta(days=20))
    cheap = lineup("cheap", "c1", price=10.0)
    dear = lineup("dear", "d1", price=500.0)
    provider, run = budgeted(cache, 1)

    value_packs([(cheap, RATES), (dear, RATES)], Filters(), Overrides(), run)

    assert provider.called_names == ["c1"]


def test_rejected_packs_spend_no_credits(cache):
    rejected = pack("rejected", [card("r1", hit_rate=0.5), card("r2", hit_rate=0.4)])
    kept = lineup("kept", "k1", price=500.0)
    provider, run = budgeted(cache, 10)

    resolution = value_packs(
        [(rejected, RATES), (kept, RATES)], Filters(), Overrides(), run
    )

    assert provider.called_names == ["k1"]
    assert set(resolution.values) == {"k1"}
    assert resolution.needs_review == ()


def test_a_card_shared_between_two_packs_is_valued_once(cache):
    cheap = pack("cheap", [card("c1", hit_rate=0.5), card("shared", hit_rate=0.5)])
    dear = pack(
        "dear",
        [card("shared", GRAIL, 0.5), card("d1", hit_rate=0.5)],
        price=500.0,
    )
    provider, run = budgeted(cache, 10)

    resolution = value_packs(
        [(dear, RATES), (cheap, RATES)], Filters(), Overrides(), run
    )

    assert provider.called_names == ["c1", "shared", "d1"]
    assert set(resolution.values) == {"c1", "shared", "d1"}


def test_an_unavailable_provider_is_asked_once_across_packs(cache):
    down = FakeProvider("pokeprice", lambda _: ProviderUnavailable(reason="down"))
    backup = FakeProvider(
        "cardvolume", lambda i: quote(5.0, "cardvolume", citation=i.card_name)
    )
    pairs = [
        (lineup("a", "a1", "a2"), RATES),
        (lineup("b", "b1", price=200.0), RATES),
    ]

    resolution = value_packs(
        pairs, Filters(), Overrides(), sources(cache, down, backup)
    )

    assert len(down.calls) == 1
    assert backup.called_names == ["a1", "a2", "b1"]
    assert resolution.unavailable == {"pokeprice": "down"}


def test_overrides_then_pokeprice_then_cardvolume(cache):
    pokeprice = FakeProvider("pokeprice", {"both": quote(70.0, "pokeprice")})
    cardvolume = FakeProvider(
        "cardvolume",
        {"both": quote(80.0, "cardvolume"), "cv": quote(90.0, "cardvolume")},
    )
    overridden = card("overridden", hit_rate=0.25, cert="12345678")
    others = [card(c, hit_rate=0.25) for c in ["both", "cv", "none"]]
    p = pack("p", [overridden, *others])
    overrides = Overrides(by_cert={"12345678": Override(500.0)})

    resolution = value_packs(
        [(p, RATES)], Filters(), overrides, sources(cache, pokeprice, cardvolume)
    )

    assert {k: v.source for k, v in resolution.values.items()} == {
        "overridden": "override",
        "both": "pokeprice",
        "cv": "cardvolume",
    }
    assert resolution.values["overridden"].market_value == 500.0
    assert "overridden" not in pokeprice.called_names
    assert "both" not in cardvolume.called_names
    assert [e.card_id for e in resolution.needs_review] == ["none"]


def test_a_second_run_is_served_from_the_cache(cache):
    provider = FakeProvider("pokeprice", {"a": quote(10.0, "pokeprice")})
    pairs = [(lineup("p", "a"), RATES)]

    value_packs(pairs, Filters(), Overrides(), sources(cache, provider))
    again = value_packs(pairs, Filters(), Overrides(), sources(cache, provider))

    assert len(provider.calls) == 1
    assert again.origins == {"a": ValueOrigin.CACHE}


# --- the Needs-review List ------------------------------------------------------


def test_review_entries_name_each_cards_slab_packs(cache):
    a1 = card("a1", hit_rate=0.5, cert="111", label="Card A1")
    a = pack("a", [a1, card("a2", hit_rate=0.5)])
    b = lineup("b", "b1", category="Football", price=200.0)
    pairs = [(a, RATES), (b, RATES)]
    resolution = value_packs(pairs, Filters(), Overrides(), sources(cache))

    entries = review_entries(pairs, Filters(), resolution)

    assert entries[0] == ReviewEntry(
        card_id="a1",
        cert_number="111",
        label="Card A1",
        reason="no value providers",
        slab_packs=("Gold Pokemon",),
        waiting=False,
    )
    assert [e.card_id for e in entries] == ["a1", "a2", "b1"]
    assert entries[2].slab_packs == ("Gold Football",)


def test_review_entries_leave_out_packs_rank_leaves_out(cache):
    shared = card("shared")
    pairs = [
        (pack("live", [shared], level="Gold"), RATES),
        (pack("sold-out", [shared], level="Silver", active=False), RATES),
        (pack("no-buyback", [shared], level="Bronze"), NO_BUYBACK),
        (pack("dear", [shared], level="Platinum", price=500.0), RATES),
    ]
    filters = Filters(price_max=150.0)
    resolution = value_packs(pairs, filters, Overrides(), sources(cache))

    [entry] = review_entries(pairs, filters, resolution)

    assert entry.slab_packs == ("Gold Pokemon",)


def test_review_entries_mark_cards_waiting(cache):
    budget = CreditBudget(cache, {"pokeprice": 2}, clock=Clock(NOW))
    provider = FakeProvider("pokeprice", credit_cost=2, budget=budget)
    lasting = FakeProvider(
        "cardvolume",
        lambda _: ProviderUnavailable(reason="key not set", lasting=True),
    )
    pairs = [(lineup("p", "first", "second"), RATES)]
    run = ValueSources((provider, lasting), cache, budget)
    resolution = value_packs(pairs, Filters(), Overrides(), run)

    entries = review_entries(pairs, Filters(), resolution)

    assert [(e.card_id, e.waiting) for e in entries] == [
        ("first", False),
        ("second", True),
    ]


# --- the real value sources -------------------------------------------------------


def test_open_value_sources_asks_pokeprice_then_cardvolume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open_value_sources(tmp_path / "cache") as opened:
        assert [p.name for p in opened.providers] == ["pokeprice", "cardvolume"]
        assert opened.cache.directory == tmp_path / "cache"
        assert opened.budget.remaining("pokeprice") == 100
        assert opened.budget.remaining("cardvolume") is None


def test_open_value_sources_defaults_to_the_cache_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open_value_sources() as opened:
        assert opened.cache.directory == cache_dir()


def test_cardvolume_keeps_its_index_beside_the_value_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open_value_sources(tmp_path / "cache") as opened:
        cardvolume = opened.providers[1]
        assert cardvolume.index_path.parent == tmp_path / "cache"


def test_without_a_key_pokeprice_is_unavailable_and_sends_nothing(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with open_value_sources(tmp_path / "cache") as opened:
        pokeprice = opened.providers[0]
        with pytest.raises(ProviderUnavailable, match="key not set"):
            pokeprice.quote(card("a").identity)


def test_the_value_cache_is_closed_after_the_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open_value_sources(tmp_path / "cache") as opened:
        cache = opened.cache
    with pytest.raises(Exception, match="closed"):
        cache.get("a", "pokeprice", now=NOW)


def test_quotes_keep_their_confidence(cache):
    low = quote(10.0, "pokeprice", confidence=ValueConfidence.LOW)
    provider = FakeProvider("pokeprice", {"a": low})

    resolution = value_packs(
        [(pack("p", [card("a")]), RATES)],
        Filters(),
        Overrides(),
        sources(cache, provider),
    )

    assert resolution.values["a"] == low
    assert isinstance(resolution.values["a"], ValueQuote)


def test_no_test_can_reach_the_network():
    with pytest.raises(RuntimeError, match="network"):
        httpx.get("https://cardvolume.com/api/v1/cards.json")
