"""PokePriceProvider against httpx.MockTransport.

The fixtures in tests/fixtures/pokeprice/ are built from the response examples
in PokéPrice Tracker's OpenAPI spec (v2.0.0): `/sets`, the `/cards`
`multipleCardsWithPagination`, `simpleCard` and `cardWithEbayData` examples,
and the documented `Error` and 429 fields. Dates are moved to Sprint 3, and a
`cgc9_5` grade and `smartPriceOutlierByGrade` are added from the `EbayData`
schema's own examples. No test calls the live API.
"""

import dataclasses
import json
import logging
import sqlite3
import traceback
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from fake_values import NOW, Clock, checklist_card, identity

from arenaclub_optimizer.config import Overrides
from arenaclub_optimizer.model import ValueConfidence
from arenaclub_optimizer.providers.pokeprice import (
    API_KEY_ENV,
    BASE_URL,
    KEY_NOT_SET,
    PokePriceProvider,
    load_api_key,
)
from arenaclub_optimizer.values import (
    BudgetExhausted,
    CreditBudget,
    NotFound,
    ProviderUnavailable,
    Unmatched,
    ValueCache,
    ValueQuote,
    resolve_values,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pokeprice"
KEY = "ppt_test_0123456789abcdefSECRET"
API_PATH = "/api/v2"
SETS = f"{API_PATH}/sets"
CARDS = f"{API_PATH}/cards"
CHARIZARD = f"{API_PATH}/cards/233294"
TOMORROW_MIDNIGHT = datetime(2026, 11, 25, tzinfo=UTC)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def charizard(**overrides):
    """Base Set Charizard #4 in PSA 10, as Arena Club describes it."""
    fields = {
        "year": 1999,
        "set_name": "1999 Pokemon Base Set",
        "insert": "Base Set",
        "subset_name": "Base",
        "parallel_name": "Base",
        "set_number": "4",
        "grader": "psa",
        "grade": "10",
    }
    fields.update(overrides)
    return identity("Charizard", **fields)


class FakeClock:
    """A monotonic clock that only moves when the provider sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Api:
    """A MockTransport handler serving PokéPrice fixtures by path, recording
    each request. A route is a fixture name (200) or (status, body, headers)."""

    def __init__(self, routes: dict | None = None, clock: FakeClock | None = None):
        self.routes = {
            SETS: "sets_base_set.json",
            CARDS: "cards_search_base_set.json",
            CHARIZARD: "card_charizard_ebay.json",
        }
        self.routes.update(routes or {})
        self.requests: list[httpx.Request] = []
        self.sent_at: list[float] = []
        self.clock = clock

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.clock is not None:
            self.sent_at.append(self.clock())
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json=fixture("error_404.json"))
        if isinstance(route, str):
            if route == "card_charizard_ebay.json" and not _wants_ebay(request):
                route = "card_charizard_basic.json"
            return httpx.Response(200, json=fixture(route))
        status, body, headers = route
        return httpx.Response(status, json=body, headers=headers)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def _wants_ebay(request: httpx.Request) -> bool:
    return request.url.params.get("includeEbay") == "true"


@pytest.fixture
def cache(tmp_path):
    with ValueCache(tmp_path / "values") as c:
        yield c


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def budget(cache, clock):
    return CreditBudget(cache, {"pokeprice": 100}, clock=clock)


@pytest.fixture
def api():
    return Api()


def make_provider(cache, budget, api, *, api_key=KEY, mono=None, **kwargs):
    mono = mono or FakeClock()
    return PokePriceProvider(
        api_key,
        cache,
        budget,
        transport=httpx.MockTransport(api),
        clock=mono,
        sleep=mono.sleep,
        **kwargs,
    )


@pytest.fixture
def provider(cache, budget, api):
    with make_provider(cache, budget, api) as p:
        yield p


# --- basics ------------------------------------------------------------------


def test_name_and_base_url():
    assert PokePriceProvider.name == "pokeprice"
    assert BASE_URL == "https://www.pokemonpricetracker.com/api/v2"


def test_credit_cost_covers_resolution_and_the_priced_lookup(provider):
    # /sets (1) + /cards search at limit 5 (5 x 1) + /cards/{id} with eBay (1 x 2)
    assert provider.credit_cost == 8


def test_requests_go_to_the_v2_api_with_bearer_auth_and_an_honest_ua(provider, api):
    provider.quote(charizard())
    assert api.requests
    for request in api.requests:
        assert str(request.url).startswith(BASE_URL + "/")
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        user_agent = request.headers["User-Agent"]
        assert user_agent.startswith(
            f"arenaclub-optimizer/{version('arenaclub-optimizer')}"
        )
        assert "github.com/jakedlr/arenaclub-optimizer" in user_agent


def test_every_request_passes_an_explicit_small_limit(provider, api):
    provider.quote(charizard())
    provider.quote(charizard(grader=None, grade=None))
    assert len(api.requests) == 4
    for request in api.requests:
        assert 1 <= int(request.url.params["limit"]) <= 5


def test_requests_are_spaced_at_least_a_second_apart(cache, budget, api):
    mono = FakeClock()
    api.clock = mono
    with make_provider(cache, budget, api, mono=mono) as provider:
        provider.quote(charizard())
    gaps = [b - a for a, b in zip(api.sent_at, api.sent_at[1:], strict=False)]
    assert len(gaps) == 2
    assert all(gap >= 1.0 for gap in gaps)


# --- resolving a card --------------------------------------------------------


def test_a_first_quote_resolves_the_set_then_the_card_then_prices_it(provider, api):
    result = provider.quote(charizard())

    assert isinstance(result, ValueQuote)
    assert api.paths == [SETS, CARDS, CHARIZARD]
    sets, cards, card = (request.url.params for request in api.requests)
    assert sets["search"] == "Base Set"
    assert sets["language"] == "english"
    assert sets["limit"] == "5"
    assert cards["setId"] == "604"
    assert cards["search"] == "Charizard 4"
    assert cards["language"] == "english"
    assert cards["limit"] == "5"
    assert "includeEbay" not in cards
    assert card["includeEbay"] == "true"
    assert card["language"] == "english"


def test_the_set_falls_back_to_the_subset_when_there_is_no_insert(provider, api):
    provider.quote(charizard(insert=None, subset_name="Evolving Skies"))
    assert api.requests[0].url.params["search"] == "Evolving Skies"


def test_a_base_subset_falls_back_to_the_set_name_without_year_and_brand(provider, api):
    provider.quote(charizard(insert=None, subset_name="Base"))
    assert api.requests[0].url.params["search"] == "Base Set"


def test_japanese_cards_query_the_japanese_database(provider, api):
    provider.quote(charizard(language="japanese"))
    for request in api.requests:
        assert request.url.params["language"] == "japanese"


def test_a_second_quote_for_the_same_card_makes_one_call(provider, api):
    provider.quote(charizard())
    api.requests.clear()

    result = provider.quote(charizard(grade="9"))

    assert isinstance(result, ValueQuote)
    assert api.paths == [CHARIZARD]


def test_the_tcgplayer_id_cache_is_permanent_across_provider_instances(
    cache, budget, api
):
    with make_provider(cache, budget, api) as first:
        first.quote(charizard())
    api.requests.clear()
    with make_provider(cache, budget, api) as second:
        second.quote(charizard())
    assert api.paths == [CHARIZARD]


def test_the_set_id_is_cached_by_set_name_and_language(provider, api):
    provider.quote(charizard())
    api.requests.clear()

    provider.quote(identity("Blastoise", insert="Base Set", set_number="2"))
    assert api.paths == [CARDS, f"{API_PATH}/cards/233295"]

    api.requests.clear()
    provider.quote(charizard(language="japanese"))
    assert api.paths[0] == SETS


def test_the_exact_set_name_wins_over_the_first_result(provider, api):
    api.routes[SETS] = (
        200,
        {
            "data": [
                {"name": "Base Set 2", "tcgPlayerNumericId": 605},
                {"name": "Base Set", "tcgPlayerNumericId": 604},
            ]
        },
        {},
    )
    provider.quote(charizard())
    assert api.requests[1].url.params["setId"] == "604"


def test_the_card_is_matched_by_normalised_number(provider, api):
    provider.quote(charizard(set_number="004"))
    assert api.paths[-1] == CHARIZARD


def test_a_card_without_a_number_is_searched_by_name(provider, api):
    provider.quote(charizard(set_number=None))
    assert api.requests[1].url.params["search"] == "Charizard"
    assert api.paths[-1] == CHARIZARD


def test_no_matching_set_is_unmatched(provider, api):
    api.routes[SETS] = "sets_empty.json"
    assert provider.quote(charizard()) == Unmatched()
    assert api.paths == [SETS]


def test_no_matching_card_is_unmatched(provider, api):
    api.routes[CARDS] = "cards_search_empty.json"
    assert provider.quote(charizard()) == Unmatched()
    assert api.paths == [SETS, CARDS]


def test_a_search_result_with_another_number_is_unmatched(provider, api):
    assert provider.quote(charizard(set_number="58")) == Unmatched()
    assert api.paths == [SETS, CARDS]


def test_an_unmatched_card_caches_no_tcgplayer_id(provider, api):
    api.routes[CARDS] = "cards_search_empty.json"
    provider.quote(charizard())
    api.routes[CARDS] = "cards_search_base_set.json"
    api.requests.clear()
    provider.quote(charizard())
    assert api.paths == [CARDS, CHARIZARD]


def test_a_404_on_the_card_is_unmatched(provider, api):
    api.routes[CHARIZARD] = (404, fixture("error_404.json"), {})
    assert provider.quote(charizard()) == Unmatched()


def celebrations_routes(api: Api) -> None:
    """Arena Club's "Celebrations CC" subset isn't a PokéPrice set name, so the
    set search falls back to "Celebrations", whose #4 is a different card."""
    api.routes[SETS] = (
        200,
        {"data": [{"name": "Celebrations", "tcgPlayerNumericId": 2867}]},
        {},
    )
    api.routes[CARDS] = (
        200,
        {"data": [{"tcgPlayerId": "250311", "name": "Palkia", "cardNumber": "4/25"}]},
        {},
    )


def test_a_number_match_with_another_name_is_unmatched(provider, api):
    celebrations_routes(api)
    assert provider.quote(charizard(insert="Celebrations CC")) == Unmatched()
    assert api.paths == [SETS, CARDS]


def test_a_fallback_set_is_not_cached_when_the_card_does_not_match(provider, api):
    celebrations_routes(api)
    provider.quote(charizard(insert="Celebrations CC"))
    api.requests.clear()
    provider.quote(charizard(insert="Celebrations CC"))
    assert api.paths == [SETS, CARDS]


def test_a_fallback_set_is_cached_once_a_card_matches_in_it(provider, api):
    api.routes[SETS] = (
        200,
        {"data": [{"name": "Base Set (Shadowless)", "tcgPlayerNumericId": 1663}]},
        {},
    )
    assert isinstance(provider.quote(charizard()), ValueQuote)
    api.requests.clear()

    provider.quote(identity("Blastoise", insert="Base Set", set_number="2"))

    assert api.paths[0] == CARDS
    assert api.requests[0].url.params["setId"] == "1663"


def test_a_tag_team_name_matches_across_slash_and_ampersand(provider, api):
    tag_team = {
        "tcgPlayerId": "201280",
        "name": "Greninja & Zoroark-GX",
        "cardNumber": "107/214",
    }
    decoy = {"tcgPlayerId": "1", "name": "Greninja", "cardNumber": "107"}
    api.routes[CARDS] = (200, {"data": [decoy, tag_team]}, {})
    body = fixture("card_charizard_ebay.json")
    body["data"].update(tag_team)
    api.routes[f"{API_PATH}/cards/201280"] = (200, body, {})

    result = provider.quote(
        identity("Greninja/Zoroark GX", insert="Unbroken Bonds", set_number="107")
    )

    assert isinstance(result, ValueQuote)
    assert api.paths[-1] == f"{API_PATH}/cards/201280"


def test_an_exact_name_match_wins_over_a_word_subset_match(provider, api, cache):
    dark = {"tcgPlayerId": "107004", "name": "Dark Charizard", "cardNumber": "4"}
    plain = {"tcgPlayerId": "233294", "name": "Charizard", "cardNumber": "4/102"}
    api.routes[CARDS] = (200, {"data": [dark, plain]}, {})

    result = provider.quote(charizard())

    assert isinstance(result, ValueQuote)
    assert api.paths[-1] == CHARIZARD
    assert cache.get_provider_id("pokeprice", "card:english|base set|4|charizard") == (
        "233294"
    )


def test_a_word_subset_match_is_still_used_when_nothing_matches_exactly(provider, api):
    secret = {
        "tcgPlayerId": "233294",
        "name": "Charizard (Secret)",
        "cardNumber": "4/102",
    }
    api.routes[CARDS] = (200, {"data": [secret]}, {})
    assert isinstance(provider.quote(charizard()), ValueQuote)
    assert api.paths[-1] == CHARIZARD


def test_without_a_name_a_single_number_match_is_enough(provider, api):
    result = provider.quote(identity(None, insert="Base Set", set_number="4"))
    assert isinstance(result, ValueQuote)
    assert api.requests[1].url.params["search"] == "4"
    assert api.paths[-1] == CHARIZARD


def test_the_card_search_sends_name_and_number(provider, api):
    provider.quote(charizard(set_number="004"))
    assert api.requests[1].url.params["search"] == "Charizard 004"


def test_a_404_on_a_cached_card_forgets_its_tcgplayer_id(provider, api):
    provider.quote(charizard())
    api.routes[CHARIZARD] = (404, fixture("error_404.json"), {})
    assert provider.quote(charizard()) == Unmatched()

    api.routes[CHARIZARD] = "card_charizard_ebay.json"
    api.requests.clear()
    assert isinstance(provider.quote(charizard()), ValueQuote)
    assert api.paths == [CARDS, CHARIZARD]


@pytest.mark.parametrize(("field", "value"), [("name", "Palkia"), ("cardNumber", "5")])
def test_a_cached_id_whose_card_differs_is_forgotten(provider, api, field, value):
    provider.quote(charizard())
    body = fixture("card_charizard_ebay.json")
    body["data"][field] = value
    api.routes[CHARIZARD] = (200, body, {})
    assert provider.quote(charizard()) == Unmatched()

    api.routes[CHARIZARD] = "card_charizard_ebay.json"
    api.requests.clear()
    assert isinstance(provider.quote(charizard()), ValueQuote)
    assert api.paths == [CARDS, CHARIZARD]


def test_a_fetched_card_without_name_or_number_is_accepted(provider, api):
    body = fixture("card_charizard_ebay.json")
    del body["data"]["name"], body["data"]["cardNumber"]
    api.routes[CHARIZARD] = (200, body, {})
    assert isinstance(provider.quote(charizard()), ValueQuote)


# --- pricing -----------------------------------------------------------------


def test_a_graded_quote_uses_the_smart_market_price(provider):
    result = provider.quote(charizard())
    assert result == ValueQuote(
        market_value=14650,
        source="pokeprice",
        as_of=date(2026, 11, 22),
        sales_count=23,
        confidence=ValueConfidence.HIGH,
    )


def test_an_outlier_grade_uses_the_median_price(provider):
    result = provider.quote(charizard(grade="9"))
    assert isinstance(result, ValueQuote)
    assert result.market_value == 1450
    assert result.sales_count == 78


def test_a_half_grade_maps_to_an_underscore_key(provider):
    result = provider.quote(charizard(grader="cgc", grade="9.5"))
    assert isinstance(result, ValueQuote)
    assert result.market_value == 8950
    assert result.as_of == date(2026, 11, 21)


@pytest.mark.parametrize(("grader", "grade"), [("PSA", "10"), ("psa", "10.0")])
def test_grader_case_and_a_trailing_zero_do_not_matter(provider, grader, grade):
    result = provider.quote(charizard(grader=grader, grade=grade))
    assert isinstance(result, ValueQuote)
    assert result.market_value == 14650


def test_fewer_than_five_sales_is_low_confidence(provider):
    result = provider.quote(charizard(grader="cgc", grade="9.5"))
    assert result.sales_count == 3
    assert result.confidence is ValueConfidence.LOW


def test_five_or_more_sales_is_high_confidence(provider, api):
    body = fixture("card_charizard_ebay.json")
    body["data"]["ebay"]["salesByGrade"]["psa10"]["count"] = 5
    api.routes[CHARIZARD] = (200, body, {})
    assert provider.quote(charizard()).confidence is ValueConfidence.HIGH
    body["data"]["ebay"]["salesByGrade"]["psa10"]["count"] = 4
    assert provider.quote(charizard()).confidence is ValueConfidence.LOW


def test_a_missing_grade_key_is_not_found(provider):
    assert provider.quote(charizard(grade="7")) == NotFound()


def test_a_card_without_ebay_data_is_not_found(provider, api):
    body = fixture("card_charizard_ebay.json")
    del body["data"]["ebay"]
    api.routes[CHARIZARD] = (200, body, {})
    assert provider.quote(charizard()) == NotFound()


@pytest.mark.parametrize(("grader", "grade"), [("ace", "9.3"), ("hga", "10")])
def test_a_grade_pokeprice_cannot_key_is_not_found_without_spending(
    provider, api, budget, grader, grade
):
    assert provider.quote(charizard(grader=grader, grade=grade)) == NotFound()
    assert api.requests == []
    assert budget.remaining("pokeprice") == 100


def test_a_raw_quote_uses_the_market_price_at_low_confidence(provider, api):
    result = provider.quote(charizard(grader=None, grade=None))
    assert result == ValueQuote(
        market_value=420,
        source="pokeprice",
        as_of=date(2026, 11, 23),
        sales_count=None,
        confidence=ValueConfidence.LOW,
    )
    assert "includeEbay" not in api.requests[-1].url.params


def test_a_raw_quote_without_a_market_price_is_not_found(provider, api):
    body = fixture("card_charizard_basic.json")
    body["data"]["prices"]["market"] = None
    api.routes[CHARIZARD] = (200, body, {})
    assert provider.quote(charizard(grader=None, grade=None)) == NotFound()


# --- credits -----------------------------------------------------------------


def test_a_first_quote_spends_set_search_and_priced_lookup(provider, budget):
    provider.quote(charizard())
    assert budget.remaining("pokeprice") == 100 - (1 + 5 + 2)


def test_a_cached_quote_spends_only_the_priced_lookup(provider, budget):
    provider.quote(charizard())
    provider.quote(charizard(grade="9"))
    assert budget.remaining("pokeprice") == 100 - 8 - 2


def test_a_raw_lookup_spends_one_credit_for_the_card(provider, budget):
    provider.quote(charizard(grader=None, grade=None))
    assert budget.remaining("pokeprice") == 100 - (1 + 5 + 1)


def test_the_budget_is_spent_before_each_request(cache, clock, api):
    budget = CreditBudget(cache, {"pokeprice": 3}, clock=clock)
    with make_provider(cache, budget, api) as provider:
        with pytest.raises(BudgetExhausted) as raised:
            provider.quote(charizard())
    assert api.paths == [SETS]
    assert budget.remaining("pokeprice") == 2
    assert raised.value.retry_after == TOMORROW_MIDNIGHT


def test_an_exhausted_budget_sends_nothing(cache, clock, api):
    budget = CreditBudget(cache, {"pokeprice": 0}, clock=clock)
    with make_provider(cache, budget, api) as provider:
        with pytest.raises(BudgetExhausted):
            provider.quote(charizard())
    assert api.requests == []


def test_estimated_cost_when_nothing_is_cached(provider):
    assert provider.estimated_cost(charizard()) == 8
    assert provider.estimated_cost(charizard(grader=None, grade=None)) == 7


def test_estimated_cost_with_only_the_set_cached(provider):
    provider.quote(charizard())
    blastoise = identity("Blastoise", insert="Base Set", set_number="2")
    assert provider.estimated_cost(blastoise) == 7
    raw = dataclasses.replace(blastoise, grader=None, grade=None)
    assert provider.estimated_cost(raw) == 6


def test_estimated_cost_with_the_tcgplayer_id_cached(provider):
    provider.quote(charizard())
    assert provider.estimated_cost(charizard(grade="9")) == 2
    assert provider.estimated_cost(charizard(grader=None, grade=None)) == 1


def test_estimated_cost_is_zero_when_a_quote_would_send_nothing(
    provider, cache, budget, api
):
    assert provider.estimated_cost(charizard(grader="hga")) == 0
    nowhere = identity(None, insert=None, subset_name=None, set_name=None)
    assert provider.estimated_cost(nowhere) == 0
    with make_provider(cache, budget, api, api_key=None) as keyless:
        assert keyless.estimated_cost(charizard()) == 0


def test_estimated_cost_sends_and_spends_nothing(provider, api, budget):
    provider.estimated_cost(charizard())
    assert api.requests == []
    assert budget.remaining("pokeprice") == 100


def test_with_seven_credits_left_a_cached_card_is_still_priced(cache, clock, api):
    budget = CreditBudget(cache, {"pokeprice": 15}, clock=clock)
    card = dataclasses.replace(
        checklist_card("card_2", grade="9"), identity=charizard(grade="9")
    )
    with make_provider(cache, budget, api) as provider:
        provider.quote(charizard())
        assert budget.remaining("pokeprice") == 7

        resolution = resolve_values([card], [provider], Overrides(), cache, budget)

    assert resolution.values["card_2"].market_value == 1450
    assert budget.remaining("pokeprice") == 5


# --- errors ------------------------------------------------------------------


def charged(api: Api, header: str) -> None:
    """The priced lookup answers with `X-API-Calls-Consumed: header`."""
    body = fixture("card_charizard_ebay.json")
    api.routes[CHARIZARD] = (200, body, {"X-API-Calls-Consumed": header})


def spend_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "X-API-Calls-Consumed" in r.getMessage()]


