"""Core data models for Instagram Auto Uploader."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class MediaType(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    UNKNOWN = "unknown"


class PostStatus(str, Enum):
    """Lifecycle of a post inside the store.

    queued -> uploading -> uploaded
                        -> failed   (can be re-queued)
    skipped: known but intentionally never uploaded (e.g. first-run baseline)
    """

    QUEUED = "queued"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class MediaItem:
    """Represents a media file (image/video) associated with a post."""

    url: Optional[str] = None
    media_type: MediaType = MediaType.PHOTO
    local_path: Optional[Path] = None
    original_path: Optional[Path] = None
    width: Optional[int] = None
    height: Optional[int] = None
    content_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> Optional[float]:
        """Width to height ratio (w / h)."""
        if self.width and self.height and self.height > 0:
            return self.width / self.height
        return None

    @property
    def is_valid_local(self) -> bool:
        """Check if local file exists and is non-empty."""
        return bool(self.local_path and self.local_path.is_file() and self.local_path.stat().st_size > 0)

    def to_dict(self) -> Dict[str, Any]:
        # original_path is the file the source handed us (e.g. incoming/art.png);
        # local_path may point into a temp dir that no longer exists.
        source_path = self.original_path or self.local_path
        return {
            "url": self.url,
            "media_type": self.media_type.value,
            "path": str(source_path) if source_path else None,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MediaItem":
        path = Path(d["path"]) if d.get("path") else None
        try:
            media_type = MediaType(d.get("media_type", "photo"))
        except ValueError:
            media_type = MediaType.UNKNOWN
        return cls(
            url=d.get("url"),
            media_type=media_type,
            local_path=path,
            original_path=path,
            width=d.get("width"),
            height=d.get("height"),
        )


@dataclass
class Post:
    """Represents an incoming post from any source (Twitter, local, etc.)."""

    id: str
    source_name: str
    author_username: str = "unknown"
    author_name: str = "Unknown"
    text: str = ""
    caption: str = ""
    url: Optional[str] = None
    media_items: List[MediaItem] = field(default_factory=list)
    is_repost: bool = False
    created_at: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)
    extra_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_media(self) -> bool:
        return len(self.media_items) > 0

    @property
    def photo_count(self) -> int:
        return sum(1 for m in self.media_items if m.media_type == MediaType.PHOTO)

    @property
    def video_count(self) -> int:
        return sum(1 for m in self.media_items if m.media_type == MediaType.VIDEO)

    @property
    def link(self) -> str:
        """Best human-facing link for this post."""
        if self.url:
            return self.url
        if self.source_name == "twitter":
            return f"https://x.com/{self.author_username}/status/{self.id}"
        return ""

    @property
    def media_summary(self) -> str:
        parts = []
        if self.photo_count:
            parts.append(f"{self.photo_count} photo")
        if self.video_count:
            parts.append(f"{self.video_count} video")
        return ", ".join(parts) or f"{len(self.media_items)} media"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_name": self.source_name,
            "author_username": self.author_username,
            "author_name": self.author_name,
            "text": self.text,
            "caption": self.caption,
            "url": self.url,
            "is_repost": self.is_repost,
            "created_at": self.created_at,
            "media_items": [m.to_dict() for m in self.media_items],
            "extra_data": self.extra_data,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Post":
        return cls(
            id=str(d["id"]),
            source_name=d.get("source_name", "twitter"),
            author_username=d.get("author_username", "unknown"),
            author_name=d.get("author_name", "Unknown"),
            text=d.get("text", "") or "",
            caption=d.get("caption", "") or "",
            url=d.get("url"),
            media_items=[MediaItem.from_dict(m) for m in d.get("media_items", [])],
            is_repost=d.get("is_repost", False),
            created_at=d.get("created_at"),
            raw_data=d.get("raw_data", {}) or {},
            extra_data=d.get("extra_data", {}) or {},
        )


@dataclass
class UploadResult:
    """Represents the result of an upload attempt."""

    success: bool
    media_id: Optional[str] = None
    error_message: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
