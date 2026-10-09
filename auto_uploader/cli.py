"""Command line interface. Every command is a thin wrapper around the services."""

import asyncio
import time
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .context import AppContext
from .logging_setup import setup_logging
from .models import PostStatus
from .plugins.caption import PLACEHOLDER_HELP
from .selection import parse_selection
from .store import PostRecord

console = Console()

app = typer.Typer(
    help="Instagram Auto Uploader: X reposts / local art -> Instagram.\n\nRun without a command to open the TUI.",
    invoke_without_command=True,
    no_args_is_help=False,
    rich_markup_mode="rich",
    add_completion=False,
)
queue_app = typer.Typer(help="Inspect and manage the upload queue.", no_args_is_help=True)
tools_app = typer.Typer(help="Utilities: crop preview, caption repair, plugin list.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")
app.add_typer(tools_app, name="tools")

STATUS_STYLE = {
    PostStatus.QUEUED: "cyan",
    PostStatus.UPLOADING: "yellow",
    PostStatus.UPLOADED: "green",
    PostStatus.FAILED: "red",
    PostStatus.SKIPPED: "dim",
}


class _State:
    def __init__(self, config_path: str, verbose: bool):
        self.config_path = config_path
        self.verbose = verbose
        self._ctx: Optional[AppContext] = None

    def context(self, console_logging: bool = True) -> AppContext:
        if self._ctx is None:
            if not Path(self.config_path).is_file():
                console.print(f"[yellow]Config '{self.config_path}' not found; using defaults. "
                              f"Copy config.example.yaml to get started.[/yellow]")
            self._ctx = AppContext.load(self.config_path)
            svc = self._ctx.config.service
            setup_logging("DEBUG" if self.verbose else svc.log_level, svc.log_file, console=console_logging)
        return self._ctx


def _ctx(ctx: typer.Context) -> AppContext:
    return ctx.obj.context()


def _status_text(status: PostStatus) -> str:
    return f"[{STATUS_STYLE[status]}]{status.value}[/]"


def _records_table(records: List[PostRecord], title: str) -> Table:
    table = Table(title=title, header_style="bold magenta", expand=True)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Post ID", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Author", no_wrap=True)
    table.add_column("Media", justify="right")
    table.add_column("Tries", justify="right")
    table.add_column("Link / Error", overflow="fold")
    for i, r in enumerate(records, 1):
        detail = f"[red]{r.last_error}[/red]" if r.status == PostStatus.FAILED and r.last_error else r.link
        table.add_row(str(i), r.post_id, _status_text(r.status), r.source, f"@{r.author}" if r.author else "-",
                      str(r.media_count), str(r.attempts), detail)
    return table


def _print_counts(actx: AppContext) -> None:
    counts = actx.store.counts()
    console.print("  ".join(f"{_status_text(s)}: {counts[s]}" for s in PostStatus))


def _confirm(message: str, yes: bool) -> bool:
    return yes or typer.confirm(message, default=False)


def _report_batch(result) -> None:
    console.print(f"\n[green]Uploaded: {len(result.uploaded)}[/green]  "
                  f"[red]Failed: {len(result.failed)}[/red]  "
                  f"[dim]Not started: {result.not_started}[/dim]")
    for pid, err in result.failed.items():
        console.print(f"  [red]✗[/red] {pid}: {err}")
    if result.stopped_reason:
        console.print(f"[yellow]Stopped: {result.stopped_reason}[/yellow]")


# ── root ─────────────────────────────────────────────────────────────────


@app.callback()
def main(
    ctx: typer.Context,
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to YAML config."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
):
    ctx.obj = _State(config, verbose)
    if ctx.invoked_subcommand is None:
        tui(ctx)


@app.command()
def tui(ctx: typer.Context):
    """Open the interactive control center."""
    from .tui import run_tui

    run_tui(ctx.obj.config_path, verbose=ctx.obj.verbose)


