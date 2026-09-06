"""Sources module for Instagram Auto Uploader."""

from .base import BaseSource
from .twitter_cli import TwitterCliSource
from .local_folder import LocalFolderSource

__all__ = [
    "BaseSource",
    "TwitterCliSource",
    "LocalFolderSource",
]
