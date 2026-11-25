"""CardVolumeProvider against recorded CardVolume responses. No network."""

import json
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from fake_values import NOW, Clock, identity

from arenaclub_optimizer.model import CardIdentity, Language, ValueConfidence
from arenaclub_optimizer.providers.cardvolume import (
    INDEX_FILENAME,
    CardVolumeProvider,
    match_card,
)
from arenaclub_optimizer.values import (
    CreditBudget,
    NotFound,
    ProviderUnavailable,
    Unmatched,
    ValueCache,
    ValueProvider,
    ValueQuote,
    default_cache_dir,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cardvolume"
INDEX_PATH = "/api/v1/cards.json"
UMBREON = "pokemon--evolving-skies--umbreon-vmax-215-203"
MEGA = "pokemon--mega-evolution-black-star-promos--mega-charizard-x-ex-023"
UMBREON_PATH = f"/api/v1/cards/{UMBREON}.json"
MEGA_PATH = f"/api/v1/cards/{MEGA}.json"
ETAG = 'W/"27237-6610424ae21f5"'
LAST_MODIFIED = "Tue, 24 Nov 2026 11:38:03 GMT"
USER_AGENT = (
    f"arenaclub-optimizer/{version('arenaclub-optimizer')} "
    "(+https://github.com/jakedlr/arenaclub-optimizer)"
)
UMBREON_CITATION = (
    "Umbreon VMAX 215/203 (Evolving Skies). CardVolume, "
    "https://cardvolume.com/pokemon/evolving-skies/umbreon-vmax-215-203/ "
    "(accessed 2026-11-24)."
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


INDEX = fixture("cards.json")
INDEX_CARDS = INDEX["cards"]


def mega_identity(**overrides) -> CardIdentity:
    """Mega Charizard X ex 023 as Arena Club describes it (no insert)."""
    fields = {
        "year": 2025,
        "set_name": "2025 Pokemon Mega Evolution",
        "insert": None,
        "subset_name": "Promo",
        "parallel_name": "Mega Charizard X ex Ultra-Premium Collection",
        "card_name": "Mega Charizard X ex",
        "set_number": "023",
        "grader": "psa",
        "grade": "10",
    }
    fields.update(overrides)
    return CardIdentity(**fields)


class FakeMonotonic:
    """A monotonic clock that only moves when the provider sleeps or a test ticks."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """A MockTransport handler serving the fixtures, recording each request.

    `routes` maps a path to an httpx.Response (or a callable returning one);
    the index answers 304 to a matching If-None-Match. Unknown paths get
    CardVolume's 404 HTML page.
    """

    def __init__(self, monotonic: FakeMonotonic | None = None) -> None:
        self.monotonic = monotonic
        self.requests: list[httpx.Request] = []
        self.sent_at: list[float] = []
        self.index = INDEX
        self.etag: str | None = ETAG
        self.last_modified: str | None = LAST_MODIFIED
        self.routes: dict[str, object] = {
            UMBREON_PATH: lambda: httpx.Response(200, json=fixture(f"{UMBREON}.json")),
            MEGA_PATH: lambda: httpx.Response(200, json=fixture(f"{MEGA}.json")),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.monotonic is not None:
            self.sent_at.append(self.monotonic())
        path = request.url.path
        if path in self.routes:
            route = self.routes[path]
            return route() if callable(route) else route
        if path == INDEX_PATH:
            return self._index(request)
        return httpx.Response(
            404,
            content=(FIXTURES / "not_found.html").read_bytes(),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    def _index(self, request: httpx.Request) -> httpx.Response:
        if self.etag is not None and request.headers.get("If-None-Match") == self.etag:
            return httpx.Response(304, headers={"ETag": self.etag})
        headers = {}
        if self.etag is not None:
            headers["ETag"] = self.etag
        if self.last_modified is not None:
            headers["Last-Modified"] = self.last_modified
        return httpx.Response(200, json=self.index, headers=headers)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def make_provider(
    tmp_path: Path,
    server: Server,
    *,
    utc_now: Clock | None = None,
    monotonic: FakeMonotonic | None = None,
    **kwargs,
) -> CardVolumeProvider:
    monotonic = monotonic or server.monotonic or FakeMonotonic()
    return CardVolumeProvider(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(server),
        clock=monotonic,
        sleep=monotonic.sleep,
        utc_now=utc_now or Clock(),
        **kwargs,
    )


@pytest.fixture
def server() -> Server:
    return Server(FakeMonotonic())


@pytest.fixture
def provider(tmp_path, server):
    with make_provider(tmp_path, server) as provider:
        yield provider


# --- the provider ------------------------------------------------------------


def test_is_a_free_value_provider_named_cardvolume(provider):
    as_protocol: ValueProvider = provider
    assert as_protocol.name == "cardvolume"
    assert as_protocol.credit_cost == 0


def test_quotes_umbreon_vmax_psa_10_at_its_market_price(provider):
    result = provider.quote(identity())

    assert result == ValueQuote(
        market_value=3800.0,
        source="cardvolume",
        as_of=date(2026, 11, 24),
        sales_count=47,
        confidence=ValueConfidence.HIGH,
        citation=UMBREON_CITATION,
    )


def test_fetches_the_index_then_the_matched_card(provider, server):
    provider.quote(identity())

    assert [str(r.url) for r in server.requests] == [
        "https://cardvolume.com/api/v1/cards.json",
        f"https://cardvolume.com/api/v1/cards/{UMBREON}.json",
    ]
    assert all(r.method == "GET" for r in server.requests)


def test_every_request_sends_the_honest_user_agent(provider, server):
    provider.quote(identity())
    provider.quote(mega_identity())

    assert len(server.requests) == 3
    assert all(r.headers["User-Agent"] == USER_AGENT for r in server.requests)


def test_requests_are_spaced_at_least_one_second_apart(provider, server):
    provider.quote(identity())
    provider.quote(mega_identity())

    gaps = [b - a for a, b in zip(server.sent_at, server.sent_at[1:], strict=False)]
    assert gaps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_quotes_the_mega_charizard_promo_as_arena_club_names_it(provider):
    result = provider.quote(mega_identity())

    assert isinstance(result, ValueQuote)
    assert result.market_value == 132.5
    assert result.sales_count == 23
    assert result.confidence is ValueConfidence.HIGH
    assert result.as_of == date(2026, 11, 24)
    assert result.citation is not None
    assert result.citation.startswith("Mega Charizard X ex 023/132")


def test_grader_matches_case_insensitively(provider):
    result = provider.quote(identity(grader="PSA"))
    assert isinstance(result, ValueQuote)
    assert result.market_value == 3800.0


def test_exact_grade_wins_over_a_suffixed_one(provider):
    result = provider.quote(mega_identity(grader="cgc", grade="10"))

    assert result == ValueQuote(
        market_value=172.41,
        source="cardvolume",
        as_of=date(2026, 9, 12),
        sales_count=1,
        confidence=ValueConfidence.LOW,
        citation=fixture(f"{MEGA}.json")["cite_as"],
    )


def test_a_grade_with_a_suffix_matches_its_number(tmp_path, server):
    card = fixture(f"{MEGA}.json")
    card["grades"] = [g for g in card["grades"] if g["grade"] != "10"]
    server.routes[MEGA_PATH] = httpx.Response(200, json=card)

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(mega_identity(grader="cgc", grade="10"))

    assert isinstance(result, ValueQuote)
    assert result.market_value == 229.3
    assert result.as_of == date(2026, 9, 22)


def test_without_a_market_price_quotes_the_last_verified_sale_at_low_confidence(
    provider,
):
    result = provider.quote(identity(grade="7"))

    assert result == ValueQuote(
        market_value=1677.7,
        source="cardvolume",
        as_of=date(2026, 9, 20),
        sales_count=1,
        confidence=ValueConfidence.LOW,
        citation=UMBREON_CITATION,
    )


def test_without_a_market_price_or_a_sale_is_not_found(tmp_path, server):
    card = fixture(f"{UMBREON}.json")
    for grade in card["grades"]:
        if grade["grade"] == "7":
            grade["last_verified_sale"] = None
    server.routes[UMBREON_PATH] = httpx.Response(200, json=card)

    with make_provider(tmp_path, server) as provider:
        assert provider.quote(identity(grade="7")) == NotFound()


def test_a_grade_the_card_has_no_entry_for_is_not_found(provider):
    assert provider.quote(identity(grade="8")) == NotFound()
    assert provider.quote(identity(grader="bgs")) == NotFound()


def test_a_grade_needs_its_whole_number_not_just_a_prefix(provider):
    # "1" must not match "10".
    assert provider.quote(identity(grade="1")) == NotFound()


def test_an_ungraded_card_is_not_found_without_fetching_it(provider, server):
    assert provider.quote(identity(grader=None, grade=None)) == NotFound()
    assert server.paths() == [INDEX_PATH]


def test_a_card_missing_from_the_index_is_unmatched_without_fetching(provider, server):
    assert provider.quote(identity("Mewtwo", set_number="10")) == Unmatched()
    assert server.paths() == [INDEX_PATH]


def test_a_card_page_answering_404_html_is_unmatched(tmp_path, server):
    del server.routes[UMBREON_PATH]

    with make_provider(tmp_path, server) as provider:
        assert provider.quote(identity()) == Unmatched()
    assert server.paths() == [INDEX_PATH, UMBREON_PATH]


def test_a_card_is_fetched_once_per_provider(provider, server):
    provider.quote(identity(grade="10"))
    provider.quote(identity(grade="9"))

    assert server.paths() == [INDEX_PATH, UMBREON_PATH]


# --- matching ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "pokemon/evolving-skies/umbreon-vmax-215-203"),
        ({"card_name": "UMBREON VMAX"}, "pokemon/evolving-skies/umbreon-vmax-215-203"),
        ({"set_number": "215/203"}, "pokemon/evolving-skies/umbreon-vmax-215-203"),
        ({"set_number": "0215"}, "pokemon/evolving-skies/umbreon-vmax-215-203"),
        (
            {"insert": None, "subset_name": "Evolving Skies"},
            "pokemon/evolving-skies/umbreon-vmax-215-203",
        ),
        (
            {
                "card_name": "Moltres/Zapdos/Articuno GX",
                "insert": "SM Black Star Promos",
                "set_number": "SM210",
                "year": 2019,
            },
            "pokemon/sm-black-star-promos/moltres-and-zapdos-and-articuno-gx-sm210",
        ),
        (
            {
                "card_name": "Moltres & Zapdos & Articuno-GX",
                "insert": "SM Black Star Promos",
                "set_number": "SM210",
            },
            "pokemon/sm-black-star-promos/moltres-and-zapdos-and-articuno-gx-sm210",
        ),
        (
            {
                "card_name": "Mega Charizard X EX",
                "insert": "Mega Evolution Promo Cards",
                "set_number": "023",
            },
            "pokemon/mega-evolution-black-star-promos/mega-charizard-x-ex-023",
        ),
        (
            {
                "card_name": "Mega Charizard X-ex",
                "insert": "Mega Evolution Promo Cards",
                "set_number": "23",
            },
            "pokemon/mega-evolution-black-star-promos/mega-charizard-x-ex-023",
        ),
        (
            {
                "card_name": "Radiant Charizard",
                "insert": "Crown Zenith",
                "set_number": "020",
            },
            "pokemon/crown-zenith/radiant-charizard-020-159",
        ),
        (
            {
                "card_name": "Mewtwo VSTAR",
                "insert": "Crown Zenith Galarian Gallery",
                "set_number": "GG44",
            },
            "pokemon/crown-zenith-galarian-gallery/mewtwo-vstar-gg44-gg70",
        ),
        (
            {
                "card_name": "Charizard",
                "insert": "Japanese Base Set",
                "set_number": "6",
                "language": Language.JAPANESE,
            },
            "pokemon/japanese-base-set/charizard-no-rarity-006-japanese",
        ),
    ],
)
def test_match_card_normalises_both_sides(fields, expected):
    entry = match_card(INDEX_CARDS, identity(**fields))
    assert entry is not None
    assert entry["id"] == expected


def test_match_card_finds_a_promo_by_name_and_number_in_a_promo_set():
    entry = match_card(INDEX_CARDS, mega_identity())
    assert entry is not None
    assert entry["id"] == (
        "pokemon/mega-evolution-black-star-promos/mega-charizard-x-ex-023"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"set_number": "214"},
        {"set_number": "21"},
        {"card_name": "Umbreon ex"},
        {"card_name": "Umbreon"},
        {"insert": "Fusion Strike"},
        {"insert": None, "subset_name": None},
        {"set_number": None},
        {"card_name": None},
        {"language": Language.JAPANESE},
        # A Japanese card isn't matched for an English identity.
        {"card_name": "Charizard", "insert": "Japanese Base Set", "set_number": "6"},
        # Same name, another set's number.
        {
            "card_name": "Mega Charizard X ex",
            "insert": "Mega Evolution Promo Cards",
            "set_number": "125",
        },
    ],
)
def test_match_card_rejects_near_misses(fields):
    assert match_card(INDEX_CARDS, identity(**fields)) is None


def test_match_card_skips_malformed_entries():
    cards = [{"id": 1}, "junk", {"name": None}, *INDEX_CARDS]
    entry = match_card(cards, identity())
    assert entry is not None
    assert entry["id"] == "pokemon/evolving-skies/umbreon-vmax-215-203"


# --- the index cache ---------------------------------------------------------


def test_the_index_is_fetched_once_per_provider(provider, server):
    provider.quote(identity())
    provider.quote(mega_identity())
    provider.quote(identity("Nobody"))

    assert server.paths().count(INDEX_PATH) == 1


def test_the_index_is_cached_on_disk_next_to_the_value_cache(tmp_path, server):
    with make_provider(tmp_path, server) as provider:
        provider.quote(identity())

    assert (tmp_path / INDEX_FILENAME).is_file()
    with ValueCache(tmp_path) as cache:
        assert cache.path.parent == (tmp_path / INDEX_FILENAME).parent


def test_the_default_cache_dir_is_the_value_cache_dir(tmp_path):
    with CardVolumeProvider() as provider:
        assert provider.index_path == default_cache_dir() / INDEX_FILENAME


def test_a_fresh_index_on_disk_is_not_downloaded_again(tmp_path, server):
    clock = Clock()
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        provider.quote(identity())

    clock.now = NOW + timedelta(hours=23)
    later = Server(FakeMonotonic())
    with make_provider(tmp_path, later, utc_now=clock) as provider:
        assert isinstance(provider.quote(identity()), ValueQuote)

    assert later.paths() == [UMBREON_PATH]


def test_the_first_download_sends_no_conditional_headers(provider, server):
    provider.quote(identity())

    index_request = server.requests[0]
    assert "If-None-Match" not in index_request.headers
    assert "If-Modified-Since" not in index_request.headers


def test_a_day_old_index_is_revalidated_with_its_etag_and_date(tmp_path, server):
    clock = Clock()
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        provider.quote(identity())

    clock.now = NOW + timedelta(hours=25)
    later = Server(FakeMonotonic())
    with make_provider(tmp_path, later, utc_now=clock) as provider:
        assert isinstance(provider.quote(identity()), ValueQuote)

    request = later.requests[0]
    assert request.url.path == INDEX_PATH
    assert request.headers["If-None-Match"] == ETAG
    assert request.headers["If-Modified-Since"] == LAST_MODIFIED


def test_a_304_keeps_the_cached_index_and_restarts_its_day(tmp_path, server):
    clock = Clock()
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        provider.quote(identity())

    clock.now = NOW + timedelta(hours=25)
    revalidated = Server(FakeMonotonic())
    with make_provider(tmp_path, revalidated, utc_now=clock) as provider:
        provider.quote(identity())

    clock.now = NOW + timedelta(hours=30)
    third = Server(FakeMonotonic())
    with make_provider(tmp_path, third, utc_now=clock) as provider:
        assert isinstance(provider.quote(identity()), ValueQuote)

    assert revalidated.paths() == [INDEX_PATH, UMBREON_PATH]
    assert third.paths() == [UMBREON_PATH]


def test_only_the_validators_the_server_sent_are_used(tmp_path, server):
    server.etag = None
    clock = Clock()
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        provider.quote(identity())

    clock.now = NOW + timedelta(days=2)
    later = Server(FakeMonotonic())
    with make_provider(tmp_path, later, utc_now=clock) as provider:
        provider.quote(identity())

    request = later.requests[0]
    assert "If-None-Match" not in request.headers
    assert request.headers["If-Modified-Since"] == LAST_MODIFIED


def test_a_changed_index_replaces_the_cached_one(tmp_path, server):
    clock = Clock()
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        assert provider.quote(identity("Nobody")) == Unmatched()

    clock.now = NOW + timedelta(days=2)
    later = Server(FakeMonotonic())
    later.etag = 'W/"new"'
    later.index = {
        **INDEX,
        "cards": [
            *INDEX_CARDS,
            {**INDEX_CARDS[0], "id": "pokemon/x/nobody-215", "name": "Nobody"},
        ],
    }
    later.routes["/api/v1/cards/pokemon--x--nobody-215.json"] = lambda: httpx.Response(
        200, json=fixture(f"{UMBREON}.json")
    )
    with make_provider(tmp_path, later, utc_now=clock) as provider:
        assert isinstance(provider.quote(identity("Nobody")), ValueQuote)

    clock.now += timedelta(hours=1)
    third = Server(FakeMonotonic())
    third.routes = later.routes
    with make_provider(tmp_path, third, utc_now=clock) as provider:
        assert isinstance(provider.quote(identity("Nobody")), ValueQuote)
    assert INDEX_PATH not in third.paths()


def test_an_unreadable_index_file_is_downloaded_again(tmp_path, server):
    (tmp_path / INDEX_FILENAME).write_text("{not json", encoding="utf-8")

    with make_provider(tmp_path, server) as provider:
        assert isinstance(provider.quote(identity()), ValueQuote)

    assert server.paths() == [INDEX_PATH, UMBREON_PATH]
    assert "If-None-Match" not in server.requests[0].headers


def test_an_index_from_the_future_is_downloaded_again(tmp_path, server):
    clock = Clock(NOW + timedelta(days=3))
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        provider.quote(identity())

    clock.now = NOW
    later = Server(FakeMonotonic())
    with make_provider(tmp_path, later, utc_now=clock) as provider:
        provider.quote(identity())

    assert later.paths()[0] == INDEX_PATH


# --- errors ------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
@pytest.mark.parametrize("path", [INDEX_PATH, UMBREON_PATH])
def test_rate_limits_and_server_errors_raise_provider_unavailable(
    tmp_path, server, status, path
):
    server.routes[path] = httpx.Response(status, text="busy")

    with make_provider(tmp_path, server) as provider:
        with pytest.raises(ProviderUnavailable) as caught:
            provider.quote(identity())

    assert str(status) in caught.value.reason


@pytest.mark.parametrize("status", [403, 429])
def test_a_refusal_exhausts_todays_budget_when_given_one(tmp_path, server, status):
    server.routes[UMBREON_PATH] = httpx.Response(status, text="slow down")
    clock = Clock()
    with ValueCache(tmp_path) as cache:
        budget = CreditBudget(cache, {}, clock=clock)
        with make_provider(tmp_path, server, utc_now=clock, budget=budget) as p:
            with pytest.raises(ProviderUnavailable) as caught:
                p.quote(identity())

        assert not budget.can_spend("cardvolume", 0)
        assert caught.value.retry_after == budget.resets_at()


def test_a_403_is_treated_like_a_429(tmp_path, server):
    server.routes[UMBREON_PATH] = httpx.Response(403, text="forbidden")

    with make_provider(tmp_path, server) as provider:
        with pytest.raises(ProviderUnavailable, match="403"):
            provider.quote(identity())


def test_a_server_error_does_not_exhaust_the_budget(tmp_path, server):
    server.routes[UMBREON_PATH] = httpx.Response(503, text="down")
    clock = Clock()
    with ValueCache(tmp_path) as cache:
        budget = CreditBudget(cache, {}, clock=clock)
        with make_provider(tmp_path, server, utc_now=clock, budget=budget) as p:
            with pytest.raises(ProviderUnavailable):
                p.quote(identity())

        assert budget.can_spend("cardvolume", 0)


def test_a_network_failure_raises_provider_unavailable(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    with make_provider(tmp_path, handler, monotonic=FakeMonotonic()) as provider:
        with pytest.raises(ProviderUnavailable):
            provider.quote(identity())


@pytest.mark.parametrize("path", [INDEX_PATH, UMBREON_PATH])
def test_a_body_that_is_not_json_raises_provider_unavailable(tmp_path, server, path):
    server.routes[path] = httpx.Response(200, text="<html>maintenance</html>")

    with make_provider(tmp_path, server) as provider:
        with pytest.raises(ProviderUnavailable):
            provider.quote(identity())


def test_an_index_without_a_cards_list_raises_provider_unavailable(tmp_path, server):
    server.index = {"as_of": "2026-11-24"}

    with make_provider(tmp_path, server) as provider:
        with pytest.raises(ProviderUnavailable):
            provider.quote(identity())


def test_an_unknown_client_error_raises_provider_unavailable(tmp_path, server):
    server.routes[UMBREON_PATH] = httpx.Response(400, text="bad")

    with make_provider(tmp_path, server) as provider:
        with pytest.raises(ProviderUnavailable, match="400"):
            provider.quote(identity())


def test_an_index_id_with_unsafe_characters_is_unmatched_without_a_request(
    tmp_path, server
):
    server.index = {
        **INDEX,
        "cards": [{**INDEX_CARDS[0], "id": "pokemon/../../etc/umbreon"}],
    }

    with make_provider(tmp_path, server) as provider:
        assert provider.quote(identity()) == Unmatched()
    assert server.paths() == [INDEX_PATH]


# --- review follow-ups: promo matching and ambiguity -------------------------


def entry(card_id: str, name: str, set_name: str, number: str, year: int) -> dict:
    return {
        "id": card_id,
        "name": name,
        "category": "pokemon",
        "set": set_name,
        "number": number,
        "year": year,
        "language": "English",
    }


WOTC_PIKACHU = entry(
    "pokemon/wotc-promo/pikachu-27", "Pikachu", "Wizards Promo Cards", "27", 2000
)
SV_PIKACHU = entry(
    "pokemon/sv-promo/pikachu-027",
    "Pikachu",
    "Scarlet & Violet Promo Cards",
    "027",
    2023,
)


def promo_identity(name: str, number: str, year: int | None) -> CardIdentity:
    return CardIdentity(
        year=year,
        set_name=f"{year} Pokemon",
        insert=None,
        subset_name="Promo",
        card_name=name,
        set_number=number,
        grader="psa",
        grade="10",
    )


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2023, SV_PIKACHU["id"]), (2000, WOTC_PIKACHU["id"])],
)
def test_the_promo_wildcard_needs_the_year_to_match(year, expected):
    cards = [WOTC_PIKACHU, SV_PIKACHU]
    found = match_card(cards, promo_identity("Pikachu", "027", year))
    assert found is not None
    assert found["id"] == expected


def test_the_promo_wildcard_rejects_another_years_promo():
    assert match_card([WOTC_PIKACHU], promo_identity("Pikachu", "27", 2023)) is None
    assert match_card(INDEX_CARDS, mega_identity(year=2024)) is None


def test_several_promo_matches_without_a_year_are_unmatched():
    cards = [WOTC_PIKACHU, SV_PIKACHU]
    assert match_card(cards, promo_identity("Pikachu", "27", None)) is None


def test_an_ambiguous_match_is_unmatched():
    twin = {**INDEX_CARDS[0], "id": "pokemon/evolving-skies/umbreon-vmax-215-twin"}
    assert match_card([*INDEX_CARDS, twin], identity()) is None


def test_the_promo_wildcard_matches_plural_promos_sets():
    charizard = entry(
        "pokemon/sm-black-star-promos/charizard-gx-sm211",
        "Charizard-GX",
        "SM Black Star Promos",
        "SM211/248",
        2019,
    )
    found = match_card(
        [*INDEX_CARDS, charizard], promo_identity("Charizard GX", "SM211", 2019)
    )
    assert found is not None
    assert found["id"] == charizard["id"]


def test_the_plural_promos_wildcard_still_needs_year_and_number():
    found = match_card(
        INDEX_CARDS, promo_identity("Moltres & Zapdos & Articuno GX", "SM210", 2019)
    )
    assert found is not None
    assert (
        match_card(
            INDEX_CARDS, promo_identity("Moltres & Zapdos & Articuno GX", "SM210", 2020)
        )
        is None
    )
    assert (
        match_card(
            INDEX_CARDS, promo_identity("Moltres & Zapdos & Articuno GX", "SM211", 2019)
        )
        is None
    )


# --- review follow-ups: an unchecked promo year ------------------------------


def index_with_mega(server: Server, **changes) -> None:
    """Serve the index with the Mega Charizard promo entry changed; a change
    to None removes the key."""

    def changed(card: dict) -> dict:
        if not card["id"].endswith("mega-charizard-x-ex-023"):
            return card
        merged = {**card, **changes}
        return {k: v for k, v in merged.items() if v is not None}

    server.index = {**INDEX, "cards": [changed(card) for card in INDEX_CARDS]}


def test_a_promo_matched_without_the_identitys_year_is_low_confidence(provider):
    result = provider.quote(mega_identity(year=None))

    assert isinstance(result, ValueQuote)
    assert result.market_value == 132.5
    assert result.confidence is ValueConfidence.LOW


def test_a_promo_matched_without_the_index_year_is_low_confidence(tmp_path, server):
    index_with_mega(server, year=None)
    with make_provider(tmp_path, server) as provider:
        result = provider.quote(mega_identity())

    assert isinstance(result, ValueQuote)
    assert result.confidence is ValueConfidence.LOW


def test_a_promo_whose_index_year_is_a_matching_string_is_low_confidence(
    tmp_path, server
):
    index_with_mega(server, year="2025")
    with make_provider(tmp_path, server) as provider:
        result = provider.quote(mega_identity())

    assert isinstance(result, ValueQuote)
    assert result.confidence is ValueConfidence.LOW


def test_a_promo_whose_index_year_is_another_years_string_is_unmatched(
    tmp_path, server
):
    index_with_mega(server, year="2023")
    with make_provider(tmp_path, server) as provider:
        assert provider.quote(mega_identity()) == Unmatched()


def test_match_card_compares_a_string_year_as_a_number():
    wotc = {**WOTC_PIKACHU, "year": " 2000 "}
    assert match_card([wotc], promo_identity("Pikachu", "27", 2023)) is None
    found = match_card([wotc], promo_identity("Pikachu", "27", 2000))
    assert found is not None
    assert found["id"] == WOTC_PIKACHU["id"]


def test_a_promo_matched_on_both_years_stays_high_confidence(provider):
    result = provider.quote(mega_identity())
    assert isinstance(result, ValueQuote)
    assert result.confidence is ValueConfidence.HIGH


def test_a_named_set_without_a_year_stays_high_confidence(provider):
    result = provider.quote(identity(year=None))
    assert isinstance(result, ValueQuote)
    assert result.confidence is ValueConfidence.HIGH


# --- review follow-ups: confidence -------------------------------------------


def umbreon_grade(grader: str, grade: str, price: float, version_key: str) -> dict:
    return {
        "version_key": version_key,
        "grader": grader,
        "grade": grade,
        "market_price_usd": price,
        "market_sales_n": 6,
        "last_verified_sale": {"date": "2026-11-01", "price_usd": price},
    }


def test_a_suffix_only_grade_match_is_low_confidence(tmp_path, server):
    card = fixture(f"{UMBREON}.json")
    card["grades"].append(
        umbreon_grade("BGS", "10 Black Label", 9000, "english-alternate-art-secret")
    )
    server.routes[UMBREON_PATH] = lambda: httpx.Response(200, json=card)

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(identity(grader="bgs", grade="10"))

    assert isinstance(result, ValueQuote)
    assert result.market_value == 9000
    assert result.sales_count == 6
    assert result.confidence is ValueConfidence.LOW


def test_several_versions_at_the_grade_are_low_confidence(tmp_path, server):
    card = fixture(f"{UMBREON}.json")
    card["grades"].append(umbreon_grade("PSA", "10", 500, "japanese-promo"))
    server.routes[UMBREON_PATH] = lambda: httpx.Response(200, json=card)

    with make_provider(tmp_path, server) as provider:
        ten = provider.quote(identity(grade="10"))
        nine = provider.quote(identity(grade="9"))

    assert isinstance(ten, ValueQuote)
    assert ten.market_value == 3800.0
    assert ten.confidence is ValueConfidence.LOW
    assert isinstance(nine, ValueQuote)
    assert nine.confidence is ValueConfidence.HIGH


# --- review follow-ups: a stale index as a fallback --------------------------


def stale_index(tmp_path: Path) -> Clock:
    """Cache the fixture index, then move the clock two days on."""
    clock = Clock()
    with make_provider(tmp_path, Server(FakeMonotonic()), utc_now=clock) as provider:
        provider.quote(identity("Nobody"))
    clock.now = NOW + timedelta(days=2)
    return clock


def broken_index(server: Server, failure: str) -> None:
    if failure == "network":

        def fail(request=None):
            raise httpx.ConnectError("no route")

        server.routes[INDEX_PATH] = fail
    elif failure == "not json":
        server.routes[INDEX_PATH] = httpx.Response(200, text="<html>oops</html>")
    else:
        server.routes[INDEX_PATH] = httpx.Response(503, text="down")


@pytest.mark.parametrize("failure", ["503", "network", "not json"])
def test_a_failed_refresh_falls_back_to_the_stale_index(
    tmp_path, server, caplog, failure
):
    clock = stale_index(tmp_path)
    broken_index(server, failure)

    with make_provider(tmp_path, server, utc_now=clock) as provider:
        result = provider.quote(identity())

    assert isinstance(result, ValueQuote)
    assert result.market_value == 3800.0
    assert server.paths() == [INDEX_PATH, UMBREON_PATH]
    assert any(
        "stale" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records
    )


def test_a_stale_fallback_keeps_the_index_stale_for_the_next_run(tmp_path, server):
    clock = stale_index(tmp_path)
    broken_index(server, "503")
    with make_provider(tmp_path, server, utc_now=clock) as provider:
        provider.quote(identity())

    later = Server(FakeMonotonic())
    with make_provider(tmp_path, later, utc_now=clock) as provider:
        provider.quote(identity())

    assert later.paths()[0] == INDEX_PATH


@pytest.mark.parametrize("status", [403, 429])
def test_a_refused_refresh_still_raises_and_backs_off(tmp_path, server, status):
    clock = stale_index(tmp_path)
    server.routes[INDEX_PATH] = httpx.Response(status, text="no")

    with ValueCache(tmp_path) as cache:
        budget = CreditBudget(cache, {}, clock=clock)
        with make_provider(tmp_path, server, utc_now=clock, budget=budget) as p:
            with pytest.raises(ProviderUnavailable, match=str(status)):
                p.quote(identity())
        assert not budget.can_spend("cardvolume", 0)
    assert server.paths() == [INDEX_PATH]


# --- unavailability is temporary -------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
def test_cardvolume_unavailability_is_temporary(tmp_path, server, status):
    server.routes[UMBREON_PATH] = httpx.Response(status, text="busy")

    with make_provider(tmp_path, server) as provider:
        with pytest.raises(ProviderUnavailable) as caught:
            provider.quote(identity())

    assert caught.value.lasting is False


# --- attribution without cite_as ---------------------------------------------------

UMBREON_URL = "https://cardvolume.com/pokemon/evolving-skies/umbreon-vmax-215-203/"


def serve_umbreon(server: Server, card: dict) -> None:
    server.routes[UMBREON_PATH] = lambda: httpx.Response(200, json=card)


def without_cite_as() -> dict:
    card = fixture(f"{UMBREON}.json")
    del card["cite_as"]
    return card


def test_a_missing_cite_as_is_built_from_the_url_grade_figure_and_date(
    tmp_path, server
):
    serve_umbreon(server, without_cite_as())

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(identity())

    assert isinstance(result, ValueQuote)
    assert result.citation == (
        "Umbreon VMAX 215/203 (Evolving Skies), PSA 10: $3,800.00 as of "
        f"2026-11-24. CardVolume, {UMBREON_URL}"
    )


def test_a_cite_as_that_is_not_text_is_replaced_too(tmp_path, server):
    card = fixture(f"{UMBREON}.json")
    card["cite_as"] = {"text": "odd"}
    serve_umbreon(server, card)

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(identity())

    assert isinstance(result, ValueQuote)
    assert result.citation is not None
    assert result.citation.endswith(f"CardVolume, {UMBREON_URL}")


def test_a_built_citation_for_a_last_sale_gives_the_sale_and_its_date(tmp_path, server):
    serve_umbreon(server, without_cite_as())

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(identity(grade="7"))

    assert isinstance(result, ValueQuote)
    assert result.citation == (
        "Umbreon VMAX 215/203 (Evolving Skies), PSA 7: $1,677.70 as of "
        f"2026-09-20. CardVolume, {UMBREON_URL}"
    )


def test_a_built_citation_without_a_card_url_uses_the_index_url(tmp_path, server):
    card = without_cite_as()
    del card["url"]
    serve_umbreon(server, card)

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(identity())

    assert isinstance(result, ValueQuote)
    assert result.citation is not None
    assert result.citation.endswith(f"CardVolume, {UMBREON_URL}")


def test_a_built_citation_without_any_url_uses_the_cards_path(tmp_path, server):
    card = without_cite_as()
    for key in ("url", "name", "set", "number"):
        del card[key]
    serve_umbreon(server, card)
    server.index = {
        **INDEX,
        "cards": [{k: v for k, v in c.items() if k != "url"} for c in INDEX_CARDS],
    }

    with make_provider(tmp_path, server) as provider:
        result = provider.quote(identity())

    assert isinstance(result, ValueQuote)
    assert result.citation == (
        "Umbreon VMAX 215/203 (Evolving Skies), PSA 10: $3,800.00 as of "
        f"2026-11-24. CardVolume, {UMBREON_URL}"
    )


def test_every_cardvolume_quote_carries_a_citation(tmp_path, server):
    serve_umbreon(server, without_cite_as())
    identities = [identity(), identity(grade="9"), identity(grade="7")]

    with make_provider(tmp_path, server) as provider:
        results = [provider.quote(i) for i in identities]

    assert all(isinstance(r, ValueQuote) and r.citation for r in results)
