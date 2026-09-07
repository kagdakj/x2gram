"""Twitter/X CLI post source.

Fetches posts or reposts from Twitter/X using the twitter-cli tool.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Tuple

from ..models import MediaItem, MediaType, Post
from .base import BaseSource

logger = logging.getLogger("auto_uploader.sources.twitter")


class TwitterCliSource(BaseSource):
    """Source that extracts posts and reposts using twitter-cli."""

    name: str = "twitter"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.username = str(self.config.get("username", "")).strip().lstrip("@")
        self.auth_token = str(self.config.get("auth_token", "")).strip()
        self.ct0 = str(self.config.get("ct0", "")).strip()
        self.initial_load = int(self.config.get("initial_load", 20))
        self.only_reposts = bool(self.config.get("only_reposts", True))
        self.custom_binary = self.config.get("binary_path")

    def _find_binary(self) -> str:
        """Locate twitter CLI binary."""
        if self.custom_binary and os.path.isfile(self.custom_binary):
            return self.custom_binary

        binary = shutil.which("twitter")
        if binary:
            return binary

        virtual_env = os.environ.get("VIRTUAL_ENV")
        if virtual_env:
            candidate = os.path.join(virtual_env, "bin", "twitter")
            if os.path.isfile(candidate):
                return candidate

        return "twitter"

    def mask_secrets(self, text: str) -> str:
        """Mask sensitive authentication cookies in logs and errors."""
        if not text:
            return ""
        for secret in (self.auth_token, self.ct0):
            if secret and len(secret) > 4:
                text = text.replace(secret, "***")
        text = re.sub(r"(auth_token=)[^;\s]+", r"\1***", text, flags=re.I)
        text = re.sub(r"(ct0=)[^;\s]+", r"\1***", text, flags=re.I)
        return text

    def _run_cli(self, *args: str) -> Tuple[str, str]:
        """Execute twitter CLI command."""
        if not self.username:
            raise ValueError("Twitter username is not configured.")
        if not self.auth_token or not self.ct0:
            raise ValueError("Twitter auth_token or ct0 cookies are missing.")

        cmd = [self._find_binary(), *args]
        env = os.environ.copy()
        env["TWITTER_AUTH_TOKEN"] = self.auth_token
        env["TWITTER_CT0"] = self.ct0

        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "twitter CLI binary not found. Install it via: pipx install twitter-cli"
            )

        if process.returncode != 0:
            err = process.stderr.strip() or process.stdout.strip() or "twitter CLI execution failed"
            raise RuntimeError(self.mask_secrets(err))

        return process.stdout, process.stderr

    def test_connection(self) -> bool:
        """Verify twitter CLI can query the account."""
        try:
            posts = self.fetch_new_posts(limit=1)
            logger.info("Twitter CLI connection successful. Fetched sample posts.")
            return True
        except Exception as e:
            logger.error("Twitter CLI connection test failed: %s", self.mask_secrets(str(e)))
            return False

    def fetch_new_posts(self, limit: int = None) -> List[Post]:
        """Fetch latest posts/reposts from target Twitter account."""
        max_posts = max(1, limit or self.initial_load)
        stdout, _ = self._run_cli(
            "user-posts",
            self.username,
            "--max",
            str(max_posts),
            "--json",
        )

        try:
            result = json.loads(stdout)
        except Exception as e:
            raise RuntimeError(f"twitter-cli returned invalid JSON: {e}")

        if not isinstance(result, dict):
            raise RuntimeError("twitter-cli returned unexpected data structure.")

        if result.get("ok") is False:
            raise RuntimeError(str(result.get("error", "twitter-cli reported failure.")))

        raw_posts = result.get("data", [])
        if not isinstance(raw_posts, list):
            raw_posts = []

        parsed_posts: List[Post] = []

        for item in raw_posts:
            if not isinstance(item, dict):
                continue

            is_retweet = bool(item.get("isRetweet", False))
            if self.only_reposts and not is_retweet:
                continue

            post_id = str(item.get("id") or "")
            if not post_id:
                continue

            author = item.get("author", {}) or {}
            author_username = (
                author.get("screenName") or author.get("username") or "unknown"
            )
            author_name = author.get("name") or author_username

            text = item.get("text") or ""
            source_url = f"https://x.com/{author_username}/status/{post_id}"

            # Parse media items
            media_items: List[MediaItem] = []
            for m in item.get("media", []):
                if not isinstance(m, dict):
                    continue
                m_type_str = str(m.get("type") or "").lower()
                m_url = m.get("url")
                if not m_url:
                    continue

                if m_type_str in ("video", "animated_gif"):
                    m_type = MediaType.VIDEO
                elif m_type_str in ("photo", "image"):
                    m_type = MediaType.PHOTO
                elif ".mp4" in m_url.lower() or "video" in m_url.lower():
                    m_type = MediaType.VIDEO
                else:
                    m_type = MediaType.PHOTO

                media_items.append(
                    MediaItem(
                        url=m_url,
                        media_type=m_type,
                    )
                )

            post = Post(
                id=post_id,
                source_name=self.name,
                author_username=author_username,
                author_name=author_name,
                text=text,
                url=source_url,
                media_items=media_items,
                is_repost=is_retweet,
                raw_data=item,
            )
            parsed_posts.append(post)

        return parsed_posts
