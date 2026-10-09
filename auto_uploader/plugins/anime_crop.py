"""Anime face detection and intelligent layout cropping plugin.

Uses anime-face-detector (https://github.com/hysts/anime-face-detector)
to find anime faces and perform minimal cropping to match Instagram aspect ratios
while ensuring faces are never cut off.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageFilter

from ..models import MediaItem, MediaType, Post
from .base import BasePlugin, register_plugin

logger = logging.getLogger("auto_uploader.plugins.anime_crop")

# Instagram Feed Aspect Ratio Limits
INSTAGRAM_MIN_RATIO = 0.80   # 4:5 portrait (width / height)
INSTAGRAM_MAX_RATIO = 1.91   # 1.91:1 landscape (width / height)
INSTAGRAM_SQUARE_RATIO = 1.0 # 1:1 square


@register_plugin("anime_crop")
class AnimeCropPlugin(BasePlugin):
    """Detects anime faces and crops/pads image to match Instagram's layout."""

    name: str = "anime_crop"
    description: str = "Minimal aspect-ratio crop preserving anime faces."
    priority: int = 20  # High priority (run before watermarking)

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.target_ratio_mode = str(self.config.get("target_aspect_ratio", "auto")).lower()
        self.model_type = str(self.config.get("model_type", "yolov3")).lower()
        self.device = str(self.config.get("device", "cpu")).lower()
        self.confidence_threshold = float(self.config.get("confidence_threshold", 0.5))
        self.padding_margin_pct = float(self.config.get("padding_margin_pct", 0.15))
        self.fallback_mode = str(self.config.get("fallback_mode", "center")).lower()
        self.pad_color = str(self.config.get("pad_color", "black")).lower()

        self._detector = None
        self._detector_initialized = False

    def on_init(self) -> None:
        """Model loading is deferred to the first image (torch import is slow)."""

    def _init_detector(self) -> None:
        if self._detector_initialized:
            return

        try:
            from anime_face_detector import create_detector
            logger.info(
                "Initializing anime-face-detector (%s on %s)...",
                self.model_type,
                self.device,
            )
            self._detector = create_detector(self.model_type, device=self.device)
            self._detector_initialized = True
            logger.info("anime-face-detector successfully loaded.")
        except ImportError:
            logger.warning(
                "anime-face-detector or torch is not installed. "
                "Smart face-guided crop will fall back to center crop. "
                "To enable anime face detection, run: pip install anime-face-detector torch torchvision"
            )
            self._detector = None
            self._detector_initialized = True
        except Exception as e:
            logger.warning("Could not initialize anime-face-detector: %s. Falling back to center crop.", e)
            self._detector = None
            self._detector_initialized = True

    def detect_anime_faces(self, image_bgr: np.ndarray) -> List[List[float]]:
        """Detect anime faces in BGR image.

        Returns list of [x1, y1, x2, y2, score] bounding boxes.
        """
        if not self._detector:
            return []

        try:
            preds = self._detector(image_bgr)
            boxes = []
            for pred in preds:
                bbox = pred.get("bbox")
                if bbox is not None and len(bbox) >= 5:
                    score = float(bbox[4])
                    if score >= self.confidence_threshold:
                        boxes.append([float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]), score])
            return boxes
        except Exception as e:
            logger.error("Error during anime face detection inference: %s", e)
            return []

    def compute_target_dimensions(self, width: int, height: int) -> Tuple[int, int, bool]:
        """Compute optimal (target_width, target_height) for Instagram.

        Returns (target_w, target_h, needs_crop).
        """
        current_ratio = width / height

        # Target ratio resolution based on config
        if self.target_ratio_mode == "4:5" or self.target_ratio_mode == "portrait":
            target_ratio = INSTAGRAM_MIN_RATIO  # 0.8
        elif self.target_ratio_mode == "1:1" or self.target_ratio_mode == "square":
            target_ratio = INSTAGRAM_SQUARE_RATIO  # 1.0
        elif self.target_ratio_mode == "1.91:1" or self.target_ratio_mode == "landscape":
            target_ratio = INSTAGRAM_MAX_RATIO  # 1.91
        else:
            # "auto" / "minimal" mode:
            # If already within valid Instagram bounds [0.8, 1.91], minimal crop is 0!
            if INSTAGRAM_MIN_RATIO <= current_ratio <= INSTAGRAM_MAX_RATIO:
                return width, height, False
            elif current_ratio < INSTAGRAM_MIN_RATIO:
                target_ratio = INSTAGRAM_MIN_RATIO  # 0.8 (too tall -> crop to 4:5)
            else:
                target_ratio = INSTAGRAM_MAX_RATIO  # 1.91 (too wide -> crop to 1.91:1)

        # Calculate minimal crop dimensions
        if current_ratio < target_ratio:
            # Image is taller than target -> keep width, reduce height
            target_w = width
            target_h = int(round(width / target_ratio))
            target_h = min(target_h, height)
        else:
            # Image is wider than target -> keep height, reduce width
            target_h = height
            target_w = int(round(height * target_ratio))
            target_w = min(target_w, width)

        needs_crop = (target_w != width) or (target_h != height)
        return target_w, target_h, needs_crop

    def calculate_crop_box(
        self,
        img_w: int,
        img_h: int,
        crop_w: int,
        crop_h: int,
        faces: List[List[float]],
    ) -> Tuple[int, int, int, int]:
        """Calculate [x1, y1, x2, y2] crop coordinates keeping anime faces inside."""
        if not faces:
            # Center crop fallback
            crop_x = max(0, (img_w - crop_w) // 2)
            crop_y = max(0, (img_h - crop_h) // 2)
            return crop_x, crop_y, crop_x + crop_w, crop_y + crop_h

        # Compute union bounding box of all detected faces
        f_x1 = min(f[0] for f in faces)
        f_y1 = min(f[1] for f in faces)
        f_x2 = max(f[2] for f in faces)
        f_y2 = max(f[3] for f in faces)

        face_w = f_x2 - f_x1
        face_h = f_y2 - f_y1

        # Add margin padding for hair, ears, chin
        margin_y = face_h * self.padding_margin_pct
        margin_x = face_w * self.padding_margin_pct

        safe_y1 = max(0.0, f_y1 - margin_y)
        safe_y2 = min(float(img_h), f_y2 + margin_y)
        safe_x1 = max(0.0, f_x1 - margin_x)
        safe_x2 = min(float(img_w), f_x2 + margin_x)

        # 1. Vertical crop calculation (crop_h < img_h)
        if crop_h < img_h:
            center_y = (f_y1 + f_y2) / 2.0
            ideal_y1 = center_y - crop_h / 2.0

            if (safe_y2 - safe_y1) <= crop_h:
                # Face comfortably fits within crop height
                # Ensure top of head/hair is not cut
                if ideal_y1 > safe_y1:
                    ideal_y1 = safe_y1
                # Ensure chin is not cut
                if ideal_y1 + crop_h < safe_y2:
                    ideal_y1 = safe_y2 - crop_h
            else:
                # Face is taller than crop window (extreme close-up)
                # Anchor to top of face (eyes / upper head)
                ideal_y1 = safe_y1

            crop_y = int(round(max(0.0, min(float(img_h - crop_h), ideal_y1))))
        else:
            crop_y = 0

        # 2. Horizontal crop calculation (crop_w < img_w)
        if crop_w < img_w:
            center_x = (f_x1 + f_x2) / 2.0
            ideal_x1 = center_x - crop_w / 2.0

            if (safe_x2 - safe_x1) <= crop_w:
                if ideal_x1 > safe_x1:
                    ideal_x1 = safe_x1
                if ideal_x1 + crop_w < safe_x2:
                    ideal_x1 = safe_x2 - crop_w
            else:
                ideal_x1 = center_x - crop_w / 2.0

            crop_x = int(round(max(0.0, min(float(img_w - crop_w), ideal_x1))))
        else:
            crop_x = 0

        return crop_x, crop_y, crop_x + crop_w, crop_y + crop_h

    def apply_smart_pad(
        self,
        pil_img: Image.Image,
        target_w: int,
        target_h: int,
    ) -> Image.Image:
        """Pad image to target dimensions without cutting pixels."""
        img_w, img_h = pil_img.size

        if self.pad_color == "blur":
            # Blurred background
            bg = pil_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=25))
        elif self.pad_color == "white":
            bg = Image.new("RGB", (target_w, target_h), (255, 255, 255))
        else:
            # Default black
            bg = Image.new("RGB", (target_w, target_h), (0, 0, 0))

        offset_x = (target_w - img_w) // 2
        offset_y = (target_h - img_h) // 2
        bg.paste(pil_img, (offset_x, offset_y))
        return bg

    def process_media(self, media_item: MediaItem, post: Post) -> MediaItem:
        """Process image: detect anime faces and crop to Instagram ratio."""
        if media_item.media_type != MediaType.PHOTO or not media_item.is_valid_local:
            return media_item

        file_path = media_item.local_path

        try:
            pil_img = Image.open(file_path).convert("RGB")
            img_w, img_h = pil_img.size

            target_w, target_h, needs_crop = self.compute_target_dimensions(img_w, img_h)

            if not needs_crop and file_path.suffix.lower() in (".jpg", ".jpeg"):
                # No crop needed and already JPEG
                media_item.width = img_w
                media_item.height = img_h
                return media_item

            # Load image for face detection
            self._init_detector()
            img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            faces = self.detect_anime_faces(img_bgr)

            logger.info(
                "Post %s: detected %d anime face(s) in %s (%dx%d). Target layout: %dx%d",
                post.id,
                len(faces),
                file_path.name,
                img_w,
                img_h,
                target_w,
                target_h,
            )

            # Record metadata
            media_item.metadata["anime_faces_detected"] = len(faces)
            media_item.metadata["anime_face_boxes"] = faces

            if needs_crop:
                if self.fallback_mode == "smart_pad" and not faces:
                    # Pad instead of crop when no face found
                    processed_img = self.apply_smart_pad(pil_img, target_w, target_h)
                else:
                    crop_box = self.calculate_crop_box(img_w, img_h, target_w, target_h, faces)
                    logger.debug("Applying crop box: %s", crop_box)
                    processed_img = pil_img.crop(crop_box)
            else:
                processed_img = pil_img

            # Output always as high quality JPEG for Instagram compatibility
            output_dir = file_path.parent
            cropped_path = output_dir / f"{file_path.stem}_ig.jpg"
            processed_img.save(cropped_path, "JPEG", quality=95, optimize=True)

            # Update media item
            media_item.local_path = cropped_path
            media_item.width, media_item.height = processed_img.size
            media_item.content_type = "image/jpeg"
            media_item.metadata["cropped_for_instagram"] = True

        except Exception as e:
            logger.error("Failed to crop image %s: %s", file_path, e, exc_info=True)

        return media_item
