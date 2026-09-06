"""Unit tests for anime face cropping math and bounds."""

import pytest
from auto_uploader.plugins.anime_crop import (
    AnimeCropPlugin,
    INSTAGRAM_MIN_RATIO,
    INSTAGRAM_MAX_RATIO,
    INSTAGRAM_SQUARE_RATIO,
)


def test_target_dimensions_auto_in_bounds():
    """Images already within [0.8, 1.91] should not be cropped in 'auto' mode."""
    plugin = AnimeCropPlugin({"target_aspect_ratio": "auto"})

    # 1:1 square (ratio 1.0)
    w, h, needs_crop = plugin.compute_target_dimensions(1000, 1000)
    assert not needs_crop
    assert w == 1000 and h == 1000

    # 4:5 portrait (ratio 0.8)
    w, h, needs_crop = plugin.compute_target_dimensions(1080, 1350)
    assert not needs_crop
    assert w == 1080 and h == 1350

    # 16:9 landscape (ratio 1.777, within 1.91)
    w, h, needs_crop = plugin.compute_target_dimensions(1920, 1080)
    assert not needs_crop
    assert w == 1920 and h == 1080


def test_target_dimensions_auto_too_tall():
    """Images with aspect ratio < 0.8 (e.g. 9:16) should minimally crop to 4:5."""
    plugin = AnimeCropPlugin({"target_aspect_ratio": "auto"})

    # 1080x1920 (ratio 0.5625 < 0.8)
    w, h, needs_crop = plugin.compute_target_dimensions(1080, 1920)
    assert needs_crop
    assert w == 1080
    assert h == 1350  # 1080 / 0.8 = 1350


def test_target_dimensions_auto_too_wide():
    """Images with aspect ratio > 1.91 (e.g. 21:9) should minimally crop to 1.91:1."""
    plugin = AnimeCropPlugin({"target_aspect_ratio": "auto"})

    # 2560x1080 (ratio 2.37 > 1.91)
    w, h, needs_crop = plugin.compute_target_dimensions(2560, 1080)
    assert needs_crop
    assert h == 1080
    assert w == int(round(1080 * 1.91))


def test_target_dimensions_explicit_modes():
    """Test explicit 4:5 and 1:1 modes."""
    plugin_square = AnimeCropPlugin({"target_aspect_ratio": "1:1"})
    w, h, needs_crop = plugin_square.compute_target_dimensions(1920, 1080)
    assert needs_crop
    assert w == 1080 and h == 1080

    plugin_portrait = AnimeCropPlugin({"target_aspect_ratio": "4:5"})
    w, h, needs_crop = plugin_portrait.compute_target_dimensions(1000, 1000)
    assert needs_crop
    assert w == 800 and h == 1000


def test_crop_box_face_at_top():
    """Face near top of a tall image: crop box must include face without going out of bounds."""
    plugin = AnimeCropPlugin({"padding_margin_pct": 0.1})
    img_w, img_h = 1000, 2000
    crop_w, crop_h = 1000, 1250  # 4:5 crop

    # Face located at y: [150, 450], center = 300
    faces = [[250, 150, 750, 450, 0.95]]

    x1, y1, x2, y2 = plugin.calculate_crop_box(img_w, img_h, crop_w, crop_h, faces)

    assert x1 == 0 and x2 == 1000
    assert 0 <= y1 <= img_h - crop_h
    assert y2 - y1 == crop_h
    # Face [150, 450] MUST be completely inside [y1, y2]
    assert y1 <= 150
    assert y2 >= 450


def test_crop_box_face_at_bottom():
    """Face near bottom of a tall image: crop box must contain bottom face."""
    plugin = AnimeCropPlugin({"padding_margin_pct": 0.1})
    img_w, img_h = 1000, 2000
    crop_w, crop_h = 1000, 1250

    # Face near bottom: [1500, 1850]
    faces = [[250, 1500, 750, 1850, 0.92]]

    x1, y1, x2, y2 = plugin.calculate_crop_box(img_w, img_h, crop_w, crop_h, faces)

    assert 0 <= y1 <= img_h - crop_h
    assert y2 - y1 == crop_h
    # Face [1500, 1850] must be contained
    assert y1 <= 1500
    assert y2 >= 1850


def test_crop_box_multiple_faces():
    """Multiple anime faces: crop box must contain both faces if height permits."""
    plugin = AnimeCropPlugin({"padding_margin_pct": 0.05})
    img_w, img_h = 1000, 2000
    crop_w, crop_h = 1000, 1250

    # Two characters: one at y=[300, 500], another at y=[700, 950]
    faces = [
        [100, 300, 400, 500, 0.9],
        [600, 700, 900, 950, 0.88],
    ]

    x1, y1, x2, y2 = plugin.calculate_crop_box(img_w, img_h, crop_w, crop_h, faces)

    assert y1 <= 300
    assert y2 >= 950
    assert 0 <= y1 <= img_h - crop_h


def test_crop_box_no_faces_center_fallback():
    """When no faces detected, fall back cleanly to center crop."""
    plugin = AnimeCropPlugin()
    img_w, img_h = 1000, 2000
    crop_w, crop_h = 1000, 1250

    x1, y1, x2, y2 = plugin.calculate_crop_box(img_w, img_h, crop_w, crop_h, [])

    expected_y = (2000 - 1250) // 2  # 375
    assert y1 == expected_y
    assert y2 == expected_y + 1250
