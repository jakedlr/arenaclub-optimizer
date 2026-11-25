"""Daily credit budgets for value providers, kept in the value cache's file."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, time, timedelta
from types import MappingProxyType

from arenaclub_optimizer.values.cache import ValueCache
from arenaclub_optimizer.values.quote import BudgetExhausted, require_aware

# PokéPrice Tracker's free tier: 100 credits a day (docs/research/value-apis.md).
DEFAULT_DAILY_LIMITS: Mapping[str, int] = MappingProxyType({"pokeprice": 100})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CreditBudget:
    """How many credits each provider may spend per UTC day.

    `limits` maps a provider name to its daily limit; a provider not listed
    is unlimited unless `exhaust` marks its day used up. Spending is stored in
    `cache`'s file, so it carries over between runs and processes, and starts
    again at 00:00 UTC. Every method takes an optional `now` (time-zone
    aware); without it, `clock` is used. `resolve_values` defaults to the
    same clock, so the resolver's checks and the providers' spending agree.
    """

    def __init__(
        self,
        cache: ValueCache,
        limits: Mapping[str, int] = DEFAULT_DAILY_LIMITS,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        for provider, limit in limits.items():
            if limit < 0:
                raise ValueError(f"daily limit for {provider!r} is negative: {limit}")
        self._cache = cache
        self._limits = dict(limits)
        self._clock = clock

    def now(self) -> datetime:
        """The current time by this budget's clock."""
        return require_aware(self._clock(), "clock time")

    def try_spend(self, provider: str, n: int, *, now: datetime | None = None) -> bool:
        """Spend `n` credits if today's remaining budget covers them.

        Returns whether they were spent; a refused spend spends nothing.
        Raises ValueError if `n` is negative.
        """
        if n < 0:
            raise ValueError(f"can't spend a negative number of credits: {n}")
        day = self._day(now)
        if n == 0:
            # Spending nothing still needs a day that isn't exhausted, so
            # `exhaust` stops free providers too.
            return self.can_spend(provider, 0, now=now)
        return self._cache._spend_credits(provider, day, n, self._limits.get(provider))

    def spend_or_raise(
        self, provider: str, n: int, *, now: datetime | None = None
    ) -> None:
        """Spend `n` credits, or raise BudgetExhausted (spending nothing) if
        today's remaining budget can't cover them. For providers, before each
        request."""
        if not self.try_spend(provider, n, now=now):
            raise self.exhausted_error(now=now)

    def exhausted_error(self, *, now: datetime | None = None) -> BudgetExhausted:
        """A BudgetExhausted to raise, retrying at the next reset."""
        return BudgetExhausted(self.resets_at(now=now))

    def exhaust(self, provider: str, *, now: datetime | None = None) -> None:
        """Mark the rest of today's allowance for `provider` as spent, e.g.
        when its API reports the daily limit reached. Applies to unlimited
        providers too, and lasts until the next 00:00 UTC."""
        self._cache._exhaust_credits(provider, self._day(now))

    def can_spend(self, provider: str, n: int, *, now: datetime | None = None) -> bool:
        """Whether today's remaining budget covers `n` credits, without spending.
        False for any `n`, even 0, once `exhaust` has marked today used up."""
        spent, exhausted = self._cache._credits(provider, self._day(now))
        if exhausted:
            return False
        limit = self._limits.get(provider)
        return limit is None or n <= max(limit - spent, 0)

    def remaining(self, provider: str, *, now: datetime | None = None) -> int | None:
        """Credits left today, or None for an unlimited provider."""
        spent, exhausted = self._cache._credits(provider, self._day(now))
        if exhausted:
            return 0
        limit = self._limits.get(provider)
        return None if limit is None else max(limit - spent, 0)

    def resets_at(self, *, now: datetime | None = None) -> datetime:
        """The next 00:00 UTC, when every budget starts again."""
        today = self._now(now).date()
        return datetime.combine(today + timedelta(days=1), time(), tzinfo=UTC)

    def _now(self, now: datetime | None) -> datetime:
        when = self._clock() if now is None else now
        return require_aware(when, "now").astimezone(UTC)

    def _day(self, now: datetime | None) -> str:
        return self._now(now).date().isoformat()
