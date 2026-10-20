"""
Web UI and its management API.

The management API lives under /api and is used by the single-page web UI
served at /ui. It is independent of the original public API (which keeps
its behaviour unchanged) and has its own, more generous rate limit so the
UI can poll for progress.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app import __version__
from app.auth import is_insecure_mode, validate_api_key
from app.database import db
from app.events import notifier
from app.downloader import (
    DOWNLOADS_BASE_PATH,
    cancel_download,
    discard_interrupted_download,
    get_active_downloads,
    get_interrupted_downloads,
    get_pause_remaining_seconds,
    get_queue_position,
    get_slots,
    is_globally_paused,
    pause_all,
    pause_download,
    resume_all,
    resume_download,
    resume_interrupted_download,
    set_download_limit,
    start_download,
)
from app.models import DownloadRequestBase
from app.rate_limiter import RateLimiter
from app.runtime import reset_setting as reset_runtime_setting
from app.runtime import save_settings
from app.scheduler import ACTIONS, next_occurrence, scheduler
from app.settings import SETTING_DEFINITIONS, settings
from app.utils import memory_log_handler

logger = logging.getLogger("file-downloader")

UI_RATE_LIMIT_REQUESTS = int(os.getenv("UI_RATE_LIMIT_REQUESTS", "600"))
UI_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("UI_RATE_LIMIT_WINDOW_SECONDS", "60"))

STATIC_DIR = Path(__file__).parent / "static"

ui_rate_limiter = RateLimiter(
    max_requests=UI_RATE_LIMIT_REQUESTS,
    window_seconds=UI_RATE_LIMIT_WINDOW_SECONDS
)

# Service start time, set by the application lifespan
startup_time: Optional[float] = None

api_router = APIRouter(prefix="/api", tags=["web-ui"])
ui_router = APIRouter(include_in_schema=False)

HISTORY_STATUSES = ("completed", "failed", "cancelled")


class NewDownloadRequest(DownloadRequestBase):
    """Request for starting a download from the web UI (API key in header)."""

    limit_mbps: Optional[float] = Field(None, ge=0, description="Speed limit for this download")
    paused: bool = Field(False, description="Add the download paused")


class PauseRequest(BaseModel):
    """Request for pausing all downloads, optionally for a limited time."""

    minutes: Optional[float] = Field(None, gt=0, le=60 * 24 * 365,
                                     description="Resume automatically after this many minutes")


class ScheduleRequest(BaseModel):
    """A scheduled action."""

    enabled: bool = True
    days: list[int] = Field(..., description="Weekdays, 0 = Monday ... 6 = Sunday")
    time: str = Field(..., description="Local time HH:MM")
    action: str = Field(..., description="speed_limit, pause or resume")
    value: Optional[float] = Field(None, ge=0, le=100000, description="Mbps for speed_limit (0 = unlimited)")

    @field_validator("days")
    @classmethod
    def validate_days(cls, v: list[int]) -> list[int]:
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("Select at least one day (0 = Monday ... 6 = Sunday)")
        return sorted(set(v))

    @field_validator("time")
    @classmethod
    def validate_time(cls, v: str) -> str:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", v.strip())
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("Time must be HH:MM")
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError(f"Action must be one of {', '.join(ACTIONS)}")
        return v


class DownloadLimitRequest(BaseModel):
    """Request for changing the speed limit of a single download."""

    limit_mbps: Optional[float] = Field(None, ge=0, description="Speed limit (null or 0 = global only)")


def _authorize(request: Request) -> None:
    """Rate limit and authenticate a management API request."""
    ui_rate_limiter.check_rate_limit(request)
    validate_api_key(request)


def _iso(value: Any) -> Optional[str]:
    """Format a naive UTC datetime (or ISO string) as ISO 8601 with Z suffix."""
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.isoformat()
    return value if value.endswith("Z") or "+" in value[10:] else value + "Z"


def _download_view(d: dict) -> dict:
    """Public representation of an active download (no cookies or headers)."""
    state = d.get("state", "downloading")
    return {
        "download_id": d["download_id"],
        "filename": d["filename"],
        "url": d["url"],
        "path": d["path"],
        "state": state,
        "paused": d.get("paused", False),
        "progress_percent": round(d.get("progress_percent") or 0, 2),
        "downloaded_bytes": d.get("downloaded_bytes") or 0,
        "total_bytes": d.get("total_bytes"),
        "speed_mbps": d.get("speed_mbps") if state == "downloading" else None,
        "eta_seconds": d.get("eta_seconds") if state == "downloading" else None,
        "started_at": _iso(d.get("started_at")),
        "limit_mbps": d.get("limit_mbps"),
        "attempt": d.get("attempt", 0),
        "last_error": d.get("last_error"),
        "queue_position": get_queue_position(d["download_id"]) if state == "queued" else 0,
        "method": d.get("method", "GET"),
        "has_cookie": bool(d.get("cookie")),
        "expected_sha256": d.get("expected_sha256"),
    }


def _interrupted_view(row: dict) -> dict:
    """Public representation of a download interrupted by a restart."""
    return {
        "download_id": row["download_id"],
        "filename": row["filename"],
        "url": row["url"],
        "path": row["path"],
        "downloaded_bytes": row.get("downloaded_bytes") or 0,
        "total_bytes": row.get("total_bytes"),
        "progress_percent": round(row.get("progress_percent") or 0, 2),
        "started_at": _iso(row.get("started_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _history_view(row: dict) -> dict:
    """Public representation of a history record."""
    return {
        "download_id": row["download_id"],
        "filename": row["filename"],
        "url": row["url"],
        "path": row["path"],
        "status": row["status"],
        "size_bytes": row.get("size_bytes"),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "duration_seconds": row.get("duration_seconds"),
        "error_message": row.get("error_message"),
        "decompressed": bool(row.get("decompressed")),
        "sha256": row.get("sha256"),
        "checksum_verified": bool(row.get("expected_sha256")) and row.get("expected_sha256") == row.get("sha256"),
    }


def _disk_usage(path: str) -> dict:
    """Disk usage of the downloads volume in bytes."""
    try:
        usage = shutil.disk_usage(path)
        return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError:
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None}


def _not_found(download_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Download not found or already finished: {download_id}"
    )


# ---------------------------------------------------------------------------
# Web UI pages
# ---------------------------------------------------------------------------

@ui_router.get("/")
async def root_redirect():
    """Redirect the service root to the web UI."""
    return RedirectResponse(url="/ui")


@ui_router.get("/ui")
async def web_ui():
    """Serve the single-page web UI."""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@api_router.get("/status")
async def get_status(request: Request):
    """Everything the dashboard needs in a single request."""
    _authorize(request)
    return await _status_payload()


EVENT_INTERVAL_SECONDS = 1.0
EVENT_KEEPALIVE_SECONDS = 15.0


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def event_stream(log_after: int = 0):
    """
    Server-Sent Events stream for the web UI.

    Sends the dashboard status whenever it changes (at most a few times per
    second, at least every second while something changes) and new log
    records. A comment line is sent periodically to keep proxies from
    closing an idle connection.
    """
    yield "retry: 3000\n\n"
    last_status = None
    last_sent = time.monotonic()
    while True:
        seen_version = notifier.version
        status_data = await _status_payload()
        # Uptime changes every second; it does not count as a change on its own
        comparable = json.dumps({**status_data, "uptime_seconds": None}, sort_keys=True)
        if comparable != last_status:
            last_status = comparable
            last_sent = time.monotonic()
            yield _sse("status", status_data)

        logs = memory_log_handler.get_records(after_id=log_after)
        if logs:
            log_after = logs[-1]["id"]
            last_sent = time.monotonic()
            yield _sse("logs", logs)

        if time.monotonic() - last_sent >= EVENT_KEEPALIVE_SECONDS:
            last_sent = time.monotonic()
            yield ": keepalive\n\n"

        if await notifier.wait(seen_version, timeout=EVENT_INTERVAL_SECONDS):
            # Coalesce bursts of changes into one message
            await asyncio.sleep(0.1)


@api_router.get("/events")
async def events(
    request: Request,
    log_after: int = Query(0, ge=0, description="Send log records with id greater than this")
):
    """Live updates for the web UI (text/event-stream)."""
    _authorize(request)
    return StreamingResponse(
        event_stream(log_after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _status_payload() -> dict:
    """Dashboard data."""
    active = [_download_view(d) for d in get_active_downloads().values()]
    active.sort(key=lambda d: d["started_at"] or "")
    interrupted = [_interrupted_view(row) for row in await get_interrupted_downloads()]
    db_metrics = await db.get_metrics()
    slots = get_slots()

    return {
        "version": __version__,
        "insecure_mode": is_insecure_mode(),
        "paused": is_globally_paused(),
        "pause_remaining_seconds": get_pause_remaining_seconds(),
        "uptime_seconds": int(time.time() - startup_time) if startup_time else 0,
        "total_speed_mbps": round(sum(d["speed_mbps"] or 0 for d in active), 2),
        "running": slots.active,
        "max_concurrent_downloads": slots.limit,
        "max_bandwidth_mbps": settings.get("max_bandwidth_mbps"),
        "total_bandwidth_mbps": settings.get("total_bandwidth_mbps"),
        "downloads_path": DOWNLOADS_BASE_PATH,
        "disk": _disk_usage(DOWNLOADS_BASE_PATH),
        "active_downloads": active,
        "interrupted_downloads": interrupted,
        "history_counts": await db.get_history_stats(),
        "totals": {
            "completed": int(db_metrics.get("downloads_completed", 0)),
            "failed": int(db_metrics.get("downloads_failed", 0)),
            "cancelled": int(db_metrics.get("downloads_cancelled", 0)),
            "bytes_downloaded": int(db_metrics.get("bytes_downloaded", 0)),
        },
    }


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@api_router.post("/downloads", status_code=status.HTTP_202_ACCEPTED)
async def create_download(request: Request, body: NewDownloadRequest):
    """Start a new download."""
    _authorize(request)
    download_id = await start_download(
        url=body.url,
        filename=body.filename,
        path=body.path,
        cookie=body.cookie,
        headers=body.headers,
        method=body.method,
        post_data=body.post_data,
        webhook_url_complete=body.webhook_url_complete,
        webhook_url_error=body.webhook_url_error,
        expected_sha256=body.sha256,
        limit_mbps=body.limit_mbps,
        paused=body.paused,
    )
    return {"download_id": download_id, "status": "started"}


@api_router.post("/downloads/{download_id}/pause")
async def pause_download_endpoint(request: Request, download_id: str):
    """Pause a download (the partial file is kept)."""
    _authorize(request)
    if not pause_download(download_id):
        raise _not_found(download_id)
    return {"download_id": download_id, "paused": True}


@api_router.post("/downloads/{download_id}/resume")
async def resume_download_endpoint(request: Request, download_id: str):
    """Resume a paused download."""
    _authorize(request)
    if not resume_download(download_id):
        raise _not_found(download_id)
    return {"download_id": download_id, "paused": False}


@api_router.patch("/downloads/{download_id}")
async def update_download_endpoint(request: Request, download_id: str, body: DownloadLimitRequest):
    """Change the speed limit of a single download."""
    _authorize(request)
    if not set_download_limit(download_id, body.limit_mbps):
        raise _not_found(download_id)
    return {"download_id": download_id, "limit_mbps": get_active_downloads()[download_id]["limit_mbps"]}


@api_router.delete("/downloads/{download_id}")
async def cancel_download_endpoint(request: Request, download_id: str):
    """Cancel a download."""
    _authorize(request)
    if not cancel_download(download_id):
        raise _not_found(download_id)
    return {"download_id": download_id, "status": "cancelled"}


@api_router.post("/pause-all")
async def pause_all_endpoint(request: Request, body: Optional[PauseRequest] = None):
    """
    Pause all downloads, optionally only for the given number of minutes
    (not persisted across restarts).
    """
    _authorize(request)
    minutes = body.minutes if body else None
    pause_all(duration_seconds=minutes * 60 if minutes else None)
    return {"paused": True, "pause_remaining_seconds": get_pause_remaining_seconds()}


@api_router.post("/resume-all")
async def resume_all_endpoint(request: Request):
    """Resume all downloads."""
    _authorize(request)
    resume_all()
    return {"paused": False}


@api_router.post("/interrupted/{download_id}/resume")
async def resume_interrupted_endpoint(request: Request, download_id: str):
    """Restart a download interrupted by a service restart."""
    _authorize(request)
    if not await resume_interrupted_download(download_id):
        raise _not_found(download_id)
    return {"download_id": download_id, "status": "started"}


@api_router.delete("/interrupted/{download_id}")
async def discard_interrupted_endpoint(request: Request, download_id: str):
    """Move an interrupted download to history as cancelled."""
    _authorize(request)
    if not await discard_interrupted_download(download_id):
        raise _not_found(download_id)
    return {"download_id": download_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _check_history_status(value: Optional[str]) -> None:
    if value and value not in HISTORY_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid status filter. Use: completed, failed, or cancelled"
        )


@api_router.get("/history")
async def get_history(
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    download_status: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = Query(None, max_length=500)
):
    """Paginated download history with status and text filter."""
    _authorize(request)
    _check_history_status(download_status)
    rows, total = await db.get_history_page(
        limit=limit, offset=offset, status=download_status, search=search or None
    )
    return {"downloads": [_history_view(row) for row in rows], "total": total}


@api_router.delete("/history/{download_id}")
async def delete_history_entry(request: Request, download_id: str):
    """Delete a single history record (the downloaded file is kept)."""
    _authorize(request)
    if not await db.delete_history(download_id=download_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="History record not found")
    return {"deleted": 1}


@api_router.delete("/history")
async def clear_history(
    request: Request,
    download_status: Optional[str] = Query(None, alias="status")
):
    """Delete history records, optionally only those with a given status."""
    _authorize(request)
    _check_history_status(download_status)
    deleted = await db.delete_history(status=download_status)
    logger.info(f"History cleared ({download_status or 'all'}): {deleted} records")
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@api_router.get("/settings")
async def get_settings(request: Request):
    """List runtime settings with current values and defaults."""
    _authorize(request)
    return {
        "settings": settings.describe(),
        "readonly": {
            "downloads_path": DOWNLOADS_BASE_PATH,
            "database_path": db.db_path,
            "api_key_configured": not is_insecure_mode(),
            "rate_limit": os.getenv("RATE_LIMIT_REQUESTS", "10") + " / " +
                          os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60") + " s",
            "ui_rate_limit": f"{UI_RATE_LIMIT_REQUESTS} / {UI_RATE_LIMIT_WINDOW_SECONDS} s",
            "version": __version__,
        },
    }


@api_router.put("/settings")
async def update_settings(request: Request, body: dict[str, Any]):
    """
    Change one or more settings. Values are validated first and applied
    only if all of them are valid. Changes are persisted in the database.
    """
    _authorize(request)
    try:
        validated = await save_settings(body)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if validated:
        logger.info(f"Settings changed: {validated}")
    return {"settings": settings.describe()}


@api_router.delete("/settings/{key}")
async def reset_setting(request: Request, key: str):
    """Reset a setting to its environment / default value."""
    _authorize(request)
    if key not in SETTING_DEFINITIONS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown setting: {key}")

    await reset_runtime_setting(key)
    logger.info(f"Setting reset to default: {key}")
    return {"settings": settings.describe()}


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def _schedule_view(schedule: dict, now: datetime) -> dict:
    upcoming = next_occurrence(schedule, now) if schedule["enabled"] else None
    return {**schedule, "next_run": upcoming.isoformat() if upcoming else None}


def _schedules_response() -> dict:
    now = datetime.now()
    return {
        "schedules": [_schedule_view(s, now) for s in scheduler.schedules],
        "server_time": now.isoformat(),
        "timezone": time.strftime("%Z"),
    }


def _schedule_values(body: ScheduleRequest) -> dict:
    if body.action == "speed_limit" and body.value is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Speed limit action needs a value (0 = unlimited)")
    return {
        "enabled": body.enabled,
        "days": body.days,
        "time": body.time,
        "action": body.action,
        "value": body.value if body.action == "speed_limit" else None,
    }


@api_router.get("/schedules")
async def list_schedules(request: Request):
    """List scheduled actions with their next run (server local time)."""
    _authorize(request)
    return _schedules_response()


@api_router.post("/schedules", status_code=status.HTTP_201_CREATED)
async def create_schedule(request: Request, body: ScheduleRequest):
    """Add a scheduled action."""
    _authorize(request)
    values = _schedule_values(body)
    schedule_id = await db.add_schedule(**values)
    await scheduler.reload()
    logger.info(f"Schedule #{schedule_id} added: {values}")
    return _schedules_response()


@api_router.put("/schedules/{schedule_id}")
async def update_schedule(request: Request, schedule_id: int, body: ScheduleRequest):
    """Replace a scheduled action."""
    _authorize(request)
    values = _schedule_values(body)
    if not await db.update_schedule(schedule_id, **values):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    await scheduler.reload()
    logger.info(f"Schedule #{schedule_id} changed: {values}")
    return _schedules_response()


@api_router.delete("/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: int):
    """Delete a scheduled action."""
    _authorize(request)
    if not await db.delete_schedule(schedule_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    await scheduler.reload()
    logger.info(f"Schedule #{schedule_id} deleted")
    return _schedules_response()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@api_router.get("/logs")
async def get_logs(
    request: Request,
    after: int = Query(0, ge=0, description="Return records with id greater than this"),
    limit: int = Query(500, ge=1, le=500)
):
    """Recent service log records (kept in memory)."""
    _authorize(request)
    return {"logs": memory_log_handler.get_records(after_id=after, limit=limit)}