@app.command()
def run(
    ctx: typer.Context,
    once: bool = typer.Option(False, "--once", help="Run a single poll + upload cycle and exit."),
    local: bool = typer.Option(False, "--local", help="Also watch the local incoming/ folder."),
    no_baseline: bool = typer.Option(False, "--no-baseline", help="Don't skip existing posts on the first run."),
):
    """Run the auto-upload daemon (poll sources, upload queue, repeat)."""
    obj: _State = ctx.obj
    actx = AppContext.load(obj.config_path)
    if local:
        actx.config.local.enabled = True
    if no_baseline:
        actx.config.service.initial_skip = False
    svc = actx.config.service
    setup_logging("DEBUG" if obj.verbose else svc.log_level, svc.log_file)

    names = ", ".join(s.name for s in actx.sources) or "none"
    console.print(f"[bold]Sources:[/bold] {names}   [bold]Interval:[/bold] {svc.check_interval}s   "
                  f"[bold]Delay:[/bold] {svc.post_delay_min}-{svc.post_delay_max}s   "
                  f"[bold]Rate limit:[/bold] {'on' if svc.rate_limit_enabled else 'off'}")
    if actx.config.local.enabled:
        console.print(f"Drop images, videos or album folders into [cyan]{Path(actx.config.local.watch_dir).resolve()}[/cyan]")
    console.print("[dim]Ctrl+C to stop.[/dim]\n")

    try:
        asyncio.run(actx.daemon.run(once=once))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    finally:
        actx.stager.cleanup_all()
    _print_counts(actx)


# ── queue ────────────────────────────────────────────────────────────────


@queue_app.command("list")
def queue_list(
    ctx: typer.Context,
    status: Optional[List[PostStatus]] = typer.Option(None, "--status", "-s",
                                                      help="Filter (repeatable). Default: queued + failed."),
    limit: int = typer.Option(100, "--limit", "-n"),
):
    """Show queued / failed posts."""
    actx = _ctx(ctx)
    statuses = status or [PostStatus.QUEUED, PostStatus.UPLOADING, PostStatus.FAILED]
    records = actx.store.list(statuses, limit=limit)
    if not records:
        console.print("[dim]Nothing here. Try `queue fetch` or `queue fetch --deep`.[/dim]")
    else:
        console.print(_records_table(records, "Upload queue"))
    _print_counts(actx)


@queue_app.command("fetch")
def queue_fetch(
    ctx: typer.Context,
    deep: bool = typer.Option(False, "--deep", help="Pull the larger backlog (twitter.initial_load)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would be queued."),
):
    """Poll sources now and queue new posts."""
    actx = _ctx(ctx)
    result = asyncio.run(actx.ingest.fetch(deep=deep, dry_run=dry_run))
    if dry_run:
        table = Table(title=f"Would queue {len(result.new_posts)} post(s)", header_style="bold magenta")
        for col in ("#", "Post ID", "Author", "Media", "Link"):
            table.add_column(col)
        for i, p in enumerate(result.new_posts, 1):
            table.add_row(str(i), p.id, f"@{p.author_username}", p.media_summary, p.link)
        console.print(table)
        return
    for err in result.errors:
        console.print(f"[red]{err}[/red]")
    _print_counts(actx)


