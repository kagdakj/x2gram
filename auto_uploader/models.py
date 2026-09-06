"""Core data models for Instagram Auto Uploader."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class MediaType(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    UNKNOWN = "unknown"


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


@dataclass
class UploadResult:
    """Represents the result of an upload attempt."""

    success: bool
    media_id: Optional[str] = None
    error_message: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
