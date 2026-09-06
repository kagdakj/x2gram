"""Extensible plugin architecture for Instagram Auto Uploader."""

import logging
from abc import ABC
from typing import Any, Dict, List, Type

from ..models import MediaItem, Post, UploadResult

logger = logging.getLogger("auto_uploader.plugins")


class BasePlugin(ABC):
    """Abstract base class for all feature plugins.

    Subclasses can implement media transformations (cropping, watermarking, filters),
    caption enhancements (hashtags, AI tagging), and post-upload hooks.
    """

    name: str = "base"
    description: str = "Base plugin"
    priority: int = 100  # Lower runs earlier in pipeline

    def __init__(self, config: Dict[str, Any] = None):
        self.config: Dict[str, Any] = config or {}
        self.enabled: bool = bool(self.config.get("enabled", True))

    def on_init(self) -> None:
        """Called once when plugin is loaded."""
        pass

    def process_media(self, media_item: MediaItem, post: Post) -> MediaItem:
        """Transform a media item (e.g., smart crop, watermark, resize)."""
        return media_item

    def process_caption(self, caption: str, post: Post) -> str:
        """Enhance or alter post caption (e.g., append hashtags, format credits)."""
        return caption

    def process_post(self, post: Post) -> Post:
        """Process full post before upload (can filter, reorder, or alter media)."""
        return post

    def on_post_uploaded(self, post: Post, result: UploadResult) -> None:
        """Hook called after an upload attempt finishes."""
        pass


# Global plugin registry
_PLUGIN_REGISTRY: Dict[str, Type[BasePlugin]] = {}


def register_plugin(name: str):
    """Decorator to register a plugin class."""
    def decorator(cls: Type[BasePlugin]):
        _PLUGIN_REGISTRY[name] = cls
        cls.name = name
        return cls
    return decorator


class PluginManager:
    """Manages plugin lifecycle and execution pipeline."""

    def __init__(self, plugins_config: Any = None):
        self.plugins_config = plugins_config
        self.plugins: List[BasePlugin] = []
        self._load_registered_plugins()

    def _load_registered_plugins(self) -> None:
        """Instantiate registered plugins with their respective configs."""
        self.plugins.clear()

        # Convert pydantic config to dict if needed
        config_dict: Dict[str, Any] = {}
        if hasattr(self.plugins_config, "model_dump"):
            config_dict = self.plugins_config.model_dump()
        elif isinstance(self.plugins_config, dict):
            config_dict = self.plugins_config

        for name, plugin_cls in _PLUGIN_REGISTRY.items():
            sub_cfg = config_dict.get(name, {})
            # If sub_cfg is pydantic model
            if hasattr(sub_cfg, "model_dump"):
                sub_cfg = sub_cfg.model_dump()

            try:
                instance = plugin_cls(config=sub_cfg)
                if instance.enabled:
                    instance.on_init()
                    self.plugins.append(instance)
                    logger.info("Plugin '%s' enabled (priority %d)", name, instance.priority)
                else:
                    logger.debug("Plugin '%s' is disabled", name)
            except Exception as e:
                logger.error("Failed to initialize plugin '%s': %s", name, e, exc_info=True)

        # Sort by priority ascending
        self.plugins.sort(key=lambda p: p.priority)

    def apply_media_pipeline(self, media_item: MediaItem, post: Post) -> MediaItem:
        """Run a single media item through all enabled media processors."""
        current_item = media_item
        for plugin in self.plugins:
            try:
                current_item = plugin.process_media(current_item, post)
            except Exception as e:
                logger.error(
                    "Plugin '%s' error processing media for post %s: %s",
                    plugin.name,
                    post.id,
                    e,
                    exc_info=True,
                )
        return current_item

    def apply_caption_pipeline(self, caption: str, post: Post) -> str:
        """Run post caption through all enabled caption processors."""
        current_caption = caption
        for plugin in self.plugins:
            try:
                current_caption = plugin.process_caption(current_caption, post)
            except Exception as e:
                logger.error(
                    "Plugin '%s' error processing caption for post %s: %s",
                    plugin.name,
                    post.id,
                    e,
                    exc_info=True,
                )
        return current_caption

    def apply_post_pipeline(self, post: Post) -> Post:
        """Run entire post through all plugins."""
        current_post = post

        # 1. Process each media item
        processed_media = []
        for media in current_post.media_items:
            processed_media.append(self.apply_media_pipeline(media, current_post))
        current_post.media_items = processed_media

        # 2. Process caption
        current_post.caption = self.apply_caption_pipeline(current_post.caption or current_post.text, current_post)

        # 3. Post-level processing
        for plugin in self.plugins:
            try:
                current_post = plugin.process_post(current_post)
            except Exception as e:
                logger.error(
                    "Plugin '%s' error processing post %s: %s",
                    plugin.name,
                    post.id,
                    e,
                    exc_info=True,
                )

        return current_post

    def notify_upload_complete(self, post: Post, result: UploadResult) -> None:
        """Notify all plugins of completed upload."""
        for plugin in self.plugins:
            try:
                plugin.on_post_uploaded(post, result)
            except Exception as e:
                logger.error(
                    "Plugin '%s' on_post_uploaded hook failed: %s",
                    plugin.name,
                    e,
                )
