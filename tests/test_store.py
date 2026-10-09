import sqlite3

from auto_uploader.models import Post, PostStatus
from auto_uploader.store import Store


def test_lifecycle(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    posts = [Post(id=str(i), source_name="twitter", author_username="a") for i in (30, 10, 20)]
    assert store.add_posts(posts) == 3
    assert store.add_posts(posts) == 0  # idempotent

    # Same discovery time -> Snowflake order
    assert [r.post_id for r in store.next_queued(3)] == ["10", "20", "30"]

    store.mark_uploading("10")
    assert store.get("10").attempts == 1
    store.mark_failed("10", "boom")
    assert store.get("10").status == PostStatus.FAILED
    assert store.get("10").last_error == "boom"

    store.mark_uploaded("20", "ig1")
    assert store.uploads_since(24) == 1
    assert store.last_upload_time() is not None

    assert store.requeue() == 1
    assert store.get("10").status == PostStatus.QUEUED

    store.mark_uploading("30")
    assert store.recover_stale_uploads() == 1
    assert store.get("30").status == PostStatus.FAILED  # may already be on Instagram
    assert "Interrupted" in store.get("30").last_error
    store.requeue(["30"])

    counts = store.counts()
    assert counts[PostStatus.QUEUED] == 2 and counts[PostStatus.UPLOADED] == 1

    assert store.mark_found_on_instagram(["10"]) == 1
    assert store.get("10").uploaded_at is None  # doesn't count toward rate limit
    assert store.uploads_since(24) == 1

    store.set_initialized(True)
    assert store.reset_history() == 2
    assert not store.is_initialized()
    assert store.counts()[PostStatus.QUEUED] == 1


def test_migration_from_oldest_schema(tmp_path):
    """seen_posts without url/status columns (pre-v1 databases)."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE seen_posts (post_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                                 seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'success');
        CREATE TABLE upload_queue (post_id TEXT PRIMARY KEY, source_name TEXT NOT NULL, post_json TEXT NOT NULL,
                                   status TEXT DEFAULT 'pending', added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                   updated_at TIMESTAMP);
        CREATE TABLE state_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO seen_posts (post_id, source, status) VALUES ('1', 'twitter', 'uploaded'), ('9', 'local', 'success');
        INSERT INTO upload_queue (post_id, source_name, post_json, status) VALUES
          ('1', 'twitter', '{"id": "1", "source_name": "twitter", "author_username": "a", "url": "https://x.com/a/status/1", "media_items": []}', 'done'),
          ('2', 'twitter', '{"id": "2", "source_name": "twitter", "author_username": "b", "url": "https://x.com/b/status/2", "media_items": [{"url": "u", "media_type": "photo"}]}', 'pending'),
          ('3', 'twitter', 'not json', 'failed');
    """)
    con.commit()
    con.close()

    store = Store(str(db))
    counts = store.counts()
    assert counts[PostStatus.UPLOADED] == 2 and counts[PostStatus.QUEUED] == 1 and counts[PostStatus.FAILED] == 1
    # The queue payload backfills what seen_posts could not store.
    assert store.get("1").url == "https://x.com/a/status/1" and store.get("1").author == "a"
    assert store.get("2").status == PostStatus.QUEUED and store.get("2").media_count == 1
    assert store.get("3").status == PostStatus.FAILED  # unparsable payload is kept as-is
    assert store.is_initialized()


def test_legacy_migration(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE seen_posts (post_id TEXT PRIMARY KEY, source TEXT, seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                 status TEXT, url TEXT);
        CREATE TABLE upload_queue (post_id TEXT PRIMARY KEY, source_name TEXT, post_json TEXT, status TEXT,
                                   added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP);
        CREATE TABLE state_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO seen_posts (post_id, source, status) VALUES ('1', 'twitter', 'uploaded'), ('2', 'baseline', 'baseline');
        INSERT INTO upload_queue (post_id, source_name, post_json, status) VALUES
          ('3', 'twitter', '{"id": "3", "author_username": "x", "media_items": [{"url": "u", "media_type": "photo"}], "raw_data": {"a": 1}}', 'pending'),
          ('1', 'twitter', '{"id": "1"}', 'done');
    """)
    con.commit()
    con.close()

    store = Store(str(db))
    assert store.get("1").status == PostStatus.UPLOADED
    assert store.get("2").status == PostStatus.SKIPPED
    queued = store.get("3")
    assert queued.status == PostStatus.QUEUED and queued.author == "x" and queued.media_count == 1
    assert queued.post().media_items[0].url == "u"
    assert store.is_initialized()
    Store(str(db))  # re-open is a no-op
    assert store.counts()[PostStatus.QUEUED] == 1
