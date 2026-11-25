"""RequestSpacer: at least `min_interval` seconds between requests."""

import pytest

from arenaclub_optimizer.spacing import RequestSpacer


class FakeClock:
    def __init__(self) -> None:
        self.now = 50.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_first_wait_does_not_sleep():
    clock = FakeClock()
    RequestSpacer(1.0, clock=clock, sleep=clock.sleep).wait()
    assert clock.sleeps == []


def test_back_to_back_waits_sleep_the_full_interval():
    clock = FakeClock()
    spacer = RequestSpacer(1.0, clock=clock, sleep=clock.sleep)
    spacer.wait()
    spacer.wait()
    spacer.wait()
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_wait_sleeps_only_the_remainder():
    clock = FakeClock()
    spacer = RequestSpacer(2.5, clock=clock, sleep=clock.sleep)
    spacer.wait()
    clock.now += 1.0
    spacer.wait()
    assert clock.sleeps == [pytest.approx(1.5)]


def test_no_sleep_once_the_interval_has_passed():
    clock = FakeClock()
    spacer = RequestSpacer(1.0, clock=clock, sleep=clock.sleep)
    spacer.wait()
    clock.now += 5.0
    spacer.wait()
    assert clock.sleeps == []


def test_negative_interval_is_rejected():
    with pytest.raises(ValueError):
        RequestSpacer(-1.0)
