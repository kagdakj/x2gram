import asyncio

from auto_uploader.models import PostStatus
from auto_uploader.tui.app import UploaderApp
from auto_uploader.tui.screens import ConfirmScreen

from .conftest import make_image, tweet


def test_tui_select_and_upload(app_ctx, tmp_path):
    ctx, _, uploader = app_ctx
    img = make_image(tmp_path / "a.jpg")
    ctx.store.add_posts([tweet(str(i), img) for i in (1, 2, 3)])

    async def scenario():
        app = UploaderApp(ctx)
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#queue-table")
            assert table.row_count == 3
            await pilot.press("space", "space")          # select rows 1 and 2
            assert table.selected == {"1", "2"}
            await pilot.press("u")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click("#choice-upload")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [p.id for p in uploader.uploaded] == ["1", "2"]
            assert table.row_count == 1

            await pilot.press("k")                         # skip the remaining one
            assert ctx.store.get("3").status == PostStatus.SKIPPED

            await pilot.press("2")
            assert app.query_one("TabbedContent").active == "tab-sync"
            await pilot.press("question_mark")
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(scenario())


def test_tui_daemon_toggle(app_ctx):
    ctx, _, _ = app_ctx
    ctx.config.service.check_interval = 3600

    async def scenario():
        app = UploaderApp(ctx)
        async with app.run_test() as pilot:
            await pilot.press("s")
            await pilot.pause(0.3)
            assert ctx.daemon.running
            await pilot.press("s")
            await app.workers.wait_for_complete()
            assert not ctx.daemon.running

    asyncio.run(scenario())
