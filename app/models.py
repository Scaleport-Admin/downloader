"""
Pydantic models for request/response validation.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import re


class DownloadRequest(BaseModel):
    """Request model for starting a new download."""

    url: str = Field(..., description="URL of the file to download")
    filename: str = Field(..., description="Name for the downloaded file")
    path: str = Field(..., description="Absolute path where file should be saved")
    cookie: Optional[str] = Field(None, description="Cookie string for authentication")
    headers: Optional[dict[str, str]] = Field(None, description="Custom HTTP headers")
    method: str = Field("GET", description="HTTP method (GET or POST)")
    post_data: Optional[dict] = Field(None, description="POST request body data")
    webhook_url_complete: Optional[str] = Field(None, description="Webhook URL for completion")
    webhook_url_error: Optional[str] = Field(None, description="Webhook URL for errors")
    api_key: str = Field(..., description="API key for authentication")

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        """Validate HTTP method is GET or POST."""
        v = v.upper()
        if v not in ("GET", "POST"):
            raise ValueError("Method must be GET or POST")
        return v

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Validate path is absolute and safe."""
        # Check for path traversal attempts
        if ".." in v or v.startswith("~"):
            raise ValueError("Path traversal detected - '..' and '~' not allowed")
        # Check for absolute path
        if not v.startswith("/"):
            raise ValueError("Path must be absolute (start with /)")
        return v


class DownloadResponse(BaseModel):
    """Response model for download initiation."""

    download_id: str
    status: str
    message: str


class ActiveDownload(BaseModel):
    """Model for an active download."""

    download_id: str
    filename: str
    url: str
    status: str
    progress_percent: float
    downloaded_bytes: int
    total_bytes: Optional[int]
    speed_mbps: Optional[float]
    started_at: datetime
    eta_seconds: Optional[int]


class ActiveDownloadsResponse(BaseModel):
    """Response model for listing active downloads."""

    active_downloads: list[ActiveDownload]


class HistoryDownload(BaseModel):
    """Model for a download in history."""

    download_id: str
    filename: str
    url: str
    status: str
    size_bytes: Optional[int]
    started_at: datetime
    completed_at: Optional[datetime]
    duration_seconds: Optional[int]
    error_message: Optional[str]


class HistoryResponse(BaseModel):
    """Response model for download history."""

    downloads: list[HistoryDownload]
    total: int


class CancelResponse(BaseModel):
    """Response model for download cancellation."""

    download_id: str
    status: str
    message: str


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str
    active_downloads: int
    disk_space_gb: float
    uptime_seconds: int


class WebhookComplete(BaseModel):
    """Webhook payload for completed download."""

    download_id: str
    status: str = "completed"
    filename: str
    path: str
    size_bytes: int
    started_at: datetime
    completed_at: datetime
    duration_seconds: int
    decompressed: bool


class WebhookError(BaseModel):
    """Webhook payload for failed download."""

    download_id: str
    status: str = "failed"
    filename: str
    error: str
    started_at: datetime
    failed_at: datetime


class ErrorResponse(BaseModel):
    """Generic error response model."""

    detail: str