def test_a_server_charge_that_differs_from_the_local_spend_is_logged(
    provider, api, budget, caplog
):
    charged(api, "50")
    with caplog.at_level(logging.DEBUG, logger="arenaclub_optimizer.providers"):
        provider.quote(charizard())

    [record] = spend_records(caplog)
    assert record.levelno == logging.DEBUG
    assert record.name == "arenaclub_optimizer.providers.pokeprice"
    message = record.getMessage()
    assert "50" in message
    assert "2" in message
    assert "/cards/233294" in message
    assert KEY not in caplog.text
    # Only logged for now: the budget keeps the local spend.
    assert budget.remaining("pokeprice") == 100 - 8


@pytest.mark.parametrize("header", ["2", " 2 ", "lots", "-1", ""])
def test_a_matching_or_unreadable_server_charge_is_not_logged(
    provider, api, caplog, header
):
    charged(api, header)
    with caplog.at_level(logging.DEBUG, logger="arenaclub_optimizer.providers"):
        assert isinstance(provider.quote(charizard()), ValueQuote)
    assert spend_records(caplog) == []


def test_no_server_charge_header_logs_nothing(provider, caplog):
    with caplog.at_level(logging.DEBUG, logger="arenaclub_optimizer.providers"):
        provider.quote(charizard())
    assert spend_records(caplog) == []


