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
from collections.abc import Callable, Generator, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, NamedTuple, Self

import httpx

from arenaclub_optimizer.arena_client import USER_AGENT
from arenaclub_optimizer.model import CardIdentity, ValueConfidence
from arenaclub_optimizer.spacing import RequestSpacer
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

# The credits PokéPrice says a response cost (docs/research/value-apis.md).
CALLS_CONSUMED_HEADER = "X-API-Calls-Consumed"
# A 429 that names no limit type but asks us to wait longer than this is
# taken as the daily limit.
DAILY_LIMIT_WAIT = timedelta(hours=1)
# A graded quote resting on fewer sales than this is low confidence.
HIGH_CONFIDENCE_SALES = 5
# Grading companies PokéPrice keys `salesByGrade` by.
GRADERS = frozenset({"psa", "cgc", "bgs", "sgc", "tag", "ace"})

# Printable ASCII with no spaces; anything else can't go in a header as is.
_KEY_PATTERN = re.compile(r"[\x21-\x7e]+")
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# A whole number of at least 0, e.g. `Retry-After` seconds.
_WHOLE_NUMBER = re.compile(r"[0-9]+")
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


class _BearerAuth(httpx.Auth):
    """Adds `Authorization: Bearer <key>` to each request as it is sent, so
    the key is never held in a plain local or dict that a traceback with
    locals could show."""

    def __init__(self, key: _Secret) -> None:
        self._key = key

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = "Bearer " + self._key.reveal()
        yield request

    def __repr__(self) -> str:
        return "_BearerAuth(<redacted>)"


class _Lookup(NamedTuple):
    """How a card is looked up: the language, the set name and card text to
    search for, and the provider-id keys its set id and tcgPlayerId are
    cached under."""

    language: str
    set_query: str
    search: str
    set_key: str
    card_key: str


