"""
Rate limiting implementation for the file downloader service.
"""

import logging
import os
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request, status

logger = logging.getLogger("file-downloader")

# Configuration from environment
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "10"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))


class RateLimiter:
    """
    Simple in-memory rate limiter using sliding window.
    """

    def __init__(
        self,
        max_requests: int = RATE_LIMIT_REQUESTS,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS
    ):
        """
        Initialize rate limiter.

        Args:
            max_requests: Maximum requests allowed per window
            window_seconds: Time window in seconds
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _get_client_id(self, request: Request) -> str:
        """
        Get unique client identifier from request.

        Args:
            request: FastAPI request object

        Returns:
            Client IP address or 'unknown'
        """
        # Check for forwarded headers (behind proxy)
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Get the first IP in the chain
            return forwarded.split(",")[0].strip()

        # Check for real IP header
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        # Use client host
        if request.client:
            return request.client.host

        return "unknown"

    def _cleanup_old_requests(self, client_id: str) -> None:
        """
        Remove requests outside the current window.

        Args:
            client_id: Client identifier
        """
        current_time = time.time()
        cutoff_time = current_time - self.window_seconds

        self._requests[client_id] = [
            req_time for req_time in self._requests[client_id]
            if req_time > cutoff_time
        ]

    def check_rate_limit(self, request: Request) -> bool:
        """
        Check if request is within rate limit.

        Args:
            request: FastAPI request object

        Returns:
            True if within limit

        Raises:
            HTTPException: If rate limit exceeded (429)
        """
        client_id = self._get_client_id(request)
        current_time = time.time()

        # Cleanup old requests
        self._cleanup_old_requests(client_id)

        # Check if limit exceeded
        if len(self._requests[client_id]) >= self.max_requests:
            # Calculate retry-after
            oldest_request = min(self._requests[client_id])
            retry_after = int(oldest_request + self.window_seconds - current_time) + 1

            logger.warning(
                f"Rate limit exceeded for {client_id}: "
                f"{len(self._requests[client_id])}/{self.max_requests} requests"
            )

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Maximum {self.max_requests} requests per {self.window_seconds} seconds.",
                headers={"Retry-After": str(retry_after)}
            )

        # Record this request
        self._requests[client_id].append(current_time)
        return True

    def get_remaining_requests(self, request: Request) -> int:
        """
        Get number of remaining requests for client.

        Args:
            request: FastAPI request object

        Returns:
            Number of remaining requests
        """
        client_id = self._get_client_id(request)
        self._cleanup_old_requests(client_id)
        return max(0, self.max_requests - len(self._requests[client_id]))

    def get_reset_time(self, request: Request) -> int:
        """
        Get time until rate limit resets.

        Args:
            request: FastAPI request object

        Returns:
            Seconds until reset
        """
        client_id = self._get_client_id(request)
        self._cleanup_old_requests(client_id)

        if not self._requests[client_id]:
            return 0

        oldest_request = min(self._requests[client_id])
        reset_time = oldest_request + self.window_seconds - time.time()
        return max(0, int(reset_time))


# Global rate limiter instance
rate_limiter = RateLimiter()
