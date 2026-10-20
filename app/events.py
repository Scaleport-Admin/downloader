"""
Change notifications for live updates of the web UI (Server-Sent Events).
"""

import asyncio
from typing import Optional


class ChangeNotifier:
    """
    Wakes up waiting event streams when the download state changes.

    A version counter makes sure a change that happens while a stream is
    busy (not waiting) is not missed. Must only be used from the event loop
    thread.
    """

    def __init__(self) -> None:
        self.version = 0
        self._event: Optional[asyncio.Event] = None

    def notify(self) -> None:
        """Signal that something changed."""
        self.version += 1
        if self._event is not None:
            self._event.set()
            self._event = None

    async def wait(self, since: int, timeout: float) -> bool:
        """
        Wait until the version differs from `since`, or until timeout.

        Returns:
            True if something changed
        """
        if self.version != since:
            return True
        if self._event is None:
            self._event = asyncio.Event()
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


# Global change notifier
notifier = ChangeNotifier()
