"""
Scheduler for time-based actions (speed limit, pause, resume).

Each schedule fires at a time of day on selected weekdays (local time of the
container, see TZ). Like SABnzbd, an action is applied when its time comes;
a manual change made later stays in effect until the next scheduled action.
At startup the most recent past action of each kind is applied so the
service is in the state the schedule expects.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from app.database import db
from app.downloader import pause_all, resume_all
from app.runtime import save_settings

logger = logging.getLogger("file-downloader")

ACTIONS = ("speed_limit", "pause", "resume")

# Actions of the same kind override each other (the most recent one wins)
ACTION_KINDS = {"speed_limit": "speed", "pause": "pause", "resume": "pause"}

CHECK_INTERVAL_SECONDS = 20

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_time(value: str) -> tuple[int, int]:
    hours, minutes = value.split(":")
    return int(hours), int(minutes)


def latest_occurrence(schedule: dict, now: datetime) -> Optional[datetime]:
    """Most recent time at or before now when the schedule fired (within a week)."""
    hours, minutes = _parse_time(schedule["time"])
    for days_back in range(8):
        day = (now - timedelta(days=days_back)).date()
        if day.weekday() not in schedule["days"]:
            continue
        occurrence = datetime(day.year, day.month, day.day, hours, minutes)
        if occurrence <= now:
            return occurrence
    return None


def next_occurrence(schedule: dict, now: datetime) -> Optional[datetime]:
    """Next time after now when the schedule fires."""
    hours, minutes = _parse_time(schedule["time"])
    for days_ahead in range(8):
        day = (now + timedelta(days=days_ahead)).date()
        if day.weekday() not in schedule["days"]:
            continue
        occurrence = datetime(day.year, day.month, day.day, hours, minutes)
        if occurrence > now:
            return occurrence
    return None


def latest_actions(schedules: list[dict], now: datetime) -> dict[str, tuple[datetime, dict]]:
    """For each action kind, the most recent past occurrence and its schedule."""
    latest: dict[str, tuple[datetime, dict]] = {}
    for schedule in schedules:
        if not schedule["enabled"]:
            continue
        occurrence = latest_occurrence(schedule, now)
        if occurrence is None:
            continue
        kind = ACTION_KINDS[schedule["action"]]
        if kind not in latest or occurrence >= latest[kind][0]:
            latest[kind] = (occurrence, schedule)
    return latest


def describe(schedule: dict) -> str:
    """Short human-readable description for the log."""
    days = ",".join(DAY_NAMES[d] for d in sorted(schedule["days"]))
    action = schedule["action"]
    if action == "speed_limit":
        value = schedule.get("value") or 0
        action = f"speed limit {value:g} Mbps" if value else "speed limit off"
    return f"#{schedule['id']} {days} {schedule['time']} {action}"


class Scheduler:
    """Fires scheduled actions."""

    def __init__(self) -> None:
        self._schedules: list[dict] = []
        # Occurrence last handled for each action kind
        self._handled: dict[str, datetime] = {}

    @property
    def schedules(self) -> list[dict]:
        return self._schedules

    async def reload(self, apply_latest: bool = False, now: Optional[datetime] = None) -> None:
        """
        Reload schedules from the database.

        Args:
            apply_latest: Apply the most recent past action of each kind
                (used at startup). Otherwise past occurrences are only marked
                as handled, so editing schedules never fires them immediately.
            now: Current local time (for tests)
        """
        now = now or datetime.now()
        self._schedules = await db.get_schedules()
        latest = latest_actions(self._schedules, now)
        for kind, (occurrence, schedule) in latest.items():
            if apply_latest:
                await self._apply(schedule, "startup")
            self._handled[kind] = max(occurrence, self._handled.get(kind, occurrence))

    async def check(self, now: Optional[datetime] = None) -> None:
        """Apply actions whose time has come since the last check."""
        now = now or datetime.now()
        for kind, (occurrence, schedule) in latest_actions(self._schedules, now).items():
            handled = self._handled.get(kind)
            if handled is not None and occurrence <= handled:
                continue
            self._handled[kind] = occurrence
            await self._apply(schedule, "scheduled")

    async def _apply(self, schedule: dict, trigger: str) -> None:
        reason = f"{trigger} {describe(schedule)}"
        try:
            if schedule["action"] == "speed_limit":
                await save_settings({"total_bandwidth_mbps": float(schedule.get("value") or 0)})
                logger.info(f"Schedule applied: {reason}")
            elif schedule["action"] == "pause":
                pause_all(reason=reason)
            elif schedule["action"] == "resume":
                resume_all(reason=reason)
        except Exception as e:
            logger.error(f"Schedule {reason} failed: {e}")

    async def run(self) -> None:
        """Background loop."""
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                await self.check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")


# Global scheduler instance
scheduler = Scheduler()
