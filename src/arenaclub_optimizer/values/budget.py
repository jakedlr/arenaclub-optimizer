"""Daily credit budgets for value providers, kept in the value cache's file."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, time, timedelta
from types import MappingProxyType

from arenaclub_optimizer.values.cache import ValueCache
from arenaclub_optimizer.values.quote import require_aware

# PokéPrice Tracker's free tier: 100 credits a day (docs/research/value-apis.md).
DEFAULT_DAILY_LIMITS: Mapping[str, int] = MappingProxyType({"pokeprice": 100})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CreditBudget:
    """How many credits each provider may spend per UTC day.

    `limits` maps a provider name to its daily limit; a provider not listed
    is unlimited. Spending is stored in `cache`'s file, so it carries over
    between runs and processes, and starts again at 00:00 UTC. Every method
    takes an optional `now` (time-zone aware); without it, `clock` is used.
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

    def try_spend(self, provider: str, n: int, *, now: datetime | None = None) -> bool:
        """Spend `n` credits if today's remaining budget covers them.

        Returns whether they were spent; a refused spend spends nothing.
        Raises ValueError if `n` is negative.
        """
        if n < 0:
            raise ValueError(f"can't spend a negative number of credits: {n}")
        day = self._day(now)
        if n == 0:
            return True
        return self._cache._spend_credits(provider, day, n, self._limits.get(provider))

    def can_spend(self, provider: str, n: int, *, now: datetime | None = None) -> bool:
        """Whether today's remaining budget covers `n` credits, without spending."""
        remaining = self.remaining(provider, now=now)
        return remaining is None or n <= remaining

    def remaining(self, provider: str, *, now: datetime | None = None) -> int | None:
        """Credits left today, or None for a provider without a limit."""
        day = self._day(now)
        limit = self._limits.get(provider)
        if limit is None:
            return None
        return max(limit - self._cache._credits_spent(provider, day), 0)

    def resets_at(self, *, now: datetime | None = None) -> datetime:
        """The next 00:00 UTC, when every budget starts again."""
        today = self._now(now).date()
        return datetime.combine(today + timedelta(days=1), time(), tzinfo=UTC)

    def _now(self, now: datetime | None) -> datetime:
        when = self._clock() if now is None else now
        return require_aware(when, "now").astimezone(UTC)

    def _day(self, now: datetime | None) -> str:
        return self._now(now).date().isoformat()
