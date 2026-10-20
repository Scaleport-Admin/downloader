"""
Core download engine with streaming, progress tracking, and resume support.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from app.database import db
from app.metrics import metrics
from app.settings import settings
from app.throttle import BandwidthThrottle, DownloadSlots, mbps_to_bytes_per_second
from app.utils import (
    calculate_eta,
    decompress_file,
    format_bytes,
    generate_download_id,
    sanitize_filename,
)
from app.webhooks import send_completion_webhook, send_error_webhook

logger = logging.getLogger("file-downloader")

# Configuration from environment
# Download behaviour (concurrency, bandwidth, retries, decompression) is read
# from app.settings at runtime so it can be changed without a restart.
DOWNLOADS_BASE_PATH = os.getenv("DOWNLOADS_BASE_PATH", "/downloads")

# Chunk size for streaming (64KB)
CHUNK_SIZE = 65536

# How often paused downloads check whether they may continue
PAUSE_POLL_INTERVAL = 0.5

# Global state for active downloads
_active_downloads: dict[str, dict] = {}
_download_tasks: dict[str, asyncio.Task] = {}
_download_slots: Optional[DownloadSlots] = None
_total_throttle = BandwidthThrottle()
_globally_paused = False
# Wall-clock time (time.time()) when a timed global pause ends, None = until resumed
_pause_until: Optional[float] = None


class DownloadPaused(Exception):
    """Raised inside the streaming loop when a download is paused."""


def get_slots() -> DownloadSlots:
    """Get or create the download concurrency limiter."""
    global _download_slots
    if _download_slots is None:
        _download_slots = DownloadSlots(settings.get("max_concurrent_downloads"))
    return _download_slots


def apply_settings() -> None:
    """Apply changed runtime settings to running components."""
    get_slots().limit = max(1, settings.get("max_concurrent_downloads"))


def normalize_download_path(path: str) -> str:
    """
    Normalize download path to be within DOWNLOADS_BASE_PATH.

    If path doesn't start with DOWNLOADS_BASE_PATH, it will be treated
    as a relative path and prepended with DOWNLOADS_BASE_PATH.

    Args:
        path: Requested download path

    Returns:
        Normalized absolute path within DOWNLOADS_BASE_PATH
    """
    # Remove leading slash for relative path handling
    clean_path = path.lstrip("/")

    # Check if path already starts with downloads base (without leading slash)
    base_without_slash = DOWNLOADS_BASE_PATH.lstrip("/")
    if clean_path.startswith(base_without_slash + "/") or clean_path == base_without_slash:
        # Path already includes base, just ensure it starts with /
        return "/" + clean_path

    # Check if path already starts with full base path
    if path.startswith(DOWNLOADS_BASE_PATH + "/") or path == DOWNLOADS_BASE_PATH:
        return path

    # Treat as relative path, prepend base
    normalized = os.path.join(DOWNLOADS_BASE_PATH, clean_path)
    logger.info(f"Path normalized: '{path}' -> '{normalized}'")
    return normalized


def get_active_downloads() -> dict[str, dict]:
    """Get dictionary of active downloads."""
    return _active_downloads


def is_download_cancelled(download_id: str) -> bool:
    """Check if a download has been cancelled."""
    download = _active_downloads.get(download_id)
    if download:
        return download.get("cancelled", False)
    return True  # If not in active downloads, consider it cancelled


def _is_runnable(download_id: str) -> bool:
    """Check if a download is allowed to transfer data (not paused)."""
    download = _active_downloads.get(download_id)
    if not download:
        return False
    return not download.get("paused", False) and not is_globally_paused()


def _set_state(download_id: str, state: str, **fields) -> None:
    """Update the detailed state of an active download (used by the web UI)."""
    download = _active_downloads.get(download_id)
    if download:
        download["state"] = state
        if state != "downloading":
            download["speed_mbps"] = None
            download["eta_seconds"] = None
        download.update(fields)


def is_globally_paused() -> bool:
    """Check if all downloads are paused (a timed pause ends automatically)."""
    if _globally_paused and _pause_until is not None and time.time() >= _pause_until:
        resume_all(reason="pause timer expired")
    return _globally_paused


def get_pause_remaining_seconds() -> Optional[int]:
    """Seconds until a timed global pause ends, or None if not a timed pause."""
    if not is_globally_paused() or _pause_until is None:
        return None
    return max(0, int(round(_pause_until - time.time())))


def pause_all(duration_seconds: Optional[float] = None, reason: Optional[str] = None) -> None:
    """
    Pause all downloads; queued and new downloads will not start until resumed.

    Args:
        duration_seconds: Resume automatically after this time (None = until resumed)
        reason: Optional reason for the log
    """
    global _globally_paused, _pause_until
    _globally_paused = True
    _pause_until = time.time() + duration_seconds if duration_seconds else None
    suffix = f" for {round(duration_seconds / 60)} min" if duration_seconds else ""
    logger.info(f"All downloads paused{suffix}" + (f" ({reason})" if reason else ""))


def resume_all(reason: Optional[str] = None) -> None:
    """Resume all downloads (individually paused downloads stay paused)."""
    global _globally_paused, _pause_until
    _globally_paused = False
    _pause_until = None
    logger.info("All downloads resumed" + (f" ({reason})" if reason else ""))


def pause_download(download_id: str) -> bool:
    """
    Pause a download. The partial file is kept and the download continues
    from the same position when resumed.

    Returns:
        True if download was found
    """
    download = _active_downloads.get(download_id)
    if not download or download.get("cancelled"):
        return False
    download["paused"] = True
    logger.info(f"[{download_id}] Download paused")
    return True


def resume_download(download_id: str) -> bool:
    """
    Resume a paused download.

    Returns:
        True if download was found
    """
    download = _active_downloads.get(download_id)
    if not download or download.get("cancelled"):
        return False
    download["paused"] = False
    logger.info(f"[{download_id}] Download resumed")
    return True


def set_download_limit(download_id: str, limit_mbps: Optional[float]) -> bool:
    """
    Set a speed limit for a single download (None or 0 = only global limits apply).

    Returns:
        True if download was found
    """
    download = _active_downloads.get(download_id)
    if not download:
        return False
    download["limit_mbps"] = limit_mbps if limit_mbps and limit_mbps > 0 else None
    logger.info(f"[{download_id}] Speed limit set to {download['limit_mbps'] or 'global'} Mbps")
    return True


def get_queue_position(download_id: str) -> int:
    """1-based position of a queued download, or 0 if it is not waiting."""
    return get_slots().queue_position(download_id)


async def get_interrupted_downloads() -> list[dict]:
    """
    Get downloads left in the database by a previous run of the service
    (e.g. container restart) that are not running now.
    """
    rows = await db.get_active_downloads()
    return [row for row in rows if row["download_id"] not in _active_downloads]


async def resume_interrupted_download(download_id: str) -> bool:
    """
    Restart an interrupted download from its persisted parameters. The
    existing partial file is resumed using an HTTP Range request.

    Returns:
        True if the download was found and restarted
    """
    if download_id in _active_downloads:
        return False
    row = await db.get_download(download_id)
    if not row:
        return False

    _active_downloads[download_id] = _new_download_state(
        download_id=download_id,
        url=row["url"],
        filename=row["filename"],
        path=row["path"],
        cookie=row.get("cookie"),
        headers=json.loads(row["headers"]) if row.get("headers") else None,
        method=row.get("method") or "GET",
        post_data=json.loads(row["post_data"]) if row.get("post_data") else None,
        webhook_url_complete=row.get("webhook_url_complete"),
        webhook_url_error=row.get("webhook_url_error"),
        started_at=datetime.fromisoformat(row["started_at"]),
    )
    _active_downloads[download_id]["downloaded_bytes"] = row.get("downloaded_bytes") or 0
    _active_downloads[download_id]["total_bytes"] = row.get("total_bytes")
    _active_downloads[download_id]["progress_percent"] = row.get("progress_percent") or 0.0

    task = asyncio.create_task(_download_worker(download_id))
    _download_tasks[download_id] = task
    logger.info(f"[{download_id}] Interrupted download restarted")
    return True


async def discard_interrupted_download(download_id: str) -> bool:
    """
    Move an interrupted download to history as cancelled.

    Returns:
        True if the download was found
    """
    if download_id in _active_downloads:
        return False
    data = await db.cancel_download(download_id)
    return bool(data)


def _new_download_state(
    download_id: str,
    url: str,
    filename: str,
    path: str,
    cookie: Optional[str],
    headers: Optional[dict[str, str]],
    method: str,
    post_data: Optional[dict],
    webhook_url_complete: Optional[str],
    webhook_url_error: Optional[str],
    started_at: datetime,
    limit_mbps: Optional[float] = None,
    paused: bool = False
) -> dict:
    """Build the in-memory state of an active download."""
    return {
        "download_id": download_id,
        "url": url,
        "filename": filename,
        "path": path,
        # Legacy status reported by GET /downloads (always "downloading")
        "status": "downloading",
        # Detailed state: queued, downloading, paused, retrying, processing
        "state": "queued",
        "progress_percent": 0.0,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed_mbps": None,
        "started_at": started_at,
        "eta_seconds": None,
        "cancelled": False,
        "paused": paused,
        "limit_mbps": limit_mbps if limit_mbps and limit_mbps > 0 else None,
        "attempt": 0,
        "last_error": None,
        "cookie": cookie,
        "headers": headers,
        "method": method,
        "post_data": post_data,
        "webhook_url_complete": webhook_url_complete,
        "webhook_url_error": webhook_url_error,
    }


def cancel_download(download_id: str) -> bool:
    """
    Mark a download for cancellation.

    Args:
        download_id: Download identifier

    Returns:
        True if download was found and marked for cancellation
    """
    if download_id in _active_downloads:
        _active_downloads[download_id]["cancelled"] = True
        logger.info(f"[{download_id}] Download marked for cancellation")
        return True
    return False


async def start_download(
    url: str,
    filename: str,
    path: str,
    cookie: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
    method: str = "GET",
    post_data: Optional[dict] = None,
    webhook_url_complete: Optional[str] = None,
    webhook_url_error: Optional[str] = None,
    limit_mbps: Optional[float] = None,
    paused: bool = False
) -> str:
    """
    Start a new download in the background.

    Args:
        url: Download URL
        filename: Target filename
        path: Target directory path
        cookie: Optional cookie string
        headers: Optional custom headers
        method: HTTP method (GET or POST)
        post_data: Optional POST data
        webhook_url_complete: Webhook URL for completion
        webhook_url_error: Webhook URL for errors
        limit_mbps: Optional speed limit for this download
        paused: Add the download in paused state

    Returns:
        Download ID
    """
    # Generate unique ID
    download_id = generate_download_id()

    # Sanitize filename
    safe_filename = sanitize_filename(filename)

    # Normalize path to be within downloads directory
    normalized_path = normalize_download_path(path)

    # Initialize active download state
    _active_downloads[download_id] = _new_download_state(
        download_id=download_id,
        url=url,
        filename=safe_filename,
        path=normalized_path,
        cookie=cookie,
        headers=headers,
        method=method,
        post_data=post_data,
        webhook_url_complete=webhook_url_complete,
        webhook_url_error=webhook_url_error,
        started_at=datetime.utcnow(),
        limit_mbps=limit_mbps,
        paused=paused,
    )

    # Add to database
    await db.add_download(
        download_id=download_id,
        url=url,
        filename=safe_filename,
        path=normalized_path,
        cookie=cookie,
        headers=json.dumps(headers) if headers else None,
        method=method,
        post_data=json.dumps(post_data) if post_data else None,
        webhook_url_complete=webhook_url_complete,
        webhook_url_error=webhook_url_error
    )

    # Start download task
    task = asyncio.create_task(_download_worker(download_id))
    _download_tasks[download_id] = task

    logger.info(f"[{download_id}] Download started: {url} -> {normalized_path}/{safe_filename}")
    return download_id


async def _download_worker(download_id: str) -> None:
    """
    Worker coroutine that performs the actual download.

    Args:
        download_id: Download identifier
    """
    download = _active_downloads.get(download_id)
    if not download:
        return

    slots = get_slots()

    def should_leave_queue() -> bool:
        return is_download_cancelled(download_id) or not _is_runnable(download_id)

    try:
        while True:
            # Wait while paused (individually or globally)
            while not is_download_cancelled(download_id) and not _is_runnable(download_id):
                _set_state(download_id, "paused")
                await asyncio.sleep(PAUSE_POLL_INTERVAL)

            if is_download_cancelled(download_id):
                logger.info(f"[{download_id}] Download cancelled before start")
                await _handle_download_cancelled(download_id)
                return

            # Wait for a free download slot
            _set_state(download_id, "queued")
            if not await slots.acquire(download_id, should_leave_queue):
                continue

            try:
                _set_state(download_id, "downloading")
                outcome = await _perform_download(download_id)
            finally:
                slots.release()

            if outcome != "paused":
                return
    except Exception as e:
        logger.error(f"[{download_id}] Download worker error: {e}")
        await _handle_download_failure(download_id, str(e))
    finally:
        # Cleanup
        if download_id in _download_tasks:
            del _download_tasks[download_id]


async def _interruptible_sleep(download_id: str, seconds: float) -> None:
    """Sleep, waking up early if the download is cancelled or paused."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_download_cancelled(download_id) or not _is_runnable(download_id):
            return
        await asyncio.sleep(min(PAUSE_POLL_INTERVAL, deadline - time.monotonic()))