def test_a_daily_429_exhausts_the_budget_and_uses_retry_after(provider, api, budget):
    api.routes[SETS] = (429, fixture("error_429_daily.json"), {"Retry-After": "43200"})
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert raised.value.retry_after == NOW + timedelta(seconds=43200)
    assert "daily" in raised.value.reason
    assert budget.remaining("pokeprice") == 0


def test_a_per_minute_429_uses_retry_after_and_keeps_the_budget(provider, api, budget):
    api.routes[SETS] = (
        429,
        fixture("error_429_per_minute.json"),
        {"Retry-After": "30"},
    )
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert raised.value.retry_after == NOW + timedelta(seconds=30)
    assert budget.remaining("pokeprice") == 99


def test_a_429_without_retry_after_uses_resets_at(provider, api):
    api.routes[SETS] = (429, fixture("error_429_daily.json"), {})
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert raised.value.retry_after == TOMORROW_MIDNIGHT


def test_a_429_without_a_limit_type_and_a_long_retry_after_is_daily(
    provider, api, budget
):
    body = {"error": "Rate limit exceeded", "message": "Too many requests"}
    api.routes[SETS] = (429, body, {"Retry-After": "7200"})
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert raised.value.retry_after == NOW + timedelta(hours=2)
    assert "daily" in raised.value.reason
    assert budget.remaining("pokeprice") == 0


