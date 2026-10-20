"""
Runtime settings that can be changed without restarting the service.

Every setting starts with the value from its environment variable. A value
changed through the web UI / settings API is persisted in the database and
overrides the environment value until it is reset.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("file-downloader")


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() == "true"


@dataclass(frozen=True)
class SettingDefinition:
    """Description of a single runtime setting."""

    key: str
    type: type
    env: str
    default: Any
    label: str
    description: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[tuple] = None


SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    d.key: d for d in [
        SettingDefinition(
            key="max_concurrent_downloads",
            type=int,
            env="MAX_CONCURRENT_DOWNLOADS",
            default=int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1")),
            label="Max concurrent downloads",
            description="How many downloads run at the same time; the rest wait in the queue.",
            minimum=1,
            maximum=50,
        ),
        SettingDefinition(
            key="max_bandwidth_mbps",
            type=float,
            env="MAX_BANDWIDTH_MBPS",
            default=float(os.getenv("MAX_BANDWIDTH_MBPS", "0")),
            label="Max speed per download (Mbps)",
            description="Speed limit applied to each download separately. 0 = unlimited.",
            minimum=0,
            maximum=100000,
        ),
        SettingDefinition(
            key="total_bandwidth_mbps",
            type=float,
            env="TOTAL_BANDWIDTH_MBPS",
            default=float(os.getenv("TOTAL_BANDWIDTH_MBPS", "0")),
            label="Max total speed (Mbps)",
            description="Speed limit shared by all running downloads together. 0 = unlimited.",
            minimum=0,
            maximum=100000,
        ),
        SettingDefinition(
            key="auto_decompress",
            type=bool,
            env="AUTO_DECOMPRESS",
            default=_env_bool("AUTO_DECOMPRESS", "true"),
            label="Auto decompress",
            description="Extract .gz and .zip files after the download finishes.",
        ),
        SettingDefinition(
            key="max_retry_attempts",
            type=int,
            env="MAX_RETRY_ATTEMPTS",
            default=int(os.getenv("MAX_RETRY_ATTEMPTS", "3")),
            label="Max retry attempts",
            description="Download attempts before a download is marked as failed.",
            minimum=1,
            maximum=100,
        ),
        SettingDefinition(
            key="retry_backoff_base_seconds",
            type=int,
            env="RETRY_BACKOFF_BASE_SECONDS",
            default=int(os.getenv("RETRY_BACKOFF_BASE_SECONDS", "5")),
            label="Retry backoff base (s)",
            description="Exponential backoff between attempts (base, 3x base, 9x base, ...).",
            minimum=0,
            maximum=3600,
        ),
        SettingDefinition(
            key="webhook_retry_attempts",
            type=int,
            env="WEBHOOK_RETRY_ATTEMPTS",
            default=int(os.getenv("WEBHOOK_RETRY_ATTEMPTS", "3")),
            label="Webhook retry attempts",
            description="Delivery attempts for completion/error webhooks.",
            minimum=1,
            maximum=20,
        ),
        SettingDefinition(
            key="webhook_retry_backoff_seconds",
            type=int,
            env="WEBHOOK_RETRY_BACKOFF_SECONDS",
            default=int(os.getenv("WEBHOOK_RETRY_BACKOFF_SECONDS", "5")),
            label="Webhook retry backoff (s)",
            description="Exponential backoff base between webhook attempts.",
            minimum=0,
            maximum=3600,
        ),
        SettingDefinition(
            key="history_retention_days",
            type=int,
            env="HISTORY_RETENTION_DAYS",
            default=int(os.getenv("HISTORY_RETENTION_DAYS", "30")),
            label="History retention (days)",
            description="History records older than this are removed by the daily cleanup.",
            minimum=1,
            maximum=36500,
        ),
        SettingDefinition(
            key="log_level",
            type=str,
            env="LOG_LEVEL",
            default=os.getenv("LOG_LEVEL", "INFO").upper(),
            label="Log level",
            description="Verbosity of the service log.",
            choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        ),
    ]
}


class RuntimeSettings:
    """Holds the current values of all runtime settings."""

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        """Get the current value of a setting."""
        if key in self._overrides:
            return self._overrides[key]
        return SETTING_DEFINITIONS[key].default

    def is_overridden(self, key: str) -> bool:
        """Whether the setting was changed at runtime (differs from the environment)."""
        return key in self._overrides

    def validate(self, key: str, value: Any) -> Any:
        """
        Validate and coerce a value for a setting.

        Raises:
            ValueError: If the key is unknown or the value is invalid
        """
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            raise ValueError(f"Unknown setting: {key}")

        if definition.type is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key}: expected true or false")
            return value

        if definition.type in (int, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key}: expected a number")
            if definition.type is int:
                if float(value) != int(value):
                    raise ValueError(f"{key}: expected a whole number")
                value = int(value)
            else:
                value = float(value)
            if definition.minimum is not None and value < definition.minimum:
                raise ValueError(f"{key}: must be at least {definition.minimum:g}")
            if definition.maximum is not None and value > definition.maximum:
                raise ValueError(f"{key}: must be at most {definition.maximum:g}")
            return value

        if not isinstance(value, str):
            raise ValueError(f"{key}: expected a string")
        if definition.choices:
            value = value.upper()
            if value not in definition.choices:
                raise ValueError(f"{key}: must be one of {', '.join(definition.choices)}")
        return value

    def load(self, stored: dict[str, str]) -> None:
        """
        Load persisted overrides (JSON-encoded values keyed by setting name).

        Invalid or unknown stored values are ignored so that a bad record can
        never prevent the service from starting.
        """
        self._overrides = {}
        for key, raw in stored.items():
            try:
                self._overrides[key] = self.validate(key, json.loads(raw))
            except (ValueError, TypeError) as e:
                logger.warning(f"Ignoring stored setting {key}={raw!r}: {e}")
        if self._overrides:
            logger.info(f"Loaded runtime settings overrides: {self._overrides}")

    def set(self, key: str, value: Any) -> Any:
        """Set an override (value must already be validated)."""
        self._overrides[key] = value
        return value

    def reset(self, key: str) -> None:
        """Remove an override so the environment value applies again."""
        self._overrides.pop(key, None)

    def describe(self) -> list[dict]:
        """Describe all settings for the settings API."""
        return [
            {
                "key": d.key,
                "value": self.get(d.key),
                "default": d.default,
                "overridden": self.is_overridden(d.key),
                "type": d.type.__name__,
                "env": d.env,
                "label": d.label,
                "description": d.description,
                "minimum": d.minimum,
                "maximum": d.maximum,
                "choices": list(d.choices) if d.choices else None,
            }
            for d in SETTING_DEFINITIONS.values()
        ]


# Global runtime settings instance
settings = RuntimeSettings()
