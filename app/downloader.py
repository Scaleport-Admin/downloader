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
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
MAX_BANDWIDTH_MBPS = float(os.getenv("MAX_BANDWIDTH_MBPS", "0"))  # 0 = unlimited
AUTO_DECOMPRESS = os.getenv("AUTO_DECOMPRESS", "true").lower() == "true"
MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_BASE_SECONDS = int(os.getenv("RETRY_BACKOFF_BASE_SECONDS", "5"))
DOWNLOADS_BASE_PATH = os.getenv("DOWNLOADS_BASE_PATH", "/downloads")

# Chunk size for streaming (8KB)
CHUNK_SIZE = 8192

# Global state for active downloads
_active_downloads: dict[str, dict] = {}
_download_tasks: dict[str, asyncio.Task] = {}
_download_semaphore: Optional[asyncio.Semaphore] = None


def get_semaphore() -> asyncio.Semaphore:
    """Get or create the download semaphore."""
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _download_semaphore


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
    webhook_url_error: Optional[str] = None
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
    _active_downloads[download_id] = {
        "download_id": download_id,
        "url": url,
        "filename": safe_filename,
        "path": normalized_path,
        "status": "downloading",
        "progress_percent": 0.0,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed_mbps": None,
        "started_at": datetime.utcnow(),
        "eta_seconds": None,
        "cancelled": False,
        "cookie": cookie,
        "headers": headers,
        "method": method,
        "post_data": post_data,
        "webhook_url_complete": webhook_url_complete,
        "webhook_url_error": webhook_url_error,
    }

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

    semaphore = get_semaphore()

    try:
        async with semaphore:
            await _perform_download(download_id)
    except Exception as e:
        logger.error(f"[{download_id}] Download worker error: {e}")
        await _handle_download_failure(download_id, str(e))
    finally:
        # Cleanup
        if download_id in _download_tasks:
            del _download_tasks[download_id]


async def _perform_download(download_id: str) -> None:
    """
    Perform the download with retry logic.

    Args:
        download_id: Download identifier
    """
    download = _active_downloads.get(download_id)
    if not download:
        return

    url = download["url"]
    filename = download["filename"]
    path = download["path"]
    method = download["method"]
    retry_count = 0
    downloaded_bytes = 0

    # Ensure directory exists
    Path(path).mkdir(parents=True, exist_ok=True)
    filepath = os.path.join(path, filename)

    while retry_count < MAX_RETRY_ATTEMPTS:
        if is_download_cancelled(download_id):
            logger.info(f"[{download_id}] Download cancelled before attempt")
            await _handle_download_cancelled(download_id)
            return

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
                return

        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 400 and e.response.status_code < 500:
                # Client errors - don't retry
                error_msg = f"HTTP {e.response.status_code}: {e.response.reason_phrase}"
                logger.error(f"[{download_id}] Client error (no retry): {error_msg}")
                await _handle_download_failure(download_id, error_msg)
                return

            logger.warning(f"[{download_id}] HTTP error: {e}")

        except httpx.TimeoutException as e:
            logger.warning(f"[{download_id}] Timeout: {e}")

        except httpx.RequestError as e:
            logger.warning(f"[{download_id}] Request error: {e}")

        except Exception as e:
            logger.error(f"[{download_id}] Unexpected error: {e}")

        # Check if cancelled before retry
        if is_download_cancelled(download_id):
            await _handle_download_cancelled(download_id)
            return

        # Calculate backoff
        retry_count += 1
        if retry_count < MAX_RETRY_ATTEMPTS:
            backoff = RETRY_BACKOFF_BASE_SECONDS * (3 ** (retry_count - 1))  # 5s, 15s, 45s
            logger.info(f"[{download_id}] Retry {retry_count}/{MAX_RETRY_ATTEMPTS} in {backoff}s")
            await asyncio.sleep(backoff)

    # All retries exhausted
    error_msg = f"Download failed after {MAX_RETRY_ATTEMPTS} retries"
    await _handle_download_failure(download_id, error_msg)


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

    # Calculate bandwidth limit (bytes per second)
    bytes_per_second = (MAX_BANDWIDTH_MBPS * 1024 * 1024 / 8) if MAX_BANDWIDTH_MBPS > 0 else 0

    # For bandwidth limiting - track chunk timing
    chunk_start_time = time.time()

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

                    # Write chunk
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    # Apply bandwidth limiting using token bucket approach
                    if bytes_per_second > 0:
                        # Calculate how long this chunk should take at target rate
                        expected_time = len(chunk) / bytes_per_second
                        # Calculate actual elapsed time for this chunk
                        actual_time = time.time() - chunk_start_time
                        # Sleep if we're going too fast
                        if actual_time < expected_time:
                            await asyncio.sleep(expected_time - actual_time)
                        # Reset chunk timer for next iteration
                        chunk_start_time = time.time()

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

    # Auto decompress if enabled
    if AUTO_DECOMPRESS:
        decompressed_path = decompress_file(filepath)
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