@pytest.mark.parametrize("seconds", ["30", "3600"])
def test_a_429_without_a_limit_type_and_a_short_retry_after_is_not_daily(
    provider, api, budget, seconds
):
    body = {"error": "Rate limit exceeded", "message": "Too many requests"}
    api.routes[SETS] = (429, body, {"Retry-After": seconds})
    with pytest.raises(ProviderUnavailable):
        provider.quote(charizard())
    assert budget.remaining("pokeprice") == 99


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_disables_the_provider_for_the_run(provider, api, status):
    body = fixture(f"error_{status}.json")
    api.routes[SETS] = (status, body, {})
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert str(status) in raised.value.reason
    assert "key" in raised.value.reason

    api.requests.clear()
    with pytest.raises(ProviderUnavailable):
        provider.quote(charizard())
    assert api.requests == []


def test_a_server_error_makes_the_provider_unavailable(provider, api):
    api.routes[SETS] = (500, {"error": "Internal", "message": "boom"}, {})
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert "500" in raised.value.reason


def test_a_network_error_makes_the_provider_unavailable(cache, budget):
    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)

    with make_provider(cache, budget, fail) as provider:
        with pytest.raises(ProviderUnavailable):
            provider.quote(charizard())


def test_a_body_that_is_not_json_makes_the_provider_unavailable(cache, budget):
    def garbage(request):
        return httpx.Response(200, text="<html>oops</html>")

    with make_provider(cache, budget, garbage) as provider:
        with pytest.raises(ProviderUnavailable):
            provider.quote(charizard())


