import asyncio
import time

from auto_uploader.models import MediaItem, MediaType, Post, PostStatus
from auto_uploader.sources.local_folder import LocalFolderSource
from auto_uploader.uploader.instagram import InstagramMedia

from .conftest import make_image, tweet


def run(coro):
    return asyncio.run(coro)


# ── ingest ────────────────────────────────────────────────────────────────


def test_first_poll_is_baseline_then_new_posts_queue(app_ctx, tmp_path):
    ctx, source, _ = app_ctx
    img = make_image(tmp_path / "a.jpg")
    source.posts = [tweet("100", img), tweet("101", img), Post(id="102", source_name="twitter")]  # 102: no media

    result = run(ctx.ingest.fetch())
    assert result.baseline and result.skipped == 2 and result.queued == 0
    assert ctx.store.is_initialized()

    source.posts.append(tweet("103", img))
    result = run(ctx.ingest.fetch())
    assert not result.baseline and result.queued == 1
    assert ctx.store.get("103").status == PostStatus.QUEUED


def test_deep_fetch_queues_backlog_and_prevents_baseline(app_ctx, tmp_path):
    """Regression: seed + initial_skip used to silently drop the seeded queue."""
    ctx, source, _ = app_ctx
    img = make_image(tmp_path / "a.jpg")
    source.posts = [tweet(str(i), img) for i in range(5)]

    result = run(ctx.ingest.fetch(deep=True))
    assert result.queued == 5 and source.calls == [200]

    result = run(ctx.ingest.fetch())
    assert result.queued == 0
    assert ctx.store.counts()[PostStatus.QUEUED] == 5
    assert ctx.store.counts()[PostStatus.SKIPPED] == 0


def test_dry_run_stores_nothing(app_ctx, tmp_path):
    ctx, source, _ = app_ctx
    source.posts = [tweet("1", make_image(tmp_path / "a.jpg"))]
    result = run(ctx.ingest.fetch(dry_run=True))
    assert len(result.new_posts) == 1
    assert ctx.store.get("1") is None


# ── publisher ─────────────────────────────────────────────────────────────


def test_local_post_survives_queue_and_is_moved(app_ctx, tmp_path):
    """Regression: queued local posts lost their file path and always failed."""
    ctx, _, uploader = app_ctx
    watch, processed = tmp_path / "incoming", tmp_path / "processed"
    art = make_image(watch / "art.png")
    local = LocalFolderSource({"watch_dir": str(watch), "processed_dir": str(processed)})
    ctx.__dict__["sources"] = [local]
    ctx.config.service.initial_skip = True

    fetched = run(ctx.ingest.fetch())
    assert fetched.queued == 1  # local drops are never baseline-skipped
    post_id = fetched.new_posts[0].id

    result = run(ctx.publisher.upload_next())
    assert result.uploaded == [post_id]
    assert ctx.store.get(post_id).status == PostStatus.UPLOADED
    assert not art.exists() and list(processed.rglob("art.png"))
    assert uploader.uploaded[0].caption  # caption plugin ran
    assert not any(ctx.stager.temp_root.iterdir())  # temp cleaned


def test_failure_is_recorded_and_retryable(app_ctx, tmp_path):
    ctx, _, uploader = app_ctx
    ctx.store.add_posts([tweet("1", make_image(tmp_path / "a.jpg"))])
    uploader.fail_with = "network down"

    result = run(ctx.publisher.upload(["1"]))
    assert result.failed == {"1": "network down"}
    record = ctx.store.get("1")
    assert record.status == PostStatus.FAILED and record.attempts == 1

    uploader.fail_with = None
    assert ctx.store.requeue(["1"]) == 1
    assert run(ctx.publisher.upload(["1"])).uploaded == ["1"]
    assert ctx.store.get("1").attempts == 2


def test_missing_media_fails_cleanly(app_ctx):
    ctx, _, _ = app_ctx
    ctx.store.add_posts([Post(id="1", source_name="twitter",
                              media_items=[MediaItem(media_type=MediaType.PHOTO)])])
    result = run(ctx.publisher.upload(["1"]))
    assert "No valid media" in result.failed["1"]


def test_rate_limit_applies_unless_forced(app_ctx, tmp_path):
    ctx, _, _ = app_ctx
    svc = ctx.config.service
    svc.rate_limit_enabled, svc.max_posts_per_day, svc.min_post_interval = True, 1, 0
    img = make_image(tmp_path / "a.jpg")
    ctx.store.add_posts([tweet(str(i), img) for i in (1, 2, 3)])

    result = run(ctx.publisher.upload(["1", "2"]))
    assert result.uploaded == ["1"]
    assert "daily limit" in result.stopped_reason and result.skipped == {"2": result.stopped_reason}
    assert ctx.store.get("2").status == PostStatus.QUEUED

    assert run(ctx.publisher.upload(["2"], respect_limits=False)).uploaded == ["2"]


