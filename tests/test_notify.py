import base64
import logging
import stat
from dataclasses import dataclass

import httpx
import pytest

from arenaclub_optimizer.arena_client import USER_AGENT
from arenaclub_optimizer.config import Config, ConfigError, Watch, is_valid_topic
from arenaclub_optimizer.notify import (
    TOPIC_FILENAME,
    TOPIC_LENGTH,
    NotifyError,
    NtfyNotifier,
    PlainNotification,
    get_or_create_topic,
    subscribe_url,
)

SERVER = "https://ntfy.example"
TOPIC = "AbCdEfGhIjKlMnOpQrStUv_-"


@dataclass(frozen=True)
class FakeAlert:
    """Has the fields #36's Alert will have; `send` needs nothing more."""

    title: str
    message: str
    priority: str
    tags: tuple[str, ...]


ALERT = FakeAlert(
    title="Gold Pokemon",
    message="Buyback EV +4.2% ($4.20), P(profit) 31%",
    priority="default",
    tags=("chart_with_upwards_trend", "pokemon"),
)


def config_with_topic(topic: str | None) -> Config:
    return Config(watch=Watch(topic=topic))


class Recorder:
    """An httpx.MockTransport handler that records requests and answers with
    `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"id": "abc"})


def notifier(handler, server: str = SERVER, topic: str = TOPIC) -> NtfyNotifier:
    return NtfyNotifier(server, topic, transport=httpx.MockTransport(handler))


def decode_header(value: str) -> str:
    """Undo RFC 2047 base64 encoding, which ntfy accepts for any header."""
    if value.startswith("=?UTF-8?B?") and value.endswith("?="):
        return base64.b64decode(value[len("=?UTF-8?B?") : -2]).decode("utf-8")
    return value


# --- Topic ----------------------------------------------------------------------------


def test_topic_is_generated_once_and_persisted(tmp_path):
    state = tmp_path / "state"

    first = get_or_create_topic(Config(), state)
    second = get_or_create_topic(Config(), state)

    assert first == second
    assert (state / TOPIC_FILENAME).read_text(encoding="utf-8").strip() == first


def test_generated_topic_is_24_url_safe_characters(tmp_path):
    topic = get_or_create_topic(Config(), tmp_path)

    assert TOPIC_LENGTH == 24
    assert len(topic) == 24
    assert is_valid_topic(topic)


def test_generated_topics_differ(tmp_path):
    topics = {get_or_create_topic(Config(), tmp_path / str(i)) for i in range(20)}

    assert len(topics) == 20


def test_topic_file_is_private(tmp_path):
    get_or_create_topic(Config(), tmp_path)

    mode = stat.S_IMODE((tmp_path / TOPIC_FILENAME).stat().st_mode)
    assert mode & 0o077 == 0


def test_stored_topic_is_reused(tmp_path):
    (tmp_path / TOPIC_FILENAME).write_text(TOPIC + "\n", encoding="utf-8")

    assert get_or_create_topic(Config(), tmp_path) == TOPIC


def test_configured_topic_takes_precedence(tmp_path):
    (tmp_path / TOPIC_FILENAME).write_text(TOPIC, encoding="utf-8")

    assert get_or_create_topic(config_with_topic("mine_from_config"), tmp_path) == (
        "mine_from_config"
    )


def test_configured_topic_writes_no_state(tmp_path):
    state = tmp_path / "state"

    get_or_create_topic(config_with_topic("mine_from_config"), state)

    assert not state.exists()


def test_invalid_configured_topic_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="topic"):
        get_or_create_topic(config_with_topic("not a topic"), tmp_path)


@pytest.mark.parametrize("stored", ["", "has space", "x" * 65, "slash/topic"])
def test_invalid_stored_topic_is_regenerated_with_a_warning(tmp_path, caplog, stored):
    path = tmp_path / TOPIC_FILENAME
    path.write_text(stored, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="arenaclub_optimizer.notify"):
        topic = get_or_create_topic(Config(), tmp_path)

    assert is_valid_topic(topic)
    assert topic != stored
    assert path.read_text(encoding="utf-8").strip() == topic
    assert any(
        r.levelno == logging.WARNING and "topic" in r.getMessage().lower()
        for r in caplog.records
    )


def test_never_writes_the_config_dir(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    get_or_create_topic(Config(), tmp_path / "state")

    assert not config_home.exists()


# --- subscribe URL --------------------------------------------------------------------


def test_subscribe_url_on_ntfy_sh():
    assert subscribe_url("https://ntfy.sh", TOPIC) == f"https://ntfy.sh/{TOPIC}"


@pytest.mark.parametrize(
    "server, expected",
    [
        ("https://ntfy.example", f"https://ntfy.example/{TOPIC}"),
        ("https://ntfy.example/", f"https://ntfy.example/{TOPIC}"),
        ("http://host:8080/ntfy", f"http://host:8080/ntfy/{TOPIC}"),
    ],
)
def test_subscribe_url_on_a_custom_server(server, expected):
    assert subscribe_url(server, TOPIC) == expected


# --- NtfyNotifier ---------------------------------------------------------------------


def test_send_posts_the_message_to_the_topic():
    recorder = Recorder()

    with notifier(recorder) as n:
        n.send(ALERT)

    [request] = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == f"{SERVER}/{TOPIC}"
    assert request.content.decode("utf-8") == ALERT.message
    assert request.headers["Title"] == "Gold Pokemon"
    assert request.headers["Priority"] == "default"
    assert request.headers["Tags"] == "chart_with_upwards_trend,pokemon"
    assert request.headers["User-Agent"] == USER_AGENT


def test_send_to_a_server_with_a_trailing_slash():
    recorder = Recorder()

    with notifier(recorder, server=SERVER + "/") as n:
        n.send(ALERT)

    assert str(recorder.requests[0].url) == f"{SERVER}/{TOPIC}"


def test_send_omits_tags_when_there_are_none():
    recorder = Recorder()

    with notifier(recorder) as n:
        n.send(FakeAlert("t", "m", "high", ()))

    assert "Tags" not in recorder.requests[0].headers
    assert recorder.requests[0].headers["Priority"] == "high"


def test_utf8_title_is_rfc2047_encoded():
    recorder = Recorder()
    title = "Gold Pokémon 🔥"

    with notifier(recorder) as n:
        n.send(FakeAlert(title, "Pokémon body ✓", "default", ("pokémon",)))

    request = recorder.requests[0]
    raw = request.headers.raw
    assert all(value.isascii() for _, value in raw)
    assert decode_header(request.headers["Title"]) == title
    assert decode_header(request.headers["Tags"]) == "pokémon"
    assert request.content.decode("utf-8") == "Pokémon body ✓"


def test_header_values_cannot_inject_headers():
    recorder = Recorder()

    with notifier(recorder) as n:
        n.send(FakeAlert("line one\r\nX-Evil: 1", "m", "default", ()))

    headers = recorder.requests[0].headers
    assert "X-Evil" not in headers
    assert "\n" not in decode_header(headers["Title"])
    assert "\r" not in decode_header(headers["Title"])


def test_send_uses_a_10_second_timeout():
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return httpx.Response(200)

    with notifier(handler) as n:
        n.send(ALERT)

    assert seen == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limit_and_server_errors_raise_notify_error(status):
    recorder = Recorder(status)

    with notifier(recorder) as n, pytest.raises(NotifyError, match=str(status)):
        n.send(ALERT)

    assert len(recorder.requests) == 1  # no retries


@pytest.mark.parametrize("status", [400, 403, 413])
def test_other_rejections_raise_notify_error(status):
    with notifier(Recorder(status)) as n, pytest.raises(NotifyError):
        n.send(ALERT)


def test_a_network_failure_raises_notify_error():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    with notifier(handler) as n, pytest.raises(NotifyError):
        n.send(ALERT)


def test_plain_notification_can_be_sent():
    recorder = Recorder()

    with notifier(recorder) as n:
        n.send(PlainNotification(title="Hi", message="there"))

    request = recorder.requests[0]
    assert request.headers["Title"] == "Hi"
    assert request.headers["Priority"] == "default"
    assert request.content == b"there"
