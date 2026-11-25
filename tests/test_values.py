"""ValueQuote, ValueCache and CreditBudget."""

import ast
import dataclasses
import math
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fake_values import NOW, TODAY, quote

from arenaclub_optimizer import values
from arenaclub_optimizer.model import ValueConfidence
from arenaclub_optimizer.values import (
    DEFAULT_DAILY_LIMITS,
    DEFAULT_NEGATIVE_MAX_AGE,
    DEFAULT_QUOTE_MAX_AGE,
    BudgetExhausted,
    CreditBudget,
    NotFound,
    ProviderUnavailable,
    Unmatched,
    ValueCache,
    ValueQuote,
    default_cache_dir,
)

# --- ValueQuote --------------------------------------------------------------


def test_value_quote_holds_its_fields():
    q = ValueQuote(
        market_value=3800.0,
        source="cardvolume",
        as_of=TODAY,
        sales_count=47,
        confidence=ValueConfidence.HIGH,
        citation="Umbreon VMAX 215/203. CardVolume, https://example.test/",
    )
    assert q.market_value == 3800.0
    assert q.source == "cardvolume"
    assert q.as_of == TODAY
    assert q.sales_count == 47
    assert q.confidence is ValueConfidence.HIGH
    assert q.citation.startswith("Umbreon")


def test_value_quote_citation_defaults_to_none():
    q = ValueQuote(12.0, "pokeprice", TODAY, None, ValueConfidence.LOW)
    assert q.citation is None
    assert q.sales_count is None


def test_value_quote_accepts_zero():
    assert quote(0.0, "x").market_value == 0.0


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -0.01])
def test_value_quote_rejects_a_value_that_is_not_finite_and_non_negative(value):
    with pytest.raises(ValueError, match="Market Value"):
        quote(value, "pokeprice")


@pytest.mark.parametrize("source", ["", "   "])
def test_value_quote_rejects_an_empty_source(source):
    with pytest.raises(ValueError, match="source"):
        quote(1.0, source)


def test_value_quote_rejects_a_negative_sales_count():
    with pytest.raises(ValueError, match="sales"):
        quote(1.0, "pokeprice", sales_count=-1)


def test_value_quote_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        quote(1.0, "x").market_value = 2.0  # type: ignore[misc]


def test_not_found_and_unmatched_are_distinct_values():
    assert NotFound() == NotFound()
    assert Unmatched() == Unmatched()
    assert NotFound() != Unmatched()


# --- ProviderUnavailable -----------------------------------------------------


def test_provider_unavailable_carries_retry_after_and_reason():
    when = datetime(2026, 11, 25, 0, 0, tzinfo=UTC)
    error = ProviderUnavailable(when, reason="daily limit reached")
    assert error.retry_after == when
    assert error.reason == "daily limit reached"
    assert str(error) == "daily limit reached, retry after 2026-11-25 00:00 UTC"


def test_provider_unavailable_defaults():
    error = ProviderUnavailable()
    assert error.retry_after is None
    assert str(error) == "unavailable"


def test_provider_unavailable_shows_retry_after_in_utc():
    eastern = timezone(timedelta(hours=-5))
    error = ProviderUnavailable(datetime(2026, 11, 24, 19, 0, tzinfo=eastern))
    assert str(error) == "unavailable, retry after 2026-11-25 00:00 UTC"


# --- ValueCache: location ----------------------------------------------------


def test_default_cache_dir_uses_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == tmp_path / "xdg" / "arenaclub-optimizer"


