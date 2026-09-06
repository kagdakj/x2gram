"""Instagram Auto Uploader.

An extensible service for uploading anime pictures to Instagram with intelligent face-preserving cropping.
"""

from .config import AppConfig
from .engine import AutoUploaderEngine
from .models import MediaItem, MediaType, Post, UploadResult

__version__ = "2.0.0"

__all__ = [
    "AppConfig",
    "AutoUploaderEngine",
    "MediaItem",
    "MediaType",
    "Post",
    "UploadResult",
]
