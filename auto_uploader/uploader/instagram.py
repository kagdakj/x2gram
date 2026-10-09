"""Instagram uploader using instagrapi.

Docs reference: https://github.com/subzeroid/instagrapi/tree/master/docs/usage-guide
"""

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from ..models import MediaType, Post, UploadResult
from .base import BaseUploader

logger = logging.getLogger("auto_uploader.uploader.instagram")


@dataclass
class InstagramMedia:
    """Minimal view of a published Instagram post."""
    pk: str
    code: str
    caption: str
    taken_at: float  # unix timestamp


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
        self.request_delay = (
            float(self.config.get("request_delay_min", 2) or 0),
            float(self.config.get("request_delay_max", 5) or 0),
        )
        self.device = self.config.get("device") or {}
        self.locale = str(self.config.get("locale") or "").strip()
        self.country = str(self.config.get("country") or "").strip()
        self.country_code = self.config.get("country_code")
        self.timezone_offset = self.config.get("timezone_offset")
        self.app_version = str(self.config.get("app_version") or "").strip()
        self.caption_repair_attempts = int(self.config.get("caption_repair_attempts", 1))
        self.caption_repair_delay = int(self.config.get("caption_repair_delay", 90))

        self._client = None
        self._logged_in = False

    def client(self) -> Any:
        """Logged-in instagrapi Client (logs in or restores the session on first use)."""
        return self._ensure_client()

    # ── session / device handling ─────────────────────────────────────────

    def _new_client(self) -> Any:
        try:
            from instagrapi import Client
        except ImportError:
            raise RuntimeError("instagrapi is not installed. Install it via: pip install instagrapi")

        cl = Client()
        if self.device:
            cl.set_device(dict(self.device), reset=True)
        return cl

    def _apply_overrides(self, cl: Any) -> None:
        """Settings that come from config, applied after the stored session is loaded."""
        if self.proxy:
            cl.set_proxy(self.proxy)
        low, high = self.request_delay
        if high > 0:
            cl.delay_range = [low, max(low, high)]

        # Only applied when explicitly configured; otherwise the saved values stay untouched.
        if self.locale:
            cl.set_locale(self.locale)
        if self.country:
            cl.set_country(self.country)
        if self.country_code is not None:
            cl.set_country_code(int(self.country_code))
        if self.timezone_offset is not None:
            cl.set_timezone_offset(int(self.timezone_offset))
        if self.app_version:
            self._pin_app_version(cl, self.app_version)

    @staticmethod
    def _pin_app_version(cl: Any, app_version: str) -> None:
        from instagrapi import config as ig_config

        app = ig_config.APP_SETTINGS.get(app_version)
        if not app:
            raise RuntimeError(
                f"Unknown instagram.app_version '{app_version}'. "
                f"Supported by this instagrapi: {', '.join(ig_config.APP_SETTINGS)}"
            )
        cl.device_settings.update(app)
        cl.set_user_agent()

    @staticmethod
    def app_version_of(settings: Optional[Dict[str, Any]]) -> Optional[str]:
        return ((settings or {}).get("device_settings") or {}).get("app_version")

    def _load_into(self, cl: Any) -> bool:
        """Restore the stored session, refreshing the Instagram app version it carries.

        Instagram rejects logins from an app version it considers outdated
        ("Your version of Instagram is out of date"). `override_app_version` keeps the
        uuids and the phone model but swaps in instagrapi's current app profile, which is
        what a real phone looks like after the app auto-updates.
        """
        before = self.app_version_of(self._load_settings())
        try:
            cl.load_settings(str(self.session_file), override_app_version=True)
        except TypeError:  # instagrapi < 3.0 has no override_app_version
            cl.load_settings(str(self.session_file))
        except Exception as e:
            logger.warning("Could not load saved session %s: %s", self.session_file, e)
            return False

        after = self.app_version_of(cl.get_settings())
        if before and after and before != after:
            logger.info("Instagram app profile upgraded %s -> %s (same device, same uuids).", before, after)
            self._save_settings(cl)
        return True

    def _load_settings(self) -> Optional[Dict[str, Any]]:
        if not self.session_file.is_file():
            return None
        try:
            return json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read saved session %s: %s", self.session_file, e)
            return None

    def _save_settings(self, cl: Any) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        if self.session_file.is_file():
            # Keep the previous device fingerprint recoverable.
            shutil.copyfile(self.session_file, self.session_file.with_suffix(".json.bak"))
        cl.dump_settings(str(self.session_file))
        device = cl.get_settings().get("device_settings", {})
        logger.info("Saved Instagram session to %s (device: %s %s)",
                    self.session_file, device.get("manufacturer", "?"), device.get("model", "?"))

    def _ensure_client(self) -> Any:
        """Return a logged-in client, reusing the stored session and device whenever possible."""
        if self._client and self._logged_in:
            return self._client

        cl = self._new_client()
        settings = self._load_into(cl) if self.session_file.is_file() else False
        self._apply_overrides(cl)

        # 1. Reuse the stored session without hitting the login endpoint.
        if settings:
            try:
                logger.info("Restoring Instagram session from %s", self.session_file)
                cl.get_timeline_feed()
                logger.info("✅ Session valid for @%s (no login call needed).", self.username or "cached session")
                self._client = cl
                self._logged_in = True
                return self._client
            except Exception as session_err:
                logger.warning("Saved session expired (%s). Re-authenticating with the SAME device...",
                               session_err)
                # Deliberately not deleting the file: the device identity in it must survive.

        # 2. Authenticate only when there is no usable session.
        try:
            if self.session_id:
                logger.info("Logging into Instagram using session ID...")
                cl.login_by_sessionid(self.session_id)
            elif self.username and self.password:
                logger.info("Logging into Instagram as '%s'...", self.username)
                cl.login(
                    username=self.username,
                    password=self.password,
                    relogin=bool(settings),
                    verification_code=self.verification_code or "",
                )
            else:
                raise ValueError(
                    "Instagram credentials missing: provide either session_id or username & password."
                )

            self._save_settings(cl)
            self._client = cl
            self._logged_in = True
            return self._client

        except Exception as e:
            self._logged_in = False
            err_msg = str(e)
            if "challenge_required" in err_msg.lower() or "feedback_required" in err_msg.lower():
                logger.error("🛑 Instagram security block triggered: %s. Log in via the official app to verify.",
                             err_msg)
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

    def list_recent_media(self, limit: int = 50) -> List[InstagramMedia]:
        """Most recent posts of the logged-in account."""
        cl = self._ensure_client()
        user_id = cl.user_id or cl.user_id_from_username(self.username)
        medias = cl.user_medias(user_id, amount=limit)
        return [
            InstagramMedia(
                pk=str(m.pk),
                code=m.code,
                caption=m.caption_text or "",
                taken_at=m.taken_at.timestamp(),
            )
            for m in medias
        ]

    def edit_caption(self, media_pk: str, caption: str) -> None:
        self._ensure_client().media_edit(str(media_pk), caption)

    def _repair_caption(self, cl: Any, media_id: str, caption: str) -> bool:
        """Push the caption back with media_edit.

        Kept deliberately small: a stripped caption already means Instagram is suspicious,
        and hammering media_edit right after the upload makes that worse. Whatever is left
        is repaired later by `tools fix-captions`.
        """
        for attempt in range(1, max(0, self.caption_repair_attempts) + 1):
            try:
                time.sleep(self.caption_repair_delay)
                cl.media_edit(media_id, caption)
                edited = cl.media_info(int(media_id))
                if (edited.caption_text or "").strip():
                    logger.info("✅ Caption restored for PK %s (attempt %d).", media_id, attempt)
                    return True
                logger.warning("media_edit accepted but caption still empty for PK %s.", media_id)
            except Exception as edit_err:
                logger.warning("media_edit attempt %d failed for PK %s: %s", attempt, media_id, edit_err)
                if "feedback_required" in str(edit_err).lower():
                    logger.error("🛑 Instagram action block during caption repair. Giving up for now.")
                    break
        logger.info("Caption of PK %s left empty; run `tools fix-captions` later.", media_id)
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

            # Detect if Instagram accepted the media but silently stripped the caption
            actual_caption = getattr(media_obj, "caption_text", None)
            if caption.strip() and not actual_caption:
                logger.warning(
                    "⚠️ Instagram uploaded media %s (PK: %s) but stripped the caption.",
                    post.id, media_id,
                )
                restored = self._repair_caption(cl, media_id, caption)
                return UploadResult(
                    success=True,
                    media_id=media_id,
                    extra={"caption_stripped": not restored, "caption_restored": restored},
                )

            return UploadResult(success=True, media_id=media_id)

        except Exception as e:
            logger.error("Instagram upload failed for post %s: %s", post.id, e, exc_info=True)
            return UploadResult(success=False, error_message=str(e))