async def _perform_download(download_id: str) -> Optional[str]:
    """
    Perform the download with retry logic.

    Args:
        download_id: Download identifier

    Returns:
        Outcome: "completed", "failed", "cancelled" or "paused"
    """
    download = _active_downloads.get(download_id)
    if not download:
        return None

    url = download["url"]
    filename = download["filename"]
    path = download["path"]
    method = download["method"]
    retry_count = 0
    downloaded_bytes = 0

    # Ensure directory exists
    Path(path).mkdir(parents=True, exist_ok=True)
    filepath = os.path.join(path, filename)

    while retry_count < settings.get("max_retry_attempts"):
        if is_download_cancelled(download_id):
            logger.info(f"[{download_id}] Download cancelled before attempt")
            await _handle_download_cancelled(download_id)
            return "cancelled"

        if not _is_runnable(download_id):
            return "paused"

        _set_state(download_id, "downloading", attempt=retry_count + 1)

        try:
            # Check for existing partial file for resume
            if os.path.exists(filepath):
                downloaded_bytes = os.path.getsize(filepath)
                logger.info(f"[{download_id}] Resuming from {format_bytes(downloaded_bytes)}")

            success = await _stream_download(
                download_id=download_id,
                url=url,
                filepath=filepath,
                start_byte=downloaded_bytes,
                cookie=download.get("cookie"),
                custom_headers=download.get("headers"),
                method=method,
                post_data=download.get("post_data")
            )

            if success:
                await _handle_download_complete(download_id, filepath)
                return "completed"

        except DownloadPaused:
            logger.info(f"[{download_id}] Download paused at {format_bytes(os.path.getsize(filepath))}")
            return "paused"

        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 400 and e.response.status_code < 500:
                # Client errors - don't retry
                error_msg = f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
                logger.error(f"[{download_id}] Client error (no retry): {error_msg}")
                await _handle_download_failure(download_id, error_msg)
                return "failed"

            logger.warning(f"[{download_id}] HTTP error: {e}")
            _set_state(download_id, "retrying", last_error=str(e))

        except httpx.TimeoutException as e:
            logger.warning(f"[{download_id}] Timeout: {e}")
            _set_state(download_id, "retrying", last_error=f"Timeout: {e}")

        except httpx.RequestError as e:
            logger.warning(f"[{download_id}] Request error: {e}")
            _set_state(download_id, "retrying", last_error=f"Request error: {e}")

        except Exception as e:
            logger.error(f"[{download_id}] Unexpected error: {e}")
            _set_state(download_id, "retrying", last_error=str(e))

        # Check if cancelled before retry
        if is_download_cancelled(download_id):
            await _handle_download_cancelled(download_id)
            return "cancelled"

        # Calculate backoff
        retry_count += 1
        max_retry_attempts = settings.get("max_retry_attempts")
        if retry_count < max_retry_attempts:
            backoff = settings.get("retry_backoff_base_seconds") * (3 ** (retry_count - 1))  # 5s, 15s, 45s
            logger.info(f"[{download_id}] Retry {retry_count}/{max_retry_attempts} in {backoff}s")
            await _interruptible_sleep(download_id, backoff)

    # All retries exhausted
    error_msg = f"Download failed after {settings.get('max_retry_attempts')} retries"
    await _handle_download_failure(download_id, error_msg)
    return "failed"