class PokePriceProvider:
    """A ValueProvider backed by PokéPrice Tracker's v2 API.

    `api_key` None (or blank) leaves the provider disabled: every quote raises
    ProviderUnavailable("PokéPrice key not set", lasting) without a request. A
    401 or 403 disables it for the rest of the run, also lasting. Requests
    carry an honest user-agent and are spaced at least `min_interval`
    seconds apart.

    Each request first spends its credits from `budget`. The set id (keyed by
    set name and language) and the card's tcgPlayerId are kept in `cache`'s
    provider ids until PokéPrice no longer knows the id or returns another
    card for it. Use it as a context manager, or call `close`.
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
            auth=None if self._key is None else _BearerAuth(self._key),
            timeout=timeout,
            transport=transport,
        )
        self._spacer = RequestSpacer(min_interval, clock=clock, sleep=sleep)

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
            raise ProviderUnavailable(reason=self._disabled, lasting=True)
        grade_key = None
        if identity.grade is not None:
            grade_key = _grade_key(identity.grader, identity.grade)
            if grade_key is None:
                return NotFound()
        lookup = _lookup(identity)
        if lookup is None:
            return Unmatched()
        tcg_player_id = self._tcg_player_id(identity, lookup)
        if tcg_player_id is None:
            return Unmatched()
        card = self._get_card(
            tcg_player_id, lookup.language, ebay=grade_key is not None
        )
        if card is None or not _same_card(card, identity, strict=False):
            # PokéPrice no longer knows the id, or it is another card: resolve
            # the card again next time.
            self._cache.delete_provider_id(NAME, lookup.card_key)
            return Unmatched()
        if grade_key is None:
            return self._raw_quote(card)
        return self._graded_quote(card, grade_key)

    def estimated_cost(self, identity: CardIdentity) -> int:
        """What quoting `identity` would cost now, from what is cached: 2
        credits graded (1 raw) with its tcgPlayerId cached, 7 (6) with only
        its set id cached, 8 (7) with neither, and 0 when a quote would send
        nothing. Makes no request and spends nothing."""
        if self._disabled is not None:
            return 0
        graded = identity.grade is not None
        if graded and _grade_key(identity.grader, identity.grade) is None:
            return 0
        lookup = _lookup(identity)
        if lookup is None:
            return 0
        cost = cards_cost(1, ebay=graded)
        if self._cache.get_provider_id(NAME, lookup.card_key) is not None:
            return cost
        cost += cards_cost(CARD_SEARCH_LIMIT, ebay=False)
        if self._cache.get_provider_id(NAME, lookup.set_key) is None:
            cost += SET_SEARCH_COST
        return cost

    # --- resolving a card ----------------------------------------------------

    def _tcg_player_id(self, identity: CardIdentity, lookup: _Lookup) -> str | None:
        """The card's tcgPlayerId, from the cache or a set and card search.

        A set id found only as the first `/sets` result (no exact name match)
        is cached once a card matches in that set by name and number.
        """
        cached = self._cache.get_provider_id(NAME, lookup.card_key)
        if cached is not None:
            return cached
        set_id, exact = self._set_id(lookup)
        if set_id is None:
            return None
        params = {
            "setId": set_id,
            "search": lookup.search,
            "language": lookup.language,
            "limit": str(CARD_SEARCH_LIMIT),
        }
        cost = cards_cost(CARD_SEARCH_LIMIT, ebay=False)
        body = self._get("/cards", params, cost=cost)
        found = _match_card(_records(body), identity)
        if found is None:
            return None
        if not exact:
            self._cache.put_provider_id(NAME, lookup.set_key, set_id)
        self._cache.put_provider_id(NAME, lookup.card_key, found)
        return found

    def _set_id(self, lookup: _Lookup) -> tuple[str | None, bool]:
        """The set id to search in, and whether it is certain: cached, or
        the exact name match from `/sets` (which is then cached)."""
        cached = self._cache.get_provider_id(NAME, lookup.set_key)
        if cached is not None:
            return cached, True
        params = {
            "search": lookup.set_query,
            "language": lookup.language,
            "limit": str(SET_SEARCH_LIMIT),
        }
        body = self._get("/sets", params, cost=SET_SEARCH_COST)
        set_id, exact = _choose_set(_records(body), lookup.set_query)
        if set_id is not None and exact:
            self._cache.put_provider_id(NAME, lookup.set_key, set_id)
        return set_id, exact

    def _get_card(
        self, tcg_player_id: str, language: str, *, ebay: bool
    ) -> Json | None:
        """The card from `/cards/{id}`, or None if PokéPrice doesn't know it."""
        if not _ID_PATTERN.fullmatch(tcg_player_id):
            return None
        params = {"language": language, "limit": "1"}
        if ebay:
            params["includeEbay"] = "true"
        cost = cards_cost(1, ebay=ebay)
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
            as_of=self._as_of(stats.get("lastMarketUpdate"), ebay.get("updatedAt")),
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
            as_of=self._as_of(prices.get("lastUpdated")),
            sales_count=None,
            confidence=ValueConfidence.LOW,
        )

    def _as_of(self, *stamps: object) -> date:
        """The date of the first usable time stamp, else today."""
        for stamp in stamps:
            when = _date(stamp)
            if when is not None:
                return when
        return self._budget.now().date()

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
        if self._disabled is not None:  # e.g. by a 401 earlier in the run
            raise ProviderUnavailable(reason=self._disabled, lasting=True)
        self._budget.spend_or_raise(NAME, cost)
        self._spacer.wait()
        logger.debug("PokéPrice GET %s (%d credits)", path, cost)
        # Raised outside the `except`, so the error keeps no reference to the
        # httpx exception and its request (which carries the key).
        response: httpx.Response | None = None
        failure = ""
        try:
            response = self._http.get(path, params=params)
        except httpx.TimeoutException:
            failure = "PokéPrice timed out"
        except httpx.HTTPError as error:
            failure = f"PokéPrice request failed ({type(error).__name__})"
        if response is None:
            raise ProviderUnavailable(reason=failure)

        status = response.status_code
        if status == 429:
            raise self._rate_limited(response)
        if status in (401, 403):
            self._disabled = f"PokéPrice rejected the API key (HTTP {status})"
            logger.warning("%s; not asking PokéPrice again this run", self._disabled)
            raise ProviderUnavailable(reason=self._disabled, lasting=True)
        if status == 404 and missing_ok:
            return None
        if not response.is_success:
            raise ProviderUnavailable(reason=f"PokéPrice HTTP {status} on {path}")
        _log_server_charge(response, path, cost)
        body = _json(response)
        if body is None:
            raise ProviderUnavailable(reason=f"PokéPrice sent an unusable {path} body")
        return body

    def _rate_limited(self, response: httpx.Response) -> ProviderUnavailable:
        """The error for a 429, exhausting today's budget on a daily limit
        (or, when the body names no limit type, a wait over an hour)."""
        body = _json(response) or {}
        now = self._budget.now()
        retry_after = _retry_after(response.headers.get("Retry-After"), now)
        if retry_after is None:
            retry_after = _aware(body.get("resetsAt"))
        limit_type = body.get("limitType")
        long_wait = retry_after is not None and retry_after - now > DAILY_LIMIT_WAIT
        if limit_type == "daily" or (limit_type is None and long_wait):
            self._budget.exhaust(NAME)
            if retry_after is None:
                retry_after = self._budget.resets_at()
            return ProviderUnavailable(
                retry_after, reason="PokéPrice daily limit reached"
            )
        return ProviderUnavailable(retry_after, reason="PokéPrice rate limited")


