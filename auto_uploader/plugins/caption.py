"""Caption formatting and hashtag plugin for Instagram.

The caption layout is a format string from the config, so it can be changed without
touching code. Available placeholders:

    {credit}       rendered `credit_format`, empty when the author is unknown
    {text}         the post text, t.co links stripped
    {source}       rendered `source_format`, empty when the link is omitted
    {tags}         rendered `tags_format`, empty when there are no hashtags
    {hashtags}     just the hashtags, space separated ("#anime #fanart")
    {author}       X handle, e.g. "artist_chan"
    {author_tag}   handle reduced to letters/digits/underscore, for use after '#'
    {author_name}  display name, falls back to the handle
    {url}          https://x.com/artist_chan/status/123
    {url_plain}    x.com/artist_chan/status/123  (no scheme: fewer link-spam flags)
    {post_id}      123
    {nl}           newline (handy inside single-quoted YAML)

Blank parts disappear together with the empty lines around them, so a template can list
every placeholder without worrying about posts that lack text or an author.
"""

import logging
import random
import re
from typing import Any, Dict, List

from ..models import Post
from .base import BasePlugin, register_plugin

logger = logging.getLogger("auto_uploader.plugins.caption")

# Used when `templates` is empty. The first one is the classic layout.
DEFAULT_TEMPLATES = [
    "{credit}\n\n{text}\n\n{source}\n\n{tags}",
    "{text}\n\n{credit}\n\n{source}\n\n{tags}",
    "{credit}\n\n{text}\n\n{tags}\n\n{source}",
]

PLACEHOLDER_HELP = [
    "credit", "text", "source", "tags", "hashtags", "author", "author_tag",
    "author_name", "url", "url_plain", "post_id", "nl",
]


class _Fields(dict):
    """Missing placeholders render as empty instead of raising."""

    def __missing__(self, key: str) -> str:
        logger.warning("Unknown caption placeholder '{%s}'. Known: %s", key, ", ".join(PLACEHOLDER_HELP))
        return ""


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
        self.templates: List[str] = [t for t in (self.config.get("templates") or []) if str(t).strip()]
        self.credit_format = self.config.get("credit_format", "Artist: {author_name} (#{author_tag}) on X")
        self.source_format = self.config.get("source_format", "Source: {url_plain}")
        self.tags_format = self.config.get("tags_format", ".\n.\n{hashtags}")
        self.hashtag_count = self.config.get("hashtag_count")          # None -> use all tags, in order
        self.source_link_probability = float(self.config.get("source_link_probability", 1.0))
        self.vary_templates = bool(self.config.get("vary_templates", False))

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

    def pick_hashtags(self, rng: random.Random) -> List[str]:
        """A shuffled subset when `hashtag_count` is set, otherwise the configured list as-is."""
        tags = list(self.custom_hashtags)
        if not tags or not self.hashtag_count:
            return tags
        counts = self.hashtag_count if isinstance(self.hashtag_count, (list, tuple)) else [self.hashtag_count]
        low = max(1, int(counts[0]))
        high = max(low, int(counts[-1]))
        return rng.sample(tags, min(len(tags), rng.randint(low, high)))

    def build_fields(self, post: Post, rng: random.Random) -> _Fields:
        """Values every template can use."""
        known_author = bool(post.author_username) and post.author_username != "unknown"
        author_tag = re.sub(r"[^A-Za-z0-9_]", "", post.author_username or "")
        fields = _Fields(
            author=post.author_username if known_author else "",
            author_tag=author_tag if known_author else "",
            author_name=(post.author_name or post.author_username) if known_author else "",
            text=self.clean_text(post.text),
            url=post.url or "",
            url_plain=re.sub(r"^https?://", "", post.url or ""),
            post_id=post.id,
            hashtags=" ".join(self.pick_hashtags(rng)),
            nl="\n",
            credit="",
            source="",
            tags="",
        )

        if self.add_author_hashtag and known_author:
            fields["credit"] = self.credit_format.format_map(fields).strip()
        # Link omitted on some posts when source_link_probability < 1 (less repetitive).
        if self.add_source_link and post.url and rng.random() < self.source_link_probability:
            fields["source"] = self.source_format.format_map(fields).strip()
        if fields["hashtags"]:
            fields["tags"] = self.tags_format.format_map(fields).strip()
        return fields

    def render(self, template: str, fields: _Fields) -> str:
        """Fill the template and drop the gaps left by empty placeholders."""
        text = template.format_map(fields)
        text = "\n".join(line.rstrip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def available_templates(self) -> List[str]:
        return self.templates or DEFAULT_TEMPLATES

    def process_caption(self, caption: str, post: Post) -> str:
        """Construct a polished Instagram caption safe from spam filters."""
        # Seeded per post: a retry (or `tools fix-captions`) rebuilds the very same caption.
        rng = random.Random(f"caption:{post.id}")
        fields = self.build_fields(post, rng)
        templates = self.available_templates()
        template = rng.choice(templates) if (self.vary_templates and len(templates) > 1) else templates[0]

        full_caption = self.render(template, fields)

        # Instagram max caption length is 2200 characters
        if len(full_caption) > self.max_length:
            full_caption = full_caption[: self.max_length - 3] + "..."

        return full_caption