async def _stream_download(
    download_id: str,
    url: str,
    filepath: str,
    start_byte: int = 0,
    cookie: Optional[str] = None,
    custom_headers: Optional[dict[str, str]] = None,
    method: str = "GET",
    post_data: Optional[dict] = None
) -> bool:
    """
    Stream download file with progress tracking.

    Args:
        download_id: Download identifier
        url: Download URL
        filepath: Target file path
        start_byte: Byte to resume from
        cookie: Optional cookie string
        custom_headers: Optional custom headers
        method: HTTP method
        post_data: Optional POST data

    Returns:
        True if download completed successfully
    """
    download = _active_downloads.get(download_id)
    if not download:
        return False

    # Build headers
    headers = custom_headers.copy() if custom_headers else {}
    if cookie:
        headers["Cookie"] = cookie

    # Add range header for resume
    if start_byte > 0:
        headers["Range"] = f"bytes={start_byte}-"

    # Bandwidth limiting: per-download limit and limit shared by all downloads.
    # Limits are re-read for every chunk so changes apply immediately.
    download_throttle = BandwidthThrottle()

    # Progress tracking
    downloaded_bytes = start_byte
    total_bytes = None
    last_update_time = time.time()
    last_update_bytes = downloaded_bytes
    last_progress_percent = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        # Build request
        if method == "POST":
            request = client.build_request("POST", url, headers=headers, json=post_data)
        else:
            request = client.build_request("GET", url, headers=headers)

        async with client.stream(request.method, str(request.url), headers=dict(request.headers), content=request.content) as response:
            response.raise_for_status()

            # Get total size
            content_length = response.headers.get("Content-Length")
            content_range = response.headers.get("Content-Range")

            if content_range:
                # Format: bytes start-end/total
                total_bytes = int(content_range.split("/")[1])
            elif content_length:
                total_bytes = int(content_length) + start_byte

            # Check if server supports range requests
            if start_byte > 0 and response.status_code != 206:
                logger.warning(f"[{download_id}] Server doesn't support resume, starting from beginning")
                downloaded_bytes = 0
                start_byte = 0

            # Update active download with total
            if download_id in _active_downloads:
                _active_downloads[download_id]["total_bytes"] = total_bytes

            # Open file for writing (append if resuming)
            mode = "ab" if start_byte > 0 and response.status_code == 206 else "wb"

            with open(filepath, mode) as f:
                async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                    # Check for cancellation
                    if is_download_cancelled(download_id):
                        logger.info(f"[{download_id}] Download cancelled during streaming")
                        return False

                    # Check for pause (before writing, so a paused file is never complete)
                    if not _is_runnable(download_id):
                        raise DownloadPaused()

                    # Write chunk
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    # Apply bandwidth limiting
                    await download_throttle.consume(len(chunk), _download_rate_limit(download_id))
                    await _total_throttle.consume(
                        len(chunk), mbps_to_bytes_per_second(settings.get("total_bandwidth_mbps"))
                    )

                    # Update progress
                    current_time = time.time()
                    elapsed = current_time - last_update_time

                    # Calculate progress
                    progress_percent = 0.0
                    if total_bytes and total_bytes > 0:
                        progress_percent = (downloaded_bytes / total_bytes) * 100

                    # Update every 1% or every second
                    should_update = (
                        progress_percent - last_progress_percent >= 1.0 or
                        elapsed >= 1.0
                    )

                    if should_update:
                        # Calculate speed
                        bytes_delta = downloaded_bytes - last_update_bytes
                        speed_mbps = (bytes_delta / elapsed / 1024 / 1024 * 8) if elapsed > 0 else 0

                        # Calculate ETA
                        speed_bps = bytes_delta / elapsed if elapsed > 0 else 0
                        eta_seconds = calculate_eta(downloaded_bytes, total_bytes, speed_bps) if total_bytes else None

                        # Update in-memory state
                        if download_id in _active_downloads:
                            _active_downloads[download_id].update({
                                "downloaded_bytes": downloaded_bytes,
                                "progress_percent": progress_percent,
                                "speed_mbps": round(speed_mbps, 2),
                                "eta_seconds": eta_seconds
                            })

                        # Update database
                        await db.update_progress(
                            download_id=download_id,
                            downloaded_bytes=downloaded_bytes,
                            total_bytes=total_bytes,
                            progress_percent=progress_percent,
                            speed_mbps=round(speed_mbps, 2)
                        )

                        # Log progress every 10%
                        if int(progress_percent) % 10 == 0 and int(progress_percent) != int(last_progress_percent):
                            logger.info(
                                f"[{download_id}] Progress: {progress_percent:.1f}% "
                                f"({format_bytes(downloaded_bytes)}/{format_bytes(total_bytes) if total_bytes else '?'}) "
                                f"@ {speed_mbps:.2f} Mbps"
                            )

                        last_update_time = current_time
                        last_update_bytes = downloaded_bytes
                        last_progress_percent = progress_percent

    return True