def _log_server_charge(response: httpx.Response, path: str, cost: int) -> None:
    """Log at DEBUG when PokéPrice's `X-API-Calls-Consumed` differs from the
    credits spent locally for the request, so a billing surprise shows up.
    The budget keeps the local figure: the server's isn't verified yet."""
    header = response.headers.get(CALLS_CONSUMED_HEADER, "").strip()
    if not _WHOLE_NUMBER.fullmatch(header):
        return
    charged = int(header)
    if charged != cost:
        logger.debug(
            "PokéPrice %s: %s is %d, local spend was %d credits",
            path,
            CALLS_CONSUMED_HEADER,
            charged,
            cost,
        )


# --- matching ----------------------------------------------------------------


def _lookup(identity: CardIdentity) -> _Lookup | None:
    """How to look `identity` up, or None if it has no set or nothing to
    search for."""
    set_query = _set_query(identity)
    words = (identity.card_name, identity.set_number)
    search = " ".join(word for word in words if word is not None)
    if set_query is None or not search:
        return None
    language = identity.language.value
    return _Lookup(
        language=language,
        set_query=set_query,
        search=search,
        set_key=f"set:{language}|{_normalise(set_query)}",
        card_key=_card_key(identity, set_query),
    )


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


def _card_key(identity: CardIdentity, set_query: str) -> str:
    """The provider-id key a card's tcgPlayerId is cached under: language,
    set, number and name, normalised, and never the grade."""
    parts = (
        identity.language.value,
        _normalise(set_query),
        _number_key(identity.set_number) or "",
        _normalise(identity.card_name or ""),
    )
    return "card:" + "|".join(parts)


def _choose_set(sets: list[Json], set_query: str) -> tuple[str | None, bool]:
    """The id of the set named `set_query` and True, else the first set's id
    and False (to be trusted only once a card matches in it)."""
    usable = [(s, set_id) for s in sets if (set_id := _set_id_of(s)) is not None]
    wanted = _normalise(set_query)
    for found, set_id in usable:
        if _normalise(str(found.get("name") or "")) == wanted:
            return set_id, True
    return (usable[0][1], False) if usable else (None, False)


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
    """The tcgPlayerId of the search result that is the card, or None.

    A result must have the card's number (ignoring "/total", case and
    leading zeros) and contain its name's words. A result whose whole name
    is the card's name wins over one that only contains its words, so
    "Charizard" picks Charizard over Dark Charizard. A card number alone
    can't tell cards apart, so a card without a name needs exactly one match.
    """
    matches = [
        card
        for card in cards
        if _tcg_player_id_of(card) is not None
        and _same_card(card, identity, strict=True)
    ]
    if not matches or (identity.card_name is None and len(matches) > 1):
        return None
    if identity.card_name is not None:
        wanted = _normalise(identity.card_name)
        exact = [c for c in matches if _normalise(str(c.get("name"))) == wanted]
        matches = exact or matches
    return _tcg_player_id_of(matches[0])


def _same_card(card: Json, identity: CardIdentity, *, strict: bool) -> bool:
    """Whether a PokéPrice card has `identity`'s name words and number (each
    checked when the identity has it). Unless `strict`, a field the card
    leaves out is not held against it."""
    checks = (
        (identity.card_name, card.get("name"), _name_matches),
        (identity.set_number, card.get("cardNumber"), _same_number),
    )
    for wanted, found, same in checks:
        if wanted is None or (found is None and not strict):
            continue
        if not same(wanted, found):
            return False
    return True


def _tcg_player_id_of(card: Json) -> str | None:
    value = card.get("tcgPlayerId")
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    text = str(value).strip()
    return text if _ID_PATTERN.fullmatch(text) else None


def _name_matches(card_name: str, candidate: object) -> bool:
    """Whether every word of `card_name` is in `candidate`, so "Umbreon VMAX"
    matches "Umbreon VMAX (Alternate Art Secret)"."""
    if not isinstance(candidate, str):
        return False
    wanted = _normalise(card_name).split()
    return bool(wanted) and set(wanted) <= set(_normalise(candidate).split())


def _same_number(number: str, candidate: object) -> bool:
    wanted = _number_key(number)
    return wanted is not None and _number_key(candidate) == wanted


def _number_key(number: object) -> str | None:
    """A card number for comparing: before any "/", upper case, no leading
    zeros on its digit runs ("059" and "59/102" are both "59")."""
    if not isinstance(number, str | int) or isinstance(number, bool):
        return None
    text = str(number).split("/", 1)[0].strip().upper()
    return _LEADING_ZEROS.sub("", text) or None


def _normalise(text: str) -> str:
    """Lower case without accents, "/" as "&" and "&" a word of its own
    ("Greninja/Zoroark GX" and "Greninja & Zoroark-GX" read alike), other
    punctuation as spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = plain.casefold().replace("/", "&").replace("&", " & ")
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
    if value is None or not _WHOLE_NUMBER.fullmatch(value.strip()):
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
