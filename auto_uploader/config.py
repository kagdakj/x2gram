"""Configuration management for Instagram Auto Uploader."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class InstagramConfig(BaseModel):
    """Instagram authentication and client settings."""
    username: str = ""
    password: str = ""
    session_id: str = ""
    session_file: str = "data/instagram_session.json"
    proxy: Optional[str] = None
    verification_code: Optional[str] = None
    upload_timeout: int = 120

    # Pacing of individual API requests (instagrapi sleeps between calls).
    request_delay_min: float = 2.0
    request_delay_max: float = 5.0

    # Device identity. Empty values keep whatever the saved session already uses:
    # changing them makes the account look like it moved to another phone.
    device: Dict[str, Any] = Field(default_factory=dict)
    app_version: str = ""               # pin an Instagram app version known to instagrapi; "" = its current default
    locale: str = ""                    # e.g. "ko_KR"
    country: str = ""                   # e.g. "KR"
    country_code: Optional[int] = None  # e.g. 82
    timezone_offset: Optional[int] = None  # seconds, e.g. 32400 for KST

    # Instagram sometimes drops the caption of a fresh post. Retrying hard looks spammy,
    # so only a few attempts happen here; the rest is left to `tools fix-captions`.
    caption_repair_attempts: int = 1
    caption_repair_delay: int = 90


class TwitterSourceConfig(BaseModel):
    """Twitter/X CLI source settings."""
    enabled: bool = True
    username: str = ""
    auth_token: str = ""
    ct0: str = ""
    initial_load: int = 200             # posts pulled by a deep fetch (queue fetch --deep)
    poll_limit: int = 20                # posts pulled by each regular poll
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

    # Layout. `templates` are format strings; placeholders are documented in
    # auto_uploader/plugins/caption.py (PLACEHOLDER_HELP). Empty parts and the blank lines
    # around them are removed. Empty list = built-in layout.
    templates: List[str] = Field(default_factory=list)
    credit_format: str = "Artist: {author_name} (#{author_tag}) on X"
    source_format: str = "Source: {url_plain}"
    tags_format: str = ".\n.\n{hashtags}"

    # Variation (off by default = every post gets the exact same block of tags/links).
    # Choices are seeded with the post id, so a retry produces the same caption.
    hashtag_count: Optional[List[int]] = None      # e.g. [4, 8] -> pick 4-8 of the tags, shuffled
    source_link_probability: float = 1.0           # 1.0 = always append "Source: ..."
    vary_templates: bool = False                   # rotate between the templates above


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
    check_interval: int = 60            # seconds between polls (min 15)
    post_delay_min: int = 15            # random wait before each upload (seconds)
    post_delay_max: int = 60
    uploads_per_cycle: int = 10         # max uploads the daemon performs per poll cycle
    db_path: str = "data/uploader.db"   # SQLite state database
    temp_dir: str = "data/temp_media"   # Temporary downloaded media storage
    log_file: str = "data/uploader.log"
    log_level: str = "INFO"             # DEBUG, INFO, WARNING, ERROR
    initial_skip: bool = True           # First poll marks existing posts as skipped instead of uploading

    # Rate limiting & schedule controls
    rate_limit_enabled: bool = False    # Set True to enable daily upload limits
    max_posts_per_day: int = 10         # Maximum posts allowed within rolling 24 hours
    min_post_interval: int = 3600       # Minimum seconds between uploads (e.g. 3600 = 1 hour)
    action_block_cooldown: int = 21600  # Pause uploads this long after Instagram feedback_required

    # Human-like scheduling (all off by default; they only apply with rate_limit_enabled).
    active_hours: str = ""              # "" = any time, e.g. "10:00-23:00" (local time, may wrap midnight)
    interval_jitter: float = 0.0        # 0 = fixed interval; 0.8 = wait 1.0x-1.8x min_post_interval
    daily_limit_jitter: int = 0         # 0 = fixed cap; 1 = max_posts_per_day +/- 1, stable per day
    rest_day_probability: float = 0.0   # 0 = never; 0.15 = ~15% of days upload nothing

    @model_validator(mode="before")
    @classmethod
    def _legacy_post_delay(cls, data: Any) -> Any:
        # Older configs only had `post_delay`; treat it as the minimum delay.
        if isinstance(data, dict) and "post_delay" in data and "post_delay_min" not in data:
            data = dict(data)
            data["post_delay_min"] = data["post_delay"]
        return data

    @model_validator(mode="after")
    def _ordered_delays(self) -> "ServiceConfig":
        if self.post_delay_max < self.post_delay_min:
            self.post_delay_max = self.post_delay_min
        return self


class AppConfig(BaseModel):
    """Root configuration object."""
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)
    twitter: TwitterSourceConfig = Field(default_factory=TwitterSourceConfig)
    local: LocalSourceConfig = Field(default_factory=LocalSourceConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)

    @classmethod
    def load(cls, config_path: str = "config.yaml", env_file: str = ".env") -> "AppConfig":
        """Load configuration from YAML file, then apply .env / environment overrides."""
        data: Dict[str, Any] = {}
        path = Path(config_path)

        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    data = loaded

        _load_env_file(Path(env_file))
        cls._apply_env_overrides(data)

        return cls(**data)

    @staticmethod
    def _apply_env_overrides(data: Dict[str, Any]) -> None:
        """Override configuration with non-empty environment variables."""
        env_mappings = {
            "INSTAGRAM_USERNAME": ("instagram", "username"),
            "INSTAGRAM_PASSWORD": ("instagram", "password"),
            "INSTAGRAM_SESSION_ID": ("instagram", "session_id"),
            "INSTAGRAM_PROXY": ("instagram", "proxy"),
            "TWITTER_USERNAME": ("twitter", "username"),
            "TWITTER_AUTH_TOKEN": ("twitter", "auth_token"),
            "TWITTER_CT0": ("twitter", "ct0"),
            "CHECK_INTERVAL": ("service", "check_interval"),
            "POST_DELAY_MIN": ("service", "post_delay_min"),
            "POST_DELAY_MAX": ("service", "post_delay_max"),
            "POST_DELAY": ("service", "post_delay_min"),  # legacy name
        }

        for env_var, (section, key) in env_mappings.items():
            val = os.environ.get(env_var, "").strip()
            if not val:
                continue
            section_data = data.setdefault(section, {}) or {}
            data[section] = section_data
            section_data[key] = int(val) if val.isdigit() else val


def _load_env_file(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)
