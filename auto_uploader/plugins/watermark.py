"""Watermark and branding plugin for Instagram Auto Uploader."""

import logging
from pathlib import Path
from typing import Any, Dict

from PIL import Image, ImageDraw, ImageFont

from ..models import MediaItem, MediaType, Post
from .base import BasePlugin, register_plugin

logger = logging.getLogger("auto_uploader.plugins.watermark")


@register_plugin("watermark")
class WatermarkPlugin(BasePlugin):
    """Adds a subtle text or logo watermark to photos."""

    name: str = "watermark"
    description: str = "Adds custom text or image watermark to media."
    priority: int = 80  # Runs after cropping

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.text = str(self.config.get("text", "")).strip()
        self.image_path = self.config.get("image_path")
        self.position = str(self.config.get("position", "bottom_right")).lower()
        self.opacity = float(self.config.get("opacity", 0.7))

    def process_media(self, media_item: MediaItem, post: Post) -> MediaItem:
        if not self.enabled or media_item.media_type != MediaType.PHOTO or not media_item.is_valid_local:
            return media_item

        if not self.text and not self.image_path:
            return media_item

        try:
            pil_img = Image.open(media_item.local_path).convert("RGBA")
            overlay = Image.new("RGBA", pil_img.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)
            img_w, img_h = pil_img.size

            if self.text:
                font_size = max(16, int(img_h * 0.025))
                try:
                    font = ImageFont.truetype("DejaVuSans.ttf", font_size)
                except Exception:
                    font = ImageFont.load_default()

                # Calculate text position
                alpha = int(255 * self.opacity)
                text_color = (255, 255, 255, alpha)
                margin = int(img_w * 0.03)

                if self.position == "bottom_left":
                    pos = (margin, img_h - margin - font_size)
                elif self.position == "center":
                    pos = (img_w // 2, img_h // 2)
                else:  # bottom_right
                    pos = (img_w - margin - int(len(self.text) * font_size * 0.6), img_h - margin - font_size)

                draw.text(pos, self.text, fill=text_color, font=font)

            combined = Image.alpha_composite(pil_img, overlay).convert("RGB")
            combined.save(media_item.local_path, "JPEG", quality=95)
            logger.info("Applied watermark to %s", media_item.local_path.name)

        except Exception as e:
            logger.error("Failed to apply watermark: %s", e)

        return media_item