# --- the API key -------------------------------------------------------------


def test_a_missing_key_is_unavailable_on_the_first_quote(cache, budget, api):
    with make_provider(cache, budget, api, api_key=None) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
    assert raised.value.reason == KEY_NOT_SET == "PokéPrice key not set"
    assert api.requests == []
    assert budget.remaining("pokeprice") == 100


def test_without_a_key_the_needs_review_reason_says_so(cache, budget, api):
    with make_provider(cache, budget, api, api_key=None) as provider:
        resolution = resolve_values(
            [checklist_card("card_1", "Charizard")],
            [provider],
            Overrides(),
            cache,
            budget,
        )
    [item] = resolution.needs_review
    assert item.reason == "PokéPrice key not set"
    assert resolution.unavailable == {"pokeprice": "PokéPrice key not set"}


def test_with_a_key_the_resolver_gets_a_value(cache, budget, api):
    card = dataclasses.replace(checklist_card("card_1"), identity=charizard())
    with make_provider(cache, budget, api) as provider:
        resolution = resolve_values([card], [provider], Overrides(), cache, budget)
    assert resolution.needs_review == ()
    assert resolution.values["card_1"].source == "pokeprice"


def test_the_key_is_read_from_the_environment(tmp_path):
    environ = {API_KEY_ENV: f"  {KEY}\n"}
    assert API_KEY_ENV == "POKEPRICE_API_KEY"
    assert load_api_key(environ, env_file=tmp_path / ".env") == KEY


