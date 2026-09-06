"""Unit tests for plugins and caption generation."""

from auto_uploader.models import Post
from auto_uploader.plugins.caption import CaptionPlugin
from auto_uploader.plugins.base import PluginManager


def test_caption_formatting():
    plugin = CaptionPlugin({
        "add_author_hashtag": True,
        "add_source_link": True,
        "custom_hashtags": ["#anime", "#fanart"],
    })

    post = Post(
        id="12345",
        source_name="twitter",
        author_username="artist_chan",
        text="Check out my new drawing! https://t.co/xyz123",
        url="https://x.com/artist_chan/status/12345",
    )

    caption = plugin.process_caption(post.text, post)

    # Must contain author hashtag
    assert "#artist_chan" in caption
    assert "@artist_chan" in caption

    # Must contain cleaned text without t.co
    assert "https://t.co" not in caption
    assert "Check out my new drawing!" in caption

    # Must contain source link
    assert "https://x.com/artist_chan/status/12345" in caption

    # Must contain custom hashtags
    assert "#anime" in caption
    assert "#fanart" in caption


def test_plugin_manager_pipeline():
    manager = PluginManager({
        "caption": {
            "enabled": True,
            "add_author_hashtag": False,
            "add_source_link": False,
            "custom_hashtags": ["#testtag"],
        }
    })

    post = Post(
        id="999",
        source_name="test",
        author_username="test_author",
        text="Sample artwork",
    )

    processed_post = manager.apply_post_pipeline(post)
    assert "#testtag" in processed_post.caption
    assert "Sample artwork" in processed_post.caption
