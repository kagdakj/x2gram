"""Local folder source for drag-and-drop anime art uploads.

Supports:
- Dragging single photos/videos into incoming/
- Dragging folders into incoming/ (uploaded as multi-image Instagram carousels/albums)
- Optional .txt sidecar captions (e.g. image.txt or album_folder/caption.txt)
- Moving uploaded items to processed/ folder after successful upload
"""

import datetime
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..models import MediaItem, MediaType, Post, UploadResult
from .base import BaseSource

logger = logging.getLogger("auto_uploader.sources.local")

SUPPORTED_IMAGE_EXTS: Set[str] = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_VIDEO_EXTS: Set[str] = {".mp4", ".mov"}


class LocalFolderSource(BaseSource):
    """Monitors incoming/ directory for dragged files or albums."""

    name: str = "local"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.watch_dir = Path(self.config.get("watch_dir", "incoming"))
        self.processed_dir = Path(self.config.get("processed_dir", "data/processed"))
        self.move_to_processed = bool(self.config.get("move_to_processed", True))
        self.default_caption = str(
            self.config.get("default_caption", "#anime #illustration #animeart")
        )

        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    def test_connection(self) -> bool:
        return self.watch_dir.is_dir()

    def _is_file_ready(self, path: Path) -> bool:
        """Check if file is completely copied and ready to read."""
        if not path.is_file() or path.stat().st_size == 0:
            return False
        # Small check: verify file isn't currently growing (drag/copy in progress)
        try:
            initial_size = path.stat().st_size
            time.sleep(0.05)
            return path.stat().st_size == initial_size
        except OSError:
            return False

    def _read_caption_file(self, caption_path: Path) -> Optional[str]:
        """Read text from sidecar caption file if present."""
        if caption_path.is_file():
            try:
                content = caption_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception as e:
                logger.warning("Could not read caption file %s: %s", caption_path, e)
        return None

    def fetch_new_posts(self, limit: int = 20) -> List[Post]:
        """Scan watch_dir for both individual files and album directories."""
        posts: List[Post] = []
        if not self.watch_dir.is_dir():
            return posts

        entries = sorted(
            [e for e in self.watch_dir.iterdir() if not e.name.startswith(".")],
            key=lambda p: p.stat().st_mtime,
        )

        for entry in entries:
            if len(posts) >= limit:
                break

            # CASE 1: Subdirectory dragged in -> Treat as multi-item Carousel / Album
            if entry.is_dir():
                album_files = sorted(
                    [
                        f
                        for f in entry.iterdir()
                        if f.is_file()
                        and not f.name.startswith(".")
                        and f.suffix.lower() in (SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS)
                    ],
                    key=lambda p: p.name,
                )

                if not album_files:
                    continue

                # Ensure all files are fully copied
                if not all(self._is_file_ready(f) for f in album_files):
                    continue

                # Look for album caption: folder/caption.txt or folder.txt
                caption_text = (
                    self._read_caption_file(entry / "caption.txt")
                    or self._read_caption_file(entry.with_suffix(".txt"))
                    or self.default_caption
                )

                media_items: List[MediaItem] = []
                for f in album_files:
                    ext = f.suffix.lower()
                    m_type = (
                        MediaType.PHOTO
                        if ext in SUPPORTED_IMAGE_EXTS
                        else MediaType.VIDEO
                    )
                    media_items.append(
                        MediaItem(
                            url=None,
                            media_type=m_type,
                            local_path=f,
                            original_path=f,
                        )
                    )

                post_id = f"local_album_{entry.name}_{int(entry.stat().st_mtime)}"
                post = Post(
                    id=post_id,
                    source_name=self.name,
                    author_username="drag_and_drop",
                    author_name="Local Art",
                    text=caption_text,
                    media_items=media_items,
                    is_repost=False,
                    extra_data={"is_album_dir": True, "album_dir": str(entry)},
                )
                posts.append(post)

            # CASE 2: Standalone media file dragged in
            elif entry.is_file():
                ext = entry.suffix.lower()
                if ext not in (SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS):
                    continue

                if not self._is_file_ready(entry):
                    continue

                # Check sidecar caption: file.txt
                txt_sidecar = entry.with_suffix(".txt")
                caption_text = self._read_caption_file(txt_sidecar) or self.default_caption

                m_type = (
                    MediaType.PHOTO
                    if ext in SUPPORTED_IMAGE_EXTS
                    else MediaType.VIDEO
                )
                media_item = MediaItem(
                    url=None,
                    media_type=m_type,
                    local_path=entry,
                    original_path=entry,
                )

                post_id = f"local_file_{entry.stem}_{int(entry.stat().st_mtime)}"
                post = Post(
                    id=post_id,
                    source_name=self.name,
                    author_username="drag_and_drop",
                    author_name="Local Art",
                    text=caption_text,
                    media_items=[media_item],
                    is_repost=False,
                    extra_data={
                        "is_album_dir": False,
                        "file_path": str(entry),
                        "caption_path": str(txt_sidecar) if txt_sidecar.is_file() else None,
                    },
                )
                posts.append(post)

        return posts

    def on_post_uploaded(self, post: Post, result: UploadResult) -> None:
        """Move successfully uploaded files from incoming/ to processed/ folder."""
        if not result.success or not self.move_to_processed:
            return

        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        dest_folder = self.processed_dir / date_str
        dest_folder.mkdir(parents=True, exist_ok=True)

        extra = post.extra_data
        if extra.get("is_album_dir"):
            # Move whole album folder
            album_dir = Path(extra["album_dir"])
            if album_dir.is_dir():
                target_album = dest_folder / album_dir.name
                if target_album.exists():
                    target_album = dest_folder / f"{album_dir.name}_{int(time.time())}"
                try:
                    shutil.move(str(album_dir), str(target_album))
                    logger.info("Moved uploaded album folder to %s", target_album)
                except Exception as e:
                    logger.error("Failed to move album folder %s: %s", album_dir, e)
        else:
            # Move single file + caption sidecar
            file_path = Path(extra.get("file_path", ""))
            if file_path.is_file():
                try:
                    target_file = dest_folder / file_path.name
                    if target_file.exists():
                        target_file = dest_folder / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
                    shutil.move(str(file_path), str(target_file))
                    logger.info("Moved uploaded file to %s", target_file)
                except Exception as e:
                    logger.error("Failed to move file %s: %s", file_path, e)

            caption_str = extra.get("caption_path")
            if caption_str:
                caption_path = Path(caption_str)
                if caption_path.is_file():
                    try:
                        shutil.move(str(caption_path), str(dest_folder / caption_path.name))
                    except Exception:
                        pass
