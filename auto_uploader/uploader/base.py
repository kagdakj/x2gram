"""Base interface for upload destinations."""

from abc import ABC, abstractmethod
from typing import Any, Dict

from ..models import Post, UploadResult


class BaseUploader(ABC):
    """Abstract uploader interface."""

    name: str = "base"

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

    @abstractmethod
    def upload_post(self, post: Post) -> UploadResult:
        """Publish post content to the target platform."""
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        """Verify authentication and service reachability."""
        pass
