"""PokéPrice Tracker value provider: graded Market Values from eBay sales.

See ADR 0002 and docs/research/value-apis.md (PokéPrice section). Facts about
the API come from its OpenAPI spec (v2.0.0). The provider resolves a card to
its tcgPlayerId once (a set search, then a card search) and caches both ids
permanently in the value cache, so later quotes cost only the priced lookup.

The API key comes from `POKEPRICE_API_KEY` or a `.env` file. It is only ever
put in the Authorization header: never logged, never in an exception, a repr
or the cache.
"""

import logging
import math
import os
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from arenaclub_optimizer.arena_client import USER_AGENT
from arenaclub_optimizer.model import CardIdentity, ValueConfidence
from arenaclub_optimizer.values import (
    CreditBudget,
    NotFound,
    ProviderUnavailable,
    QuoteResult,
    Unmatched,
    ValueCache,
    ValueQuote,
)

BASE_URL = "https://www.pokemonpricetracker.com/api/v2"
API_KEY_ENV = "POKEPRICE_API_KEY"
DOTENV_FILENAME = ".env"
KEY_NOT_SET = "PokéPrice key not set"
KEY_MALFORMED = f"PokéPrice key is malformed (check {API_KEY_ENV})"
NAME = "pokeprice"
# 1 s apart keeps us under the free plan's 60 calls a minute.
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_TIMEOUT = 15.0

# Credits, per the spec. `/sets` costs 1 per request whatever its `limit`.
# `/cards` (and `/cards/{id}`) costs `limit × (1 + includeEbay + ...)`, billed
# on the requested limit, not the number of results, so every request passes
# a small explicit limit.
SET_SEARCH_LIMIT = 5
CARD_SEARCH_LIMIT = 5
SET_SEARCH_COST = 1


def cards_cost(limit: int, *, ebay: bool) -> int:
    """Credits for a `/cards` request: `limit × (1 + includeEbay)`."""
    return limit * (1 + int(ebay))


CARD_SEARCH_COST = cards_cost(CARD_SEARCH_LIMIT, ebay=False)
PRICED_LOOKUP_COST = cards_cost(1, ebay=True)
RAW_LOOKUP_COST = cards_cost(1, ebay=False)
# The most one quote can cost: resolving the set and the card, then pricing it.
CREDIT_COST = SET_SEARCH_COST + CARD_SEARCH_COST + PRICED_LOOKUP_COST

# A graded quote resting on fewer sales than this is low confidence.
HIGH_CONFIDENCE_SALES = 5
# Grading companies PokéPrice keys `salesByGrade` by.
GRADERS = frozenset({"psa", "cgc", "bgs", "sgc", "tag", "ace"})

# Printable ASCII with no spaces; anything else can't go in a header as is.
_KEY_PATTERN = re.compile(r"[\x21-\x7e]+")
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_DELTA_SECONDS = re.compile(r"[0-9]+")
_LEADING_ZEROS = re.compile(r"(?<![0-9])0+(?=[0-9])")
_YEAR = re.compile(r"[0-9]{4}")
# Subsets that don't name a set, so the set comes from `set_name` instead.
_NOT_A_SET = frozenset({"base", "promo"})
# Words in Arena Club's `setName` that aren't part of PokéPrice's set name.
_SET_NAME_NOISE = frozenset({"pokemon", "jpn", "japanese"})

logger = logging.getLogger(__name__)

type Json = dict[str, Any]


def load_api_key(
    environ: Mapping[str, str] | None = None, *, env_file: Path | None = None
) -> str | None:
    """The PokéPrice API key from `POKEPRICE_API_KEY` in `environ` (default
    the process environment), else from `env_file` (default `.env` in the
    working directory), else None. Surrounding whitespace and quotes in the
    file are dropped; an empty value counts as unset."""
    env = os.environ if environ is None else environ
    key = (env.get(API_KEY_ENV) or "").strip()
    if key:
        return key
    path = Path(DOTENV_FILENAME) if env_file is None else env_file
    return _read_dotenv(path, API_KEY_ENV)


