"""Unit tests for SQLite StateStore."""

import tempfile
from pathlib import Path
from auto_uploader.state import StateStore


def test_state_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_state.db"
        store = StateStore(str(db_path))

        # Initially uninitialized and empty
        assert not store.is_initialized()
        assert not store.is_seen("tweet_100")
        assert len(store.get_seen_ids()) == 0

        # Mark seen
        store.mark_seen("tweet_100", source="twitter")
        assert store.is_seen("tweet_100")
        assert "tweet_100" in store.get_seen_ids()

        # Batch mark
        store.mark_seen_batch(["tweet_101", "tweet_102"], source="twitter")
        assert store.is_seen("tweet_101")
        assert store.is_seen("tweet_102")

        # Initialization flag
        store.set_initialized(True)
        assert store.is_initialized()

        # Metrics
        store.increment_stat("published", 2)
        store.increment_stat("checked", 5)
        stats = store.get_stats()
        assert stats.get("published") == 2
        assert stats.get("checked") == 5

        # Reset
        store.reset_history()
        assert not store.is_seen("tweet_100")
        assert not store.is_initialized()
