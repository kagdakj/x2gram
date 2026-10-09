"""Persistent state (SQLite).

A single `posts` table is the source of truth for every post the app knows about.
Its `status` column follows `PostStatus`: queued -> uploading -> uploaded | failed,
or skipped for posts that were intentionally never uploaded (first-run baseline).
"""

import calendar
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set

from .models import Post, PostStatus

logger = logging.getLogger("auto_uploader.store")

SCHEMA_VERSION = "2"


@dataclass
class PostRecord:
    post_id: str
    source: str
    status: PostStatus
    url: Optional[str]
    author: Optional[str]
    media_count: int
    attempts: int
    last_error: Optional[str]
    media_id: Optional[str]
    created_at: str
    updated_at: str
    uploaded_at: Optional[str]
    post_json: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PostRecord":
        return cls(
            post_id=row["post_id"],
            source=row["source"],
            status=PostStatus(row["status"]),
            url=row["url"],
            author=row["author"],
            media_count=row["media_count"] or 0,
            attempts=row["attempts"] or 0,
            last_error=row["last_error"],
            media_id=row["media_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            uploaded_at=row["uploaded_at"],
            post_json=row["post_json"],
        )

    def post(self) -> Optional[Post]:
        """Rebuild the stored Post, or None if this record has no payload."""
        if not self.post_json:
            return None
        return Post.from_dict(json.loads(self.post_json))

    @property
    def link(self) -> str:
        if self.url:
            return self.url
        if self.source == "twitter":
            return f"https://x.com/i/status/{self.post_id}"
        return ""


def _utc_to_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return None


class Store:
    """SQLite-backed storage. Opens a short-lived connection per call (thread-safe)."""

    def __init__(self, db_path: str = "data/uploader.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── plumbing ──────────────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    def _execute(self, sql: str, params: Sequence = ()) -> int:
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    def _query(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    post_id     TEXT PRIMARY KEY,
                    source      TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    url         TEXT,
                    author      TEXT,
                    media_count INTEGER DEFAULT 0,
                    post_json   TEXT,
                    attempts    INTEGER DEFAULT 0,
                    last_error  TEXT,
                    media_id    TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    uploaded_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
                CREATE TABLE IF NOT EXISTS state_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    key TEXT PRIMARY KEY,
                    value INTEGER DEFAULT 0
                );
            """)
            for metric in ("checked", "published", "errors"):
                conn.execute("INSERT OR IGNORE INTO metrics (key, value) VALUES (?, 0)", (metric,))
            self._migrate_legacy(conn)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_legacy(self, conn: sqlite3.Connection) -> None:
        """Fold older `seen_posts` + `upload_queue` tables into `posts`, then rename them.

        Old databases differ in which columns exist (e.g. `seen_posts.url` was added late),
        so every optional column is resolved against the actual schema.
        """
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        if "seen_posts" in tables:
            cols = self._columns(conn, "seen_posts")
            url = "url" if "url" in cols else "NULL"
            source = ("CASE WHEN source = 'baseline' THEN 'twitter' ELSE source END"
                      if "source" in cols else "'twitter'")
            seen_at = "seen_at" if "seen_at" in cols else "CURRENT_TIMESTAMP"
            # Without a status column every row is a post we already published.
            uploaded = "status IN ('uploaded', 'success')" if "status" in cols else "1"
            conn.execute(f"""
                INSERT OR IGNORE INTO posts (post_id, source, status, url, created_at, updated_at, uploaded_at)
                SELECT post_id,
                       {source},
                       CASE WHEN {uploaded} THEN 'uploaded' ELSE 'skipped' END,
                       {url}, {seen_at}, {seen_at},
                       CASE WHEN {uploaded} THEN {seen_at} END
                FROM seen_posts
            """)
            conn.execute("ALTER TABLE seen_posts RENAME TO legacy_seen_posts")

        if "upload_queue" in tables:
            cols = self._columns(conn, "upload_queue")
            source_name = "source_name" if "source_name" in cols else "'twitter'"
            status = "status" if "status" in cols else "'pending'"
            added_at = "added_at" if "added_at" in cols else "CURRENT_TIMESTAMP"
            rows = conn.execute(
                f"""SELECT post_id, {source_name} AS source_name, post_json,
                           {status} AS status, {added_at} AS added_at
                    FROM upload_queue"""
            ).fetchall()
            status_map = {"pending": "queued", "failed": "failed", "done": "skipped"}
            for row in rows:
                author, url, media_count = None, None, 0
                try:
                    post = Post.from_dict(json.loads(row["post_json"]))
                    author, url, media_count = post.author_username, post.link, len(post.media_items)
                except Exception:
                    logger.warning("Could not parse queued payload of post %s during migration", row["post_id"])
                # Existing (seen) rows keep their status but gain the payload.
                conn.execute(
                    """UPDATE posts SET post_json = ?, author = COALESCE(author, ?),
                       url = COALESCE(url, ?), media_count = ? WHERE post_id = ?""",
                    (row["post_json"], author, url, media_count, row["post_id"]),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO posts
                       (post_id, source, status, url, author, media_count, post_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["post_id"], row["source_name"], status_map.get(row["status"], "queued"),
                     url, author, media_count, row["post_json"], row["added_at"], row["added_at"]),
                )
            conn.execute("ALTER TABLE upload_queue RENAME TO legacy_upload_queue")

        if tables & {"seen_posts", "upload_queue"}:
            # An existing v1 install is already past its first run; don't baseline-skip again.
            conn.execute("INSERT OR IGNORE INTO state_kv (key, value) VALUES ('initialized', '1')")

        conn.execute(
            "INSERT OR REPLACE INTO state_kv (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )

    # ── posts ─────────────────────────────────────────────────────────────

    def add_posts(self, posts: Iterable[Post], status: PostStatus = PostStatus.QUEUED) -> int:
        """Insert posts that are not yet known. Returns number inserted."""
        rows = [
            (p.id, p.source_name, status.value, p.link or None, p.author_username,
             len(p.media_items), json.dumps(p.to_dict(), ensure_ascii=False))
            for p in posts
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT OR IGNORE INTO posts
                   (post_id, source, status, url, author, media_count, post_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return conn.total_changes - before

    def known_ids(self, ids: Iterable[str]) -> Set[str]:
        ids = [str(i) for i in ids]
        if not ids:
            return set()
        found: Set[str] = set()
        # Chunk to stay below SQLite's variable limit.
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found.update(r[0] for r in self._query(f"SELECT post_id FROM posts WHERE post_id IN ({marks})", chunk))
        return found

    def get(self, post_id: str) -> Optional[PostRecord]:
        rows = self._query("SELECT * FROM posts WHERE post_id = ?", (str(post_id),))
        return PostRecord.from_row(rows[0]) if rows else None

    def list(
        self,
        statuses: Optional[Iterable[PostStatus]] = None,
        limit: Optional[int] = None,
        newest_first: bool = False,
    ) -> List[PostRecord]:
        sql = "SELECT * FROM posts"
        params: List = []
        if statuses:
            values = [PostStatus(s).value for s in statuses]
            sql += f" WHERE status IN ({','.join('?' * len(values))})"
            params.extend(values)
        if newest_first:
            sql += " ORDER BY COALESCE(uploaded_at, updated_at) DESC, CAST(post_id AS INTEGER) DESC"
        else:
            # Discovery order, then Snowflake order (chronological for tweets).
            sql += " ORDER BY created_at ASC, CAST(post_id AS INTEGER) ASC, post_id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [PostRecord.from_row(r) for r in self._query(sql, params)]

    def next_queued(self, limit: int = 1) -> List[PostRecord]:
        return self.list([PostStatus.QUEUED], limit=limit)

    def counts(self) -> Dict[PostStatus, int]:
        result = {s: 0 for s in PostStatus}
        for row in self._query("SELECT status, COUNT(*) AS n FROM posts GROUP BY status"):
            result[PostStatus(row["status"])] = row["n"]
        return result

    def mark_uploading(self, post_id: str) -> None:
        self._execute(
            """UPDATE posts SET status = 'uploading', attempts = attempts + 1,
               updated_at = CURRENT_TIMESTAMP WHERE post_id = ?""",
            (post_id,),
        )

    def mark_uploaded(self, post_id: str, media_id: Optional[str] = None) -> None:
        self._execute(
            """UPDATE posts SET status = 'uploaded', media_id = ?, last_error = NULL,
               uploaded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE post_id = ?""",
            (media_id, post_id),
        )

    def mark_failed(self, post_id: str, error: str) -> None:
        self._execute(
            """UPDATE posts SET status = 'failed', last_error = ?,
               updated_at = CURRENT_TIMESTAMP WHERE post_id = ?""",
            (error[:1000], post_id),
        )

    def set_status(self, post_id: str, status: PostStatus) -> None:
        self._execute(
            "UPDATE posts SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE post_id = ?",
            (PostStatus(status).value, str(post_id)),
        )

    def mark_found_on_instagram(self, post_ids: Iterable[str]) -> int:
        """Mark posts already present on Instagram as uploaded (without touching rate-limit timestamps)."""
        count = 0
        for pid in post_ids:
            count += self._execute(
                """UPDATE posts SET status = 'uploaded', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE post_id = ? AND status != 'uploaded' AND status != 'uploading'""",
                (str(pid),),
            )
        return count

    def requeue(self, post_ids: Optional[Iterable[str]] = None) -> int:
        """Move failed/skipped posts (all failed ones when no ids given) back to the queue."""
        if post_ids is None:
            return self._execute(
                "UPDATE posts SET status = 'queued', updated_at = CURRENT_TIMESTAMP WHERE status = 'failed'"
            )
        count = 0
        for pid in post_ids:
            count += self._execute(
                """UPDATE posts SET status = 'queued', updated_at = CURRENT_TIMESTAMP
                   WHERE post_id = ? AND status IN ('failed', 'skipped')""",
                (str(pid),),
            )
        return count

    def recover_stale_uploads(self) -> int:
        """Posts left in 'uploading' by a crashed process become failed.

        Not re-queued automatically: the upload may have reached Instagram before the crash.
        """
        return self._execute(
            """UPDATE posts SET status = 'failed', updated_at = CURRENT_TIMESTAMP,
               last_error = 'Interrupted during upload. Check Instagram (or run sync) before retrying.'
               WHERE status = 'uploading'"""
        )

    def delete(self, post_ids: Iterable[str]) -> int:
        count = 0
        for pid in post_ids:
            count += self._execute(
                "DELETE FROM posts WHERE post_id = ? AND status != 'uploading'", (str(pid),)
            )
        return count

    def clear(self, status: Optional[PostStatus] = None) -> int:
        """Delete queued/failed entries (never upload history unless explicitly requested)."""
        if status is None:
            return self._execute("DELETE FROM posts WHERE status IN ('queued', 'failed')")
        return self._execute("DELETE FROM posts WHERE status = ?", (PostStatus(status).value,))

    def reset_history(self) -> int:
        """Forget uploaded/skipped history and the baseline flag. Queue is kept."""
        count = self._execute("DELETE FROM posts WHERE status IN ('uploaded', 'skipped')")
        self._execute("DELETE FROM state_kv WHERE key = 'initialized'")
        return count

    # ── rate-limit helpers ────────────────────────────────────────────────

    def uploads_since(self, hours: int = 24) -> int:
        rows = self._query(
            "SELECT COUNT(*) FROM posts WHERE uploaded_at IS NOT NULL AND uploaded_at >= datetime('now', ?)",
            (f"-{hours} hours",),
        )
        return int(rows[0][0])

    def last_upload_time(self) -> Optional[float]:
        rows = self._query("SELECT MAX(uploaded_at) FROM posts WHERE uploaded_at IS NOT NULL")
        return _utc_to_epoch(rows[0][0])

    def upload_timeline(self, source: Optional[str] = None) -> List[tuple]:
        """[(post_id, uploaded_epoch)] for timestamp-based matching against Instagram."""
        sql = "SELECT post_id, uploaded_at FROM posts WHERE uploaded_at IS NOT NULL"
        params: List = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        return [(r[0], _utc_to_epoch(r[1])) for r in self._query(sql, params)]

    # ── key/value + metrics ───────────────────────────────────────────────

    def is_initialized(self) -> bool:
        rows = self._query("SELECT value FROM state_kv WHERE key = 'initialized'")
        return bool(rows and rows[0][0] == "1")

    def set_initialized(self, value: bool = True) -> None:
        self._execute(
            "INSERT OR REPLACE INTO state_kv (key, value) VALUES ('initialized', ?)",
            ("1" if value else "0",),
        )

    def get_stats(self) -> Dict[str, int]:
        return {r["key"]: int(r["value"]) for r in self._query("SELECT key, value FROM metrics")}

    def increment_stat(self, key: str, count: int = 1) -> None:
        self._execute(
            "INSERT INTO metrics (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = value + ?",
            (key, count, count),
        )
