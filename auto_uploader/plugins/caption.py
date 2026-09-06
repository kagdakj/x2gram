"""Caption formatting and hashtag plugin for Instagram."""

import html
import re
from typing import Any, Dict, List

from ..models import Post
from .base import BasePlugin, register_plugin


@register_plugin("caption")
class CaptionPlugin(BasePlugin):
    """Formats clean captions, credits the original creator, and adds hashtags."""

    name: str = "caption"
    description: str = "Formats Instagram captions with author credits and anime tags."
    priority: int = 50

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.add_author_hashtag = bool(self.config.get("add_author_hashtag", True))
        self.add_source_link = bool(self.config.get("add_source_link", True))
        self.custom_hashtags: List[str] = self.config.get(
            "custom_hashtags",
            ["#anime", "#animeart", "#illustration", "#fanart"],
        )
        self.max_length = int(self.config.get("max_caption_length", 2200))

    def clean_text(self, text: str) -> str:
        """Strip t.co links and clean excessive whitespace."""
        if not text:
            return ""
        # Remove twitter t.co links
        text = re.sub(r"https?://t\.co/\S+", "", text)
        # Normalize whitespace and excessive newlines
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def process_caption(self, caption: str, post: Post) -> str:
        """Construct a polished Instagram caption."""
        lines = []

        # 1. Author credit tag
        author_tag = ""
        if self.add_author_hashtag and post.author_username and post.author_username != "unknown":
            clean_author = re.sub(r"[^A-Za-z0-9_]", "", post.author_username)
            if clean_author:
                author_tag = f"#{clean_author}"
                lines.append(f"Artist: {author_tag} (@{post.author_username})")

        # 2. Original text / quote
        raw_text = self.clean_text(post.text)
        if raw_text:
            lines.append(raw_text)

        # 3. Source reference link
        if self.add_source_link and post.url:
            lines.append(f"Source: {post.url}")

        # 4. Anime hashtags
        if self.custom_hashtags:
            lines.append(".\n.\n" + " ".join(self.custom_hashtags))

        full_caption = "\n\n".join(lines).strip()

        # Instagram max caption length is 2200 characters
        if len(full_caption) > self.max_length:
            full_caption = full_caption[: self.max_length - 3] + "..."

        return full_caption
