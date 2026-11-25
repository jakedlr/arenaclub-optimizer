"""CardVolume: free graded Market Values, no key, attribution required.

Facts in docs/research/value-apis.md (CardVolume). The JSON API has no
search, so the provider downloads the card index (`cards.json`) at most once
a day, revalidating it with ETag/Last-Modified, matches cards locally on name,
set and number, then fetches the matched card's JSON. Requests carry the
project's honest user-agent and are spaced at least a second apart.
"""

import json
import logging
import math
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from arenaclub_optimizer.arena_client import USER_AGENT
from arenaclub_optimizer.config import cache_dir as default_cache_dir
from arenaclub_optimizer.model import CardIdentity, ValueConfidence
from arenaclub_optimizer.spacing import RequestSpacer
from arenaclub_optimizer.values.budget import CreditBudget
from arenaclub_optimizer.values.quote import (
    NotFound,
    ProviderUnavailable,
    QuoteResult,
    Unmatched,
    ValueQuote,
)

NAME = "cardvolume"
BASE_URL = "https://cardvolume.com/api/v1"
INDEX_FILENAME = "cardvolume-cards.json"
INDEX_MAX_AGE = timedelta(days=1)
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_TIMEOUT = 15.0

_INDEX_PATH = "/cards.json"
_PROMO = "promo"
_PROMO_WORDS = frozenset({"promo", "promos"})
# Index ids become URL paths, so only allow the characters CardVolume uses.
_CARD_ID = re.compile(r"[A-Za-z0-9-]+(?:/[A-Za-z0-9-]+)*")
_AMPERSAND = re.compile(r"\s*[&/]\s*")
_EX_GX_SUFFIX = re.compile(r"[\s-]+(ex|gx)\b")
_NUMBER_PREFIX_ZEROS = re.compile(r"^([a-z]*)0+(?=[0-9])")

logger = logging.getLogger(__name__)