@pytest.mark.parametrize(
    "line",
    [
        f"POKEPRICE_API_KEY={KEY}",
        f'POKEPRICE_API_KEY="{KEY}"',
        f"export POKEPRICE_API_KEY='{KEY}'",
        f"POKEPRICE_API_KEY = {KEY}  ",
    ],
)
def test_the_key_is_read_from_a_dotenv_file(tmp_path, line):
    env_file = tmp_path / ".env"
    env_file.write_text(f"# PokéPrice\nOTHER=1\n\n{line}\n", encoding="utf-8")
    assert load_api_key({}, env_file=env_file) == KEY


def test_the_environment_wins_over_the_dotenv_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POKEPRICE_API_KEY=from-file\n", encoding="utf-8")
    assert load_api_key({API_KEY_ENV: "from-env"}, env_file=env_file) == "from-env"


def test_an_empty_or_missing_key_is_none(tmp_path):
    assert load_api_key({}, env_file=tmp_path / "missing.env") is None
    assert load_api_key({API_KEY_ENV: "   "}, env_file=tmp_path / ".env") is None


def test_the_dotenv_file_defaults_to_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    (tmp_path / ".env").write_text(f"POKEPRICE_API_KEY={KEY}\n", encoding="utf-8")
    assert load_api_key() == KEY


