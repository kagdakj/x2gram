import sqlite3
from pathlib import Path
from typing import List, Optional

import pytest
from PIL import Image

from auto_uploader.config import AppConfig
from auto_uploader.context import AppContext
from auto_uploader.models import MediaItem, MediaType, Post, UploadResult
from auto_uploader.sources.base import BaseSource
from auto_uploader.uploader.base import BaseUploader
from auto_uploader.uploader.instagram import InstagramMedia


class FakeSource(BaseSource):
    name = "twitter"

    def __init__(self, posts: Optional[List[Post]] = None):
        super().__init__({})
        self.posts = posts or []
        self.calls = []

    def fetch_new_posts(self, limit: int = 20) -> List[Post]:
        self.calls.append(limit)
        return list(self.posts)

    def fetch(self, deep: bool = False) -> List[Post]:
        return self.fetch_new_posts(200 if deep else 20)


class FakeUploader(BaseUploader):
    name = "fake"

    def __init__(self):
        super().__init__({})
        self.uploaded: List[Post] = []
        self.fail_with: Optional[str] = None
        self.caption_stripped = False
        self.media: List[InstagramMedia] = []
        self.edits = []

    def upload_post(self, post: Post) -> UploadResult:
        if self.fail_with:
            return UploadResult(success=False, error_message=self.fail_with)
        assert all(m.is_valid_local for m in post.media_items)
        self.uploaded.append(post)
        return UploadResult(success=True, media_id=f"ig_{post.id}",
                            extra={"caption_stripped": self.caption_stripped})

    def test_connection(self) -> bool:
        return True

    def list_recent_media(self, limit: int = 50):
        return self.media[:limit]

    def edit_caption(self, pk, caption):
        self.edits.append((pk, caption))


def make_image(path: Path, size=(200, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color="blue").save(path)
    return path


def tweet(post_id: str, image: Path, author: str = "artist") -> Post:
    # A local file stands in for the remote URL so no network is needed.
    return Post(
        id=post_id, source_name="twitter", author_username=author, text=f"art {post_id}",
        url=f"https://x.com/{author}/status/{post_id}",
        media_items=[MediaItem(media_type=MediaType.PHOTO, local_path=image, original_path=image)],
    )


@pytest.fixture
def app_ctx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = AppConfig()
    cfg.service.db_path = str(tmp_path / "data/test.db")
    cfg.service.temp_dir = str(tmp_path / "data/tmp")
    cfg.service.post_delay_min = 0
    cfg.service.post_delay_max = 0
    cfg.plugins.anime_crop.enabled = False
    cfg.twitter.enabled = False
    cfg.local.enabled = False
    ctx = AppContext(cfg)
    source = FakeSource()
    uploader = FakeUploader()
    ctx.__dict__["sources"] = [source]
    ctx.__dict__["uploader"] = uploader
    return ctx, source, uploader
