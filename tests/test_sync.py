"""Unit tests for missing posts synchronization."""

import asyncio
from unittest.mock import MagicMock, AsyncMock
from auto_uploader.config import AppConfig
from auto_uploader.engine import AutoUploaderEngine
from auto_uploader.models import Post, MediaItem, MediaType, UploadResult


def test_sync_missing_posts_detection():
    async def _run():
        config = AppConfig()
        config.twitter.enabled = True
        config.instagram.username = "test_user"
        config.twitter.username = "test_twitter"

        engine = AutoUploaderEngine(config)

        # 1. Mock Instagram client with 1 post
        mock_media = MagicMock()
        mock_media.caption_text = "Artist: #artist1\n\nCute art\n\nSource: x.com/artist1/status/10001\n\n#anime"
        mock_media.taken_at.timestamp.return_value = 1700000000

        mock_client = MagicMock()
        mock_client.user_id_from_username.return_value = 12345
        mock_client.user_medias.return_value = [mock_media]

        engine.uploader._ensure_client = MagicMock(return_value=mock_client)

        # 2. Mock Twitter source with 2 reposts (one matching, one missing)
        post_on_ig = Post(
            id="10001",
            source_name="twitter",
            author_username="artist1",
            media_items=[MediaItem(media_type=MediaType.PHOTO, url="https://example.com/1.jpg")],
        )
        post_missing = Post(
            id="10002",
            source_name="twitter",
            author_username="artist2",
            media_items=[MediaItem(media_type=MediaType.PHOTO, url="https://example.com/2.jpg")],
        )

        mock_twitter = MagicMock()
        mock_twitter.name = "twitter"
        mock_twitter.fetch_new_posts.return_value = [post_on_ig, post_missing]
        engine.sources = [mock_twitter]

        # Test dry run
        summary = await engine.sync_missing_posts(twitter_limit=10, ig_limit=10, dry_run=True)

        assert summary["ig_posts_scanned"] == 1
        assert summary["ig_tweet_ids_found"] == 1
        assert len(summary["missing_posts"]) == 1
        assert summary["missing_posts"][0].id == "10002"
        assert summary["uploaded_count"] == 0

        # Test selected_ids filtering
        upload_sel_summary = await engine.sync_missing_posts(
            twitter_limit=10, ig_limit=10, dry_run=False, selected_ids=["99999"]
        )
        assert upload_sel_summary["uploaded_count"] == 0

    asyncio.run(_run())


def test_parse_selection():
    from run import parse_selection

    assert parse_selection("all", 5) == [1, 2, 3, 4, 5]
    assert parse_selection("", 5) == [1, 2, 3, 4, 5]
    assert parse_selection("1,3,5", 5) == [1, 3, 5]
    assert parse_selection("1-3, 5", 5) == [1, 2, 3, 5]
    assert parse_selection("2-4", 5) == [2, 3, 4]
    assert parse_selection("10", 5) == []
    assert parse_selection("q", 5) == []
    assert parse_selection("quit", 5) == []