def test_from_environment_reads_the_key(cache, budget, api, tmp_path):
    env_file = tmp_path / ".env"
    with PokePriceProvider.from_environment(
        cache,
        budget,
        environ={API_KEY_ENV: KEY},
        env_file=env_file,
        transport=httpx.MockTransport(api),
        sleep=lambda seconds: None,
    ) as provider:
        provider.quote(charizard())
    assert api.requests[0].headers["Authorization"] == f"Bearer {KEY}"


def test_from_environment_without_a_key_is_unavailable(cache, budget, tmp_path):
    with PokePriceProvider.from_environment(
        cache, budget, environ={}, env_file=tmp_path / ".env"
    ) as provider:
        with pytest.raises(ProviderUnavailable, match="PokéPrice key not set"):
            provider.quote(charizard())


def test_a_malformed_key_is_unavailable_without_echoing_it(cache, budget, api):
    bad_key = "bad key\nwith-é-SECRET"
    with make_provider(cache, budget, api, api_key=bad_key) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
        assert "SECRET" not in repr(provider)
    assert api.requests == []
    assert "SECRET" not in _exception_text(raised.value)


def _exception_text(error: BaseException) -> str:
    """str and repr of `error` and every exception it chains to."""
    parts = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts += [str(current), repr(current), repr(current.args)]
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def _error_routes() -> list[tuple[str, tuple]]:
    """Each failing response, with the key echoed back in its body."""
    echo = {"error": "Unauthorized", "message": f"bad key {KEY}"}
    return [
        ("401", (401, echo, {})),
        ("403", (403, echo, {})),
        ("429", (429, {**fixture("error_429_daily.json"), "key": KEY}, {})),
        ("500", (500, echo, {})),
        ("null body", (200, None, {})),
    ]


