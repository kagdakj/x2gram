import json
from pathlib import Path

from auto_uploader.models import MediaItem, MediaType, Post


def test_post_roundtrip_keeps_local_paths_and_extra():
    item = MediaItem(media_type=MediaType.PHOTO, local_path=Path("/tmp/staged.jpg"),
                     original_path=Path("incoming/art.png"))
    post = Post(id="local_file_art", source_name="local", media_items=[item],
                extra_data={"file_path": "incoming/art.png"})
    restored = Post.from_dict(json.loads(json.dumps(post.to_dict())))
    assert restored.media_items[0].local_path == Path("incoming/art.png")
    assert restored.extra_data == {"file_path": "incoming/art.png"}
    assert restored.media_items[0].media_type == MediaType.PHOTO