type Json = dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CardVolumeProvider:
    """A ValueProvider backed by cardvolume.com's free JSON API.

    The index is cached as `INDEX_FILENAME` in `cache_dir` (by default the
    value cache's directory). If refreshing a stale index fails (network,
    5xx, unusable body), the stale index is used with a warning; with no
    cached index, the failure is raised. A 403 or 429 always raises; with a
    `budget` it also marks the rest of today exhausted for "cardvolume", so
    later runs today don't ask again.
    `clock` and `sleep` drive request spacing and `utc_now` the index's age;
    tests pass fakes.
    """

    name = NAME
    credit_cost = 0

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        budget: CreditBudget | None = None,
        base_url: str = BASE_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        directory = cache_dir if cache_dir is not None else default_cache_dir()
        self.index_path = directory / INDEX_FILENAME
        self._budget = budget
        self._http = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
        )
        self._spacer = RequestSpacer(min_interval, clock=clock, sleep=sleep)
        self._utc_now = utc_now
        self._cards: list[Any] | None = None
        # Card JSON by card id; None for a card CardVolume answered 404 for.
        self._card_bodies: dict[str, Json | None] = {}

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
        """The card's Market Value at its grader and grade.

        Unmatched when the index has no such card (or its page is a 404).
        NotFound when the card is known but has no usable figure at the grade,
        or the identity is ungraded (CardVolume prices graded cards only).
        A promo matched without checking its year (see `match_card`) is
        quoted at low confidence.
        Raises ProviderUnavailable on a 403, 429, 5xx, network failure or an
        unusable response.
        """
        match = _match(self._index_cards(), identity)
        if match is None:
            return Unmatched()
        entry, unsure_match = match
        if identity.grader is None or identity.grade is None:
            return NotFound()
        card = self._card(entry["id"])
        if card is None:
            return Unmatched()
        grades = card.get("grades")
        if not isinstance(grades, list):
            raise _unusable(f"card {entry['id']}")
        found = _grade_entry(grades, identity.grader, identity.grade)
        if found is None:
            return NotFound()
        grade, uncertain = found
        return self._grade_quote(card, grade, uncertain=uncertain or unsure_match)

    def _grade_quote(self, card: Json, grade: Json, *, uncertain: bool) -> QuoteResult:
        """The quote for one `grades[]` entry. `uncertain` lowers a market
        price to low confidence (see `_grade_entry`)."""
        citation = card.get("cite_as") if isinstance(card.get("cite_as"), str) else None
        market = _price(grade.get("market_price_usd"))
        if market is not None:
            as_of = _date(card.get("as_of")) or _date(card.get("updated"))
            sales = grade.get("market_sales_n")
            return ValueQuote(
                market_value=market,
                source=NAME,
                as_of=as_of or self._utc_now().date(),
                sales_count=sales if _is_count(sales) else None,
                confidence=(ValueConfidence.LOW if uncertain else ValueConfidence.HIGH),
                citation=citation,
            )
        # Under 5 sales in 90 days CardVolume gives no market price; its docs
        # say to quote the last verified sale with its date instead.
        sale = grade.get("last_verified_sale")
        if isinstance(sale, dict):
            price, when = _price(sale.get("price_usd")), _date(sale.get("date"))
            if price is not None and when is not None:
                return ValueQuote(
                    market_value=price,
                    source=NAME,
                    as_of=when,
                    sales_count=1,
                    confidence=ValueConfidence.LOW,
                    citation=citation,
                )
        return NotFound()

    # --- the index -----------------------------------------------------------

    def _index_cards(self) -> list[Any]:
        if self._cards is None:
            self._cards = self._load_index()
        return self._cards

    def _load_index(self) -> list[Any]:
        now = self._utc_now()
        cached = _read_index_file(self.index_path)
        if cached is not None:
            age = now - cached.fetched_at
            if timedelta(0) <= age < INDEX_MAX_AGE:
                return cached.cards
        try:
            return self._refresh_index(cached, now)
        except _Refused:
            raise
        except ProviderUnavailable as error:
            if cached is None:
                raise
            # Keep answering through a short outage. The file stays stale, so
            # the next run tries again. A refusal (403/429) still backs off.
            logger.warning(
                "using the stale CardVolume index from %s: %s",
                cached.fetched_at.isoformat(),
                error,
            )
            return cached.cards

    def _refresh_index(self, cached: _CachedIndex | None, now: datetime) -> list[Any]:
        headers = {}
        if cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        response = self._get(_INDEX_PATH, headers)

        if response.status_code == 304 and cached is not None:
            index = _CachedIndex(now, cached.etag, cached.last_modified, cached.body)
        elif response.status_code == 200:
            body = _json_object(response, "cards.json")
            index = _CachedIndex(
                now,
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
                body,
            )
        else:
            raise _unexpected_status(response.status_code, "cards.json")
        if not isinstance(index.body.get("cards"), list):
            raise _unusable("cards.json")
        _write_index_file(self.index_path, index)
        return index.cards

    # --- cards ---------------------------------------------------------------

    def _card(self, card_id: str) -> Json | None:
        if card_id not in self._card_bodies:
            path = f"/cards/{card_id.replace('/', '--')}.json"
            response = self._get(path)
            if response.status_code == 404:
                # CardVolume answers an unknown card with its HTML 404 page.
                self._card_bodies[card_id] = None
            elif response.status_code == 200:
                self._card_bodies[card_id] = _json_object(response, f"card {card_id}")
            else:
                raise _unexpected_status(response.status_code, f"card {card_id}")
        return self._card_bodies[card_id]

    # --- HTTP ----------------------------------------------------------------

    def _get(
        self, path: str, headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """GET `path`, returning a 200, 304 or 404 response; anything else
        raises ProviderUnavailable."""
        self._spacer.wait()
        try:
            response = self._http.get(path, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                reason=f"CardVolume request failed: {exc}"
            ) from exc
        status = response.status_code
        if status in (403, 429):
            # Back off and report; never work around it (docs/plan.md, Data).
            retry_after = None
            if self._budget is not None:
                self._budget.exhaust(NAME)
                retry_after = self._budget.resets_at()
            raise _Refused(
                retry_after, reason=f"CardVolume refused the request (HTTP {status})"
            )
        if status in (200, 304, 404):
            return response
        raise _unexpected_status(status, path)


def match_card(cards: Iterable[Any], identity: CardIdentity) -> Json | None:
    """The one index entry that is the identity's card, or None when no entry
    or more than one matches.

    Compares name, set (the identity's insert, else its subset name) and
    number, after normalising both sides: case, "&" and "/", the "-"/" "
    before an "EX"/"GX" suffix, leading zeros and a "/total" suffix are
    ignored. An identity whose set is just "Promo" (Arena Club's subset name
    for promos) matches any set with "promo" or "promos" in its name, but then
    the year must agree too when both sides give one, compared as numbers
    even when the index gives it as text: promo numbers repeat across eras.
    The language must agree, and an entry whose id isn't a safe path is never
    matched.
    """
    match = _match(cards, identity)
    return None if match is None else match[0]


def _match(cards: Iterable[Any], identity: CardIdentity) -> tuple[Json, bool] | None:
    """`match_card`'s entry, and whether the match is unsure: a promo whose
    year couldn't be checked, because either side gives none or the index
    gives it as text rather than a number."""
    name = _text_key(identity.card_name)
    set_key = _text_key(identity.insert or identity.subset_name)
    number = _number_key(identity.set_number)
    if not (name and set_key and number):
        return None
    language = identity.language.value
    matches = []
    for entry in cards:
        if not isinstance(entry, dict):
            continue
        card_id = entry.get("id")
        if not (isinstance(card_id, str) and _CARD_ID.fullmatch(card_id)):
            continue
        if not (
            _text_key(entry.get("name")) == name
            and _number_key(entry.get("number")) == number
            and str(entry.get("language") or "English").casefold().startswith(language)
        ):
            continue
        same_set = _same_set(set_key, entry, identity.year)
        if same_set is not None:
            matches.append((entry, same_set is _SetMatch.UNSURE))
    if len(matches) > 1:
        logger.info(
            "CardVolume: %d index entries match %s, leaving it unmatched",
            len(matches),
            identity.card_name,
        )
        return None
    return matches[0] if matches else None


class _SetMatch(Enum):
    SURE = "sure"
    # A promo set whose year couldn't be checked.
    UNSURE = "unsure"


def _same_set(wanted: str, entry: Json, year: int | None) -> _SetMatch | None:
    """How `entry`'s set matches the wanted set key, or None if it doesn't."""
    offered = _text_key(entry.get("set"))
    if wanted != _PROMO:
        return _SetMatch.SURE if wanted == offered else None
    if not _PROMO_WORDS.intersection(offered.split()):
        return None
    raw_year = entry.get("year")
    entry_year = _year(raw_year)
    if year is None or entry_year is None:
        return _SetMatch.UNSURE
    if entry_year != year:
        return None
    return _SetMatch.SURE if isinstance(raw_year, int) else _SetMatch.UNSURE


def _year(value: object) -> int | None:
    """An index year as a number: an int, or whole-number text; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _text_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.casefold().split())
    text = _AMPERSAND.sub(" & ", text)
    return _EX_GX_SUFFIX.sub(r"\1", text)


def _number_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    number = value.casefold().split("/", 1)[0].strip()
    return _NUMBER_PREFIX_ZEROS.sub(r"\1", number)


def _grade_entry(
    grades: list[Any], grader: str, grade: str
) -> tuple[Json, bool] | None:
    """The `grades[]` entry for `grader` (any case) and `grade`, and whether
    it is uncertain. An exact grade wins; otherwise one whose grade carries a
    suffix, e.g. "10 Pristine" for "10".

    Uncertain means a suffix-only match (a bare "CGC 10" may not be Pristine,
    a "BGS 10" not Black Label, and those sell at a premium), or several
    versions of the card at this grade (Arena's parallel isn't mapped to a
    CardVolume version yet), in which case the first is used."""
    entries = [
        g
        for g in grades
        if isinstance(g, dict)
        and isinstance(g.get("grader"), str)
        and isinstance(g.get("grade"), str)
        and g["grader"].casefold() == grader.casefold()
    ]
    exact = [g for g in entries if _same_grade(g["grade"], grade)]
    if exact:
        return exact[0], _several_versions(exact)
    suffixed = [
        g
        for g in entries
        if (head := g["grade"].split(maxsplit=1)) and _same_grade(head[0], grade)
    ]
    if suffixed:
        return suffixed[0], True
    return None


def _several_versions(entries: list[Json]) -> bool:
    return len({str(g.get("version_key")) for g in entries}) > 1


def _same_grade(offered: str, wanted: str) -> bool:
    try:
        return Decimal(offered.strip()) == Decimal(wanted.strip())
    except InvalidOperation:
        return offered.strip().casefold() == wanted.strip().casefold()


def _price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _json_object(response: httpx.Response, what: str) -> Json:
    try:
        body = response.json()
    except ValueError as exc:
        raise _unusable(what) from exc
    if not isinstance(body, dict):
        raise _unusable(what)
    return body


class _Refused(ProviderUnavailable):
    """CardVolume answered 403 or 429: back off, never fall back or retry."""


def _unusable(what: str) -> ProviderUnavailable:
    return ProviderUnavailable(reason=f"CardVolume sent an unusable {what}")


def _unexpected_status(status: int, what: str) -> ProviderUnavailable:
    if status >= 500:
        return ProviderUnavailable(reason=f"CardVolume server error (HTTP {status})")
    return ProviderUnavailable(reason=f"CardVolume answered HTTP {status} for {what}")


# --- the index file ----------------------------------------------------------


@dataclass(frozen=True)
class _CachedIndex:
    """`cards.json` as downloaded, with when and its validators."""

    fetched_at: datetime
    etag: str | None
    last_modified: str | None
    body: Json

    @property
    def cards(self) -> list[Any]:
        return self.body["cards"]


def _read_index_file(path: Path) -> _CachedIndex | None:
    """The cached index, or None if it is missing or unreadable."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(stored["fetched_at"])
        body = stored["index"]
        etag, last_modified = stored.get("etag"), stored.get("last_modified")
    except OSError, ValueError, TypeError, KeyError:
        return None
    if (
        fetched_at.tzinfo is None
        or not isinstance(body, dict)
        or not isinstance(body.get("cards"), list)
    ):
        return None
    return _CachedIndex(
        fetched_at,
        etag if isinstance(etag, str) else None,
        last_modified if isinstance(last_modified, str) else None,
        body,
    )


def _write_index_file(path: Path, index: _CachedIndex) -> None:
    """Write atomically via a uniquely named temp file. A failed write is
    logged, not raised: the index was fetched."""
    stored = {
        "fetched_at": index.fetched_at.isoformat(),
        "etag": index.etag,
        "last_modified": index.last_modified,
        "index": index.body,
    }
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp = Path(f.name)
            json.dump(stored, f)
        tmp.replace(path)
    except OSError as exc:
        logger.warning("could not cache the CardVolume index in %s: %s", path, exc)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
