import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest

from arenaclub_optimizer.arena_client import (
    ArenaApiError,
    ArenaBlockedError,
    ArenaClient,
    default_cache_dir,
)

FIXTURES = Path(__file__).parent / "fixtures" / "arena"
PACK_ID = "c43ab013-f7d9-4930-b2fd-f06eb8191de3"
CARD_A = "card_80747173-c08f-4080-bdf6-623fedc5cef6"
CARD_B = "card_04829537-8127-4845-be06-c8130c741ed2"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


ROUTES = {
    "/v4/repacks": fixture("v4_repacks.json"),
    f"/v4/repacks/{PACK_ID}": fixture("v4_repack_gold_pokemon.json"),
    f"/v4/items/{CARD_A}": fixture("v4_item_a.json"),
    f"/v4/items/{CARD_B}": fixture("v4_item_b.json"),
}


class FakeClock:
    """A monotonic clock that only moves when the client sleeps or a test ticks."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Recorder:
    """A MockTransport handler that serves the fixtures and records each request."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.sent_at: list[float] = []
        self.clock = clock

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.clock is not None:
            self.sent_at.append(self.clock())
        body = ROUTES.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)


def make_client(
    tmp_path: Path,
    handler,
    clock: FakeClock | None = None,
    **kwargs,
) -> ArenaClient:
    clock = clock or FakeClock()
    return ArenaClient(
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path / "cards",
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )


# --- endpoints ---------------------------------------------------------------


