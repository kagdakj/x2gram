"""Persistent state database using SQLite."""

import sqlite3
from pathlib import Path
from typing import Dict, List, Set


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
                    status TEXT DEFAULT 'success'
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

    def mark_seen(self, post_id: str, source: str, status: str = "success") -> None:
        """Record post ID as seen."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO seen_posts (post_id, source, status)
                VALUES (?, ?, ?)
                """,
                (str(post_id), source, status),
            )
            conn.commit()

    def mark_seen_batch(self, post_ids: List[str], source: str) -> None:
        """Record multiple post IDs in a single transaction."""
        if not post_ids:
            return
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR IGNORE INTO seen_posts (post_id, source, status)
                VALUES (?, ?, 'baseline')
                """,
                [(str(pid), source) for pid in post_ids],
            )
            conn.commit()

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

    def reset_history(self) -> None:
        """Clear all seen posts and reinitialize baseline flag."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM seen_posts")
            cursor.execute("DELETE FROM state_kv WHERE key = 'initialized'")
            conn.commit()
