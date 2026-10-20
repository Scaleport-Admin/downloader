"""
Bandwidth throttling and concurrency control for downloads.
"""

import asyncio
import time
from collections import deque
from typing import Callable


def mbps_to_bytes_per_second(mbps: float) -> float:
    """Convert megabits per second to bytes per second (0 = unlimited)."""
    return mbps * 1024 * 1024 / 8 if mbps and mbps > 0 else 0.0


class BandwidthThrottle:
    """
    Virtual-clock bandwidth limiter.

    Each consumed chunk advances a virtual "transfer finished" clock by
    len(chunk) / rate. If the virtual clock runs ahead of real time, the
    caller sleeps for the difference. A small burst allowance lets short
    stalls (slow server, disk) be partially made up without overshooting the
    long-term average. The rate is passed on every call, so limit changes
    take effect immediately. One instance may be shared by several
    coroutines to enforce a combined limit.
    """

    BURST_SECONDS = 0.25

    def __init__(self) -> None:
        self._virtual_time = 0.0
        self._rate = 0.0

    async def consume(self, nbytes: int, rate_bytes_per_second: float) -> None:
        """Account for nbytes and sleep if the transfer runs faster than the rate."""
        if rate_bytes_per_second <= 0:
            self._rate = 0.0
            return

        now = time.monotonic()
        if rate_bytes_per_second != self._rate:
            # Start fresh when the limit changes or was just enabled
            self._rate = rate_bytes_per_second
            self._virtual_time = now
        self._virtual_time = max(self._virtual_time, now - self.BURST_SECONDS)
        self._virtual_time += nbytes / rate_bytes_per_second

        delay = self._virtual_time - now
        if delay > 0:
            await asyncio.sleep(delay)


class DownloadSlots:
    """
    FIFO concurrency limiter whose limit can be changed at runtime.

    Behaves like an asyncio.Semaphore with a fixed limit, but waiters can
    abort (paused or cancelled downloads leave the queue) and raising the
    limit lets queued downloads start immediately.
    """

    POLL_INTERVAL = 0.2

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self.active = 0
        self._queue: deque = deque()

    @property
    def waiting(self) -> int:
        return len(self._queue)

    def queue_position(self, ticket: object) -> int:
        """1-based position in the queue, or 0 if not queued."""
        try:
            return self._queue.index(ticket) + 1
        except ValueError:
            return 0

    def _can_start(self, ticket: object) -> bool:
        free = self.limit - self.active
        return free > 0 and self.queue_position(ticket) <= free

    async def acquire(self, ticket: object, should_abort: Callable[[], bool]) -> bool:
        """
        Wait for a free slot.

        Returns:
            True if a slot was acquired, False if should_abort() became true
        """
        self._queue.append(ticket)
        try:
            while not self._can_start(ticket):
                if should_abort():
                    return False
                await asyncio.sleep(self.POLL_INTERVAL)
            if should_abort():
                return False
            self.active += 1
            return True
        finally:
            self._queue.remove(ticket)

    def release(self) -> None:
        """Release a slot acquired by acquire()."""
        self.active = max(0, self.active - 1)
