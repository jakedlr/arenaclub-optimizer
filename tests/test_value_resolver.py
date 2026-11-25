"""resolve_values and apply_values, with fake providers."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fake_values import NOW, TODAY, Clock, FakeProvider, checklist_card, quote

from arenaclub_optimizer.config import Override, Overrides
from arenaclub_optimizer.model import SlabPack, ValueConfidence
from arenaclub_optimizer.values import (
    OVERRIDE_SOURCE,
    CreditBudget,
    NeedsReview,
    NotFound,
    ProviderUnavailable,
    Unmatched,
    ValueCache,
    ValueOrigin,
    ValueResolution,
    apply_values,
    resolve_values,
)

NO_OVERRIDES = Overrides()
TOMORROW_MIDNIGHT = "2026-11-25 00:00 UTC"


@pytest.fixture
def cache(tmp_path):
    with ValueCache(tmp_path) as c:
        yield c


@pytest.fixture
def budget(cache):
    return CreditBudget(cache, {})


def resolve(cards, providers, cache, budget, *, overrides=NO_OVERRIDES, now=NOW):
    return resolve_values(cards, providers, overrides, cache, budget, now)


def reasons(resolution: ValueResolution) -> dict[str, str]:
    return {item.card_id: item.reason for item in resolution.needs_review}


# --- precedence --------------------------------------------------------------


def test_a_manual_override_wins_over_cache_and_providers(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    cache.put("card_a", "pp", quote(1.0, "pp"), fetched_at=NOW)
    provider = FakeProvider("pp", {"Umbreon": quote(2.0, "pp")})
    overrides = Overrides(by_card_id={"card_a": Override(500.0)})

    result = resolve([card], [provider], cache, budget, overrides=overrides)

    q = result.values["card_a"]
    assert q.market_value == 500.0
    assert q.source == OVERRIDE_SOURCE == "override"
    assert q.confidence is ValueConfidence.HIGH
    assert q.as_of == TODAY
    assert result.origins["card_a"] is ValueOrigin.OVERRIDE
    assert provider.calls == []
    assert result.needs_review == ()


def test_an_override_by_cert_number_applies(cache, budget):
    card = checklist_card("card_a", cert="12345")
    overrides = Overrides(by_cert={"12345": Override(75.0)})

    result = resolve([card], [], cache, budget, overrides=overrides)

    assert result.values["card_a"].market_value == 75.0


def test_an_override_is_high_confidence_even_for_an_arena_club_grade(cache, budget):
    card = checklist_card("card_a", grader="arena_club")
    overrides = Overrides(by_card_id={"card_a": Override(10.0)})

    result = resolve([card], [], cache, budget, overrides=overrides)

    assert result.values["card_a"].confidence is ValueConfidence.HIGH


def test_a_fresh_cached_quote_wins_over_providers(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    cached = quote(42.0, "pp")
    cache.put("card_a", "pp", cached, fetched_at=NOW - timedelta(days=6))
    provider = FakeProvider("pp", {"Umbreon": quote(99.0, "pp")})

    result = resolve([card], [provider], cache, budget)

    assert result.values["card_a"] == cached
    assert result.origins["card_a"] is ValueOrigin.CACHE
    assert provider.calls == []


def test_a_fresh_cached_quote_from_a_later_provider_saves_calling_an_earlier_one(
    cache, budget
):
    card = checklist_card("card_a", "Umbreon")
    cache.put("card_a", "cv", quote(42.0, "cv"), fetched_at=NOW)
    first = FakeProvider("pp", {"Umbreon": quote(99.0, "pp")})
    second = FakeProvider("cv")

    result = resolve([card], [first, second], cache, budget)

    assert result.values["card_a"].source == "cv"
    assert first.calls == [] and second.calls == []


def test_cached_quotes_follow_provider_order(cache, budget):
    card = checklist_card("card_a")
    cache.put("card_a", "cv", quote(1.0, "cv"), fetched_at=NOW)
    cache.put("card_a", "pp", quote(2.0, "pp"), fetched_at=NOW)

    result = resolve([card], [FakeProvider("pp"), FakeProvider("cv")], cache, budget)

    assert result.values["card_a"].source == "pp"


def test_a_cached_quote_from_a_source_not_in_use_is_ignored(cache, budget):
    card = checklist_card("card_a")
    cache.put("card_a", "retired", quote(1.0, "retired"), fetched_at=NOW)

    result = resolve([card], [FakeProvider("pp")], cache, budget)

    assert "card_a" not in result.values


def test_a_stale_cached_quote_is_refetched_and_replaced(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    cache.put("card_a", "pp", quote(42.0, "pp"), fetched_at=NOW - timedelta(days=8))
    provider = FakeProvider("pp", {"Umbreon": quote(50.0, "pp")})

    result = resolve([card], [provider], cache, budget)

    assert result.values["card_a"].market_value == 50.0
    assert result.origins["card_a"] is ValueOrigin.FETCHED
    assert provider.called_names == ["Umbreon"]
    entry = cache.get("card_a", "pp", now=NOW)
    assert entry.result.market_value == 50.0
    assert entry.fetched_at == NOW


def test_providers_are_tried_in_order_until_one_quotes(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    first = FakeProvider("pp", {"Umbreon": NotFound()})
    second = FakeProvider("cv", {"Umbreon": quote(7.0, "cv")})
    third = FakeProvider("paid", {"Umbreon": quote(8.0, "paid")})

    result = resolve([card], [first, second, third], cache, budget)

    assert result.values["card_a"].source == "cv"
    assert result.origins["card_a"] is ValueOrigin.FETCHED
    assert len(first.calls) == 1 and len(second.calls) == 1
    assert third.calls == []


def test_the_provider_gets_the_cards_identity(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    provider = FakeProvider("pp")

    resolve([card], [provider], cache, budget)

    assert provider.calls == [card.identity]


def test_each_card_id_is_resolved_once(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    provider = FakeProvider("pp", {"Umbreon": quote(7.0, "pp")})

    result = resolve([card, card], [provider], cache, budget)

    assert len(provider.calls) == 1
    assert list(result.values) == ["card_a"]


def test_values_and_needs_review_keep_card_order(cache, budget):
    cards = [checklist_card(c) for c in ("card_c", "card_a", "card_b")]
    provider = FakeProvider("pp", {"card_a": quote(1.0, "pp")})

    result = resolve(cards, [provider], cache, budget)

    assert [n.card_id for n in result.needs_review] == ["card_c", "card_b"]


def test_a_quote_whose_source_is_not_the_provider_name_is_an_error(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    provider = FakeProvider("pp", {"Umbreon": quote(7.0, "someone-else")})

    with pytest.raises(ValueError, match="source"):
        resolve([card], [provider], cache, budget)


# --- negative outcomes -------------------------------------------------------


@pytest.mark.parametrize("outcome", [NotFound(), Unmatched()])
def test_negative_outcomes_are_cached_and_not_retried_while_fresh(
    cache, budget, outcome
):
    card = checklist_card("card_a", "Umbreon")
    provider = FakeProvider("pp", {"Umbreon": outcome})

    resolve([card], [provider], cache, budget)
    assert cache.get("card_a", "pp", now=NOW).result == outcome

    again = resolve([card], [provider], cache, budget, now=NOW + timedelta(days=29))
    assert len(provider.calls) == 1
    assert "card_a" not in again.values
    assert [n.card_id for n in again.needs_review] == ["card_a"]


def test_a_stale_negative_outcome_is_retried(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    cache.put("card_a", "pp", Unmatched(), fetched_at=NOW - timedelta(days=31))
    provider = FakeProvider("pp", {"Umbreon": quote(3.0, "pp")})

    result = resolve([card], [provider], cache, budget)

    assert provider.called_names == ["Umbreon"]
    assert result.values["card_a"].market_value == 3.0


def test_a_cached_negative_outcome_still_lets_the_next_provider_try(cache, budget):
    card = checklist_card("card_a", "Umbreon")
    cache.put("card_a", "pp", NotFound(), fetched_at=NOW)
    first = FakeProvider("pp", {"Umbreon": quote(1.0, "pp")})
    second = FakeProvider("cv", {"Umbreon": quote(2.0, "cv")})

    result = resolve([card], [first, second], cache, budget)

    assert first.calls == []
    assert result.values["card_a"].source == "cv"


# --- budget ------------------------------------------------------------------


def test_a_provider_is_skipped_when_the_budget_cannot_cover_its_cost(cache):
    budget = CreditBudget(cache, {"pp": 3}, clock=Clock())
    cards = [checklist_card("card_a"), checklist_card("card_b")]
    answers = {"card_a": quote(1.0, "pp"), "card_b": quote(2.0, "pp")}
    provider = FakeProvider("pp", answers, credit_cost=2, budget=budget)
    fallback = FakeProvider("cv", {"card_b": quote(9.0, "cv")})

    result = resolve(cards, [provider, fallback], cache, budget)

    assert provider.called_names == ["card_a"]
    assert result.values["card_a"].source == "pp"
    assert result.values["card_b"].source == "cv"
    assert budget.remaining("pp", now=NOW) == 1


def test_the_resolver_checks_the_budget_but_the_provider_spends(cache):
    budget = CreditBudget(cache, {"pp": 10})
    provider = FakeProvider("pp", {"card_a": quote(1.0, "pp")}, credit_cost=2)

    resolve([checklist_card("card_a")], [provider], cache, budget)

    assert budget.remaining("pp", now=NOW) == 10


def test_a_free_provider_is_never_skipped_for_budget(cache):
    budget = CreditBudget(cache, {"cv": 0})
    provider = FakeProvider("cv", {"card_a": quote(1.0, "cv")}, credit_cost=0)

    result = resolve([checklist_card("card_a")], [provider], cache, budget)

    assert result.values["card_a"].source == "cv"


def test_an_exhausted_budget_puts_the_card_on_needs_review(cache):
    budget = CreditBudget(cache, {"pp": 0})
    provider = FakeProvider("pp", credit_cost=2)

    result = resolve([checklist_card("card_a")], [provider], cache, budget)

    assert provider.calls == []
    assert reasons(result) == {
        "card_a": f"budget exhausted, retry after {TOMORROW_MIDNIGHT}"
    }


def test_the_budget_resets_at_midnight_utc_between_runs(cache):
    late = datetime(2026, 11, 24, 23, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 11, 25, 0, 1, tzinfo=UTC)
    clock = Clock(late)
    budget = CreditBudget(cache, {"pp": 2}, clock=clock)
    provider = FakeProvider(
        "pp",
        {"card_a": quote(1.0, "pp"), "card_b": quote(2.0, "pp")},
        credit_cost=2,
        budget=budget,
    )
    cards = [checklist_card("card_a"), checklist_card("card_b")]

    first = resolve(cards, [provider], cache, budget, now=late)
    assert list(first.values) == ["card_a"]
    assert reasons(first) == {
        "card_b": f"budget exhausted, retry after {TOMORROW_MIDNIGHT}"
    }

    clock.now = after_midnight
    second = resolve(cards, [provider], cache, budget, now=after_midnight)
    assert second.origins == {
        "card_a": ValueOrigin.CACHE,
        "card_b": ValueOrigin.FETCHED,
    }


def test_a_stale_quote_is_kept_when_the_provider_cannot_be_asked(cache):
    budget = CreditBudget(cache, {"pp": 0})
    stale = quote(42.0, "pp")
    cache.put("card_a", "pp", stale, fetched_at=NOW - timedelta(days=10))
    provider = FakeProvider("pp", credit_cost=2)

    result = resolve([checklist_card("card_a")], [provider], cache, budget)

    assert result.values["card_a"] == stale
    assert result.origins["card_a"] is ValueOrigin.STALE_CACHE
    assert result.needs_review == ()


# --- ProviderUnavailable -----------------------------------------------------


def test_provider_unavailable_stops_that_provider_but_not_the_others(cache, budget):
    retry = datetime(2026, 11, 24, 12, 5, tzinfo=UTC)
    down = FakeProvider(
        "pp", {"card_a": ProviderUnavailable(retry, reason="rate limited")}
    )
    other = FakeProvider("cv", {"card_a": quote(1.0, "cv"), "card_b": quote(2.0, "cv")})
    cards = [checklist_card("card_a"), checklist_card("card_b")]

    result = resolve(cards, [down, other], cache, budget)

    assert down.called_names == ["card_a"]
    assert other.called_names == ["card_a", "card_b"]
    assert set(result.values) == {"card_a", "card_b"}
    assert result.unavailable == {
        "pp": "rate limited, retry after 2026-11-24 12:05 UTC"
    }


def test_an_unavailable_provider_is_not_cached_as_a_negative_outcome(cache, budget):
    down = FakeProvider("pp", {"card_a": ProviderUnavailable()})

    resolve([checklist_card("card_a")], [down], cache, budget)

    assert cache.get("card_a", "pp", now=NOW) is None


def test_cards_after_an_outage_name_the_unavailable_provider(cache, budget):
    down = FakeProvider("pp", {"card_a": ProviderUnavailable(reason="key not set")})
    cards = [checklist_card("card_a"), checklist_card("card_b")]

    result = resolve(cards, [down], cache, budget)

    assert reasons(result) == {"card_a": "key not set", "card_b": "key not set"}


# --- Arena Club grades -------------------------------------------------------


def test_an_arena_club_grade_is_looked_up_raw_at_low_confidence(cache, budget):
    card = checklist_card("card_a", "Ditto", grader="arena_club", grade="10")
    provider = FakeProvider("pp", {"Ditto": quote(12.0, "pp")})

    result = resolve([card], [provider], cache, budget)

    [asked] = provider.calls
    assert asked.grade is None
    assert asked.grader is None
    assert asked.card_name == "Ditto"
    assert asked.set_number == card.identity.set_number
    q = result.values["card_a"]
    assert q.market_value == 12.0
    assert q.confidence is ValueConfidence.LOW
    assert cache.get("card_a", "pp", now=NOW).result.confidence is ValueConfidence.LOW


def test_the_arena_club_grader_is_matched_case_insensitively(cache, budget):
    card = checklist_card("card_a", grader="Arena_Club")
    provider = FakeProvider("pp", {"card_a": quote(12.0, "pp")})

    result = resolve([card], [provider], cache, budget)

    assert provider.calls[0].grade is None
    assert result.values["card_a"].confidence is ValueConfidence.LOW


def test_other_graders_are_looked_up_at_their_grade(cache, budget):
    card = checklist_card("card_a", grader="psa", grade="9")
    provider = FakeProvider("pp", {"card_a": quote(12.0, "pp")})

    result = resolve([card], [provider], cache, budget)

    assert provider.calls[0].grade == "9"
    assert provider.calls[0].grader == "psa"
    assert result.values["card_a"].confidence is ValueConfidence.HIGH


def test_an_arena_club_grade_with_no_raw_price_says_so(cache, budget):
    card = checklist_card("card_a", grader="arena_club")
    provider = FakeProvider("pp", {"card_a": NotFound()})

    result = resolve([card], [provider], cache, budget)

    assert reasons(result) == {"card_a": "no raw price"}


# --- Needs-review List -------------------------------------------------------


def test_needs_review_names_the_card(cache, budget):
    card = checklist_card("card_a", cert="8AC1", label="2021 Umbreon PSA 10")

    result = resolve([card], [FakeProvider("pp")], cache, budget)

    assert result.needs_review == (
        NeedsReview(
            card_id="card_a",
            cert_number="8AC1",
            label="2021 Umbreon PSA 10",
            reason="unmatched by all providers",
        ),
    )


def test_unmatched_by_every_provider(cache, budget):
    providers = [FakeProvider("pp"), FakeProvider("cv")]

    result = resolve([checklist_card("card_a")], providers, cache, budget)

    assert reasons(result) == {"card_a": "unmatched by all providers"}


def test_no_price_at_grade(cache, budget):
    providers = [
        FakeProvider("pp", {"card_a": NotFound()}),
        FakeProvider("cv", {"card_a": NotFound()}),
    ]

    result = resolve([checklist_card("card_a")], providers, cache, budget)

    assert reasons(result) == {"card_a": "no price at grade"}


def test_mixed_causes_name_their_providers(cache):
    budget = CreditBudget(cache, {"paid": 0})
    providers = [
        FakeProvider("pp", {"card_a": NotFound()}),
        FakeProvider("cv"),
        FakeProvider("paid", credit_cost=1),
        FakeProvider("other"),
    ]

    result = resolve([checklist_card("card_a")], providers, cache, budget)

    assert reasons(result) == {
        "card_a": (
            "no price at grade (pp); unmatched (cv, other); "
            f"budget exhausted, retry after {TOMORROW_MIDNIGHT} (paid)"
        )
    }


def test_no_providers(cache, budget):
    result = resolve([checklist_card("card_a")], [], cache, budget)

    assert reasons(result) == {"card_a": "no value providers"}


def test_naive_now_is_an_error(cache, budget):
    with pytest.raises(ValueError, match="time zone"):
        resolve([checklist_card("card_a")], [], cache, budget, now=datetime(2026, 1, 1))


def test_now_defaults_to_the_current_time(cache, budget):
    provider = FakeProvider("pp", {"card_a": quote(1.0, "pp")})

    resolve_values([checklist_card("card_a")], [provider], NO_OVERRIDES, cache, budget)

    entry = cache.get("card_a", "pp", now=datetime.now(UTC))
    assert entry is not None and entry.fresh


# --- apply_values ------------------------------------------------------------


def make_pack(*cards) -> SlabPack:
    return SlabPack(
        pack_id="pack-1",
        price_level="Gold",
        category="Pokemon",
        price=100.0,
        active=True,
        checklist=tuple(cards),
    )


def test_apply_values_sets_market_value_and_confidence(cache, budget):
    a = checklist_card("card_a")
    b = checklist_card("card_b", grader="arena_club")
    c = checklist_card("card_c")
    provider = FakeProvider(
        "pp", {"card_a": quote(10.0, "pp"), "card_b": quote(4.0, "pp")}
    )
    pack = make_pack(a, b, c)

    resolution = resolve(pack.checklist, [provider], cache, budget)
    valued = apply_values(pack, resolution)

    by_id = {card.card_id: card for card in valued.checklist}
    assert by_id["card_a"].market_value == 10.0
    assert by_id["card_a"].value_confidence is ValueConfidence.HIGH
    assert by_id["card_b"].market_value == 4.0
    assert by_id["card_b"].value_confidence is ValueConfidence.LOW
    assert by_id["card_c"] == c
    assert valued == dataclasses.replace(pack, checklist=valued.checklist)
    assert pack.checklist == (a, b, c)


def test_apply_values_keeps_card_order_and_identity(cache, budget):
    a, b = checklist_card("card_a"), checklist_card("card_b")
    resolution = ValueResolution(
        values={"card_b": quote(1.0, "pp")},
        needs_review=(),
        origins={"card_b": ValueOrigin.FETCHED},
        unavailable={},
    )

    valued = apply_values(make_pack(a, b), resolution)

    assert [card.card_id for card in valued.checklist] == ["card_a", "card_b"]
    assert valued.checklist[1].identity == b.identity
