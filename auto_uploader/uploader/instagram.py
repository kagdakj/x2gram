"""Instagram uploader using instagrapi.

Docs reference: https://github.com/subzeroid/instagrapi/tree/master/docs/usage-guide
"""

import logging
import time
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
                try:
                    cl.login_by_sessionid(self.session_id)
                except Exception as sess_err:
                    if self.session_file.is_file():
                        logger.warning("Session ID failed with cached session: %s. Clearing cache...", sess_err)
                        self.session_file.unlink(missing_ok=True)
                        cl = Client()
                        if self.proxy:
                            cl.set_proxy(self.proxy)
                        cl.login_by_sessionid(self.session_id)
                    else:
                        raise sess_err
            elif self.username and self.password:
                logger.info("Logging into Instagram as '%s'...", self.username)
                try:
                    cl.login(
                        username=self.username,
                        password=self.password,
                        verification_code=self.verification_code,
                    )
                except Exception as login_err:
                    # If old session belonged to another account or is expired, clear and retry fresh
                    if self.session_file.is_file():
                        logger.warning("Login failed with cached session (%s). Clearing old session and retrying fresh...", login_err)
                        self.session_file.unlink(missing_ok=True)
                        cl = Client()
                        if self.proxy:
                            cl.set_proxy(self.proxy)
                        cl.login(
                            username=self.username,
                            password=self.password,
                            verification_code=self.verification_code,
                        )
                    else:
                        raise login_err

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
            ext = item.local_path.suffix.lower()
            if item.media_type == MediaType.VIDEO or ext in (".mp4", ".mov", ".avi", ".webm"):
                video_paths.append(item.local_path)
            else:
                photo_paths.append(self._ensure_jpeg(item.local_path))

        all_paths = photo_paths + video_paths
        if not all_paths:
            return UploadResult(
                success=False,
                error_message="No valid local media files found for post.",
            )

        try:
            media_obj = None
            # Case 1: Multiple items -> Album (Carousel)
            if len(all_paths) > 1:
                logger.info("Uploading album with %d items for post %s...", len(all_paths), post.id)
                media_obj = cl.album_upload(
                    paths=all_paths,
                    caption=caption,
                )

            # Case 2: Single Photo
            elif len(photo_paths) == 1 and not video_paths:
                logger.info("Uploading single photo for post %s...", post.id)
                media_obj = cl.photo_upload(
                    path=photo_paths[0],
                    caption=caption,
                )

            # Case 3: Single Video (Clip / Reel / Video)
            elif len(video_paths) == 1:
                logger.info("Uploading video for post %s...", post.id)
                try:
                    media_obj = cl.clip_upload(
                        path=video_paths[0],
                        caption=caption,
                    )
                except Exception:
                    media_obj = cl.video_upload(
                        path=video_paths[0],
                        caption=caption,
                    )
            else:
                return UploadResult(success=False, error_message="Unsupported media combination.")

            media_id = str(media_obj.id if hasattr(media_obj, "id") else media_obj.pk)

            # Detect if Instagram accepted media upload but stripped the caption
            actual_caption = getattr(media_obj, "caption_text", None)
            if caption.strip() and not actual_caption:
                logger.warning(
                    "⚠️ Instagram uploaded media %s (PK: %s) but silently stripped caption. "
                    "Attempting to restore via media_edit...",
                    post.id,
                    media_id,
                )

                # Retry: push caption back via media_edit with backoff
                caption_restored = False
                for attempt, wait in enumerate([5, 15, 30], 1):
                    try:
                        time.sleep(wait)
                        cl.media_edit(media_id, caption)
                        # Verify it stuck
                        edited = cl.media_info(int(media_id))
                        if (edited.caption_text or "").strip():
                            logger.info(
                                "✅ Caption restored on attempt %d for PK %s via media_edit.",
                                attempt, media_id,
                            )
                            caption_restored = True
                            break
                        else:
                            logger.warning(
                                "media_edit attempt %d accepted but caption still empty for PK %s.",
                                attempt, media_id,
                            )
                    except Exception as edit_err:
                        logger.warning(
                            "media_edit attempt %d failed for PK %s: %s",
                            attempt, media_id, edit_err,
                        )
                        if "feedback_required" in str(edit_err).lower():
                            logger.error(
                                "🛑 Instagram action block active. Stopping caption retry."
                            )
                            break

                return UploadResult(
                    success=True,
                    media_id=media_id,
                    extra={"caption_stripped": not caption_restored, "caption_restored": caption_restored},
                )

            return UploadResult(success=True, media_id=media_id)

        except Exception as e:
            logger.error("Instagram upload failed for post %s: %s", post.id, e, exc_info=True)
            return UploadResult(success=False, error_message=str(e))
