"""Persistent state database using SQLite."""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


class StateStore:
    """Thread-safe SQLite storage for seen posts, metrics, and state."""

    def __init__(self, db_path: str = "data/uploader.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create necessary tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    post_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'success',
                    url TEXT
                )
            """)
            # Ensure url column exists in existing DBs
            cursor.execute("PRAGMA table_info(seen_posts)")
            cols = [col[1] for col in cursor.fetchall()]
            if "url" not in cols:
                cursor.execute("ALTER TABLE seen_posts ADD COLUMN url TEXT")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS upload_queue (
                    post_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    post_json TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS state_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    key TEXT PRIMARY KEY,
                    value INTEGER DEFAULT 0
                )
            """)
            # Initialize metrics if not present
            for metric in ("checked", "published", "errors"):
                cursor.execute(
                    "INSERT OR IGNORE INTO metrics (key, value) VALUES (?, 0)",
                    (metric,),
                )
            conn.commit()

    def is_seen(self, post_id: str) -> bool:
        """Check if post ID has already been recorded."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_posts WHERE post_id = ?", (str(post_id),))
            return cursor.fetchone() is not None

    def get_seen_ids(self) -> Set[str]:
        """Return all seen post IDs as a set."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT post_id FROM seen_posts")
            return {row[0] for row in cursor.fetchall()}

    def mark_seen(self, post_id: str, source: str, status: str = "success", url: Optional[str] = None) -> None:
        """Record post ID as seen with optional URL."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO seen_posts (post_id, source, status, url)
                VALUES (?, ?, ?, ?)
                """,
                (str(post_id), source, status, url),
            )
            conn.commit()

    def mark_seen_batch(self, items: List[Any], source: str) -> None:
        """Record multiple post IDs in a single transaction. items can be list of IDs or (id, url) tuples."""
        if not items:
            return
        batch_tuples = []
        for item in items:
            if isinstance(item, tuple):
                pid, url = item
                batch_tuples.append((str(pid), source, str(url) if url else None))
            else:
                batch_tuples.append((str(item), source, None))

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR IGNORE INTO seen_posts (post_id, source, status, url)
                VALUES (?, ?, 'baseline', ?)
                """,
                batch_tuples,
            )
            conn.commit()

    def get_seen_posts_details(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent seen posts with status and saved URLs."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT post_id, source, status, seen_at, url
                FROM seen_posts
                ORDER BY seen_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def is_initialized(self) -> bool:
        """Check if the service baseline has been established."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM state_kv WHERE key = 'initialized'")
            row = cursor.fetchone()
            return bool(row and row[0] == "1")

    def set_initialized(self, value: bool = True) -> None:
        """Set baseline initialization flag."""
        val_str = "1" if value else "0"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO state_kv (key, value) VALUES ('initialized', ?)",
                (val_str,),
            )
            conn.commit()

    def get_stats(self) -> Dict[str, int]:
        """Retrieve current metrics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM metrics")
            return {row["key"]: int(row["value"]) for row in cursor.fetchall()}

    def increment_stat(self, key: str, count: int = 1) -> None:
        """Increment a counter metric."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO metrics (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = value + ?
                """,
                (key, count, count),
            )
            conn.commit()

    def get_uploads_in_last_hours(self, hours: int = 24) -> int:
        """Count how many posts were uploaded within the last N hours."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM seen_posts
                WHERE status = 'uploaded'
                AND datetime(seen_at) >= datetime('now', ?)
                """,
                (f"-{hours} hours",),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_last_upload_time(self) -> Optional[float]:
        """Get unix timestamp of the most recent successful upload."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT strftime('%s', seen_at) FROM seen_posts
                WHERE status = 'uploaded'
                ORDER BY seen_at DESC LIMIT 1
                """
            )
            row = cursor.fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def reset_history(self) -> None:
        """Clear all seen posts and reinitialize baseline flag."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM seen_posts")
            cursor.execute("DELETE FROM state_kv WHERE key = 'initialized'")
            conn.commit()

    # ── Upload Queue Methods ──────────────────────────────────────────────

    def queue_has(self, post_id: str) -> bool:
        """Check if post is already in upload queue (any status)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM upload_queue WHERE post_id = ?", (str(post_id),))
            return cursor.fetchone() is not None

    def enqueue_post(self, post_id: str, source_name: str, post_json: str) -> bool:
        """Add a post to queue. Returns True if newly inserted, False if already exists."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO upload_queue (post_id, source_name, post_json) VALUES (?, ?, ?)",
                (str(post_id), source_name, post_json),
            )
            conn.commit()
            return cursor.rowcount > 0

    def enqueue_batch(self, items: list) -> int:
        """Bulk enqueue. items = list of (post_id, source_name, post_json). Returns count inserted."""
        if not items:
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                "INSERT OR IGNORE INTO upload_queue (post_id, source_name, post_json) VALUES (?, ?, ?)",
                [(str(pid), src, pj) for pid, src, pj in items],
            )
            conn.commit()
            return cursor.rowcount

    def pop_pending(self, limit: int = 1) -> list:
        """Get oldest pending queue entries (FIFO by Snowflake ID). Returns list of sqlite3.Row."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Sort by numeric post_id (Twitter Snowflake = chronological)
            cursor.execute(
                """
                SELECT post_id, source_name, post_json, added_at
                FROM upload_queue
                WHERE status = 'pending'
                ORDER BY CAST(post_id AS INTEGER) ASC
                LIMIT ?
                """,
                (limit,),
            )
            return cursor.fetchall()

    def mark_queue_status(self, post_id: str, status: str) -> None:
        """Update queue entry status (e.g. 'done', 'failed')."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE upload_queue SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE post_id = ?",
                (status, str(post_id)),
            )
            conn.commit()

    def queue_size(self, status: str = "pending") -> int:
        """Count queue entries by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM upload_queue WHERE status = ?",
                (status,),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_queue_all(self, status: str = None) -> list:
        """Get all queue entries, optionally filtered by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT post_id, source_name, post_json, status, added_at FROM upload_queue WHERE status = ? ORDER BY CAST(post_id AS INTEGER) ASC",
                    (status,),
                )
            else:
                cursor.execute(
                    "SELECT post_id, source_name, post_json, status, added_at FROM upload_queue ORDER BY CAST(post_id AS INTEGER) ASC"
                )
            return cursor.fetchall()

    def clear_queue(self, status: str = None) -> int:
        """Delete queue entries. If status given, only that status. Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("DELETE FROM upload_queue WHERE status = ?", (status,))
            else:
                cursor.execute("DELETE FROM upload_queue")
            conn.commit()
            return cursor.rowcount
