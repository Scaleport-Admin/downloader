"""
SQLite database operations for download tracking and history.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

logger = logging.getLogger("file-downloader")

# Default database path
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/downloads.db")


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: str = DATABASE_PATH):
        """
        Initialize database manager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Establish database connection and create tables."""
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
                logger.info(f"Database directory ensured: {db_dir}")
            except PermissionError as e:
                logger.error(f"Cannot create database directory {db_dir}: {e}")
                logger.error("Make sure the /data volume is mounted with correct permissions")
                raise

        # Test if we can write to the directory
        try:
            test_file = os.path.join(db_dir if db_dir else ".", ".write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
        except (PermissionError, OSError) as e:
            logger.error(f"Cannot write to database directory {db_dir}: {e}")
            logger.error("Make sure the /data volume is mounted with correct permissions")
            raise

        try:
            self._connection = await aiosqlite.connect(self.db_path)
            self._connection.row_factory = aiosqlite.Row
            await self._create_tables()
            logger.info(f"Database connected: {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    async def disconnect(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Database disconnected")

    async def _create_tables(self) -> None:
        """Create required database tables."""
        async with self._lock:
            # Active downloads table
            await self._connection.execute("""
                CREATE TABLE IF NOT EXISTS active_downloads (
                    download_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_percent REAL DEFAULT 0,
                    downloaded_bytes INTEGER DEFAULT 0,
                    total_bytes INTEGER,
                    speed_mbps REAL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_message TEXT,
                    cookie TEXT,
                    headers TEXT,
                    method TEXT DEFAULT 'GET',
                    post_data TEXT,
                    webhook_url_complete TEXT,
                    webhook_url_error TEXT
                )
            """)

            # Download history table
            await self._connection.execute("""
                CREATE TABLE IF NOT EXISTS download_history (
                    download_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    size_bytes INTEGER,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    duration_seconds INTEGER,
                    error_message TEXT,
                    decompressed BOOLEAN DEFAULT 0
                )
            """)

            # Metrics table for persistent counters
            await self._connection.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    key TEXT PRIMARY KEY,
                    value REAL DEFAULT 0
                )
            """)

            # Initialize metrics if not exists
            for key in ["downloads_completed", "downloads_failed", "downloads_cancelled", "bytes_downloaded"]:
                await self._connection.execute(
                    "INSERT OR IGNORE INTO metrics (key, value) VALUES (?, 0)",
                    (key,)
                )

            await self._connection.commit()
            logger.info("Database tables created/verified")

    async def add_download(
        self,
        download_id: str,
        url: str,
        filename: str,
        path: str,
        cookie: Optional[str] = None,
        headers: Optional[str] = None,
        method: str = "GET",
        post_data: Optional[str] = None,
        webhook_url_complete: Optional[str] = None,
        webhook_url_error: Optional[str] = None
    ) -> None:
        """
        Add a new download to active downloads.

        Args:
            download_id: Unique download identifier
            url: Download URL
            filename: Target filename
            path: Target directory path
            cookie: Optional cookie string
            headers: Optional JSON headers string
            method: HTTP method
            post_data: Optional JSON POST data string
            webhook_url_complete: Webhook URL for completion
            webhook_url_error: Webhook URL for errors
        """
        now = datetime.utcnow().isoformat()
        async with self._lock:
            await self._connection.execute(
                """
                INSERT INTO active_downloads
                (download_id, url, filename, path, status, started_at, updated_at,
                 cookie, headers, method, post_data, webhook_url_complete, webhook_url_error)
                VALUES (?, ?, ?, ?, 'downloading', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (download_id, url, filename, path, now, now,
                 cookie, headers, method, post_data, webhook_url_complete, webhook_url_error)
            )
            await self._connection.commit()
        logger.info(f"[{download_id}] Download added to database")

    async def update_progress(
        self,
        download_id: str,
        downloaded_bytes: int,
        total_bytes: Optional[int],
        progress_percent: float,
        speed_mbps: Optional[float]
    ) -> None:
        """
        Update download progress.

        Args:
            download_id: Download identifier
            downloaded_bytes: Bytes downloaded so far
            total_bytes: Total file size
            progress_percent: Download progress percentage
            speed_mbps: Current download speed in Mbps
        """
        now = datetime.utcnow().isoformat()
        async with self._lock:
            await self._connection.execute(
                """
                UPDATE active_downloads
                SET downloaded_bytes = ?, total_bytes = ?, progress_percent = ?,
                    speed_mbps = ?, updated_at = ?
                WHERE download_id = ?
                """,
                (downloaded_bytes, total_bytes, progress_percent, speed_mbps, now, download_id)
            )
            await self._connection.commit()

    async def complete_download(
        self,
        download_id: str,
        size_bytes: int,
        decompressed: bool = False
    ) -> dict:
        """
        Mark download as completed and move to history.

        Args:
            download_id: Download identifier
            size_bytes: Final file size
            decompressed: Whether file was decompressed

        Returns:
            Download data dictionary
        """
        now = datetime.utcnow().isoformat()
        async with self._lock:
            # Get download data
            cursor = await self._connection.execute(
                "SELECT * FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return {}

            data = dict(row)
            started_at = datetime.fromisoformat(data["started_at"])
            completed_at = datetime.utcnow()
            duration_seconds = int((completed_at - started_at).total_seconds())

            # Add to history
            await self._connection.execute(
                """
                INSERT INTO download_history
                (download_id, url, filename, path, status, size_bytes,
                 started_at, completed_at, duration_seconds, decompressed)
                VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?)
                """,
                (download_id, data["url"], data["filename"], data["path"],
                 size_bytes, data["started_at"], now, duration_seconds, decompressed)
            )

            # Remove from active
            await self._connection.execute(
                "DELETE FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )

            # Update metrics
            await self._connection.execute(
                "UPDATE metrics SET value = value + 1 WHERE key = 'downloads_completed'"
            )
            await self._connection.execute(
                "UPDATE metrics SET value = value + ? WHERE key = 'bytes_downloaded'",
                (size_bytes,)
            )

            await self._connection.commit()

        logger.info(f"[{download_id}] Download completed and moved to history")
        return data

    async def fail_download(
        self,
        download_id: str,
        error_message: str
    ) -> dict:
        """
        Mark download as failed and move to history.

        Args:
            download_id: Download identifier
            error_message: Error description

        Returns:
            Download data dictionary
        """
        now = datetime.utcnow().isoformat()
        async with self._lock:
            # Get download data
            cursor = await self._connection.execute(
                "SELECT * FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return {}

            data = dict(row)
            started_at = datetime.fromisoformat(data["started_at"])
            failed_at = datetime.utcnow()
            duration_seconds = int((failed_at - started_at).total_seconds())

            # Add to history
            await self._connection.execute(
                """
                INSERT INTO download_history
                (download_id, url, filename, path, status, size_bytes,
                 started_at, completed_at, duration_seconds, error_message)
                VALUES (?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?)
                """,
                (download_id, data["url"], data["filename"], data["path"],
                 data.get("downloaded_bytes", 0), data["started_at"], now,
                 duration_seconds, error_message)
            )

            # Remove from active
            await self._connection.execute(
                "DELETE FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )

            # Update metrics
            await self._connection.execute(
                "UPDATE metrics SET value = value + 1 WHERE key = 'downloads_failed'"
            )

            await self._connection.commit()

        logger.info(f"[{download_id}] Download failed and moved to history: {error_message}")
        return data

    async def cancel_download(self, download_id: str) -> dict:
        """
        Mark download as cancelled and move to history.

        Args:
            download_id: Download identifier

        Returns:
            Download data dictionary
        """
        now = datetime.utcnow().isoformat()
        async with self._lock:
            # Get download data
            cursor = await self._connection.execute(
                "SELECT * FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return {}

            data = dict(row)
            started_at = datetime.fromisoformat(data["started_at"])
            cancelled_at = datetime.utcnow()
            duration_seconds = int((cancelled_at - started_at).total_seconds())

            # Add to history
            await self._connection.execute(
                """
                INSERT INTO download_history
                (download_id, url, filename, path, status, size_bytes,
                 started_at, completed_at, duration_seconds, error_message)
                VALUES (?, ?, ?, ?, 'cancelled', ?, ?, ?, ?, 'Cancelled by user')
                """,
                (download_id, data["url"], data["filename"], data["path"],
                 data.get("downloaded_bytes", 0), data["started_at"], now, duration_seconds)
            )

            # Remove from active
            await self._connection.execute(
                "DELETE FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )

            # Update metrics
            await self._connection.execute(
                "UPDATE metrics SET value = value + 1 WHERE key = 'downloads_cancelled'"
            )

            await self._connection.commit()

        logger.info(f"[{download_id}] Download cancelled")
        return data

    async def get_active_downloads(self) -> list[dict]:
        """
        Get all active downloads.

        Returns:
            List of active download dictionaries
        """
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM active_downloads ORDER BY started_at DESC"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_download(self, download_id: str) -> Optional[dict]:
        """
        Get a specific active download.

        Args:
            download_id: Download identifier

        Returns:
            Download dictionary or None
        """
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM active_downloads WHERE download_id = ?",
                (download_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_history(
        self,
        limit: int = 100,
        status: Optional[str] = None
    ) -> tuple[list[dict], int]:
        """
        Get download history.

        Args:
            limit: Maximum number of records to return
            status: Filter by status (completed, failed, cancelled)

        Returns:
            Tuple of (list of downloads, total count)
        """
        async with self._lock:
            # Get total count
            if status:
                cursor = await self._connection.execute(
                    "SELECT COUNT(*) FROM download_history WHERE status = ?",
                    (status,)
                )
            else:
                cursor = await self._connection.execute(
                    "SELECT COUNT(*) FROM download_history"
                )
            total = (await cursor.fetchone())[0]

            # Get records
            if status:
                cursor = await self._connection.execute(
                    """
                    SELECT * FROM download_history
                    WHERE status = ?
                    ORDER BY completed_at DESC LIMIT ?
                    """,
                    (status, limit)
                )
            else:
                cursor = await self._connection.execute(
                    "SELECT * FROM download_history ORDER BY completed_at DESC LIMIT ?",
                    (limit,)
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows], total

    async def get_metrics(self) -> dict[str, float]:
        """
        Get all metrics values.

        Returns:
            Dictionary of metric name to value
        """
        async with self._lock:
            cursor = await self._connection.execute("SELECT key, value FROM metrics")
            rows = await cursor.fetchall()
            return {row["key"]: row["value"] for row in rows}

    async def get_active_count(self) -> int:
        """
        Get count of active downloads.

        Returns:
            Number of active downloads
        """
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT COUNT(*) FROM active_downloads"
            )
            return (await cursor.fetchone())[0]

    async def cleanup_old_history(self, retention_days: int = 30) -> int:
        """
        Delete history records older than retention period.

        Args:
            retention_days: Number of days to retain history

        Returns:
            Number of deleted records
        """
        cutoff_date = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        async with self._lock:
            cursor = await self._connection.execute(
                "DELETE FROM download_history WHERE completed_at < ?",
                (cutoff_date,)
            )
            deleted = cursor.rowcount
            await self._connection.commit()

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old history records")
        return deleted


# Global database instance
db = Database()
