"""
Webhook handling for download notifications.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

import httpx

from app.models import WebhookComplete, WebhookError

logger = logging.getLogger("file-downloader")

# Configuration from environment
WEBHOOK_RETRY_ATTEMPTS = int(os.getenv("WEBHOOK_RETRY_ATTEMPTS", "3"))
WEBHOOK_RETRY_BACKOFF_SECONDS = int(os.getenv("WEBHOOK_RETRY_BACKOFF_SECONDS", "5"))


async def send_webhook(
    url: str,
    payload: dict,
    max_retries: int = WEBHOOK_RETRY_ATTEMPTS,
    backoff_base: int = WEBHOOK_RETRY_BACKOFF_SECONDS
) -> bool:
    """
    Send a webhook with retry logic.

    Args:
        url: Webhook URL
        payload: JSON payload to send
        max_retries: Maximum number of retry attempts
        backoff_base: Base backoff time in seconds

    Returns:
        True if webhook was sent successfully
    """
    download_id = payload.get("download_id", "unknown")

    for attempt in range(max_retries):
        try:
            logger.info(
                f"[{download_id}] Sending webhook to {url} (attempt {attempt + 1}/{max_retries})"
            )

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )

                if response.status_code >= 200 and response.status_code < 300:
                    logger.info(
                        f"[{download_id}] Webhook sent successfully: {response.status_code}"
                    )
                    return True

                logger.warning(
                    f"[{download_id}] Webhook failed with status {response.status_code}: "
                    f"{response.text[:200]}"
                )

        except httpx.TimeoutException:
            logger.warning(f"[{download_id}] Webhook timeout to {url}")
        except httpx.RequestError as e:
            logger.warning(f"[{download_id}] Webhook request error: {e}")
        except Exception as e:
            logger.error(f"[{download_id}] Webhook unexpected error: {e}")

        # Calculate backoff for next attempt
        if attempt < max_retries - 1:
            backoff = backoff_base * (3 ** attempt)  # 5s, 15s, 45s
            logger.info(f"[{download_id}] Retrying webhook in {backoff}s...")
            await asyncio.sleep(backoff)

    logger.error(f"[{download_id}] Webhook failed after {max_retries} attempts")
    return False


async def send_completion_webhook(
    webhook_url: str,
    download_id: str,
    filename: str,
    path: str,
    size_bytes: int,
    started_at: datetime,
    completed_at: datetime,
    decompressed: bool = False
) -> bool:
    """
    Send webhook notification for completed download.

    Args:
        webhook_url: Webhook URL
        download_id: Download identifier
        filename: Downloaded filename
        path: Full path to downloaded file
        size_bytes: File size in bytes
        started_at: Download start time
        completed_at: Download completion time
        decompressed: Whether file was decompressed

    Returns:
        True if webhook was sent successfully
    """
    duration_seconds = int((completed_at - started_at).total_seconds())

    payload = WebhookComplete(
        download_id=download_id,
        status="completed",
        filename=filename,
        path=path,
        size_bytes=size_bytes,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        decompressed=decompressed
    )

    return await send_webhook(webhook_url, payload.model_dump(mode="json"))


async def send_error_webhook(
    webhook_url: str,
    download_id: str,
    filename: str,
    error: str,
    started_at: datetime,
    failed_at: datetime
) -> bool:
    """
    Send webhook notification for failed download.

    Args:
        webhook_url: Webhook URL
        download_id: Download identifier
        filename: Target filename
        error: Error message
        started_at: Download start time
        failed_at: Download failure time

    Returns:
        True if webhook was sent successfully
    """
    payload = WebhookError(
        download_id=download_id,
        status="failed",
        filename=filename,
        error=error,
        started_at=started_at,
        failed_at=failed_at
    )

    return await send_webhook(webhook_url, payload.model_dump(mode="json"))