def _download_rate_limit(download_id: str) -> float:
    """Effective per-download speed limit in bytes per second (0 = unlimited)."""
    limits = [settings.get("max_bandwidth_mbps")]
    download = _active_downloads.get(download_id)
    if download and download.get("limit_mbps"):
        limits.append(download["limit_mbps"])
    limits = [limit for limit in limits if limit and limit > 0]
    return mbps_to_bytes_per_second(min(limits)) if limits else 0.0


async def _handle_download_complete(download_id: str, filepath: str) -> None:
    """
    Handle successful download completion.

    Args:
        download_id: Download identifier
        filepath: Path to downloaded file
    """
    download = _active_downloads.get(download_id)
    if not download:
        return

    # Get final file size
    size_bytes = os.path.getsize(filepath)
    decompressed = False

    # Auto decompress if enabled (in a thread so the API stays responsive)
    if settings.get("auto_decompress"):
        _set_state(download_id, "processing")
        decompressed_path = await asyncio.to_thread(decompress_file, filepath)
        if decompressed_path:
            decompressed = True
            # Update size to decompressed size if single file
            if os.path.isfile(decompressed_path):
                size_bytes = os.path.getsize(decompressed_path)
                filepath = decompressed_path

    # Record duration in metrics
    started_at = download["started_at"]
    completed_at = datetime.utcnow()
    duration = (completed_at - started_at).total_seconds()
    metrics.record_duration(duration)

    # Update database
    data = await db.complete_download(download_id, size_bytes, decompressed)

    logger.info(
        f"[{download_id}] Download completed: {filepath} "
        f"({format_bytes(size_bytes)}) in {duration:.1f}s"
    )

    # Send webhook
    webhook_url = download.get("webhook_url_complete")
    if webhook_url:
        await send_completion_webhook(
            webhook_url=webhook_url,
            download_id=download_id,
            filename=download["filename"],
            path=filepath,
            size_bytes=size_bytes,
            started_at=started_at,
            completed_at=completed_at,
            decompressed=decompressed
        )

    # Cleanup
    if download_id in _active_downloads:
        del _active_downloads[download_id]


async def _handle_download_failure(download_id: str, error_message: str) -> None:
    """
    Handle download failure.

    Args:
        download_id: Download identifier
        error_message: Error description
    """
    download = _active_downloads.get(download_id)
    if not download:
        return

    # Update database
    data = await db.fail_download(download_id, error_message)

    logger.error(f"[{download_id}] Download failed: {error_message}")

    # Send webhook
    webhook_url = download.get("webhook_url_error")
    if webhook_url:
        await send_error_webhook(
            webhook_url=webhook_url,
            download_id=download_id,
            filename=download["filename"],
            error=error_message,
            started_at=download["started_at"],
            failed_at=datetime.utcnow()
        )

    # Cleanup
    if download_id in _active_downloads:
        del _active_downloads[download_id]


async def _handle_download_cancelled(download_id: str) -> None:
    """
    Handle download cancellation.

    Args:
        download_id: Download identifier
    """
    download = _active_downloads.get(download_id)
    if not download:
        return

    # Update database
    await db.cancel_download(download_id)

    logger.info(f"[{download_id}] Download cancelled successfully")

    # Cleanup
    if download_id in _active_downloads:
        del _active_downloads[download_id]