def test_action_block_pauses_even_forced_uploads(app_ctx, tmp_path):
    ctx, _, uploader = app_ctx
    img = make_image(tmp_path / "a.jpg")
    ctx.store.add_posts([tweet(str(i), img) for i in (1, 2)])
    uploader.fail_with = "feedback_required: try again later"

    result = run(ctx.publisher.upload(["1", "2"], respect_limits=False))
    assert list(result.failed) == ["1"] and "2" in result.skipped
    assert ctx.policy.paused_until > time.time()

    uploader.fail_with = None
    result = run(ctx.publisher.upload(["2"], respect_limits=False))
    assert result.uploaded == [] and "paused" in result.stopped_reason


def test_caption_stripped_slows_down(app_ctx, tmp_path):
    ctx, _, uploader = app_ctx
    ctx.config.service.post_delay_min, ctx.config.service.post_delay_max = 10, 20
    uploader.caption_stripped = True
    ctx.store.add_posts([tweet("1", make_image(tmp_path / "a.jpg"))])
    run(ctx.publisher.upload(["1"]))
    assert ctx.policy.delay_range() == (20, 40)
    assert ctx.policy.blocked_reason() is not None  # short cooldown


def test_uploaded_posts_are_not_uploaded_twice(app_ctx, tmp_path):
    ctx, _, uploader = app_ctx
    ctx.store.add_posts([tweet("1", make_image(tmp_path / "a.jpg"))])
    run(ctx.publisher.upload(["1"]))
    result = run(ctx.publisher.upload(["1"]))
    assert result.skipped == {"1": "status is uploaded"} and len(uploader.uploaded) == 1


def test_publisher_works_across_event_loops(app_ctx, tmp_path):
    ctx, _, _ = app_ctx
    img = make_image(tmp_path / "a.jpg")
    ctx.store.add_posts([tweet("1", img), tweet("2", img)])
    assert run(ctx.publisher.upload(["1"])).uploaded == ["1"]
    assert run(ctx.publisher.upload(["2"])).uploaded == ["2"]


# ── daemon ────────────────────────────────────────────────────────────────


def test_daemon_single_cycle(app_ctx, tmp_path):
    ctx, source, uploader = app_ctx
    ctx.config.service.initial_skip = False
    source.posts = [tweet("1", make_image(tmp_path / "a.jpg"))]
    run(ctx.daemon.run(once=True))
    assert [p.id for p in uploader.uploaded] == ["1"]
    assert not ctx.daemon.running


def test_daemon_stop_interrupts_sleep(app_ctx):
    ctx, _, _ = app_ctx
    ctx.config.service.check_interval = 3600

    async def scenario():
        task = asyncio.create_task(ctx.daemon.run())
        await asyncio.sleep(0.3)
        assert ctx.daemon.state == "sleeping"
        ctx.daemon.stop()
        await asyncio.wait_for(task, 2)

    run(scenario())
    assert ctx.daemon.state == "stopped"


# ── sync + maintenance ────────────────────────────────────────────────────


def test_sync_finds_missing_and_marks_existing(app_ctx, tmp_path):
    ctx, source, uploader = app_ctx
    ctx.__dict__["sync"] = type(ctx.sync)(ctx.store, uploader, source, ctx.events)
    img = make_image(tmp_path / "a.jpg")
    source.posts = [tweet("10003", img), tweet("10002", img), tweet("10001", img),
                    Post(id="10000", source_name="twitter")]
    uploader.media = [InstagramMedia("pk1", "c1", "Source: x.com/artist/status/10001", 1_700_000_000)]
    ctx.store.add_posts([tweet("10002", img)])
    ctx.store.mark_failed("10002", "old error")

    report = run(ctx.sync.scan(10, 10))
    assert [p.id for p in report.missing] == ["10002", "10003"]  # oldest first
    assert report.statuses == {"10002": PostStatus.FAILED}
    assert ctx.store.get("10001").status == PostStatus.UPLOADED

    ids = ctx.sync.enqueue(report.missing)
    assert all(ctx.store.get(i).status == PostStatus.QUEUED for i in ids)
    assert run(ctx.publisher.upload(ids)).uploaded == ids

    # Uploaded per DB but not visible on IG -> hidden unless asked
    report = run(ctx.sync.scan(10, 10))
    assert report.missing == [] and report.hidden_uploaded == 2
    assert len(run(ctx.sync.scan(10, 10, include_uploaded=True)).missing) == 2


def test_repair_captions_uses_stored_text(app_ctx, tmp_path):
    ctx, _, uploader = app_ctx
    ctx.store.add_posts([tweet("555", make_image(tmp_path / "a.jpg"))])
    ctx.store.mark_uploaded("555")
    uploaded_at = ctx.store.upload_timeline()[0][1]
    uploader.media = [InstagramMedia("pk9", "code9", "", uploaded_at + 5),
                      InstagramMedia("pk8", "code8", "has caption", uploaded_at)]

    report = ctx.maintenance.repair_captions(limit=10, delay=0)
    assert report.repaired == 1
    assert uploader.edits[0][0] == "pk9" and "art 555" in uploader.edits[0][1]