class _Secret:
    """A string that never shows itself in a repr or str."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


class PokePriceProvider:
    """A ValueProvider backed by PokéPrice Tracker's v2 API.

    `api_key` None (or blank) leaves the provider disabled: every quote raises
    ProviderUnavailable("PokéPrice key not set") without a request. A 401 or
    403 disables it for the rest of the run. Requests carry an honest
    user-agent and are spaced at least `min_interval` seconds apart.

    Each request first spends its credits from `budget`. The set id (keyed by
    set name and language) and the card's tcgPlayerId are kept permanently in
    `cache`'s provider ids. Use it as a context manager, or call `close`.
    """

    name = NAME
    credit_cost = CREDIT_COST

    def __init__(
        self,
        api_key: str | None,
        cache: ValueCache,
        budget: CreditBudget,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cache = cache
        self._budget = budget
        self._key: _Secret | None = None
        self._disabled: str | None = None
        key = (api_key or "").strip()
        if not key:
            self._disabled = KEY_NOT_SET
        elif not _KEY_PATTERN.fullmatch(key):
            self._disabled = KEY_MALFORMED
        else:
            self._key = _Secret(key)
        self._http = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
        )
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None

    @classmethod
    def from_environment(
        cls,
        cache: ValueCache,
        budget: CreditBudget,
        *,
        environ: Mapping[str, str] | None = None,
        env_file: Path | None = None,
        **kwargs: Any,
    ) -> Self:
        """A provider with the key from `load_api_key(environ, env_file=...)`."""
        return cls(load_api_key(environ, env_file=env_file), cache, budget, **kwargs)

    def __repr__(self) -> str:
        state = "key set" if self._key is not None else "no key"
        return f"PokePriceProvider({state})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def quote(self, identity: CardIdentity) -> QuoteResult:
        """The card's Market Value at its grade, or raw when the grade is None.

        Graded: `ebay.salesByGrade[<grader><grade>]` (e.g. `psa10`,
        `cgc9_5`), its `smartMarketPrice.price`, or `medianPrice` when
        `smartPriceOutlierByGrade` flags the grade; `sales_count` is `count`,
        high confidence from 5 sales. Raw: `prices.market` (TCGplayer), low
        confidence. NotFound when there is no price at the grade (or the
        grade can't be keyed), Unmatched when the card can't be found.

        Raises ProviderUnavailable (no key, a rejected key, a 429, a failed
        request) or BudgetExhausted.
        """
        if self._disabled is not None:
            raise ProviderUnavailable(reason=self._disabled)
        grade_key = None
        if identity.grade is not None:
            grade_key = _grade_key(identity.grader, identity.grade)
            if grade_key is None:
                return NotFound()
        tcg_player_id = self._tcg_player_id(identity)
        if tcg_player_id is None:
            return Unmatched()
        card = self._get_card(
            tcg_player_id, identity.language.value, ebay=grade_key is not None
        )
        if card is None:
            return Unmatched()
        if grade_key is None:
            return self._raw_quote(card)
        return self._graded_quote(card, grade_key)

    # --- resolving a card ----------------------------------------------------

    def _tcg_player_id(self, identity: CardIdentity) -> str | None:
        """The card's tcgPlayerId, from the cache or a set and card search."""
        set_query = _set_query(identity)
        search = identity.set_number or identity.card_name
        if set_query is None or search is None:
            return None
        language = identity.language.value
        card_key = "card:" + "|".join(
            (
                language,
                _normalise(set_query),
                _number_key(identity.set_number) or "",
                _normalise(identity.card_name or ""),
            )
        )
        cached = self._cache.get_provider_id(NAME, card_key)
        if cached is not None:
            return cached
        set_id = self._set_id(set_query, language)
        if set_id is None:
            return None
        params = {
            "setId": set_id,
            "search": search,
            "language": language,
            "limit": str(CARD_SEARCH_LIMIT),
        }
        body = self._get("/cards", params, cost=CARD_SEARCH_COST)
        found = _match_card(_records(body), identity)
        if found is not None:
            self._cache.put_provider_id(NAME, card_key, found)
        return found

    def _set_id(self, set_query: str, language: str) -> str | None:
        """The set id for `set_query`, from the cache or `/sets`."""
        set_key = f"set:{language}|{_normalise(set_query)}"
        cached = self._cache.get_provider_id(NAME, set_key)
        if cached is not None:
            return cached
        params = {
            "search": set_query,
            "language": language,
            "limit": str(SET_SEARCH_LIMIT),
        }
        body = self._get("/sets", params, cost=SET_SEARCH_COST)
        found = _choose_set(_records(body), set_query)
        if found is not None:
            self._cache.put_provider_id(NAME, set_key, found)
        return found

    def _get_card(
        self, tcg_player_id: str, language: str, *, ebay: bool
    ) -> Json | None:
        """The card from `/cards/{id}`, or None if PokéPrice doesn't know it."""
        if not _ID_PATTERN.fullmatch(tcg_player_id):
            return None
        params = {"language": language, "limit": "1"}
        if ebay:
            params["includeEbay"] = "true"
        cost = PRICED_LOOKUP_COST if ebay else RAW_LOOKUP_COST
        body = self._get(f"/cards/{tcg_player_id}", params, cost=cost, missing_ok=True)
        records = _records(body) if body is not None else []
        return records[0] if records else None

    # --- pricing -------------------------------------------------------------

    def _graded_quote(self, card: Json, grade_key: str) -> QuoteResult:
        ebay = _dict(card.get("ebay"))
        stats = _dict(_dict(ebay.get("salesByGrade")).get(grade_key))
        if not stats:
            return NotFound()
        outlier = _dict(ebay.get("smartPriceOutlierByGrade")).get(grade_key) is True
        smart = _dict(stats.get("smartMarketPrice")).get("price")
        median = stats.get("medianPrice")
        candidates = (median, smart) if outlier else (smart, median)
        price = next((p for p in candidates if _is_price(p)), None)
        if price is None:
            return NotFound()
        sales = _count(stats.get("count"))
        high = sales is not None and sales >= HIGH_CONFIDENCE_SALES
        return ValueQuote(
            market_value=float(price),
            source=NAME,
            as_of=_date(stats.get("lastMarketUpdate"))
            or _date(ebay.get("updatedAt"))
            or self._budget.now().date(),
            sales_count=sales,
            confidence=ValueConfidence.HIGH if high else ValueConfidence.LOW,
        )

    def _raw_quote(self, card: Json) -> QuoteResult:
        prices = _dict(card.get("prices"))
        market = prices.get("market")
        if not _is_price(market):
            return NotFound()
        return ValueQuote(
            market_value=float(market),
            source=NAME,
            as_of=_date(prices.get("lastUpdated")) or self._budget.now().date(),
            sales_count=None,
            confidence=ValueConfidence.LOW,
        )

    # --- HTTP ----------------------------------------------------------------

    def _get(
        self,
        path: str,
        params: Mapping[str, str],
        *,
        cost: int,
        missing_ok: bool = False,
    ) -> Json | None:
        """GET `path`, spending `cost` credits first. None for a 404 when
        `missing_ok`; ProviderUnavailable for any other failure."""
        if self._key is None:  # disabled during the run, e.g. by a 401
            raise ProviderUnavailable(reason=self._disabled or KEY_NOT_SET)
        self._budget.spend_or_raise(NAME, cost)
        self._wait_for_slot()
        logger.debug("PokéPrice GET %s (%d credits)", path, cost)
        headers = {"Authorization": f"Bearer {self._key.reveal()}"}
        try:
            response = self._http.get(path, params=params, headers=headers)
        except httpx.TimeoutException:
            raise ProviderUnavailable(reason="PokéPrice timed out") from None
        except httpx.HTTPError as error:
            reason = f"PokéPrice request failed ({type(error).__name__})"
            raise ProviderUnavailable(reason=reason) from None

        status = response.status_code
        if status == 429:
            raise self._rate_limited(response)
        if status in (401, 403):
            self._disabled = f"PokéPrice rejected the API key (HTTP {status})"
            self._key = None
            logger.warning("%s; not asking PokéPrice again this run", self._disabled)
            raise ProviderUnavailable(reason=self._disabled)
        if status == 404 and missing_ok:
            return None
        if not response.is_success:
            raise ProviderUnavailable(reason=f"PokéPrice HTTP {status} on {path}")
        body = _json(response)
        if body is None:
            raise ProviderUnavailable(reason=f"PokéPrice sent an unusable {path} body")
        return body

    def _rate_limited(self, response: httpx.Response) -> ProviderUnavailable:
        """The error for a 429, exhausting today's budget on a daily limit."""
        body = _json(response) or {}
        now = self._budget.now()
        retry_after = _retry_after(response.headers.get("Retry-After"), now)
        if retry_after is None:
            retry_after = _aware(body.get("resetsAt"))
        if body.get("limitType") == "daily":
            self._budget.exhaust(NAME)
            if retry_after is None:
                retry_after = self._budget.resets_at()
            return ProviderUnavailable(
                retry_after, reason="PokéPrice daily limit reached"
            )
        return ProviderUnavailable(retry_after, reason="PokéPrice rate limited")

    def _wait_for_slot(self) -> None:
        """Sleep until `min_interval` has passed since the previous request."""
        if self._last_request_at is not None:
            remaining = self._min_interval - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()


# --- matching ----------------------------------------------------------------


def _set_query(identity: CardIdentity) -> str | None:
    """The set name to search: `insert`, else a subset that names a set, else
    `set_name` without its year, "Pokemon" and "JPN"."""
    for name in (identity.insert, identity.subset_name):
        if name is not None and name.casefold() not in _NOT_A_SET:
            return name
    if identity.set_name is None:
        return None
    words = identity.set_name.split()
    if words and _YEAR.fullmatch(words[0]):
        words = words[1:]
    kept = [w for w in words if _normalise(w) not in _SET_NAME_NOISE]
    return " ".join(kept) or None


def _choose_set(sets: list[Json], set_query: str) -> str | None:
    """The id of the set whose name is `set_query`, else of the first set."""
    usable = [(s, set_id) for s in sets if (set_id := _set_id_of(s)) is not None]
    wanted = _normalise(set_query)
    for found, set_id in usable:
        if _normalise(str(found.get("name") or "")) == wanted:
            return set_id
    return usable[0][1] if usable else None


def _set_id_of(found: Json) -> str | None:
    """A set's id for `/cards?setId=`: the numeric GroupId, else the slug,
    else the Mongo id."""
    for field in ("tcgPlayerNumericId", "tcgPlayerId", "id"):
        value = found.get(field)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | str) and str(value).strip():
            return str(value).strip()
    return None