@pytest.mark.parametrize("value", [None, "", "relative/cache"])
def test_default_cache_dir_falls_back_to_home_cache(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", value)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / ".cache" / "arenaclub-optimizer"


def test_cache_file_lives_in_the_injected_directory(tmp_path):
    with ValueCache(tmp_path / "nested" / "dir") as cache:
        assert cache.directory == tmp_path / "nested" / "dir"
        assert cache.path == tmp_path / "nested" / "dir" / "values.sqlite"
    assert (tmp_path / "nested" / "dir" / "values.sqlite").is_file()


def test_cache_defaults_to_the_xdg_cache_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with ValueCache() as cache:
        assert cache.path == tmp_path / "arenaclub-optimizer" / "values.sqlite"


def test_default_staleness():
    assert DEFAULT_QUOTE_MAX_AGE == timedelta(days=7)
    assert DEFAULT_NEGATIVE_MAX_AGE == timedelta(days=30)


# --- ValueCache: lookups -----------------------------------------------------


@pytest.fixture
def cache(tmp_path):
    with ValueCache(tmp_path) as c:
        yield c


def test_cache_miss_is_none(cache):
    assert cache.get("card_a", "pokeprice", now=NOW) is None


def test_cache_round_trips_every_quote_field(cache):
    q = quote(
        123.45,
        "cardvolume",
        confidence=ValueConfidence.LOW,
        as_of=date(2026, 11, 20),
        sales_count=None,
        citation="cite me",
    )
    cache.put("card_a", "cardvolume", q, fetched_at=NOW)

    entry = cache.get("card_a", "cardvolume", now=NOW)
    assert entry.result == q
    assert entry.fetched_at == NOW
    assert entry.fresh


def test_cache_keys_by_card_and_source(cache):
    cache.put("card_a", "pokeprice", quote(1.0, "pokeprice"), fetched_at=NOW)
    cache.put("card_a", "cardvolume", quote(2.0, "cardvolume"), fetched_at=NOW)
    cache.put("card_b", "pokeprice", quote(3.0, "pokeprice"), fetched_at=NOW)

    assert cache.get("card_a", "pokeprice", now=NOW).result.market_value == 1.0
    assert cache.get("card_a", "cardvolume", now=NOW).result.market_value == 2.0
    assert cache.get("card_b", "pokeprice", now=NOW).result.market_value == 3.0
    assert cache.get("card_b", "cardvolume", now=NOW) is None


def test_cache_rejects_a_quote_from_another_source(cache):
    with pytest.raises(ValueError, match="source"):
        cache.put("card_a", "pokeprice", quote(1.0, "cardvolume"), fetched_at=NOW)


@pytest.mark.parametrize("outcome", [NotFound(), Unmatched()])
def test_cache_stores_negative_outcomes(cache, outcome):
    cache.put("card_a", "pokeprice", outcome, fetched_at=NOW)
    entry = cache.get("card_a", "pokeprice", now=NOW)
    assert entry.result == outcome
    assert entry.fresh


def test_a_new_result_replaces_the_old_one(cache):
    cache.put("card_a", "pokeprice", quote(1.0, "pokeprice"), fetched_at=NOW)
    cache.put("card_a", "pokeprice", NotFound(), fetched_at=NOW)
    assert cache.get("card_a", "pokeprice", now=NOW).result == NotFound()

    cache.put("card_a", "pokeprice", quote(5.0, "pokeprice"), fetched_at=NOW)
    assert cache.get("card_a", "pokeprice", now=NOW).result == quote(5.0, "pokeprice")


def test_a_quote_is_fresh_for_seven_days(cache):
    cache.put("card_a", "pokeprice", quote(1.0, "pokeprice"), fetched_at=NOW)
    at_limit = NOW + timedelta(days=7)
    assert cache.get("card_a", "pokeprice", now=at_limit).fresh
    stale = cache.get("card_a", "pokeprice", now=at_limit + timedelta(seconds=1))
    assert stale is not None
    assert not stale.fresh
    assert stale.result == quote(1.0, "pokeprice")


@pytest.mark.parametrize("outcome", [NotFound(), Unmatched()])
def test_a_negative_outcome_is_fresh_for_thirty_days(cache, outcome):
    cache.put("card_a", "pokeprice", outcome, fetched_at=NOW)
    assert cache.get("card_a", "pokeprice", now=NOW + timedelta(days=30)).fresh
    later = NOW + timedelta(days=30, seconds=1)
    assert not cache.get("card_a", "pokeprice", now=later).fresh


def test_staleness_is_a_parameter(tmp_path):
    with ValueCache(
        tmp_path, quote_max_age=timedelta(hours=1), negative_max_age=timedelta(0)
    ) as cache:
        cache.put("card_a", "p", quote(1.0, "p"), fetched_at=NOW)
        cache.put("card_b", "p", Unmatched(), fetched_at=NOW)
        later = NOW + timedelta(hours=1, seconds=1)
        assert not cache.get("card_a", "p", now=later).fresh
        assert not cache.get("card_b", "p", now=NOW + timedelta(seconds=1)).fresh


def test_cache_compares_times_across_time_zones(cache):
    eastern = timezone(timedelta(hours=-5))
    cache.put("card_a", "p", quote(1.0, "p"), fetched_at=NOW.astimezone(eastern))
    entry = cache.get("card_a", "p", now=NOW + timedelta(days=7))
    assert entry.fresh
    assert entry.fetched_at == NOW


def test_cache_rejects_naive_times(cache):
    with pytest.raises(ValueError, match="time zone"):
        cache.put("card_a", "p", quote(1.0, "p"), fetched_at=datetime(2026, 11, 24))
    with pytest.raises(ValueError, match="time zone"):
        cache.get("card_a", "p", now=datetime(2026, 11, 24))


# --- ValueCache: provider ids ------------------------------------------------


def test_provider_ids_are_stored_per_provider_and_key(cache):
    assert cache.get_provider_id("pokeprice", "card:umbreon") is None
    cache.put_provider_id("pokeprice", "card:umbreon", "246723")
    cache.put_provider_id("pokeprice", "set:evolving skies|english", "sv7")
    cache.put_provider_id("cardvolume", "card:umbreon", "pokemon--es--215")

    assert cache.get_provider_id("pokeprice", "card:umbreon") == "246723"
    assert cache.get_provider_id("pokeprice", "set:evolving skies|english") == "sv7"
    assert cache.get_provider_id("cardvolume", "card:umbreon") == "pokemon--es--215"


def test_provider_id_can_be_replaced(cache):
    cache.put_provider_id("pokeprice", "k", "1")
    cache.put_provider_id("pokeprice", "k", "2")
    assert cache.get_provider_id("pokeprice", "k") == "2"


# --- ValueCache: persistence -------------------------------------------------


def test_cache_survives_a_reopen(tmp_path):
    with ValueCache(tmp_path) as cache:
        cache.put("card_a", "pokeprice", quote(9.5, "pokeprice"), fetched_at=NOW)
        cache.put("card_b", "pokeprice", Unmatched(), fetched_at=NOW)
        cache.put("card_c", "cardvolume", NotFound(), fetched_at=NOW)
        cache.put_provider_id("pokeprice", "k", "v")
        CreditBudget(cache, {"pokeprice": 10}).try_spend("pokeprice", 4, now=NOW)

    with ValueCache(tmp_path) as cache:
        assert cache.get("card_a", "pokeprice", now=NOW).result == quote(
            9.5, "pokeprice"
        )
        assert cache.get("card_b", "pokeprice", now=NOW).result == Unmatched()
        assert cache.get("card_c", "cardvolume", now=NOW).result == NotFound()
        assert cache.get_provider_id("pokeprice", "k") == "v"
        assert CreditBudget(cache, {"pokeprice": 10}).remaining(
            "pokeprice", now=NOW
        ) == (6)


# --- CreditBudget ------------------------------------------------------------


def test_pokeprice_free_tier_is_the_default_limit():
    assert DEFAULT_DAILY_LIMITS == {"pokeprice": 100}


def test_budget_defaults_to_the_free_tier_limits(cache):
    budget = CreditBudget(cache)
    assert budget.remaining("pokeprice", now=NOW) == 100


def test_try_spend_spends_until_the_limit(cache):
    budget = CreditBudget(cache, {"pokeprice": 5})
    assert budget.try_spend("pokeprice", 2, now=NOW)
    assert budget.try_spend("pokeprice", 3, now=NOW)
    assert budget.remaining("pokeprice", now=NOW) == 0
    assert not budget.try_spend("pokeprice", 1, now=NOW)


def test_a_failed_spend_spends_nothing(cache):
    budget = CreditBudget(cache, {"pokeprice": 5})
    assert budget.try_spend("pokeprice", 4, now=NOW)
    assert not budget.try_spend("pokeprice", 2, now=NOW)
    assert budget.remaining("pokeprice", now=NOW) == 1
    assert budget.try_spend("pokeprice", 1, now=NOW)


def test_can_spend_checks_without_spending(cache):
    budget = CreditBudget(cache, {"pokeprice": 3})
    assert budget.can_spend("pokeprice", 3, now=NOW)
    assert not budget.can_spend("pokeprice", 4, now=NOW)
    assert budget.remaining("pokeprice", now=NOW) == 3


def test_spending_zero_always_succeeds(cache):
    budget = CreditBudget(cache, {"pokeprice": 0})
    assert budget.try_spend("pokeprice", 0, now=NOW)
    assert not budget.try_spend("pokeprice", 1, now=NOW)


def test_a_provider_without_a_limit_is_unlimited(cache):
    budget = CreditBudget(cache, {"pokeprice": 1})
    assert budget.remaining("cardvolume", now=NOW) is None
    assert budget.try_spend("cardvolume", 10_000, now=NOW)
    assert budget.can_spend("cardvolume", 10_000, now=NOW)


def test_budgets_are_per_provider(cache):
    budget = CreditBudget(cache, {"a": 2, "b": 2})
    assert budget.try_spend("a", 2, now=NOW)
    assert budget.remaining("b", now=NOW) == 2


def test_spending_a_negative_amount_is_an_error(cache):
    budget = CreditBudget(cache)
    with pytest.raises(ValueError, match="negative"):
        budget.try_spend("pokeprice", -1, now=NOW)


def test_a_negative_limit_is_an_error(cache):
    with pytest.raises(ValueError, match="limit"):
        CreditBudget(cache, {"pokeprice": -1})


def test_budget_resets_at_midnight_utc(cache):
    budget = CreditBudget(cache, {"pokeprice": 2})
    late = datetime(2026, 11, 24, 23, 59, 59, tzinfo=UTC)
    assert budget.try_spend("pokeprice", 2, now=late)
    assert not budget.try_spend("pokeprice", 1, now=late)

    midnight = datetime(2026, 11, 25, 0, 0, tzinfo=UTC)
    assert budget.remaining("pokeprice", now=midnight) == 2
    assert budget.try_spend("pokeprice", 2, now=midnight)


def test_budget_days_are_utc_days_whatever_the_local_zone(cache):
    eastern = timezone(timedelta(hours=-5))
    budget = CreditBudget(cache, {"pokeprice": 2})
    # 20:00 on the 24th in UTC-5 is already 01:00 on the 25th in UTC.
    assert budget.try_spend(
        "pokeprice", 2, now=datetime(2026, 11, 24, 20, tzinfo=eastern)
    )
    assert budget.remaining("pokeprice", now=datetime(2026, 11, 25, 1, tzinfo=UTC)) == 0
    assert (
        budget.remaining("pokeprice", now=datetime(2026, 11, 24, 23, tzinfo=UTC)) == 2
    )


def test_resets_at_is_the_next_utc_midnight(cache):
    budget = CreditBudget(cache)
    assert budget.resets_at(now=NOW) == datetime(2026, 11, 25, tzinfo=UTC)
    midnight = datetime(2026, 11, 25, tzinfo=UTC)
    assert budget.resets_at(now=midnight) == datetime(2026, 11, 26, tzinfo=UTC)


def test_budget_uses_its_injected_clock_by_default(cache):
    times = iter(
        [
            datetime(2026, 11, 24, 23, 0, tzinfo=UTC),
            datetime(2026, 11, 24, 23, 30, tzinfo=UTC),
            datetime(2026, 11, 25, 0, 30, tzinfo=UTC),
        ]
    )
    budget = CreditBudget(cache, {"pokeprice": 1}, clock=lambda: next(times))
    assert budget.try_spend("pokeprice", 1)
    assert not budget.try_spend("pokeprice", 1)
    assert budget.try_spend("pokeprice", 1)


def test_budget_rejects_naive_times(cache):
    budget = CreditBudget(cache)
    with pytest.raises(ValueError, match="time zone"):
        budget.try_spend("pokeprice", 1, now=datetime(2026, 11, 24))


def test_budgets_sharing_a_file_see_each_others_spending(tmp_path):
    with ValueCache(tmp_path) as first, ValueCache(tmp_path) as second:
        CreditBudget(first, {"pokeprice": 3}).try_spend("pokeprice", 2, now=NOW)
        other = CreditBudget(second, {"pokeprice": 3})
        assert other.remaining("pokeprice", now=NOW) == 1
        assert not other.try_spend("pokeprice", 2, now=NOW)


def test_spend_or_raise_spends_or_raises_budget_exhausted(cache):
    budget = CreditBudget(cache, {"pokeprice": 3})
    budget.spend_or_raise("pokeprice", 2, now=NOW)
    assert budget.remaining("pokeprice", now=NOW) == 1

    with pytest.raises(BudgetExhausted) as caught:
        budget.spend_or_raise("pokeprice", 2, now=NOW)
    assert isinstance(caught.value, ProviderUnavailable)
    assert caught.value.retry_after == datetime(2026, 11, 25, tzinfo=UTC)
    assert str(caught.value) == "budget exhausted, retry after 2026-11-25 00:00 UTC"
    assert budget.remaining("pokeprice", now=NOW) == 1


def test_exhausted_error_names_the_reset_time(cache):
    error = CreditBudget(cache).exhausted_error(now=NOW)
    assert isinstance(error, BudgetExhausted)
    assert error.retry_after == datetime(2026, 11, 25, tzinfo=UTC)


def test_exhaust_marks_the_rest_of_today_spent(cache):
    budget = CreditBudget(cache, {"pokeprice": 100})
    budget.try_spend("pokeprice", 10, now=NOW)

    budget.exhaust("pokeprice", now=NOW)

    assert budget.remaining("pokeprice", now=NOW) == 0
    assert not budget.try_spend("pokeprice", 1, now=NOW)
    assert not budget.can_spend("pokeprice", 1, now=NOW)
    assert not budget.try_spend("pokeprice", 0, now=NOW)
    tomorrow = NOW + timedelta(days=1)
    assert budget.remaining("pokeprice", now=tomorrow) == 100


def test_exhaust_applies_to_a_provider_without_a_limit(cache):
    budget = CreditBudget(cache, {})
    budget.exhaust("cardvolume", now=NOW)

    assert budget.remaining("cardvolume", now=NOW) == 0
    assert not budget.try_spend("cardvolume", 1, now=NOW)
    assert budget.remaining("cardvolume", now=NOW + timedelta(days=1)) is None


def test_exhaust_stops_a_free_provider_too(cache):
    budget = CreditBudget(cache, {})
    assert budget.can_spend("cardvolume", 0, now=NOW)
    assert budget.try_spend("cardvolume", 0, now=NOW)

    budget.exhaust("cardvolume", now=NOW)

    assert not budget.can_spend("cardvolume", 0, now=NOW)
    assert not budget.try_spend("cardvolume", 0, now=NOW)
    with pytest.raises(BudgetExhausted):
        budget.spend_or_raise("cardvolume", 0, now=NOW)
    tomorrow = NOW + timedelta(days=1)
    assert budget.can_spend("cardvolume", 0, now=tomorrow)
    assert budget.try_spend("cardvolume", 0, now=tomorrow)


def test_exhaust_is_persisted(tmp_path):
    with ValueCache(tmp_path) as cache:
        CreditBudget(cache, {"pokeprice": 100}).exhaust("pokeprice", now=NOW)
    with ValueCache(tmp_path) as cache:
        budget = CreditBudget(cache, {"pokeprice": 100})
        assert budget.remaining("pokeprice", now=NOW) == 0
        assert not budget.try_spend("pokeprice", 1, now=NOW)


def test_budget_now_reads_its_clock(cache):
    when = datetime(2026, 5, 5, 5, 5, tzinfo=UTC)
    assert CreditBudget(cache, clock=lambda: when).now() == when


# --- sqlite settings ---------------------------------------------------------


def test_cache_uses_wal_and_a_schema_version(tmp_path):
    with ValueCache(tmp_path) as cache:
        assert cache._db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    other = sqlite3.connect(tmp_path / "values.sqlite")
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert other.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        other.close()


# --- no HTTP -----------------------------------------------------------------

NETWORK_MODULES = {"httpx", "urllib", "http", "socket", "requests"}


def test_the_values_package_imports_no_network_module():
    package = Path(values.__file__).parent
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in NETWORK_MODULES, f"{path}: {name}"
