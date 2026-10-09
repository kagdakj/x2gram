"""Instagram Auto Uploader.

Uploads X/Twitter reposts and local artwork to Instagram with face-preserving cropping.
"""

from .config import AppConfig
from .context import AppContext
from .models import MediaItem, MediaType, Post, PostStatus, UploadResult

__version__ = "3.0.0"

__all__ = [
    "AppConfig",
    "AppContext",
    "MediaItem",
    "MediaType",
    "Post",
    "PostStatus",
    "UploadResult",
]
