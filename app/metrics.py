"""
Prometheus-compatible metrics for the file downloader service.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.database import db

logger = logging.getLogger("file-downloader")


@dataclass
class MetricsCollector:
    """
    Collects and formats Prometheus-compatible metrics.
    """

    # Duration histogram buckets (in seconds)
    duration_buckets: list[float] = field(
        default_factory=lambda: [60, 300, 600, 1800, 3600, float("inf")]
    )

    # In-memory histogram data
    _duration_histogram: dict[float, int] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize histogram buckets."""
        self._duration_histogram = {bucket: 0 for bucket in self.duration_buckets}

    def record_duration(self, duration_seconds: float) -> None:
        """
        Record a download duration in the histogram.

        Args:
            duration_seconds: Download duration in seconds
        """
        for bucket in self.duration_buckets:
            if duration_seconds <= bucket:
                self._duration_histogram[bucket] = self._duration_histogram.get(bucket, 0) + 1
                break

    async def format_metrics(self, active_count: int, uptime_seconds: int) -> str:
        """
        Format metrics in Prometheus text format.

        Args:
            active_count: Number of active downloads
            uptime_seconds: Service uptime in seconds

        Returns:
            Prometheus-formatted metrics string
        """
        # Get metrics from database
        db_metrics = await db.get_metrics()

        completed = int(db_metrics.get("downloads_completed", 0))
        failed = int(db_metrics.get("downloads_failed", 0))
        cancelled = int(db_metrics.get("downloads_cancelled", 0))
        bytes_total = db_metrics.get("bytes_downloaded", 0)

        lines = []

        # downloads_total counter
        lines.append("# HELP downloads_total Total number of downloads")
        lines.append("# TYPE downloads_total counter")
        lines.append(f'downloads_total{{status="completed"}} {completed}')
        lines.append(f'downloads_total{{status="failed"}} {failed}')
        lines.append(f'downloads_total{{status="cancelled"}} {cancelled}')
        lines.append("")

        # downloads_active gauge
        lines.append("# HELP downloads_active Currently active downloads")
        lines.append("# TYPE downloads_active gauge")
        lines.append(f"downloads_active {active_count}")
        lines.append("")

        # downloads_bytes_total counter
        lines.append("# HELP downloads_bytes_total Total bytes downloaded")
        lines.append("# TYPE downloads_bytes_total counter")
        lines.append(f"downloads_bytes_total {bytes_total:.1e}")
        lines.append("")

        # download_duration_seconds histogram
        lines.append("# HELP download_duration_seconds Download duration")
        lines.append("# TYPE download_duration_seconds histogram")

        cumulative = 0
        for bucket in sorted(self.duration_buckets):
            cumulative += self._duration_histogram.get(bucket, 0)
            if bucket == float("inf"):
                lines.append(f'download_duration_seconds_bucket{{le="+Inf"}} {cumulative}')
            else:
                lines.append(f'download_duration_seconds_bucket{{le="{int(bucket)}"}} {cumulative}')

        lines.append("")

        # uptime_seconds gauge
        lines.append("# HELP uptime_seconds Service uptime in seconds")
        lines.append("# TYPE uptime_seconds gauge")
        lines.append(f"uptime_seconds {uptime_seconds}")

        return "\n".join(lines)


# Global metrics collector instance
metrics = MetricsCollector()
