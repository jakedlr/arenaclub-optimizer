"""ntfy notifications for Watch Mode: the Topic and the notifier.

The Topic is the ntfy channel Alerts go to. On ntfy anyone who knows a Topic
can read it, so it's generated at random and kept private: a Topic set in the
config file wins, else the one saved in the state directory, else a new one is
generated and saved there. The user's config file is never written.

`NtfyNotifier` publishes with one plain HTTP POST per notification, as ntfy
documents (https://docs.ntfy.sh/publish/): the message is the body, and the
title, priority and tags go in headers. It never retries; a failed send raises
`NotifyError` for the caller to log, so a lost notification never stops Watch
Mode.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

import httpx

from arenaclub_optimizer.arena_client import USER_AGENT
from arenaclub_optimizer.config import Config, ConfigError, is_valid_topic

logger = logging.getLogger(__name__)

TOPIC_FILENAME = "topic"
TOPIC_LENGTH = 24
# 18 random bytes are exactly 24 URL-safe base64 characters: [A-Za-z0-9_-].
_TOPIC_BYTES = TOPIC_LENGTH * 3 // 4
DEFAULT_TIMEOUT = 10.0

# Control characters (CR and LF above all) would split or corrupt a header.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class NotifyError(RuntimeError):
    """A notification wasn't delivered: ntfy refused it or couldn't be reached."""


class Notification(Protocol):
    """What `NtfyNotifier.send` needs from an Alert."""

    @property
    def title(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def priority(self) -> str: ...

    @property
    def tags(self) -> Sequence[str]: ...


class Notifier(Protocol):
    """Sends Notifications somewhere; `NtfyNotifier` is the real one."""

    def send(self, notification: Notification) -> None: ...


@dataclass(frozen=True)
class PlainNotification:
    """A Notification that isn't an Alert, such as `arena topic --test`."""

    title: str
    message: str
    priority: str = "default"
    tags: tuple[str, ...] = ()


def generate_topic() -> str:
    """A new random Topic: 24 URL-safe characters from `secrets`."""
    return secrets.token_urlsafe(_TOPIC_BYTES)


def get_or_create_topic(config: Config, state_dir: Path) -> str:
    """The Topic: `config.watch.topic` if set, else the one saved in
    `state_dir`, else a new one, saved there for next time.

    A saved Topic that isn't valid is replaced, with a warning. Raises
    ConfigError for an invalid configured Topic, and OSError if the state
    directory can't be read or written.
    """
    configured = config.watch.topic
    if configured is not None:
        if not is_valid_topic(configured):
            raise ConfigError(
                "watch.topic must be 1-64 letters, digits, '_' or '-' "
                f"characters, got {configured!r}"
            )
        return configured

    path = state_dir / TOPIC_FILENAME
    stored = _read_topic(path)
    if stored is not None:
        if is_valid_topic(stored):
            return stored
        logger.warning(
            "The saved Topic in %s isn't a valid ntfy topic; generating a new "
            "one. Subscribe to the new Topic to keep receiving Alerts.",
            path,
        )
    topic = generate_topic()
    _write_private(path, topic + "\n")
    return topic


def subscribe_url(server: str, topic: str) -> str:
    """The URL to subscribe to `topic` on `server`, e.g. https://ntfy.sh/<topic>."""
    return f"{server.rstrip('/')}/{topic}"


class NtfyNotifier:
    """Sends notifications to one Topic on one ntfy server."""

    def __init__(
        self,
        server: str,
        topic: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = subscribe_url(server, topic)
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )

    def send(self, notification: Notification) -> None:
        """Publish `notification` once. Raises NotifyError if ntfy answers
        with anything but success (429 and 5xx included) or can't be reached.
        """
        headers = {
            "Title": _header_value(notification.title),
            "Priority": _header_value(notification.priority),
        }
        if notification.tags:
            headers["Tags"] = _header_value(",".join(notification.tags))
        try:
            response = self._client.post(
                self.url,
                content=notification.message.encode("utf-8"),
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise NotifyError(f"could not reach ntfy: {error}") from error
        if not response.is_success:
            raise NotifyError(
                f"ntfy answered HTTP {response.status_code} "
                f"{response.reason_phrase}".rstrip()
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _header_value(value: str) -> str:
    """`value` made safe for an HTTP header. Control characters become spaces;
    non-ASCII text is RFC 2047 base64-encoded (`=?UTF-8?B?...?=`), which ntfy
    decodes for any header. So is ASCII that looks encoded already."""
    value = _CONTROL.sub(" ", value).strip()
    if value.isascii() and not value.startswith("=?"):
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{encoded}?="


def _read_topic(path: Path) -> str | None:
    """The Topic saved at `path`, or None if there's none. An unreadable
    encoding comes back as "" so it's treated as invalid and replaced."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return ""


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, readable by the user only: on ntfy a
    Topic works like a password."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
