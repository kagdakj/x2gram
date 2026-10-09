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

    # Must contain author hashtag and credit
    assert "#artist_chan" in caption

    # Must contain cleaned text without t.co
    assert "https://t.co" not in caption
    assert "Check out my new drawing!" in caption

    # Must contain source link without http prefix to avoid link spam
    assert "x.com/artist_chan/status/12345" in caption

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


def _post(post_id="12345"):
    return Post(
        id=post_id,
        source_name="twitter",
        author_username="artist_chan",
        text="new drawing!",
        url=f"https://x.com/artist_chan/status/{post_id}",
    )


def test_variation_is_off_by_default():
    """Default config keeps the old behaviour: same tags, same order, link always."""
    plugin = CaptionPlugin({"custom_hashtags": ["#a", "#b", "#c"]})
    captions = {plugin.process_caption("", _post(str(i))) for i in range(5)}
    assert len(captions) == 5  # only the source link differs per id
    for caption in captions:
        assert caption.endswith("#a #b #c")
        assert "Source: x.com" in caption


def test_hashtag_subset_and_optional_link_are_seeded_by_post_id():
    config = {
        "custom_hashtags": ["#a", "#b", "#c", "#d", "#e", "#f"],
        "hashtag_count": [2, 3],
        "source_link_probability": 0.5,
        "vary_templates": True,
    }
    plugin = CaptionPlugin(config)

    # Same post -> identical caption (retries and fix-captions must match what was posted).
    assert plugin.process_caption("", _post("1")) == plugin.process_caption("", _post("1"))

    tag_sets, with_link = set(), 0
    for i in range(40):
        caption = plugin.process_caption("", _post(str(i)))
        tags = [w for w in caption.split() if w.startswith("#a") or w in ("#b", "#c", "#d", "#e", "#f")]
        assert 2 <= len(tags) <= 3
        tag_sets.add(tuple(tags))
        with_link += "Source: x.com" in caption
    assert len(tag_sets) > 5        # tags vary between posts
    assert 5 < with_link < 35       # link roughly half the time


def test_custom_template_and_block_formats():
    plugin = CaptionPlugin({
        "templates": ["{text}{nl}{nl}by #{author_tag}{nl}{nl}{hashtags}{nl}{nl}{source}"],
        "source_format": "→ {url_plain}",
        "custom_hashtags": ["#a", "#b"],
    })
    caption = plugin.process_caption("", _post())
    assert caption == (
        "new drawing!\n\n"
        "by #artist_chan\n\n"
        "#a #b\n\n"
        "→ x.com/artist_chan/status/12345"
    )


def test_empty_placeholders_do_not_leave_gaps():
    plugin = CaptionPlugin({
        "templates": ["{credit}{nl}{nl}{text}{nl}{nl}{source}{nl}{nl}{tags}"],
        "custom_hashtags": [],
    })
    bare = Post(id="7", source_name="local", text="just text")
    assert plugin.process_caption("", bare) == "just text"


def test_unknown_placeholder_renders_empty(caplog):
    plugin = CaptionPlugin({"templates": ["{text} {nope}"], "custom_hashtags": []})
    assert plugin.process_caption("", _post()).strip() == "new drawing!"
    assert "Unknown caption placeholder" in caplog.text


def test_vary_templates_uses_configured_list():
    templates = ["A: {text}", "B: {text}"]
    plugin = CaptionPlugin({"templates": templates, "vary_templates": True, "custom_hashtags": []})
    rendered = {plugin.process_caption("", _post(str(i)))[0] for i in range(20)}
    assert rendered == {"A", "B"}

    fixed = CaptionPlugin({"templates": templates, "custom_hashtags": []})
    assert all(fixed.process_caption("", _post(str(i))).startswith("A:") for i in range(5))
