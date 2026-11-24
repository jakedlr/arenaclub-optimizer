"""HTTP client for Arena Club's public API, returning raw JSON.

See ADR 0001 and docs/research/arenaclub-api.md. The client sends an honest
user-agent, spaces its requests, caches card details on disk, and never retries
or changes headers to get around a block.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

BASE_URL = "https://api.arenaclub.com"
REPO_URL = "https://github.com/jakedlr/arenaclub-optimizer"
USER_AGENT = f"arenaclub-optimizer/{version('arenaclub-optimizer')} (+{REPO_URL})"
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_TIMEOUT = 15.0

# Ids are interpolated into URL paths and cache file names, so only allow the
# characters Arena Club's ids actually use.
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class ArenaApiError(Exception):
    """A request failed: a non-2xx status, a timeout, or an unusable body."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ArenaBlockedError(ArenaApiError):
    """Arena Club answered 403 or 429. Back off and report; never work around it."""

    def __init__(self, status: int, retry_after: float | None) -> None:
        message = f"Arena Club blocked the request (HTTP {status})"
        if retry_after is not None:
            message += f"; retry after {retry_after:g}s"
        super().__init__(message, status)
        self.retry_after = retry_after


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/arenaclub-optimizer/cards``, else ``~/.cache/...``."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "arenaclub-optimizer" / "cards"


class ArenaClient:
    """Reads Price Levels, Slab Packs and cards from ``api.arenaclub.com``."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
        )
        self._min_interval = min_interval
        self._cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None

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

    def list_price_levels(self) -> list[dict[str, Any]]:
        """All Price Levels, from ``GET /v4/repacks``. Never cached."""
        body = self._get_json("/v4/repacks")
        items = body.get("items")
        if not isinstance(items, list):
            raise ArenaApiError("GET /v4/repacks: response has no 'items' list")
        return items

    def get_slab_pack(self, pack_id: str) -> dict[str, Any]:
        """One Slab Pack with its Checklist, from ``GET /v4/repacks/{id}``. Never
        cached, because the Checklist changes as cards are pulled."""
        return self._get_json(f"/v4/repacks/{_checked_id(pack_id)}")

    def get_card(self, card_id: str) -> dict[str, Any]:
        """One card, from ``GET /v4/items/{card_id}``, cached on disk by id.

        The id keeps its ``card_`` prefix. A cache hit makes no request.
        """
        path = self._cache_dir / f"{_checked_id(card_id)}.json"
        cached = _read_cache(path)
        if cached is not None:
            return cached
        card = self._get_json(f"/v4/items/{card_id}")
        _write_cache(path, card)
        return card

    def _get_json(self, path: str) -> dict[str, Any]:
        self._wait_for_slot()
        try:
            response = self._http.get(path)
        except httpx.TimeoutException as exc:
            raise ArenaApiError(f"GET {path}: timed out") from exc
        except httpx.TransportError as exc:
            raise ArenaApiError(f"GET {path}: {exc}") from exc

        status = response.status_code
        if status in (403, 429):
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise ArenaBlockedError(status, retry_after)
        if not response.is_success:
            raise ArenaApiError(f"GET {path}: HTTP {status}", status)

        try:
            body = response.json()
        except ValueError as exc:
            raise ArenaApiError(f"GET {path}: response is not JSON", status) from exc
        if not isinstance(body, dict):
            raise ArenaApiError(f"GET {path}: response is not a JSON object", status)
        return body

    def _wait_for_slot(self) -> None:
        """Sleep until ``min_interval`` has passed since the previous request."""
        if self._last_request_at is not None:
            remaining = self._min_interval - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()


def _checked_id(value: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid Arena Club id: {value!r}")
    return value


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date)."""
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return body if isinstance(body, dict) else None


def _write_cache(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    tmp.replace(path)
