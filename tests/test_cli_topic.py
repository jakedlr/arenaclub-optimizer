"""`arena topic`: print the Topic and its subscribe URL, and send a test
notification with `--test`."""

import os
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from arenaclub_optimizer import cli
from arenaclub_optimizer.cli import app
from arenaclub_optimizer.notify import TOPIC_FILENAME, NtfyNotifier

runner = CliRunner()

TOPIC = "AbCdEfGhIjKlMnOpQrStUv_-"


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    directory = tmp_path / "xdg-config" / "arenaclub-optimizer"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def state_dir(config_dir) -> Path:
    # conftest points XDG_STATE_HOME at a temp dir for every test.
    return Path(os.environ["XDG_STATE_HOME"]) / "arenaclub-optimizer"


class Ntfy:
    """Replaces `cli.make_notifier` with an NtfyNotifier on a MockTransport
    that answers `status`, and records what it was built with and sent."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.built: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={})

    def __call__(self, server: str, topic: str) -> NtfyNotifier:
        self.built.append((server, topic))
        return NtfyNotifier(server, topic, transport=httpx.MockTransport(self.handle))


@pytest.fixture
def ntfy(monkeypatch) -> Ntfy:
    fake = Ntfy()
    monkeypatch.setattr(cli, "make_notifier", fake)
    return fake


def test_topic_prints_the_topic_url_and_how_to_subscribe(config_dir, state_dir, ntfy):
    result = runner.invoke(app, ["topic"])

    assert result.exit_code == 0, result.output
    topic = (state_dir / TOPIC_FILENAME).read_text(encoding="utf-8").strip()
    lines = result.stdout.splitlines()
    assert f"Topic: {topic}" in lines
    assert f"Subscribe URL: https://ntfy.sh/{topic}" in lines
    assert any("ntfy app" in line and "browser" in line for line in lines)
    assert ntfy.requests == []  # no notification without --test


def test_topic_is_stable_across_runs(config_dir, state_dir, ntfy):
    first = runner.invoke(app, ["topic"]).stdout
    second = runner.invoke(app, ["topic"]).stdout

    assert first == second


def test_topic_uses_the_configured_topic_and_server(config_dir, state_dir, ntfy):
    (config_dir / "config.toml").write_text(
        '[watch]\ntopic = "my_topic"\nntfy_server = "https://ntfy.example/"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["topic"])

    assert result.exit_code == 0, result.output
    assert "Topic: my_topic" in result.stdout.splitlines()
    assert "Subscribe URL: https://ntfy.example/my_topic" in result.stdout.splitlines()
    assert not (state_dir / TOPIC_FILENAME).exists()


def test_topic_rejects_an_invalid_config(config_dir, ntfy):
    (config_dir / "config.toml").write_text(
        '[watch]\ntopic = "not valid"\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["topic"])

    assert result.exit_code == 2
    assert "topic" in result.stderr


def test_topic_test_sends_one_notification(config_dir, state_dir, ntfy):
    result = runner.invoke(app, ["topic", "--test"])

    assert result.exit_code == 0, result.output
    topic = (state_dir / TOPIC_FILENAME).read_text(encoding="utf-8").strip()
    assert ntfy.built == [("https://ntfy.sh", topic)]
    [request] = ntfy.requests
    assert request.method == "POST"
    assert str(request.url) == f"https://ntfy.sh/{topic}"
    assert request.headers["Title"]
    assert request.content
    assert "sent" in result.stdout.lower()


@pytest.mark.parametrize("status", [429, 503])
def test_topic_test_reports_a_failed_send(config_dir, state_dir, monkeypatch, status):
    fake = Ntfy(status)
    monkeypatch.setattr(cli, "make_notifier", fake)

    result = runner.invoke(app, ["topic", "--test"])

    assert result.exit_code == 1
    assert str(status) in result.stderr
    # The Topic is still printed, so the user can subscribe and retry.
    assert "Topic: " in result.stdout


def test_topic_reports_an_unwritable_state_dir(config_dir, tmp_path, monkeypatch, ntfy):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))

    result = runner.invoke(app, ["topic"])

    assert result.exit_code == 1
    assert "Error" in result.stderr


def test_the_default_notifier_is_ntfy():
    assert cli.make_notifier is NtfyNotifier
