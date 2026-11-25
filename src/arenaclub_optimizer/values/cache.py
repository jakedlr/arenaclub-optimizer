"""The on-disk value cache: Value Quotes, negative outcomes, provider ids and
spent credits, in one SQLite file.

The file is `values.sqlite` in `$XDG_CACHE_HOME/arenaclub-optimizer/` (else
`~/.cache/arenaclub-optimizer/`), or in a directory the caller passes.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

from arenaclub_optimizer.model import ValueConfidence
from arenaclub_optimizer.values.quote import (
    NotFound,
    QuoteResult,
    Unmatched,
    ValueQuote,
    require_aware,
)

VALUES_FILENAME = "values.sqlite"
DEFAULT_QUOTE_MAX_AGE = timedelta(days=7)
DEFAULT_NEGATIVE_MAX_AGE = timedelta(days=30)
# How long to wait for another process holding the file's write lock.
_LOCK_TIMEOUT_SECONDS = 10.0

_QUOTE, _NOT_FOUND, _UNMATCHED = "quote", "not_found", "unmatched"
_NEGATIVE_KINDS = {_NOT_FOUND: NotFound(), _UNMATCHED: Unmatched()}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    card_id      TEXT NOT NULL,
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('quote', 'not_found', 'unmatched')),
    market_value REAL,
    as_of        TEXT,
    sales_count  INTEGER,
    confidence   TEXT,
    citation     TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (card_id, source)
);
CREATE TABLE IF NOT EXISTS provider_ids (
    provider TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (provider, key)
);
CREATE TABLE IF NOT EXISTS credits (
    provider TEXT NOT NULL,
    day      TEXT NOT NULL,
    spent    INTEGER NOT NULL,
    PRIMARY KEY (provider, day)
);
"""


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/arenaclub-optimizer``, else ``~/.cache/...``.

    Per the XDG spec, an unset, empty or relative ``XDG_CACHE_HOME`` is ignored.
    """
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return base / "arenaclub-optimizer"


@dataclass(frozen=True)
class CacheEntry:
    """A cached lookup result, when it was fetched, and whether it is still
    fresh under the cache's staleness limits."""

    result: QuoteResult
    fetched_at: datetime
    fresh: bool


class ValueCache:
    """Cached lookup results keyed by (card id, source), plus a permanent
    key/value store of provider ids.

    A Value Quote stays fresh for `quote_max_age` after it was fetched, and a
    NotFound or Unmatched outcome for `negative_max_age`, so cards a provider
    can't price aren't asked about again until then. Times must be time-zone
    aware. Use it as a context manager, or call `close`.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        quote_max_age: timedelta = DEFAULT_QUOTE_MAX_AGE,
        negative_max_age: timedelta = DEFAULT_NEGATIVE_MAX_AGE,
    ) -> None:
        self.directory = directory if directory is not None else default_cache_dir()
        self.path = self.directory / VALUES_FILENAME
        self.quote_max_age = quote_max_age
        self.negative_max_age = negative_max_age
        self.directory.mkdir(parents=True, exist_ok=True)
        # Autocommit: each statement commits by itself; `_spend_credits` opens
        # its own transaction.
        self._db = sqlite3.connect(
            self.path, timeout=_LOCK_TIMEOUT_SECONDS, isolation_level=None
        )
        self._db.executescript(_SCHEMA)

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
        self._db.close()

    def get(self, card_id: str, source: str, *, now: datetime) -> CacheEntry | None:
        """The cached result for `card_id` from `source`, fresh or stale, or
        None if there is none (or it can't be read)."""
        now = require_aware(now, "now")
        row = self._db.execute(
            "SELECT kind, market_value, as_of, sales_count, confidence, citation,"
            " fetched_at FROM lookups WHERE card_id = ? AND source = ?",
            (card_id, source),
        ).fetchone()
        if row is None:
            return None
        kind, value, as_of, sales_count, confidence, citation, fetched = row
        try:
            fetched_at = datetime.fromisoformat(fetched)
            result: QuoteResult
            if kind == _QUOTE:
                result = ValueQuote(
                    market_value=value,
                    source=source,
                    as_of=date.fromisoformat(as_of),
                    sales_count=sales_count,
                    confidence=ValueConfidence(confidence),
                    citation=citation,
                )
            else:
                result = _NEGATIVE_KINDS[kind]
        except KeyError, TypeError, ValueError:
            return None
        max_age = self.quote_max_age if kind == _QUOTE else self.negative_max_age
        return CacheEntry(result, fetched_at, now - fetched_at <= max_age)

    def put(
        self, card_id: str, source: str, result: QuoteResult, *, fetched_at: datetime
    ) -> None:
        """Store `result` for `card_id` from `source`, replacing what was there.

        Raises ValueError if `result` is a quote whose own source differs.
        """
        fetched = require_aware(fetched_at, "fetched_at").astimezone(UTC).isoformat()
        if isinstance(result, ValueQuote):
            if result.source != source:
                raise ValueError(
                    f"a quote from source {result.source!r} can't be cached "
                    f"under source {source!r}"
                )
            row = (
                _QUOTE,
                result.market_value,
                result.as_of.isoformat(),
                result.sales_count,
                result.confidence.value,
                result.citation,
            )
        else:
            kind = _NOT_FOUND if isinstance(result, NotFound) else _UNMATCHED
            row = (kind, None, None, None, None, None)
        self._db.execute(
            "INSERT OR REPLACE INTO lookups (card_id, source, kind, market_value,"
            " as_of, sales_count, confidence, citation, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (card_id, source, *row, fetched),
        )

    def get_provider_id(self, provider: str, key: str) -> str | None:
        """A provider's own id stored under `key` (e.g. a resolved tcgPlayerId),
        or None. Provider ids never go stale."""
        row = self._db.execute(
            "SELECT value FROM provider_ids WHERE provider = ? AND key = ?",
            (provider, key),
        ).fetchone()
        return None if row is None else row[0]

    def put_provider_id(self, provider: str, key: str, value: str) -> None:
        """Store a provider id under `key`, replacing any earlier one."""
        self._db.execute(
            "INSERT OR REPLACE INTO provider_ids (provider, key, value)"
            " VALUES (?, ?, ?)",
            (provider, key, value),
        )

    # Credit bookkeeping for CreditBudget, which owns the rules.

    def _credits_spent(self, provider: str, day: str) -> int:
        row = self._db.execute(
            "SELECT spent FROM credits WHERE provider = ? AND day = ?",
            (provider, day),
        ).fetchone()
        return 0 if row is None else row[0]

    def _spend_credits(
        self, provider: str, day: str, n: int, limit: int | None
    ) -> bool:
        """Add `n` to `provider`'s credits spent on `day`, unless that would
        pass `limit` (None means no limit). Atomic across processes."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            spent = self._credits_spent(provider, day)
            if limit is not None and spent + n > limit:
                self._db.execute("ROLLBACK")
                return False
            self._db.execute(
                "INSERT OR REPLACE INTO credits (provider, day, spent)"
                " VALUES (?, ?, ?)",
                (provider, day, spent + n),
            )
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return True
