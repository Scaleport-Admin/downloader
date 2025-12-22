"""
Utility functions for the file downloader service.
"""

import os
import re
import logging
import gzip
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# Configure logging
logger = logging.getLogger("file-downloader")


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure structured logging with timestamps.

    Args:
        log_level: Logging level (INFO, WARNING, ERROR, DEBUG)
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = []
    root_logger.addHandler(handler)

    # Set specific logger level
    logger.setLevel(level)


def generate_download_id() -> str:
    """
    Generate a unique download ID.

    Returns:
        String in format dl_<timestamp>
    """
    timestamp = datetime.now().timestamp()
    return f"dl_{timestamp:.3f}"


def sanitize_filename(filename: str) -> str:
    """
    Remove dangerous characters from filename.

    Removes: / \ : * ? " < > |

    Args:
        filename: Original filename

    Returns:
        Sanitized filename
    """
    original = filename
    # Remove dangerous characters
    dangerous_chars = r'[/\\:*?"<>|]'
    sanitized = re.sub(dangerous_chars, "_", filename)

    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip(". ")

    # Ensure filename is not empty
    if not sanitized:
        sanitized = "unnamed_file"

    if original != sanitized:
        logger.info(f"Filename sanitized: '{original}' -> '{sanitized}'")

    return sanitized


def validate_path(path: str) -> bool:
    """
    Validate that path is safe and doesn't contain traversal attempts.

    Args:
        path: Path to validate

    Returns:
        True if path is safe, False otherwise
    """
    # Check for path traversal
    if ".." in path:
        return False
    if "\\" in path:
        return False
    if not path.startswith("/"):
        return False

    # Resolve and check the path doesn't escape
    try:
        resolved = Path(path).resolve()
        # Ensure resolved path still starts with the original base
        return str(resolved).startswith(path.rstrip("/").rsplit("/", 1)[0] if "/" in path else "/")
    except Exception:
        return False

    return True


def get_disk_space_gb(path: str = "/") -> float:
    """
    Get available disk space in gigabytes.

    Args:
        path: Path to check disk space for

    Returns:
        Available space in GB
    """
    try:
        stat = os.statvfs(path)
        available_bytes = stat.f_bavail * stat.f_frsize
        return round(available_bytes / (1024 ** 3), 2)
    except Exception as e:
        logger.error(f"Error getting disk space: {e}")
        return 0.0


def decompress_file(filepath: str) -> Optional[str]:
    """
    Decompress a file if it's a supported compressed format.

    Supports: .gz, .zip

    Args:
        filepath: Path to the compressed file

    Returns:
        Path to decompressed file(s), or None if not compressed/failed
    """
    logger.info(f"Attempting to decompress: {filepath}")

    try:
        if filepath.endswith(".gz"):
            # Handle gzip files
            output_path = filepath[:-3]  # Remove .gz extension
            logger.info(f"Decompressing gzip: {filepath} -> {output_path}")

            with gzip.open(filepath, "rb") as f_in:
                with open(output_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            # Remove the compressed file
            os.remove(filepath)
            logger.info(f"Gzip decompression complete, removed: {filepath}")
            return output_path

        elif filepath.endswith(".zip"):
            # Handle zip files
            output_dir = os.path.dirname(filepath)
            logger.info(f"Extracting zip: {filepath} -> {output_dir}")

            with zipfile.ZipFile(filepath, "r") as zip_ref:
                zip_ref.extractall(output_dir)

            # Remove the compressed file
            os.remove(filepath)
            logger.info(f"Zip extraction complete, removed: {filepath}")
            return output_dir

        else:
            logger.debug(f"File is not a supported compressed format: {filepath}")
            return None

    except Exception as e:
        logger.error(f"Decompression failed for {filepath}: {e}")
        return None


def format_bytes(bytes_value: int) -> str:
    """
    Format bytes into human-readable string.

    Args:
        bytes_value: Number of bytes

    Returns:
        Human-readable string (e.g., "1.5 GB")
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.2f} PB"


def calculate_eta(downloaded_bytes: int, total_bytes: int, speed_bps: float) -> Optional[int]:
    """
    Calculate estimated time of arrival in seconds.

    Args:
        downloaded_bytes: Bytes already downloaded
        total_bytes: Total file size in bytes
        speed_bps: Current download speed in bytes per second

    Returns:
        ETA in seconds, or None if cannot calculate
    """
    if total_bytes is None or total_bytes <= 0:
        return None
    if speed_bps <= 0:
        return None

    remaining_bytes = total_bytes - downloaded_bytes
    if remaining_bytes <= 0:
        return 0

    return int(remaining_bytes / speed_bps)
