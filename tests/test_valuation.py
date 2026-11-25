"""Valuing the cards `rank` and `review` need: priority order, one resolver
call per run, and the real value sources. Fake providers only; no network."""

import httpx
import pytest
from fake_values import NOW, Clock, FakeProvider, checklist_card, quote

from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.config import Filters, Override, Overrides, cache_dir
from arenaclub_optimizer.model import SlabPack, TierType, ValueConfidence
from arenaclub_optimizer.valuation import (
    TIER_WEIGHTS,
    ReviewEntry,
    ValueSources,
    card_priority,
    cards_by_priority,
    open_value_sources,
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
) -> SlabPack:
    return SlabPack(
        pack_id=pack_id,
        price_level="Gold",
        category=category,
        price=price,
        active=active,
        checklist=tuple(cards),
    )


def card(card_id: str, tier_type: TierType = LINE_UP, hit_rate: float = 0.02, **kw):
    return checklist_card(card_id, tier_type=tier_type, hit_rate=hit_rate, **kw)


def priority_ids(pairs, filters: Filters | None = None) -> list[str]:
    return [c.card_id for c in cards_by_priority(pairs, filters or Filters())]


# The Gold Pokemon snapshot's per-card Hit Rates: Super Grail 0.0002%,
# Grail 0.02%, Chase 0.25%, Lineup 1.96%.
SNAPSHOT_PACK = pack(
    "gold",
    [
        card("lineup", LINE_UP, 0.0196),
        card("chase", CHASE, 0.0025),
        card("super-grail", GRAIL, 0.000002),
        card("grail", GRAIL, 0.0002),
    ],
)


# --- priority order -------------------------------------------------------------


def test_priority_is_hit_rate_times_tier_weight():
    for c in SNAPSHOT_PACK.checklist:
        assert card_priority(c) == c.hit_rate * TIER_WEIGHTS[c.tier_type]


def test_tier_weights_rank_grail_over_chase_over_lineup():
    assert TIER_WEIGHTS[GRAIL] > TIER_WEIGHTS[CHASE] > TIER_WEIGHTS[LINE_UP] > 0


def test_grail_and_chase_cards_come_before_lineup_cards():
    order = priority_ids([(SNAPSHOT_PACK, RATES)])

    assert order[-1] == "lineup"
    assert set(order[:3]) == {"grail", "chase", "super-grail"}


def test_within_a_tier_a_higher_hit_rate_comes_first_across_packs():
    a = pack("a", [card("a-low", CHASE, 0.001), card("a-high", CHASE, 0.004)])
    b = pack("b", [card("b-mid", CHASE, 0.002)])

    assert priority_ids([(a, RATES), (b, RATES)]) == ["a-high", "b-mid", "a-low"]


def test_ties_keep_pack_then_checklist_order():
    a = pack("a", [card("a1"), card("a2")])
    b = pack("b", [card("b1")])

    assert priority_ids([(a, RATES), (b, RATES)]) == ["a1", "a2", "b1"]


def test_only_packs_rank_would_evaluate_are_valued():
    pairs = [
        (pack("kept", [card("kept")]), RATES),
        (pack("football", [card("football")], category="Football"), RATES),
        (pack("cheap", [card("cheap")], price=20.0), RATES),
        (pack("dear", [card("dear")], price=500.0), RATES),
        (pack("sold-out", [card("sold-out")], active=False), RATES),
        (pack("no-buyback", [card("no-buyback")]), NO_BUYBACK),
    ]
    filters = Filters(categories=("pokémon",), price_min=50.0, price_max=150.0)

    assert priority_ids(pairs, filters) == ["kept"]


# --- one resolver call per run ------------------------------------------------------


@pytest.fixture
def cache(tmp_path):
    with ValueCache(tmp_path / "values") as c:
        yield c


def sources(cache, *providers, limits=None) -> ValueSources:
    budget = CreditBudget(cache, limits or {}, clock=Clock(NOW))
    return ValueSources(tuple(providers), cache, budget)


def test_cards_are_asked_for_in_priority_order(cache):
    provider = FakeProvider("pokeprice")

    value_packs(
        [(SNAPSHOT_PACK, RATES)], Filters(), Overrides(), sources(cache, provider)
    )

    assert provider.called_names == ["grail", "chase", "super-grail", "lineup"]


def test_a_limited_budget_values_grail_and_chase_cards_first(cache):
    budget = CreditBudget(cache, {"pokeprice": 4}, clock=Clock(NOW))
    answers = {c.card_id: quote(10.0, "pokeprice") for c in SNAPSHOT_PACK.checklist}
    provider = FakeProvider("pokeprice", answers, credit_cost=2, budget=budget)
    run = ValueSources((provider,), cache, budget)

    resolution = value_packs([(SNAPSHOT_PACK, RATES)], Filters(), Overrides(), run)

    assert set(resolution.values) == {"grail", "chase"}
    assert [entry.card_id for entry in resolution.needs_review] == [
        "super-grail",
        "lineup",
    ]
    assert all("budget exhausted" in e.reason for e in resolution.needs_review)


def test_an_unavailable_provider_is_asked_once_across_packs(cache):
    down = FakeProvider("pokeprice", lambda _: ProviderUnavailable(reason="down"))
    backup = FakeProvider(
        "cardvolume", lambda i: quote(5.0, "cardvolume", citation=i.card_name)
    )
    pairs = [
        (pack("a", [card("a1"), card("a2")]), RATES),
        (pack("b", [card("b1")]), RATES),
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
    overridden = card("overridden", cert="12345678")
    p = pack("p", [overridden, card("both"), card("cv"), card("none")])
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
    pairs = [(pack("p", [card("a")]), RATES)]

    value_packs(pairs, Filters(), Overrides(), sources(cache, provider))
    again = value_packs(pairs, Filters(), Overrides(), sources(cache, provider))

    assert len(provider.calls) == 1
    assert again.origins == {"a": ValueOrigin.CACHE}


# --- the Needs-review List ------------------------------------------------------


def test_review_entries_name_each_cards_slab_packs(cache):
    a = pack("a", [card("a1", cert="111", label="Card A1"), card("a2")])
    b = pack("b", [card("b1")], category="Football")
    pairs = [(a, RATES), (b, RATES)]
    resolution = value_packs(pairs, Filters(), Overrides(), sources(cache))

    entries = review_entries(pairs, resolution)

    assert entries[0] == ReviewEntry(
        card_id="a1",
        cert_number="111",
        label="Card A1",
        reason="no value providers",
        slab_packs=("Gold Pokemon",),
    )
    assert [e.card_id for e in entries] == ["a1", "a2", "b1"]
    assert entries[2].slab_packs == ("Gold Football",)


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
