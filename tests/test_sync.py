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

        # Test actual upload run
        engine.process_and_upload = AsyncMock(return_value=UploadResult(success=True, media_id="ig_999"))
        upload_summary = await engine.sync_missing_posts(twitter_limit=10, ig_limit=10, dry_run=False)

        assert upload_summary["uploaded_count"] == 1
        assert upload_summary["failed_count"] == 0
        engine.process_and_upload.assert_awaited_once_with(post_missing)

    asyncio.run(_run())