def _match_card(cards: list[Json], identity: CardIdentity) -> str | None:
    """The tcgPlayerId of the search result that is the card.

    With a number, a result must have the same number (ignoring "/total",
    case and leading zeros); among several, the one whose name contains the
    card's name words wins, and an unresolved tie matches nothing. Without
    a number, the first result whose name contains the card's name words.
    """
    usable = [c for c in cards if _tcg_player_id_of(c) is not None]
    named = [c for c in usable if _name_matches(identity.card_name, c.get("name"))]
    if identity.set_number is not None:
        wanted = _number_key(identity.set_number)
        numbered = [c for c in usable if _number_key(c.get("cardNumber")) == wanted]
        both = [c for c in numbered if c in named]
        if both:
            return _tcg_player_id_of(both[0])
        return _tcg_player_id_of(numbered[0]) if len(numbered) == 1 else None
    return _tcg_player_id_of(named[0]) if named else None


def _tcg_player_id_of(card: Json) -> str | None:
    value = card.get("tcgPlayerId")
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    text = str(value).strip()
    return text if _ID_PATTERN.fullmatch(text) else None


def _name_matches(card_name: str | None, candidate: object) -> bool:
    if card_name is None or not isinstance(candidate, str):
        return False
    wanted = _normalise(card_name).split()
    return bool(wanted) and set(wanted) <= set(_normalise(candidate).split())