@queue_app.command("upload")
def queue_upload(
    ctx: typer.Context,
    post_ids: Optional[List[str]] = typer.Argument(None, help="Post IDs to upload."),
    index: Optional[str] = typer.Option(None, "--index", "-i",
                                        help="Positions from `queue list`, e.g. '1-3,5'."),
    next_n: int = typer.Option(0, "--next", "-n", help="Upload the N oldest queued posts."),
    force: bool = typer.Option(False, "--force", help="Ignore daily limit / min interval."),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Upload specific queued posts right now."""
    actx = _ctx(ctx)
    ids = list(post_ids or [])
    if index:
        records = actx.store.list([PostStatus.QUEUED, PostStatus.UPLOADING, PostStatus.FAILED])
        ids += [records[i - 1].post_id for i in parse_selection(index, len(records))]
    if next_n:
        ids += [r.post_id for r in actx.store.next_queued(next_n)]
    ids = list(dict.fromkeys(ids))
    if not ids:
        console.print("[yellow]Nothing selected. Pass IDs, --index or --next.[/yellow]")
        raise typer.Exit(1)

    if not _confirm(f"Upload {len(ids)} post(s) to Instagram?", yes):
        raise typer.Exit()
    result = asyncio.run(actx.publisher.upload(ids, respect_limits=not force))
    _report_batch(result)


@queue_app.command("retry")
def queue_retry(
    ctx: typer.Context,
    post_ids: Optional[List[str]] = typer.Argument(None, help="IDs to re-queue (default: all failed)."),
):
    """Move failed (or skipped) posts back into the queue."""
    actx = _ctx(ctx)
    count = actx.store.requeue(post_ids or None)
    console.print(f"Re-queued {count} post(s).")


@queue_app.command("remove")
def queue_remove(ctx: typer.Context, post_ids: List[str] = typer.Argument(..., help="IDs to delete.")):
    """Delete posts from the store (they may be fetched again later)."""
    actx = _ctx(ctx)
    console.print(f"Removed {actx.store.delete(post_ids)} post(s).")


@queue_app.command("skip")
def queue_skip(ctx: typer.Context, post_ids: List[str] = typer.Argument(..., help="IDs to never upload.")):
    """Mark posts as skipped so they stay out of the queue."""
    actx = _ctx(ctx)
    count = 0
    for pid in post_ids:
        record = actx.store.get(pid)
        if record and record.status in (PostStatus.QUEUED, PostStatus.FAILED):
            actx.store.set_status(pid, PostStatus.SKIPPED)
            count += 1
    console.print(f"Skipped {count} post(s).")


@queue_app.command("clear")
def queue_clear(
    ctx: typer.Context,
    status: Optional[PostStatus] = typer.Option(None, "--status", "-s",
                                                help="Only this status (default: queued + failed)."),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Delete queued/failed entries."""
    actx = _ctx(ctx)
    label = status.value if status else "queued + failed"
    if status in (PostStatus.UPLOADED, PostStatus.SKIPPED):
        console.print("[yellow]Warning: this deletes history; those posts can be uploaded again.[/yellow]")
    if not _confirm(f"Delete all {label} entries?", yes):
        raise typer.Exit()
    console.print(f"Deleted {actx.store.clear(status)} entries.")


# ── sync / history / stats ───────────────────────────────────────────────


@app.command()
def sync(
    ctx: typer.Context,
    ig_limit: int = typer.Option(50, "--ig-limit", "-l", help="Recent Instagram posts to inspect."),
    x_limit: int = typer.Option(50, "--x-limit", "-t", help="Recent X reposts to fetch."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only list missing posts."),
    select: Optional[str] = typer.Option(None, "--select", "-s", help="e.g. '1-3,5' or 'all'."),
    queue_only: bool = typer.Option(False, "--queue", help="Add selection to the queue instead of uploading now."),
    include_uploaded: bool = typer.Option(False, "--include-uploaded",
                                          help="Also list posts our DB says were uploaded."),
    force: bool = typer.Option(False, "--force", help="Ignore daily limit / min interval."),
):
    """Find X reposts that are missing on Instagram and upload / queue them."""
    actx = _ctx(ctx)
    try:
        report = asyncio.run(actx.sync.scan(ig_limit, x_limit, include_uploaded))
    except Exception as e:
        console.print(f"[red]Sync failed: {e}[/red]")
        raise typer.Exit(1)

    console.print(f"Instagram scanned: {report.ig_scanned}   X scanned: {report.x_scanned}   "
                  f"Linked tweet IDs: {len(report.ig_tweet_ids)}   Newly marked uploaded: {report.marked_uploaded}")
    if report.hidden_uploaded:
        console.print(f"[dim]{report.hidden_uploaded} post(s) are uploaded per DB but outside the scanned "
                      f"Instagram window (use --include-uploaded to list them).[/dim]")
    if not report.missing:
        console.print("[green]Nothing missing.[/green]")
        return

    table = Table(title=f"{len(report.missing)} missing post(s)", header_style="bold magenta", expand=True)
    for col in ("#", "Post ID", "Author", "Media", "DB status", "Text"):
        table.add_column(col, overflow="ellipsis", no_wrap=col != "Text")
    for i, p in enumerate(report.missing, 1):
        status = report.statuses.get(p.id)
        table.add_row(str(i), p.id, f"@{p.author_username}", p.media_summary,
                      _status_text(status) if status else "[dim]new[/dim]",
                      (p.text or "").replace("\n", " ")[:60])
    console.print(table)

    if dry_run:
        return
    if select is None:
        select = typer.prompt("Select posts ('1,3', '1-5', 'all', 'q' to cancel)", default="q")
    chosen = [report.missing[i - 1] for i in parse_selection(select, len(report.missing))]
    if not chosen:
        console.print("Nothing selected.")
        return

    ids = actx.sync.enqueue(chosen)
    if queue_only:
        console.print(f"Queued {len(ids)} post(s). The daemon will upload them.")
        return
    _report_batch(asyncio.run(actx.publisher.upload(ids, respect_limits=not force)))


@app.command()
def history(ctx: typer.Context, limit: int = typer.Option(50, "--limit", "-n"),
            all_: bool = typer.Option(False, "--all", help="Include skipped posts.")):
    """Show recently uploaded posts."""
    actx = _ctx(ctx)
    statuses = [PostStatus.UPLOADED, PostStatus.SKIPPED] if all_ else [PostStatus.UPLOADED]
    records = actx.store.list(statuses, limit=limit, newest_first=True)
    table = Table(title="Upload history", header_style="bold magenta", expand=True)
    for col in ("#", "Post ID", "Status", "Uploaded (UTC)", "Author", "Link"):
        table.add_column(col, overflow="fold")
    for i, r in enumerate(records, 1):
        table.add_row(str(i), r.post_id, _status_text(r.status), r.uploaded_at or "-",
                      f"@{r.author}" if r.author else "-", r.link)
    console.print(table)


@app.command()
def stats(ctx: typer.Context):
    """Counters, queue state and rate-limit status."""
    actx = _ctx(ctx)
    svc = actx.config.service
    metrics = actx.store.get_stats()
    _print_counts(actx)
    console.print(f"Checked: {metrics.get('checked', 0)}   Published: {metrics.get('published', 0)}   "
                  f"Errors: {metrics.get('errors', 0)}   Baseline done: {'yes' if actx.store.is_initialized() else 'no'}")
    used = actx.store.uploads_since(24)
    console.print(f"Uploads in last 24h: {used}"
                  + (f"/{svc.max_posts_per_day}" if svc.rate_limit_enabled else " (rate limit off)"))
    last = actx.store.last_upload_time()
    if last:
        console.print(f"Last upload: {int(time.time() - last)}s ago")
    blocked = actx.policy.blocked_reason()
    console.print(f"Next upload: {'[yellow]' + blocked + '[/yellow]' if blocked else '[green]allowed now[/green]'}")


# ── doctor / reset ───────────────────────────────────────────────────────


@app.command()
def doctor(ctx: typer.Context, offline: bool = typer.Option(False, "--offline", help="Only validate config.")):
    """Validate config and test Instagram / source connections."""
    actx = _ctx(ctx)
    checks = actx.maintenance.check_environment() + actx.maintenance.check_config()
    if not offline:
        with console.status("Testing connections..."):
            checks += actx.maintenance.check_connections()
    for c in checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        console.print(f"{mark} {c.name}" + ("" if c.ok else f"  [dim]{c.detail}[/dim]"))
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def reset(
    ctx: typer.Context,
    restore_files: bool = typer.Option(False, "--restore-files", "-r",
                                       help="Move data/processed/* back into incoming/."),
    logout: bool = typer.Option(False, "--logout",
                                help="Also drop the saved Instagram session (changes the device fingerprint)."),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Forget upload history + baseline (the Instagram session is kept)."""
    actx = _ctx(ctx)
    console.print("[yellow]Posts already on Instagram may be uploaded again after this.[/yellow]")
    if logout:
        console.print("[yellow]--logout drops the saved device fingerprint; the next login looks like a "
                      "new phone to Instagram.[/yellow]")
    if not _confirm("Reset upload history?", yes):
        raise typer.Exit()
    for line in actx.maintenance.reset(restore_files, logout=logout):
        console.print(f"✓ {line}")


# ── tools ────────────────────────────────────────────────────────────────


@tools_app.command("crop")
def tools_crop(
    image: Path = typer.Argument(..., exists=True, dir_okay=False, help="Input image."),
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    ratio: str = typer.Option("auto", "--ratio", "-r", help="auto | 4:5 | 1:1 | 1.91:1"),
    model: str = typer.Option("yolov3", help="yolov3 | faster-rcnn"),
    device: str = typer.Option("cpu", help="cpu | cuda"),
    fallback: str = typer.Option("center", help="center | smart_pad"),
):
    """Preview face-preserving crop on a single image."""
    import shutil

    from .models import MediaItem, MediaType, Post
    from .plugins.anime_crop import AnimeCropPlugin

    setup_logging("INFO")
    out_path = output or image.with_name(f"{image.stem}_cropped.jpg")
    cropper = AnimeCropPlugin({"target_aspect_ratio": ratio, "model_type": model,
                               "device": device, "fallback_mode": fallback})
    item = MediaItem(media_type=MediaType.PHOTO, local_path=image, original_path=image)
    processed = cropper.process_media(item, Post(id="crop_preview", source_name="cli"))
    if not processed.is_valid_local:
        console.print("[red]Failed to process image.[/red]")
        raise typer.Exit(1)
    if processed.local_path == image:
        shutil.copyfile(image, out_path)
    elif processed.local_path != out_path:
        shutil.move(str(processed.local_path), out_path)
    console.print(f"Output: {out_path}  ({processed.width}x{processed.height})")
    for i, b in enumerate(processed.metadata.get("anime_face_boxes", []), 1):
        console.print(f"  face {i}: x1={b[0]:.0f} y1={b[1]:.0f} x2={b[2]:.0f} y2={b[3]:.0f} score={b[4]:.2f}")


@tools_app.command("fix-captions")
def tools_fix_captions(
    ctx: typer.Context,
    limit: int = typer.Option(30, "--limit", "-n", help="Recent Instagram posts to inspect."),
    delay: float = typer.Option(4, "--delay", help="Seconds between edits."),
    force: bool = typer.Option(False, "--force", help="Rewrite captions even if present."),
):
    """Restore captions Instagram stripped from recent posts."""
    actx = _ctx(ctx)
    report = actx.maintenance.repair_captions(limit, force, delay, progress=console.print)
    console.print(f"\nRepaired: {report.repaired}   Skipped: {report.skipped}   Errors: {len(report.errors)}")
    if report.stopped_reason:
        console.print(f"[yellow]{report.stopped_reason}[/yellow]")


@tools_app.command("caption")
def tools_caption(
    ctx: typer.Context,
    post_ids: Optional[List[str]] = typer.Argument(None, help="Stored post IDs (default: a sample post)."),
    count: int = typer.Option(3, "--count", "-n", help="How many queued posts to render when no ID is given."),
    all_templates: bool = typer.Option(False, "--all-templates",
                                       help="Render every configured template for the same post."),
):
    """Preview the caption format without uploading anything."""
    from .models import MediaItem, MediaType, Post

    actx = _ctx(ctx)
    plugin = next((p for p in actx.plugins.plugins if p.name == "caption"), None)
    if plugin is None:
        console.print("[yellow]The caption plugin is disabled in config.[/yellow]")
        raise typer.Exit(1)

    posts = []
    for pid in post_ids or []:
        record = actx.store.get(pid)
        post = record.post() if record else None
        if post is None:
            console.print(f"[yellow]{pid}: not stored (or has no payload).[/yellow]")
        else:
            posts.append(post)
    if not post_ids:
        posts = [r.post() for r in actx.store.next_queued(count) if r.post()]
    if not posts:
        posts = [Post(id="1234567890", source_name="twitter", author_username="artist_chan",
                      author_name="Artist Chan", text="Sample post text.\nSecond line.",
                      url="https://x.com/artist_chan/status/1234567890",
                      media_items=[MediaItem(media_type=MediaType.PHOTO, url="https://example.com/a.jpg")])]
        console.print("[dim]No stored posts used; showing a sample post.[/dim]")

    import random as _random

    for post in posts:
        console.print(f"\n[bold cyan]{post.id}[/bold cyan] @{post.author_username}")
        if all_templates:
            for i, template in enumerate(plugin.available_templates(), 1):
                fields = plugin.build_fields(post, _random.Random(f"caption:{post.id}"))
                console.print(f"[dim]-- template {i} --[/dim]")
                console.print(escape(plugin.render(template, fields)))
        else:
            console.print(escape(plugin.process_caption("", post)))
    console.print(f"\n[dim]Placeholders: {', '.join('{' + p + '}' for p in PLACEHOLDER_HELP)}[/dim]")
    console.print("[dim]Edit plugins.caption.templates / credit_format / source_format / tags_format "
                  "in your config.[/dim]")


@tools_app.command("session")
def tools_session(
    ctx: typer.Context,
    bump: bool = typer.Option(False, "--bump", help="Rewrite the saved session with the current app version."),
):
    """Show the saved Instagram device/app profile (no login)."""
    import json as _json

    actx = _ctx(ctx)
    uploader = actx.uploader
    settings = uploader._load_settings()
    if not settings:
        console.print(f"[yellow]No saved session at {uploader.session_file}.[/yellow]")
        raise typer.Exit(1)

    device = settings.get("device_settings", {})
    from instagrapi import config as ig_config

    console.print(f"Session file : {uploader.session_file}")
    console.print(f"Device       : {device.get('manufacturer')} {device.get('model')} "
                  f"(Android {device.get('android_release')})")
    console.print(f"App version  : {device.get('app_version')}")
    console.print(f"instagrapi   : default {ig_config.DEFAULT_APP_VERSION}, "
                  f"known: {', '.join(ig_config.APP_SETTINGS)}")
    console.print(f"uuids        : {len(settings.get('uuids', {}))} stored "
                  f"(kept across logins; losing them looks like a new phone)")
    console.print(f"Locale/TZ    : {settings.get('locale')} / {settings.get('country')} / "
                  f"{settings.get('timezone_offset')}")

    outdated = device.get("app_version") != ig_config.DEFAULT_APP_VERSION
    if not outdated:
        console.print("[green]App profile is current.[/green]")
        return
    console.print("[yellow]App profile is older than instagrapi's default; Instagram may refuse to log in "
                 "(\"Your version of Instagram is out of date\").[/yellow]")
    if not bump:
        console.print("[dim]It is refreshed automatically on the next login, or now with --bump.[/dim]")
        return

    client = uploader._new_client()
    uploader._load_into(client)
    uploader._save_settings(client)
    console.print(f"[green]Updated to {ig_config.DEFAULT_APP_VERSION}; uuids and device model unchanged "
                  f"(backup: {uploader.session_file.with_suffix('.json.bak')}).[/green]")


@tools_app.command("plugins")
def tools_plugins(ctx: typer.Context):
    """List enabled plugins in pipeline order."""
    actx = _ctx(ctx)
    for p in actx.plugins.plugins:
        console.print(f"[{p.priority:02d}] [bold]{p.name}[/bold]: {p.description}")


if __name__ == "__main__":
    app()
