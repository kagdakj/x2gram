# Instagram Auto Uploader from X reposts

A modular, extensible background service that monitors art sources (Twitter/X reposts, local directories, etc.) and automatically uploads pictures and carousels to Instagram using [instagrapi](https://github.com/subzeroid/instagrapi).

Features an intelligent **Anime Face-Preserving Smart Cropping Plugin** powered by [hysts/anime-face-detector](https://github.com/hysts/anime-face-detector), ensuring images conform to Instagram aspect ratio limits without ever cutting off anime faces, hair, or expressions.

---

## Key Features

1. **Instagram Integration via `instagrapi`**:
   - Supports single photos, multi-photo carousels (albums), and videos/reels.
   - Session persistence via `session.json` to prevent repeated logins and avoid Instagram challenge flags.
   - Supports login by password or browser `sessionid` cookie.
   - Proxy support for secure operational environments.

2. **Anime Face-Preserving Smart Cropping (Plugin)**:
   - Powered by `anime-face-detector` (YOLOv3 / Faster R-CNN + HRNetV2).
   - **Minimal Cropping**: If an image is already within Instagram's feed aspect ratio range `[0.8, 1.91]`, it is **not cropped at all**.
   - If aspect ratio is outside bounds (e.g. 9:16 vertical wallpaper or ultra-wide banner), crops minimally (to 4:5 or 1.91:1) while calculating crop coordinates around detected anime faces.
   - Preserves headroom safety margins for ahoge, hair, ears, and chin.
   - Supports `"auto"` (minimal crop), `"4:5"` (portrait), `"1:1"` (square), or `"smart_pad"` (blur/solid background padding).

3. **Decoupled, Modular Architecture**:
   - **Sources**: Fetch posts from Twitter/X (`twitter-cli`), local folders (`incoming/`), or custom APIs.
   - **Plugins**: Extensible pipeline (`process_media`, `process_caption`, `process_post`, `on_post_uploaded`).
   - **Uploader**: Upload destination abstraction (`InstagramUploader`, easily extendable to other platforms).
   - **State Store**: SQLite-backed thread-safe tracking of seen posts, initialization baselines, and execution metrics.
   - **Engine**: Core scheduler and async worker with post delays, temporary directory isolation, and error tolerance.

4. **100% English & Clean Codebase**:
   - Free of third-party userbot dependencies (Hikka/Telethon).
   - Fully typed, tested, and documented.

---

## Architecture Overview

```
instagram_auto_uploader/
├── run.py                        # CLI entrypoint (start, check, crop, test, stats)
├── config.yaml                   # Active configuration file
├── config.example.yaml           # Documented template configuration
├── requirements.txt              # Project dependencies
├── auto_uploader/
│   ├── config.py                 # Pydantic configuration parser
│   ├── engine.py                 # Core orchestration & scheduling service
│   ├── models.py                 # Post, MediaItem, UploadResult models
│   ├── state.py                  # SQLite state database (seen posts, metrics)
│   ├── plugins/                  # Plugin pipeline
│   │   ├── base.py               # PluginManager and BasePlugin interface
│   │   ├── anime_crop.py         # Anime face detector & smart cropper
│   │   ├── caption.py            # Caption cleaner, author credit, hashtags
│   │   └── watermark.py          # Optional watermark plugin
│   ├── sources/                  # Content sources
│   │   ├── base.py               # BaseSource interface
│   │   ├── twitter_cli.py        # Twitter/X repost fetcher (via twitter-cli)
│   │   └── local_folder.py       # Local folder watcher for batch art
│   └── uploader/                 # Upload destinations
│       ├── base.py               # BaseUploader interface
│       └── instagram.py          # instagrapi client implementation
└── tests/                        # Comprehensive test suite
    ├── test_crop.py              # Math and bounding box tests
    ├── test_plugins.py           # Caption & plugin pipeline tests
    └── test_state.py             # SQLite persistence tests
```

---

## Installation

### 1. Clone Repository & Setup Environment

```bash
cd instagram_auto_uploader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Install Anime Face Detector (Recommended)

To enable anime face detection:

```bash
# Install PyTorch (CPU or CUDA version):
pip install torch torchvision

# Install anime face detector:
pip install anime-face-detector
```

> *Note: If `anime-face-detector` is not installed, the cropper gracefully falls back to center-weighted cropping.*

### 3. Install Twitter CLI (If using Twitter source)

```bash
pipx install twitter-cli
# or: pip install twitter-cli
```

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and configure your credentials:

```yaml
# 1. Instagram Authentication
instagram:
  username: "your_instagram_username"
  password: "your_instagram_password"
  session_id: ""                     # Or provide sessionid cookie
  session_file: "data/instagram_session.json"
  proxy: ""                          # Optional HTTP/SOCKS proxy

# 2. Twitter / X Source
twitter:
  enabled: true
  username: "target_twitter_account" # Without @
  auth_token: "your_auth_token"      # From x.com cookies
  ct0: "your_ct0_cookie"             # From x.com cookies
  only_reposts: true                 # Upload reposts only
  initial_load: 20

# 3. Anime Face Cropping Plugin
plugins:
  anime_crop:
    enabled: true
    target_aspect_ratio: "auto"      # "auto" (minimal crop), "4:5", "1:1", "1.91:1"
    model_type: "yolov3"             # "yolov3" or "faster-rcnn"
    device: "cpu"                    # "cpu" or "cuda"
    confidence_threshold: 0.5
    padding_margin_pct: 0.15         # Safety margin for hair, ears, chin
    fallback_mode: "center"          # "center" or "smart_pad"

  caption:
    enabled: true
    add_author_hashtag: true
    add_source_link: true
    custom_hashtags:
      - "#anime"
      - "#animeart"
      - "#illustration"

# 4. Service Settings
service:
  check_interval: 60                 # Seconds between polls
  post_delay: 5                      # Seconds to wait before each post
  initial_skip: true                 # Skip existing posts on first run
```

---

## CLI Usage

The service provides a unified CLI through `run.py`:

### 1. Test Connections
Test both Twitter and Instagram credentials:
```bash
python run.py test
```

### 2. Drag-and-Drop Folder Watcher (Recommended for Local Uploads)
Watch the `incoming/` directory continuously for new images:
```bash
python run.py watch
```
- **Single Photos**: Drag `art.png` into `incoming/`. (Optional: add `art.txt` for custom caption).
- **Carousels / Albums**: Drag a folder with multiple images into `incoming/my_album/`. All images will be uploaded together as a carousel post.
- Uploaded files are automatically organized and moved into `data/processed/YYYY-MM-DD/`.

### 3. Run Single Check & Upload
Poll the source once, process any new posts, and upload to Instagram:
```bash
python run.py check
```

### 4. Start Continuous Background Daemon
Run periodic polling loop across all enabled sources (Twitter / Local):
```bash
python run.py start
```

### 4. Test Anime Face Cropping on Any Image
Test the face detector and aspect ratio cropping on a local image:
```bash
python run.py crop --image art.png --output art_ig.jpg --ratio auto
```

### 5. View Statistics
```bash
python run.py stats
```

### 6. Reset History
Clear the seen posts database so past posts can be reprocessed:
```bash
python run.py reset
```

### 7. List Active Plugins
```bash
python run.py plugins
```

---

## Extending: Adding Custom Plugins

Creating a new plugin takes only a few lines. Create a class inheriting from `BasePlugin` and register it:

```python
from auto_uploader.plugins.base import BasePlugin, register_plugin
from auto_uploader.models import Post, MediaItem

@register_plugin("my_custom_filter")
class MyCustomFilter(BasePlugin):
    name = "my_custom_filter"
    description = "Logs post details and adds custom tags."
    priority = 60

    def process_caption(self, caption: str, post: Post) -> str:
        return f"{caption}\n\nUploaded with Custom Filter!"

    def process_media(self, media_item: MediaItem, post: Post) -> MediaItem:
        # Perform image manipulation or validation here
        return media_item
```

Plugins can be configured directly in `config.yaml` under `plugins.<plugin_name>`.

---

## Running Tests

Run the test suite using pytest:

```bash
pytest tests/ -v
```