@pytest.mark.parametrize(("label", "route"), _error_routes())
def test_the_key_never_appears_in_errors_or_logs(cache, budget, caplog, label, route):
    caplog.set_level(logging.DEBUG)
    api = Api({SETS: route})
    with make_provider(cache, budget, api) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
        assert KEY not in repr(provider)
        assert KEY not in str(vars(provider))
    assert KEY not in _exception_text(raised.value)
    assert KEY not in caplog.text


def _traceback_text(error: BaseException) -> str:
    """`error`'s full traceback, with every frame's locals."""
    report = traceback.TracebackException.from_exception(error, capture_locals=True)
    return "".join(report.format())


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_the_key_is_not_in_a_traceback_with_locals(cache, budget, status):
    api = Api({SETS: (status, {"error": "x", "message": "y"}, {"Retry-After": "5"})})
    with make_provider(cache, budget, api) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
    assert api.requests
    assert KEY not in _traceback_text(raised.value)


def test_a_network_error_keeps_no_request_reachable_from_the_error(cache, budget):
    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)

    with make_provider(cache, budget, fail) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert KEY not in _traceback_text(raised.value)


def test_the_key_is_not_in_the_traceback_of_an_unexpected_error(
    cache, budget, monkeypatch
):
    def broken_exhaust(provider, *, now=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(budget, "exhaust", broken_exhaust)
    api = Api({SETS: (429, fixture("error_429_daily.json"), {"Retry-After": "60"})})
    with make_provider(cache, budget, api) as provider:
        with pytest.raises(sqlite3.OperationalError) as raised:
            provider.quote(charizard())
    assert KEY not in _traceback_text(raised.value)


def test_the_key_never_appears_in_logs_on_success(cache, budget, api, caplog):
    caplog.set_level(logging.DEBUG)
    with make_provider(cache, budget, api) as provider:
        provider.quote(charizard())
        provider.quote(charizard(grader=None, grade=None))
    assert api.requests
    assert KEY not in caplog.text


def test_the_key_never_appears_in_the_value_cache(cache, budget, api):
    with make_provider(cache, budget, api) as provider:
        provider.quote(charizard())
    cache.close()
    assert KEY.encode() not in cache.path.read_bytes()


def test_no_fixture_holds_a_key_or_auth_header():
    for path in FIXTURES.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "Bearer" not in text, path.name
        assert "Authorization" not in text, path.name
        assert KEY not in text, path.name


# --- lasting or temporary --------------------------------------------------------


@pytest.mark.parametrize("api_key", [None, "bad key"])
def test_a_missing_or_malformed_key_is_a_lasting_unavailability(
    cache, budget, api, api_key
):
    with make_provider(cache, budget, api, api_key=api_key) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
    assert raised.value.lasting is True


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_a_lasting_unavailability(provider, api, status):
    api.routes[SETS] = (status, fixture(f"error_{status}.json"), {})
    for _ in range(2):
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
        assert raised.value.lasting is True


@pytest.mark.parametrize(
    "route",
    [
        (429, "error_429_daily.json", {"Retry-After": "43200"}),
        (429, "error_429_per_minute.json", {"Retry-After": "30"}),
        (500, "error_404.json", {}),
    ],
)
def test_limits_and_server_errors_are_temporary(provider, api, route):
    status, body, headers = route
    api.routes[SETS] = (status, fixture(body), headers)
    with pytest.raises(ProviderUnavailable) as raised:
        provider.quote(charizard())
    assert raised.value.lasting is False


def test_a_network_error_is_temporary(cache, budget):
    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)

    with make_provider(cache, budget, fail) as provider:
        with pytest.raises(ProviderUnavailable) as raised:
            provider.quote(charizard())
    assert raised.value.lasting is False
