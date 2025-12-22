"""
FastAPI application for file downloader service.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.auth import log_insecure_warning, validate_api_key, validate_api_key_from_body
from app.database import db
from app.downloader import (
    cancel_download,
    get_active_downloads,
    start_download,
)
from app.metrics import metrics
from app.models import (
    ActiveDownload,
    ActiveDownloadsResponse,
    CancelResponse,
    DownloadRequest,
    DownloadResponse,
    ErrorResponse,
    HealthResponse,
    HistoryDownload,
    HistoryResponse,
)
from app.rate_limiter import rate_limiter
from app.utils import get_disk_space_gb, setup_logging

# Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", "30"))

# Setup logging
setup_logging(LOG_LEVEL)
logger = logging.getLogger("file-downloader")

# Track startup time for uptime calculation
_startup_time: Optional[float] = None


async def cleanup_task() -> None:
    """Background task to cleanup old history records."""
    while True:
        try:
            await asyncio.sleep(24 * 60 * 60)  # Run every 24 hours
            deleted = await db.cleanup_old_history(HISTORY_RETENTION_DAYS)
            if deleted > 0:
                logger.info(f"Cleanup task: removed {deleted} old history records")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _startup_time

    # Startup
    logger.info("Starting File Downloader Service...")
    _startup_time = time.time()

    # Connect to database
    await db.connect()

    # Log insecure mode warning if needed
    log_insecure_warning()

    # Start cleanup task
    cleanup = asyncio.create_task(cleanup_task())

    logger.info("File Downloader Service started successfully")

    yield

    # Shutdown
    logger.info("Shutting down File Downloader Service...")
    cleanup.cancel()
    try:
        await cleanup
    except asyncio.CancelledError:
        pass

    await db.disconnect()
    logger.info("File Downloader Service stopped")


# Create FastAPI app
app = FastAPI(
    title="File Downloader Service",
    description="Production-ready service for downloading large files with cookie authentication",
    version="1.0.0",
    lifespan=lifespan
)


@app.post(
    "/download",
    response_model=DownloadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    }
)
async def create_download(request: Request, download_request: DownloadRequest):
    """
    Start a new file download in the background.

    The download will be processed asynchronously and progress can be tracked
    via the GET /downloads endpoint.
    """
    # Rate limiting
    rate_limiter.check_rate_limit(request)

    # API key validation from body
    validate_api_key_from_body(download_request.api_key)

    # Start download
    download_id = await start_download(
        url=download_request.url,
        filename=download_request.filename,
        path=download_request.path,
        cookie=download_request.cookie,
        headers=download_request.headers,
        method=download_request.method,
        post_data=download_request.post_data,
        webhook_url_complete=download_request.webhook_url_complete,
        webhook_url_error=download_request.webhook_url_error
    )

    return DownloadResponse(
        download_id=download_id,
        status="started",
        message="Download started in background"
    )


@app.get(
    "/downloads",
    response_model=ActiveDownloadsResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    }
)
async def list_downloads(
    request: Request,
    api_key: Optional[str] = Query(None, description="API key for authentication")
):
    """
    Get list of all active downloads with progress information.
    """
    # Rate limiting
    rate_limiter.check_rate_limit(request)

    # API key validation
    validate_api_key(request, api_key)

    # Get active downloads from memory (more up-to-date)
    active = get_active_downloads()

    downloads = [
        ActiveDownload(
            download_id=d["download_id"],
            filename=d["filename"],
            url=d["url"],
            status=d["status"],
            progress_percent=d.get("progress_percent", 0),
            downloaded_bytes=d.get("downloaded_bytes", 0),
            total_bytes=d.get("total_bytes"),
            speed_mbps=d.get("speed_mbps"),
            started_at=d["started_at"],
            eta_seconds=d.get("eta_seconds")
        )
        for d in active.values()
    ]

    return ActiveDownloadsResponse(active_downloads=downloads)


@app.get(
    "/downloads/history",
    response_model=HistoryResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    }
)
async def get_download_history(
    request: Request,
    api_key: Optional[str] = Query(None, description="API key for authentication"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    download_status: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status (completed, failed, cancelled)"
    )
):
    """
    Get download history with optional filtering.

    History is retained for 30 days by default (configurable via HISTORY_RETENTION_DAYS).
    """
    # Rate limiting
    rate_limiter.check_rate_limit(request)

    # API key validation
    validate_api_key(request, api_key)

    # Validate status filter
    if download_status and download_status not in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid status filter. Use: completed, failed, or cancelled"
        )

    # Get history from database
    downloads, total = await db.get_history(limit=limit, status=download_status)

    history = [
        HistoryDownload(
            download_id=d["download_id"],
            filename=d["filename"],
            url=d["url"],
            status=d["status"],
            size_bytes=d.get("size_bytes"),
            started_at=datetime.fromisoformat(d["started_at"]),
            completed_at=datetime.fromisoformat(d["completed_at"]) if d.get("completed_at") else None,
            duration_seconds=d.get("duration_seconds"),
            error_message=d.get("error_message")
        )
        for d in downloads
    ]

    return HistoryResponse(downloads=history, total=total)


@app.delete(
    "/downloads/{download_id}",
    response_model=CancelResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Download not found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    }
)
async def cancel_download_endpoint(
    request: Request,
    download_id: str,
    api_key: Optional[str] = Query(None, description="API key for authentication")
):
    """
    Cancel a running download.

    The download will be stopped and moved to history with 'cancelled' status.
    """
    # Rate limiting
    rate_limiter.check_rate_limit(request)

    # API key validation
    validate_api_key(request, api_key)

    # Try to cancel
    if not cancel_download(download_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Download not found or already completed: {download_id}"
        )

    return CancelResponse(
        download_id=download_id,
        status="cancelled",
        message="Download cancelled successfully"
    )


@app.get(
    "/health",
    response_model=HealthResponse
)
async def health_check():
    """
    Health check endpoint.

    Returns service status, active download count, available disk space, and uptime.
    No authentication required.
    """
    active_count = len(get_active_downloads())
    disk_space = get_disk_space_gb("/downloads")
    uptime = int(time.time() - _startup_time) if _startup_time else 0

    return HealthResponse(
        status="healthy",
        active_downloads=active_count,
        disk_space_gb=disk_space,
        uptime_seconds=uptime
    )


@app.get(
    "/metrics",
    response_class=PlainTextResponse
)
async def get_metrics():
    """
    Prometheus-compatible metrics endpoint.

    Returns metrics in Prometheus text format. No authentication required.
    """
    active_count = len(get_active_downloads())
    uptime = int(time.time() - _startup_time) if _startup_time else 0

    return await metrics.format_metrics(active_count, uptime)


# Error handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions with consistent response format."""
    return PlainTextResponse(
        content=exc.detail,
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None)
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
