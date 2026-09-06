"""Instagram uploader using instagrapi.

Docs reference: https://github.com/subzeroid/instagrapi/tree/master/docs/usage-guide
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from ..models import MediaType, Post, UploadResult
from .base import BaseUploader

logger = logging.getLogger("auto_uploader.uploader.instagram")


class InstagramUploader(BaseUploader):
    """Handles publishing photos, carousels (albums), and videos to Instagram."""

    name: str = "instagram"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.username = str(self.config.get("username", "")).strip()
        self.password = str(self.config.get("password", "")).strip()
        self.session_id = str(self.config.get("session_id", "")).strip()
        self.session_file = Path(self.config.get("session_file", "data/instagram_session.json"))
        self.proxy = self.config.get("proxy")
        self.verification_code = self.config.get("verification_code")

        self.client = None
        self._logged_in = False

    def _ensure_client(self) -> Any:
        """Initialize instagrapi Client with session persistence."""
        if self.client and self._logged_in:
            return self.client

        try:
            from instagrapi import Client
        except ImportError:
            raise RuntimeError(
                "instagrapi is not installed. Install it via: pip install instagrapi"
            )

        cl = Client()

        # Configure proxy if specified
        if self.proxy:
            cl.set_proxy(self.proxy)

        self.session_file.parent.mkdir(parents=True, exist_ok=True)

        # 1. Try loading existing session
        if self.session_file.is_file():
            try:
                logger.info("Loading saved Instagram session from %s", self.session_file)
                cl.load_settings(str(self.session_file))
            except Exception as e:
                logger.warning("Failed to load existing session: %s. Proceeding to fresh login.", e)

        # 2. Authenticate
        try:
            if self.session_id:
                logger.info("Logging into Instagram using session ID...")
                cl.login_by_sessionid(self.session_id)
            elif self.username and self.password:
                logger.info("Logging into Instagram as '%s'...", self.username)
                cl.login(
                    username=self.username,
                    password=self.password,
                    verification_code=self.verification_code,
                )
                cl.dump_settings(str(self.session_file))
            else:
                raise ValueError(
                    "Instagram credentials missing: provide either session_id or username & password."
                )

            self.client = cl
            self._logged_in = True
            logger.info("Instagram authentication successful.")
            return self.client

        except Exception as e:
            self._logged_in = False
            raise RuntimeError(f"Instagram login failed: {e}")

    def test_connection(self) -> bool:
        """Verify Instagram login and account status."""
        try:
            cl = self._ensure_client()
            user_info = cl.account_info()
            logger.info("Logged in as: %s (pk: %s)", user_info.username, user_info.pk)
            return True
        except Exception as e:
            logger.error("Instagram connection test failed: %s", e)
            return False

    def _ensure_jpeg(self, file_path: Path) -> Path:
        """Ensure file is an RGB JPEG as required by Instagram."""
        ext = file_path.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            return file_path

        jpg_path = file_path.with_suffix(".jpg")
        with Image.open(file_path) as img:
            rgb_img = img.convert("RGB")
            rgb_img.save(jpg_path, "JPEG", quality=95)
        return jpg_path

    def upload_post(self, post: Post) -> UploadResult:
        """Upload post media to Instagram."""
        if not post.has_media:
            return UploadResult(
                success=False,
                error_message="Post has no media items to upload.",
            )

        cl = self._ensure_client()
        caption = post.caption or post.text or ""

        photo_paths: List[Path] = []
        video_paths: List[Path] = []

        for item in post.media_items:
            if not item.is_valid_local:
                continue
            if item.media_type == MediaType.PHOTO:
                photo_paths.append(self._ensure_jpeg(item.local_path))
            elif item.media_type == MediaType.VIDEO:
                video_paths.append(item.local_path)

        all_paths = photo_paths + video_paths
        if not all_paths:
            return UploadResult(
                success=False,
                error_message="No valid local media files found for post.",
            )

        try:
            # Case 1: Multiple items -> Album (Carousel)
            if len(all_paths) > 1:
                logger.info("Uploading album with %d items for post %s...", len(all_paths), post.id)
                media = cl.album_upload(
                    paths=all_paths,
                    caption=caption,
                )
                media_id = str(media.id if hasattr(media, "id") else media.pk)
                return UploadResult(success=True, media_id=media_id)

            # Case 2: Single Photo
            if len(photo_paths) == 1 and not video_paths:
                logger.info("Uploading single photo for post %s...", post.id)
                media = cl.photo_upload(
                    path=photo_paths[0],
                    caption=caption,
                )
                media_id = str(media.id if hasattr(media, "id") else media.pk)
                return UploadResult(success=True, media_id=media_id)

            # Case 3: Single Video (Clip / Reel / Video)
            if len(video_paths) == 1:
                logger.info("Uploading video for post %s...", post.id)
                try:
                    media = cl.clip_upload(
                        path=video_paths[0],
                        caption=caption,
                    )
                except Exception:
                    media = cl.video_upload(
                        path=video_paths[0],
                        caption=caption,
                    )
                media_id = str(media.id if hasattr(media, "id") else media.pk)
                return UploadResult(success=True, media_id=media_id)

            return UploadResult(success=False, error_message="Unsupported media combination.")

        except Exception as e:
            logger.error("Instagram upload failed for post %s: %s", post.id, e, exc_info=True)
            return UploadResult(success=False, error_message=str(e))
