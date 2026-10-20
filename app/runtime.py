"""
Loading, saving and applying runtime settings.

Shared by the settings API and the scheduler.
"""

import json
import logging
from typing import Any

from app.database import db
from app.downloader import apply_settings
from app.settings import settings
from app.utils import set_log_level

logger = logging.getLogger("file-downloader")


def apply_runtime_settings() -> None:
    """Apply current settings to running components."""
    apply_settings()
    set_log_level(settings.get("log_level"))


async def load_settings() -> None:
    """Load persisted settings overrides and apply them (called at startup)."""
    settings.load(await db.get_settings())
    apply_runtime_settings()


async def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    """
    Validate, persist and apply settings changes. Nothing is changed unless
    all values are valid.

    Raises:
        ValueError: If any key or value is invalid
    """
    validated = {key: settings.validate(key, value) for key, value in values.items()}
    for key, value in validated.items():
        await db.set_setting(key, json.dumps(value))
        settings.set(key, value)
    apply_runtime_settings()
    return validated


async def reset_setting(key: str) -> None:
    """Remove a persisted override so the environment value applies again."""
    await db.delete_setting(key)
    settings.reset(key)
    apply_runtime_settings()
