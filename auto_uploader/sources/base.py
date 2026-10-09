"""Base interface for post sources."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from ..models import Post, UploadResult


class BaseSource(ABC):
    """Abstract base class for content sources (Twitter/X, local folder, etc.)."""

    name: str = "base"
    # Remote sources get baseline-skipped on first run; local drops are always intentional.
    is_remote: bool = True

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))

    @abstractmethod
    def fetch_new_posts(self, limit: int = 20) -> List[Post]:
        """Fetch latest candidate posts from this source."""

    def fetch(self, deep: bool = False) -> List[Post]:
        """Fetch using the source's own limits. `deep` pulls a larger backlog."""
        return self.fetch_new_posts()

    def test_connection(self) -> bool:
        """Verify connection or configuration."""
        return True

    def on_post_uploaded(self, post: Post, result: UploadResult) -> None:
        """Hook called when a post from this source finishes uploading."""
