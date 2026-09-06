"""Base interface for post sources."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from ..models import Post, UploadResult


class BaseSource(ABC):
    """Abstract base class for content sources (Twitter/X, local folder, etc.)."""

    name: str = "base"

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))

    @abstractmethod
    def fetch_new_posts(self, limit: int = 20) -> List[Post]:
        """Fetch latest candidate posts from this source."""
        pass

    def test_connection(self) -> bool:
        """Verify connection or configuration."""
        return True

    def on_post_uploaded(self, post: Post, result: UploadResult) -> None:
        """Hook called when a post from this source finishes uploading."""
        pass
