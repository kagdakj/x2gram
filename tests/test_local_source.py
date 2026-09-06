"""Unit tests for LocalFolderSource drag-and-drop processing."""

import tempfile
from pathlib import Path
from PIL import Image

from auto_uploader.models import MediaType, UploadResult
from auto_uploader.sources.local_folder import LocalFolderSource


def test_local_folder_single_file_and_caption():
    with tempfile.TemporaryDirectory() as tmpdir:
        watch_dir = Path(tmpdir) / "incoming"
        proc_dir = Path(tmpdir) / "processed"
        watch_dir.mkdir()
        proc_dir.mkdir()

        # Create dummy image and sidecar caption
        img_path = watch_dir / "art_01.jpg"
        Image.new("RGB", (200, 200), color="blue").save(img_path)

        txt_path = watch_dir / "art_01.txt"
        txt_path.write_text("Custom anime caption #cute", encoding="utf-8")

        source = LocalFolderSource({
            "watch_dir": str(watch_dir),
            "processed_dir": str(proc_dir),
            "move_to_processed": True,
        })

        posts = source.fetch_new_posts()
        assert len(posts) == 1
        post = posts[0]

        assert post.id.startswith("local_file_art_01")
        assert post.text == "Custom anime caption #cute"
        assert len(post.media_items) == 1
        assert post.media_items[0].media_type == MediaType.PHOTO

        # Simulate successful upload
        source.on_post_uploaded(post, UploadResult(success=True, media_id="ig_123"))

        # Original files should now be moved to processed
        assert not img_path.exists()
        assert not txt_path.exists()
        processed_files = list(proc_dir.rglob("*.jpg"))
        assert len(processed_files) == 1
        assert processed_files[0].name == "art_01.jpg"


def test_local_folder_album_carousel():
    with tempfile.TemporaryDirectory() as tmpdir:
        watch_dir = Path(tmpdir) / "incoming"
        proc_dir = Path(tmpdir) / "processed"
        watch_dir.mkdir()
        proc_dir.mkdir()

        # Create album folder with 3 images
        album_dir = watch_dir / "my_carousel"
        album_dir.mkdir()
        for i in range(1, 4):
            Image.new("RGB", (300, 300), color="red").save(album_dir / f"0{i}.png")

        # Sidecar caption inside album
        (album_dir / "caption.txt").write_text("Album carousel caption #art", encoding="utf-8")

        source = LocalFolderSource({
            "watch_dir": str(watch_dir),
            "processed_dir": str(proc_dir),
            "move_to_processed": True,
        })

        posts = source.fetch_new_posts()
        assert len(posts) == 1
        post = posts[0]

        assert post.id.startswith("local_album_my_carousel")
        assert post.text == "Album carousel caption #art"
        assert len(post.media_items) == 3
        assert all(m.media_type == MediaType.PHOTO for m in post.media_items)

        # Simulate upload
        source.on_post_uploaded(post, UploadResult(success=True, media_id="ig_carousel_456"))

        # Entire folder should be moved out of incoming
        assert not album_dir.exists()
        processed_albums = [d for d in proc_dir.rglob("my_carousel") if d.is_dir()]
        assert len(processed_albums) == 1
        assert len(list(processed_albums[0].glob("*.png"))) == 3