def _number_key(number: object) -> str | None:
    """A card number for comparing: before any "/", upper case, no leading
    zeros on its digit runs ("059" and "59/102" are both "59")."""
    if not isinstance(number, str | int) or isinstance(number, bool):
        return None
    text = str(number).split("/", 1)[0].strip().upper()
    return _LEADING_ZEROS.sub("", text) or None


def _normalise(text: str) -> str:
    """Lower case without accents, "/" as "&", other punctuation as spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = plain.casefold().replace("/", "&")
    words = "".join(c if c.isalnum() or c == "&" else " " for c in plain)
    return " ".join(words.split())


def _grade_key(grader: str | None, grade: str) -> str | None:
    """The `salesByGrade` key for a grade, e.g. psa10 or cgc9_5, or None for
    a grader PokéPrice doesn't track or a grade that isn't whole or half."""
    if grader is None:
        return None
    company = grader.strip().casefold()
    if company not in GRADERS:
        return None
    try:
        value = Decimal(grade)
    except InvalidOperation:
        return None
    if not value.is_finite() or not 0 < value <= 10:
        return None
    if (value * 2) != (value * 2).to_integral_value():
        return None
    whole = int(value)
    return f"{company}{whole}" if value == whole else f"{company}{whole}_5"


# --- parsing -----------------------------------------------------------------


def _json(response: httpx.Response) -> Json | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _records(body: Json | None) -> list[Json]:
    """`data` as a list of objects: it is one object for a single card."""
    data = (body or {}).get("data")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _dict(value: object) -> Json:
    return value if isinstance(value, dict) else {}


def _count(value: object) -> int | None:
    """A sales count: a whole number of at least 0, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_price(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value > 0


def _date(value: object) -> date | None:
    """The date of an ISO 8601 date or date-time, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _aware(value: object) -> datetime | None:
    """An ISO 8601 date-time with a time zone, or None."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.utcoffset() is not None else None


def _retry_after(value: str | None, now: datetime) -> datetime | None:
    """When a `Retry-After` of delta-seconds ends, or None."""
    if value is None or not _DELTA_SECONDS.fullmatch(value.strip()):
        return None
    return now + timedelta(seconds=int(value.strip()))


def _read_dotenv(path: Path, name: str) -> str | None:
    """`name`'s value in a dotenv file (the last one wins), or None.

    Reads `NAME=value` lines, with an optional `export ` and optional
    matching quotes; skips blank lines and `#` comments. Never logs values.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    found = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1].strip()
        found = value or None
    return found