def test_list_price_levels_returns_items_from_v4_repacks(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        levels = client.list_price_levels()

    assert levels == fixture("v4_repacks.json")["items"]
    assert len(levels) == 44
    assert recorder.requests[0].method == "GET"
    assert str(recorder.requests[0].url) == "https://api.arenaclub.com/v4/repacks"


def test_get_slab_pack_returns_the_pack_body(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        pack = client.get_slab_pack(PACK_ID)

    assert pack == fixture("v4_repack_gold_pokemon.json")
    assert recorder.requests[0].url.path == f"/v4/repacks/{PACK_ID}"


def test_get_card_keeps_the_card_prefix_in_the_url(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        card = client.get_card(CARD_A)

    assert card == fixture("v4_item_a.json")
    assert (
        str(recorder.requests[0].url) == f"https://api.arenaclub.com/v4/items/{CARD_A}"
    )


def test_list_price_levels_without_items_raises_api_error(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"unexpected": []})

    with make_client(tmp_path, handler) as client, pytest.raises(ArenaApiError):
        client.list_price_levels()


def test_non_json_body_raises_api_error(tmp_path):
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    with make_client(tmp_path, handler) as client, pytest.raises(ArenaApiError):
        client.get_slab_pack(PACK_ID)


@pytest.mark.parametrize("bad_id", ["", "../etc/passwd", "card_x/../../y", "a b"])
def test_ids_with_path_characters_are_rejected_without_a_request(tmp_path, bad_id):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        with pytest.raises(ValueError):
            client.get_card(bad_id)
        with pytest.raises(ValueError):
            client.get_slab_pack(bad_id)

    assert recorder.requests == []


# --- user-agent --------------------------------------------------------------


def test_every_request_sends_the_honest_user_agent(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        client.list_price_levels()
        client.get_slab_pack(PACK_ID)
        client.get_card(CARD_A)

    expected = (
        f"arenaclub-optimizer/{version('arenaclub-optimizer')} "
        "(+https://github.com/jakedlr/arenaclub-optimizer)"
    )
    assert len(recorder.requests) == 3
    assert all(r.headers["User-Agent"] == expected for r in recorder.requests)


# --- spacing -----------------------------------------------------------------


def test_first_request_does_not_wait(tmp_path):
    clock = FakeClock()
    with make_client(tmp_path, Recorder(clock), clock) as client:
        client.list_price_levels()

    assert clock.sleeps == []


def test_successive_requests_are_spaced_by_the_default_one_second(tmp_path):
    clock = FakeClock()
    recorder = Recorder(clock)
    with make_client(tmp_path, recorder, clock) as client:
        client.list_price_levels()
        client.get_slab_pack(PACK_ID)
        client.get_card(CARD_A)

    gaps = [b - a for a, b in zip(recorder.sent_at, recorder.sent_at[1:], strict=False)]
    assert gaps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_spacing_only_waits_for_the_remainder_of_the_interval(tmp_path):
    clock = FakeClock()
    with make_client(tmp_path, Recorder(clock), clock, min_interval=2.5) as client:
        client.list_price_levels()
        clock.now += 1.0
        client.get_slab_pack(PACK_ID)

    assert clock.sleeps == [pytest.approx(1.5)]


def test_no_wait_when_the_interval_has_already_passed(tmp_path):
    clock = FakeClock()
    with make_client(tmp_path, Recorder(clock), clock) as client:
        client.list_price_levels()
        clock.now += 5.0
        client.get_slab_pack(PACK_ID)

    assert clock.sleeps == []


def test_failed_requests_still_count_towards_spacing(tmp_path):
    clock = FakeClock()

    def handler(request):
        return httpx.Response(500)

    with make_client(tmp_path, handler, clock) as client:
        with pytest.raises(ArenaApiError):
            client.list_price_levels()
        with pytest.raises(ArenaApiError):
            client.list_price_levels()

    assert clock.sleeps == [pytest.approx(1.0)]


# --- card cache --------------------------------------------------------------


def test_card_cache_miss_requests_and_writes_to_disk(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        client.get_card(CARD_A)

    assert len(recorder.requests) == 1
    cached = tmp_path / "cards" / f"{CARD_A}.json"
    assert json.loads(cached.read_text()) == fixture("v4_item_a.json")


def test_card_cache_hit_makes_no_request(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        first = client.get_card(CARD_A)
        second = client.get_card(CARD_A)

    assert first == second == fixture("v4_item_a.json")
    assert len(recorder.requests) == 1


def test_card_cache_persists_across_clients(tmp_path):
    with make_client(tmp_path, Recorder()) as client:
        client.get_card(CARD_A)

    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        assert client.get_card(CARD_A) == fixture("v4_item_a.json")

    assert recorder.requests == []


def test_card_cache_is_keyed_by_card_id(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        a = client.get_card(CARD_A)
        b = client.get_card(CARD_B)

    assert a["id"] == CARD_A
    assert b["id"] == CARD_B
    assert len(recorder.requests) == 2


def test_card_cache_hit_does_not_wait(tmp_path):
    clock = FakeClock()
    with make_client(tmp_path, Recorder(clock), clock) as client:
        client.get_card(CARD_A)
        client.get_card(CARD_A)

    assert clock.sleeps == []


def test_corrupt_cache_file_is_treated_as_a_miss(tmp_path):
    cache = tmp_path / "cards"
    cache.mkdir()
    (cache / f"{CARD_A}.json").write_text("{not json")
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        assert client.get_card(CARD_A) == fixture("v4_item_a.json")

    assert len(recorder.requests) == 1


def test_failed_card_request_is_not_cached(tmp_path):
    def handler(request):
        return httpx.Response(500)

    with make_client(tmp_path, handler) as client, pytest.raises(ArenaApiError):
        client.get_card(CARD_A)

    assert not (tmp_path / "cards" / f"{CARD_A}.json").exists()


def test_pack_and_price_level_calls_are_never_cached(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        client.list_price_levels()
        client.list_price_levels()
        client.get_slab_pack(PACK_ID)
        client.get_slab_pack(PACK_ID)

    assert len(recorder.requests) == 4
    cache = tmp_path / "cards"
    assert not cache.exists() or list(cache.iterdir()) == []


def test_default_cache_dir_uses_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    assert default_cache_dir() == tmp_path / "xdg" / "arenaclub-optimizer" / "cards"


@pytest.mark.parametrize("value", [None, "", "relative/cache", "./cache"])
def test_default_cache_dir_falls_back_to_home_cache(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", value)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_cache_dir() == tmp_path / ".cache" / "arenaclub-optimizer" / "cards"


def test_cache_write_does_not_use_a_fixed_temp_file_name(tmp_path):
    # Another writer's temp file at the old fixed name must not get in the way.
    cache = tmp_path / "cards"
    (cache / f"{CARD_A}.json.tmp").mkdir(parents=True)
    with make_client(tmp_path, Recorder()) as client:
        client.get_card(CARD_A)

    assert json.loads((cache / f"{CARD_A}.json").read_text()) == fixture(
        "v4_item_a.json"
    )
    leftovers = {p.name for p in cache.iterdir()} - {
        f"{CARD_A}.json",
        f"{CARD_A}.json.tmp",
    }
    assert leftovers == set()


def test_failed_cache_write_still_returns_the_card_and_warns(tmp_path, caplog):
    not_a_dir = tmp_path / "cards"
    not_a_dir.write_text("a file where the cache dir should be")
    recorder = Recorder()
    clock = FakeClock()
    with (
        caplog.at_level(logging.WARNING, logger="arenaclub_optimizer.arena_client"),
        ArenaClient(
            transport=httpx.MockTransport(recorder),
            cache_dir=not_a_dir,
            clock=clock,
            sleep=clock.sleep,
        ) as client,
    ):
        card = client.get_card(CARD_A)

    assert card == fixture("v4_item_a.json")
    assert len(recorder.requests) == 1
    assert any(CARD_A in r.getMessage() for r in caplog.records)


def test_client_uses_default_cache_dir_when_none_given(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    with ArenaClient(
        transport=httpx.MockTransport(Recorder()), clock=clock, sleep=clock.sleep
    ) as client:
        client.get_card(CARD_A)

    assert (
        tmp_path / "xdg" / "arenaclub-optimizer" / "cards" / f"{CARD_A}.json"
    ).exists()


# --- errors ------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429])
def test_block_with_retry_after_raises_blocked_error(tmp_path, status):
    def handler(request):
        return httpx.Response(status, headers={"Retry-After": "120"})

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaBlockedError) as excinfo:
            client.list_price_levels()

    assert excinfo.value.status == status
    assert excinfo.value.retry_after == 120.0


@pytest.mark.parametrize("status", [403, 429])
def test_block_without_retry_after_has_none(tmp_path, status):
    def handler(request):
        return httpx.Response(status)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaBlockedError) as excinfo:
            client.get_slab_pack(PACK_ID)

    assert excinfo.value.status == status
    assert excinfo.value.retry_after is None


@pytest.mark.parametrize(
    "value", ["soon", "nan", "inf", "-inf", "1e3", "-5", "+5", "1.5", ""]
)
def test_unparseable_retry_after_is_none(tmp_path, value):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": value})

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaBlockedError) as excinfo:
            client.list_price_levels()

    assert excinfo.value.retry_after is None


def test_block_is_never_retried_and_headers_never_change(tmp_path):
    recorder = Recorder()

    def handler(request):
        recorder(request)
        return httpx.Response(403)

    with make_client(tmp_path, handler) as client:
        for _ in range(2):
            with pytest.raises(ArenaBlockedError):
                client.list_price_levels()

    assert len(recorder.requests) == 2
    first, second = recorder.requests
    assert dict(first.headers) == dict(second.headers)


def test_server_error_raises_api_error_without_retry(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaApiError) as excinfo:
            client.get_card(CARD_A)

    assert not isinstance(excinfo.value, ArenaBlockedError)
    assert excinfo.value.status == 500
    assert len(calls) == 1


def test_not_found_raises_api_error(tmp_path):
    with make_client(tmp_path, Recorder()) as client:
        with pytest.raises(ArenaApiError) as excinfo:
            client.get_slab_pack("does-not-exist")

    assert excinfo.value.status == 404


def test_timeout_raises_api_error_without_retry(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaApiError) as excinfo:
            client.list_price_levels()

    assert not isinstance(excinfo.value, ArenaBlockedError)
    assert excinfo.value.status is None
    assert len(calls) == 1


def test_connection_error_raises_api_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with make_client(tmp_path, handler) as client, pytest.raises(ArenaApiError):
        client.list_price_levels()


def test_timeout_defaults_to_fifteen_seconds(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        client.list_price_levels()

    timeout = recorder.requests[0].extensions["timeout"]
    assert timeout == {"connect": 15.0, "read": 15.0, "write": 15.0, "pool": 15.0}


def test_timeout_is_configurable(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder, timeout=3.0) as client:
        client.list_price_levels()

    assert recorder.requests[0].extensions["timeout"]["read"] == 3.0


def test_http_date_retry_after_is_converted_to_seconds(tmp_path):
    future = datetime.now(UTC) + timedelta(seconds=300)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": format_datetime(future)})

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaBlockedError) as excinfo:
            client.list_price_levels()

    assert 290 <= excinfo.value.retry_after <= 300


def test_http_date_retry_after_in_the_past_is_zero(tmp_path):
    def handler(request):
        return httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
        )

    with make_client(tmp_path, handler) as client:
        with pytest.raises(ArenaBlockedError) as excinfo:
            client.list_price_levels()

    assert excinfo.value.retry_after == 0.0
