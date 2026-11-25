"""PokéPrice Tracker value provider (stub: tests first)."""

from collections.abc import Mapping
from pathlib import Path

from arenaclub_optimizer.model import CardIdentity
from arenaclub_optimizer.values import QuoteResult

BASE_URL = "https://www.pokemonpricetracker.com/api/v2"
API_KEY_ENV = "POKEPRICE_API_KEY"
KEY_NOT_SET = "PokéPrice key not set"


def load_api_key(
    environ: Mapping[str, str] | None = None, *, env_file: Path | None = None
) -> str | None:
    raise NotImplementedError


class PokePriceProvider:
    name = "pokeprice"
    credit_cost = 0

    def __init__(self, api_key, cache, budget, **kwargs) -> None:
        raise NotImplementedError

    @classmethod
    def from_environment(cls, cache, budget, **kwargs) -> PokePriceProvider:
        raise NotImplementedError

    def quote(self, identity: CardIdentity) -> QuoteResult:
        raise NotImplementedError
