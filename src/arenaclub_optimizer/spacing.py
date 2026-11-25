"""Request spacing shared by the HTTP clients: at least `min_interval`
seconds between one request and the next (docs/plan.md, Data)."""

import time
from collections.abc import Callable


class RequestSpacer:
    """Call `wait()` before each request; it sleeps until `min_interval`
    seconds have passed since the previous call. The first call never sleeps.

    `clock` is a monotonic clock in seconds and `sleep` sleeps for a number of
    seconds; tests pass fakes. Raises ValueError for a negative interval.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval < 0:
            raise ValueError(f"min_interval must be >= 0, got {min_interval!r}")
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()
