"""Staging of post media into an isolated temporary directory."""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from ..models import MediaItem, Post

logger = logging.getLogger("auto_uploader.media")

_CONTENT_TYPE_EXT = (("png", ".png"), ("webp", ".webp"), ("gif", ".gif"), ("mp4", ".mp4"), ("video", ".mp4"))
_KNOWN_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov"}


class MediaStager:
    """Copies local files / downloads remote media so plugins never touch originals."""

    def __init__(self, temp_root: str):
        self.temp_root = Path(temp_root)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def create_dir(self, post_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in post_id)[:80]
        post_dir = self.temp_root / f"post_{safe_id}_{int(time.time() * 1000)}"
        post_dir.mkdir(parents=True, exist_ok=True)
        return post_dir

    @staticmethod
    def cleanup(post_dir: Path) -> None:
        shutil.rmtree(post_dir, ignore_errors=True)

    def cleanup_all(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    async def stage(self, post: Post, post_dir: Path) -> None:
        """Populate `local_path` of every media item inside `post_dir`."""
        remote = []
        for idx, media in enumerate(post.media_items):
            if media.local_path and media.local_path.is_file():
                dest = post_dir / f"media_{idx}{media.local_path.suffix.lower()}"
                await asyncio.to_thread(shutil.copyfile, media.local_path, dest)
                media.local_path = dest
            elif media.url:
                remote.append((idx, media))
            else:
                media.local_path = None

        if not remote:
            return

        timeout = aiohttp.ClientTimeout(total=120)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            await asyncio.gather(
                *(self._download(session, media, post_dir, idx) for idx, media in remote)
            )

    async def _download(self, session: aiohttp.ClientSession, media: MediaItem, target_dir: Path, idx: int) -> None:
        try:
            async with session.get(media.url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning("HTTP %d downloading %s", resp.status, media.url)
                    return
                data = await resp.read()
                content_type = resp.headers.get("Content-Type", "").lower()
                final_url = str(resp.url)
        except Exception as e:
            logger.error("Download failed for %s: %s", media.url, e)
            return

        if not data:
            return

        ext = next((e for key, e in _CONTENT_TYPE_EXT if key in content_type), None)
        if ext is None:
            path_ext = os.path.splitext(urlparse(final_url).path)[1].lower()
            ext = path_ext if path_ext in _KNOWN_EXT else ".jpg"

        local_file = target_dir / f"media_{idx}{ext}"
        await asyncio.to_thread(local_file.write_bytes, data)
        media.local_path = local_file
        media.content_type = content_type
