"""
API key authentication for the file downloader service.
"""

import logging
import os
from typing import Optional

from fastapi import HTTPException, Request, status

logger = logging.getLogger("file-downloader")

# Get API key from environment
API_KEY = os.getenv("API_KEY", "")

# Track if warning has been logged
_insecure_warning_logged = False


def get_api_key() -> str:
    """
    Get the configured API key.

    Returns:
        API key string (may be empty if not configured)
    """
    return API_KEY


def is_insecure_mode() -> bool:
    """
    Check if service is running in insecure mode (no API key).

    Returns:
        True if no API key is configured
    """
    return not bool(API_KEY)


def log_insecure_warning() -> None:
    """Log warning about insecure mode (only once)."""
    global _insecure_warning_logged
    if is_insecure_mode() and not _insecure_warning_logged:
        logger.warning(
            "WARNING: Running in insecure mode - no API_KEY configured! "
            "Set API_KEY environment variable for production use."
        )
        _insecure_warning_logged = True


def extract_api_key(request: Request, api_key_param: Optional[str] = None) -> Optional[str]:
    """
    Extract API key from request.

    Checks query parameter first, then X-API-Key header.

    Args:
        request: FastAPI request object
        api_key_param: API key from query parameter

    Returns:
        API key string or None
    """
    # Check query parameter first
    if api_key_param:
        return api_key_param

    # Check header
    header_key = request.headers.get("X-API-Key")
    if header_key:
        return header_key

    return None


def validate_api_key(
    request: Request,
    api_key: Optional[str] = None
) -> bool:
    """
    Validate API key from request.

    Args:
        request: FastAPI request object
        api_key: API key from query parameter

    Returns:
        True if valid

    Raises:
        HTTPException: If API key is invalid (401) or missing (401)
    """
    # If in insecure mode, allow all requests
    if is_insecure_mode():
        log_insecure_warning()
        return True

    # Extract API key
    provided_key = extract_api_key(request, api_key)

    if not provided_key:
        logger.warning(f"Missing API key from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Provide via 'api_key' query parameter or 'X-API-Key' header."
        )

    if provided_key != API_KEY:
        logger.warning(f"Invalid API key from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    return True


def validate_api_key_from_body(api_key: str) -> bool:
    """
    Validate API key from request body.

    Args:
        api_key: API key from request body

    Returns:
        True if valid

    Raises:
        HTTPException: If API key is invalid
    """
    # If in insecure mode, allow all requests
    if is_insecure_mode():
        log_insecure_warning()
        return True

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required in request body"
        )

    if api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    return True
