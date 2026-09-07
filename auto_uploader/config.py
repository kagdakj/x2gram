"""Configuration management for Instagram Auto Uploader."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, Field


class InstagramConfig(BaseModel):
    """Instagram authentication and client settings."""
    username: str = ""
    password: str = ""
    session_id: str = ""
    session_file: str = "data/instagram_session.json"
    proxy: Optional[str] = None
    verification_code: Optional[str] = None
    upload_timeout: int = 120


class TwitterSourceConfig(BaseModel):
    """Twitter/X CLI source settings."""
    enabled: bool = True
    username: str = ""
    auth_token: str = ""
    ct0: str = ""
    initial_load: int = 20
    only_reposts: bool = True
    binary_path: Optional[str] = None


class LocalSourceConfig(BaseModel):
    """Local folder source settings for anime art batch uploading."""
    enabled: bool = False
    watch_dir: str = "incoming"
    move_to_processed: bool = True
    processed_dir: str = "data/processed"
    default_caption: str = "#anime #illustration"


class AnimeCropConfig(BaseModel):
    """Anime face detection and intelligent cropping plugin settings."""
    model_config = {"protected_namespaces": ()}

    enabled: bool = True
    target_aspect_ratio: str = "auto"  # "auto", "4:5", "1:1", "1.91:1"
    model_type: str = "yolov3"          # "yolov3" or "faster-rcnn"
    device: str = "cpu"                 # "cuda" or "cpu"
    confidence_threshold: float = 0.5   # face detection score threshold
    padding_margin_pct: float = 0.15    # safety padding around face (hair, ears, chin)
    fallback_mode: str = "center"       # "center" or "smart_pad" when no face found
    pad_color: str = "black"            # "black", "white", or "blur" for padding


class CaptionConfig(BaseModel):
    """Caption generation and hashtag plugin settings."""
    enabled: bool = True
    add_author_hashtag: bool = True
    add_source_link: bool = True
    custom_hashtags: List[str] = Field(
        default_factory=lambda: ["#anime", "#animeart", "#illustration", "#fanart"]
    )
    max_caption_length: int = 2200


class WatermarkConfig(BaseModel):
    """Watermark / branding plugin settings."""
    enabled: bool = False
    text: str = ""
    image_path: Optional[str] = None
    position: str = "bottom_right"      # "bottom_right", "bottom_left", "center"
    opacity: float = 0.7


class PluginsConfig(BaseModel):
    """Configuration for all registered plugins."""
    anime_crop: AnimeCropConfig = Field(default_factory=AnimeCropConfig)
    caption: CaptionConfig = Field(default_factory=CaptionConfig)
    watermark: WatermarkConfig = Field(default_factory=WatermarkConfig)
    custom: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class ServiceConfig(BaseModel):
    """Main service loop settings."""
    check_interval: int = 60            # seconds between checks
    post_delay: int = 30                # delay in seconds before uploading each post
    db_path: str = "data/uploader.db"   # SQLite state database
    temp_dir: str = "data/temp_media"   # Temporary downloaded media storage
    log_level: str = "INFO"             # DEBUG, INFO, WARNING, ERROR
    initial_skip: bool = True           # Mark existing posts as seen on first start without posting

    # Rate limiting & schedule controls
    rate_limit_enabled: bool = False    # Set True to enable daily upload limits
    max_posts_per_day: int = 10         # Maximum posts allowed within rolling 24 hours
    min_post_interval: int = 3600       # Minimum seconds between uploads (e.g. 3600 = 1 hour)


class AppConfig(BaseModel):
    """Root configuration object."""
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)
    twitter: TwitterSourceConfig = Field(default_factory=TwitterSourceConfig)
    local: LocalSourceConfig = Field(default_factory=LocalSourceConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)

    @classmethod
    def load(cls, config_path: str = "config.yaml") -> "AppConfig":
        """Load configuration from YAML file and override with environment variables."""
        data: Dict[str, Any] = {}
        path = Path(config_path)

        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    data = loaded

        # Apply environment variable overrides
        cls._apply_env_overrides(data)

        return cls(**data)

    @staticmethod
    def _apply_env_overrides(data: Dict[str, Any]) -> None:
        """Override configuration with environment variables if present."""
        env_mappings = {
            "INSTAGRAM_USERNAME": ("instagram", "username"),
            "INSTAGRAM_PASSWORD": ("instagram", "password"),
            "INSTAGRAM_SESSION_ID": ("instagram", "session_id"),
            "INSTAGRAM_PROXY": ("instagram", "proxy"),
            "TWITTER_USERNAME": ("twitter", "username"),
            "TWITTER_AUTH_TOKEN": ("twitter", "auth_token"),
            "TWITTER_CT0": ("twitter", "ct0"),
            "CHECK_INTERVAL": ("service", "check_interval"),
            "POST_DELAY": ("service", "post_delay"),
        }

        for env_var, (section, key) in env_mappings.items():
            val = os.environ.get(env_var)
            if val is not None:
                if section not in data:
                    data[section] = {}
                # Convert numbers if appropriate
                if val.isdigit():
                    data[section][key] = int(val)
                else:
                    data[section][key] = val
