"""Textual control center for the uploader."""

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.worker import Worker, WorkerState
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label, RichLog, Select, Static, TabbedContent, TabPane,
)

from ..context import AppContext
from ..models import Post, PostStatus
from ..services.events import Event
from ..services.publisher import BatchResult
from ..store import PostRecord
from .screens import ConfirmScreen, HelpScreen
from .widgets import SelectableTable, status_text

QUEUE_FILTERS = [
    ("Queued + failed", "active"),
    ("Queued", "queued"),
    ("Failed", "failed"),
    ("Skipped", "skipped"),
    ("Uploaded", "uploaded"),
    ("Everything", "all"),
]
FILTER_STATUSES = {
    "active": [PostStatus.QUEUED, PostStatus.UPLOADING, PostStatus.FAILED],
    "queued": [PostStatus.QUEUED, PostStatus.UPLOADING],
    "failed": [PostStatus.FAILED],
    "skipped": [PostStatus.SKIPPED],
    "uploaded": [PostStatus.UPLOADED],
    "all": None,
}
LEVEL_STYLE = {"DEBUG": "dim", "INFO": "", "WARNING": "yellow", "ERROR": "bold red", "CRITICAL": "bold red"}
NOTIFY_KINDS = {"upload.done", "upload.failed", "upload.blocked", "upload.caption_stripped",
                "fetch.done", "fetch.error", "sync.done", "daemon.error", "daemon.recovered"}


def _ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class TuiLogHandler(logging.Handler):
    """Forwards log records into the Log tab (thread-safe)."""

    def __init__(self, app: "UploaderApp") -> None:
        super().__init__(logging.INFO)
        self.app = app
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = Text.assemble(
                (time.strftime("%H:%M:%S ", time.localtime(record.created)), "dim"),
                (f"{record.levelname[:4]:<5}", LEVEL_STYLE.get(record.levelname, "")),
                (self.format(record), LEVEL_STYLE.get(record.levelname, "")),
            )
            self.app.call_in_ui(self.app.write_log, line)
        except Exception:
            self.handleError(record)


class UploaderApp(App):
    TITLE = "Instagram Auto Uploader"
    CSS_PATH = "app.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]

    BINDINGS = [
        Binding("s", "toggle_daemon", "Daemon on/off"),
        Binding("f", "fetch", "Fetch"),
        Binding("F", "fetch(True)", "Deep fetch", show=False),
        Binding("u", "upload", "Upload"),
        Binding("r", "retry", "Retry"),
        Binding("k", "skip", "Skip", show=False),
        Binding("delete", "remove", "Remove", show=False),
        Binding("x", "stop_upload", "Stop upload", show=False),
        Binding("1", "tab('tab-queue')", "Queue", show=False),
        Binding("2", "tab('tab-sync')", "Sync", show=False),
        Binding("3", "tab('tab-history')", "History", show=False),
        Binding("4", "tab('tab-log')", "Log", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.activity = "Idle"
        self.sync_posts: Dict[str, Post] = {}
        self.sync_statuses: Dict[str, PostStatus] = {}
        self._ui_thread = threading.get_ident()
        self._dirty = True
        self._unsubscribe = None

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        with TabbedContent(initial="tab-queue"):
            with TabPane("1 Queue", id="tab-queue"):
                with Horizontal():
                    with Vertical(id="queue-main"):
                        with Horizontal(classes="toolbar"):
                            yield Select(QUEUE_FILTERS, value="active", allow_blank=False, id="queue-filter")
                            yield Label("", id="queue-selection")
                        yield SelectableTable(["Post ID", "Status", "Source", "Author", "Media", "Tries", "Info"],
                                              id="queue-table")
                    with VerticalScroll(id="queue-detail"):
                        yield Static("Select a post to see details.", id="detail-body")
            with TabPane("2 Sync", id="tab-sync"):
                with Horizontal(classes="toolbar"):
                    yield Label("Instagram posts")
                    yield Input("50", id="sync-ig", type="integer", classes="small")
                    yield Label("X reposts")
                    yield Input("50", id="sync-x", type="integer", classes="small")
                    yield Checkbox("include uploaded", id="sync-include")
                    yield Button("Scan", id="sync-scan", variant="primary")
                yield Static("Scan compares your Instagram profile with X reposts.", id="sync-summary")
                yield SelectableTable(["Post ID", "DB", "Author", "Media", "Text"], id="sync-table")
                with Horizontal(classes="toolbar"):
                    yield Button("Add selected to queue", id="sync-queue")
                    yield Button("Upload selected now", id="sync-upload", variant="warning")
            with TabPane("3 History", id="tab-history"):
                yield SelectableTable(["Post ID", "Status", "Uploaded (UTC)", "Author", "Link"],
                                      selectable=False, id="history-table")
            with TabPane("4 Log", id="tab-log"):
                yield RichLog(id="log", wrap=True, markup=False, max_lines=5000)
        yield Footer()

    def on_mount(self) -> None:
        self._ui_thread = threading.get_ident()
        cfg = self.ctx.config
        sources = ", ".join(s.name for s in self.ctx.sources) or "no sources enabled"
        self.sub_title = f"@{cfg.instagram.username or '?'}  ·  {sources}"
        self._unsubscribe = self.ctx.events.subscribe(self._on_service_event)
        recovered = self.ctx.store.recover_stale_uploads()
        if recovered:
            self.notify(f"{recovered} interrupted upload(s) marked failed; check Instagram before retrying.", severity="warning")
        if not cfg.twitter.enabled and not cfg.local.enabled:
            self.notify("No source enabled in config (twitter.enabled / local.enabled).", severity="error")
        if cfg.local.enabled:
            self.write_log(Text(f"Watching local folder: {Path(cfg.local.watch_dir).resolve()}", style="cyan"))
        self.set_interval(1.0, self.update_status)
        self.set_interval(3.0, self._refresh_if_dirty)
        self.refresh_tables()
        self.update_status()
        self.query_one("#queue-table").focus()

    def on_unmount(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()

    # ── thread-safe helpers ───────────────────────────────────────────────

    def call_in_ui(self, fn, *args) -> None:
        if threading.get_ident() == self._ui_thread:
            fn(*args)
        else:
            try:
                self.call_from_thread(fn, *args)
            except RuntimeError:
                pass  # app not running (startup/shutdown)

    def write_log(self, line: Text) -> None:
        try:
            self.query_one("#log", RichLog).write(line)
        except Exception:
            pass

    def _on_service_event(self, event: Event) -> None:
        self.call_in_ui(self._handle_event, event)

    def _handle_event(self, event: Event) -> None:
        if event.kind.startswith(("upload.", "fetch.", "sync.", "repair.")):
            self.activity = event.message
        if event.kind in ("upload.done", "upload.failed", "fetch.done", "sync.done", "daemon.cycle") \
                or event.kind == "upload.started":
            self._dirty = True
        if event.kind in NOTIFY_KINDS and not (event.kind == "fetch.done" and not event.data.get("queued")):
            severity = {"error": "error", "warning": "warning"}.get(event.level, "information")
            self.notify(event.message, severity=severity, timeout=6 if severity == "information" else 10)
        self.update_status()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            error = event.worker.error
            logging.getLogger("auto_uploader.tui").error("Task '%s' crashed: %s", event.worker.name, error,
                                                         exc_info=error)
            self.notify(f"{event.worker.name or 'Task'} failed: {error}", severity="error", timeout=10)
            self._dirty = True

    # ── status bar ────────────────────────────────────────────────────────

    def update_status(self) -> None:
        store, policy, daemon, svc = self.ctx.store, self.ctx.policy, self.ctx.daemon, self.ctx.config.service
        counts = store.counts()
        now = time.time()

        if daemon.running:
            state = f"[bold green]● RUNNING[/] [dim]({daemon.state}"
            if daemon.state == "sleeping" and daemon.next_cycle_at:
                state += f", next poll in {_ago(daemon.next_cycle_at - now)}"
            state += ")[/]"
        else:
            state = "[bold yellow]○ STOPPED[/] [dim](press s to start)[/]"

        blocked = policy.blocked_reason(now)
        gate = f"[yellow]{escape(blocked)}[/]" if blocked else "[green]ready[/]"
        used = store.uploads_since(24)
        quota = f"{used}/{svc.max_posts_per_day}" if svc.rate_limit_enabled else f"{used} (no limit)"
        lo, hi = policy.delay_range()
        busy = " [bold yellow]⟳[/]" if self.ctx.publisher.busy else ""

        line1 = (f"Daemon {state}   │   Queue [cyan]{counts[PostStatus.QUEUED]}[/]  "
                 f"Failed [red]{counts[PostStatus.FAILED]}[/]  Uploaded [green]{counts[PostStatus.UPLOADED]}[/]  "
                 f"Skipped [dim]{counts[PostStatus.SKIPPED]}[/]")
        line2 = (f"Next upload: {gate}   │   24h: {quota}   │   Delay {lo:.0f}–{hi:.0f}s"
                 f"{' (slowed x%g)' % policy.delay_multiplier if policy.delay_multiplier > 1 else ''}")
        line3 = f"Now:{busy} {escape(self.activity)}"
        self.query_one("#status", Static).update(f"{line1}\n{line2}\n{line3}")

    # ── tables ────────────────────────────────────────────────────────────

    def _refresh_if_dirty(self) -> None:
        if self._dirty:
            self.refresh_tables()

    def refresh_tables(self) -> None:
        self._dirty = False
        self._refresh_queue()
        self._refresh_history()

    def _queue_records(self) -> List[PostRecord]:
        value = self.query_one("#queue-filter", Select).value
        return self.ctx.store.list(FILTER_STATUSES.get(value), limit=1000)

    def _refresh_queue(self) -> None:
        table = self.query_one("#queue-table", SelectableTable)
        rows = []
        for r in self._queue_records():
            info = Text(r.last_error or "", style="red") if r.status == PostStatus.FAILED else Text(r.link, style="dim")
            if r.post_id == self.ctx.publisher.current_post_id:
                info = Text("uploading…", style="bold yellow")
            rows.append((r.post_id, [r.post_id, status_text(r.status), r.source,
                                     f"@{r.author}" if r.author else "-", str(r.media_count),
                                     str(r.attempts), info]))
        table.set_rows(rows)
        self._update_selection_label()
        self._show_detail(table.cursor_key)

    def _refresh_history(self) -> None:
        table = self.query_one("#history-table", SelectableTable)
        records = self.ctx.store.list([PostStatus.UPLOADED], limit=300, newest_first=True)
        table.set_rows(
            (r.post_id, [r.post_id, status_text(r.status), r.uploaded_at or "found on IG",
                         f"@{r.author}" if r.author else "-", r.link])
            for r in records
        )

    def _update_selection_label(self) -> None:
        table = self.query_one("#queue-table", SelectableTable)
        n = len(table.selected)
        text = f"{table.row_count} rows" + (f" · [b]{n} selected[/b]" if n else " · space to select")
        self.query_one("#queue-selection", Label).update(text)

    @on(Select.Changed, "#queue-filter")
    def _filter_changed(self) -> None:
        self._refresh_queue()

    @on(SelectableTable.SelectionChanged, "#queue-table")
    def _selection_changed(self) -> None:
        self._update_selection_label()

    @on(DataTable.RowHighlighted, "#queue-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail(event.row_key.value if event.row_key else None)

    def _show_detail(self, post_id: Optional[str]) -> None:
        body = self.query_one("#detail-body", Static)
        record = self.ctx.store.get(post_id) if post_id else None
        if record is None:
            body.update("Select a post to see details.")
            return
        lines = [
            f"[b]{escape(record.post_id)}[/b]",
            f"Status: {record.status.value}   Tries: {record.attempts}",
            f"Source: {record.source}   Author: @{escape(record.author or '-')}",
            f"Added:  {record.created_at} UTC",
        ]
        if record.link:
            lines.append(f"Link:   [link=\"{record.link}\"]{escape(record.link)}[/link]")
        if record.last_error:
            lines += ["", f"[red]Last error:[/red] {escape(record.last_error)}"]
        post = record.post()
        if post:
            lines += ["", f"[b]Media ({post.media_summary})[/b]"]
            for m in post.media_items:
                where = str(m.original_path) if m.original_path else (m.url or "?")
                lines.append(f"  • {m.media_type.value}: {escape(where)}")
            try:
                caption = self.ctx.publisher.preview_caption(post)
            except Exception as e:
                caption = f"(caption preview failed: {e})"
            lines += ["", "[b]Caption preview[/b]", escape(caption)]
        body.update("\n".join(lines))

    # ── actions ───────────────────────────────────────────────────────────

    def _active_tab(self) -> str:
        return self.query_one(TabbedContent).active

    def action_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.refresh_tables()
        self.notify("Refreshed.", timeout=2)

    def action_toggle_daemon(self) -> None:
        daemon = self.ctx.daemon
        if daemon.running:
            daemon.stop()
            self.notify("Stopping daemon after the current step…")
        else:
            self.run_worker(daemon.run(), group="daemon", exclusive=True, exit_on_error=False)
            self.notify("Daemon started.")
        self.update_status()

    def action_fetch(self, deep: bool = False) -> None:
        if not self.ctx.sources:
            self.notify("No source enabled in config.", severity="error")
            return
        self._fetch(deep)

    @work(group="fetch", exclusive=True, exit_on_error=False)
    async def _fetch(self, deep: bool) -> None:
        await self.ctx.ingest.fetch(deep=deep)
        self.refresh_tables()

    def action_upload(self) -> None:
        if self._active_tab() == "tab-sync":
            self._sync_upload(queue_only=False)
            return
        table = self.query_one("#queue-table", SelectableTable)
        ids = table.target_keys()
        if not ids:
            self.notify("Nothing to upload. Select rows with space.", severity="warning")
            return
        self._confirm_upload(ids, after=table.clear_selection)

    def _confirm_upload(self, ids: List[str], after=None) -> None:
        blocked = self.ctx.policy.blocked_reason()
        body = f"{len(ids)} post(s) will be uploaded in order with a random delay between them."
        choices = [("Upload", "upload", "primary")]
        if blocked:
            body += f"\n\n[yellow]Currently blocked: {escape(blocked)}[/yellow]"
            choices.append(("Upload anyway (ignore limits)", "force", "warning"))

        def done(choice: Optional[str]) -> None:
            if choice:
                if after:
                    after()
                self._upload(ids, force=choice == "force")

        self.push_screen(ConfirmScreen("Upload to Instagram?", body, choices), done)

    @work(group="upload", exit_on_error=False)
    async def _upload(self, ids: List[str], force: bool) -> None:
        result: BatchResult = await self.ctx.publisher.upload(ids, respect_limits=not force)
        self.refresh_tables()
        summary = f"Batch finished: {len(result.uploaded)} uploaded, {len(result.failed)} failed"
        if result.not_started:
            summary += f", {result.not_started} not started ({result.stopped_reason or 'skipped'})"
        self.notify(summary, severity="error" if result.failed else "information", timeout=10)

    def action_stop_upload(self) -> None:
        if self.ctx.publisher.busy:
            self.ctx.publisher.request_stop()
            self.notify("Stopping after the current post…")

    def _queue_targets(self) -> List[str]:
        if self._active_tab() != "tab-queue":
            return []
        return self.query_one("#queue-table", SelectableTable).target_keys()

    def action_retry(self) -> None:
        ids = self._queue_targets()
        if not ids:
            return
        count = self.ctx.store.requeue(ids)
        self.notify(f"Re-queued {count} post(s)." if count else "Only failed or skipped posts can be re-queued.")
        self.query_one("#queue-table", SelectableTable).clear_selection()
        self.refresh_tables()

    def action_skip(self) -> None:
        ids = self._queue_targets()
        count = 0
        for pid in ids:
            record = self.ctx.store.get(pid)
            if record and record.status in (PostStatus.QUEUED, PostStatus.FAILED):
                self.ctx.store.set_status(pid, PostStatus.SKIPPED)
                count += 1
        if ids:
            self.notify(f"Skipped {count} post(s). Filter 'Skipped' + r to bring them back.")
            self.query_one("#queue-table", SelectableTable).clear_selection()
            self.refresh_tables()

    def action_remove(self) -> None:
        ids = self._queue_targets()
        if not ids:
            return

        def done(choice: Optional[str]) -> None:
            if choice:
                count = self.ctx.store.delete(ids)
                self.notify(f"Removed {count} post(s).")
                self.query_one("#queue-table", SelectableTable).clear_selection()
                self.refresh_tables()

        self.push_screen(ConfirmScreen(
            "Remove posts?",
            f"{len(ids)} post(s) will be forgotten. They can be fetched and queued again later.\n"
            "Use [b]k[/b] (skip) instead to keep them out for good.",
            [("Remove", "remove", "error")],
        ), done)

    def action_quit_app(self) -> None:
        if not (self.ctx.publisher.busy or self.ctx.daemon.running):
            self.exit()
            return

        def done(choice: Optional[str]) -> None:
            if choice:
                self.ctx.daemon.stop()
                self.ctx.publisher.request_stop()
                self.exit()

        self.push_screen(ConfirmScreen(
            "Quit?",
            "The daemon is running or an upload is in progress. "
            "An interrupted upload is marked failed on next start (check Instagram before retrying).",
            [("Quit", "quit", "error")],
        ), done)

    # ── sync tab ──────────────────────────────────────────────────────────

    @on(Button.Pressed, "#sync-scan")
    def _sync_scan_pressed(self) -> None:
        if self.ctx.twitter is None:
            self.notify("Twitter source is disabled in config.", severity="error")
            return
        try:
            ig = int(self.query_one("#sync-ig", Input).value or 50)
            x = int(self.query_one("#sync-x", Input).value or 50)
        except ValueError:
            self.notify("Limits must be numbers.", severity="error")
            return
        include = self.query_one("#sync-include", Checkbox).value
        self.query_one("#sync-scan", Button).disabled = True
        self.query_one("#sync-summary", Static).update("Scanning… (Instagram login may take a moment)")
        self._sync_scan(ig, x, include)

    @work(group="sync", exclusive=True, exit_on_error=False)
    async def _sync_scan(self, ig: int, x: int, include: bool) -> None:
        try:
            report = await self.ctx.sync.scan(ig, x, include)
        except Exception as e:
            self.query_one("#sync-summary", Static).update(f"[red]Scan failed: {escape(str(e))}[/red]")
            self.notify(f"Sync failed: {e}", severity="error")
            return
        finally:
            self.query_one("#sync-scan", Button).disabled = False

        self.sync_posts = {p.id: p for p in report.missing}
        self.sync_statuses = report.statuses
        summary = (f"Instagram {report.ig_scanned} · X {report.x_scanned} · "
                   f"[b]{len(report.missing)} missing[/b] · {report.marked_uploaded} newly marked as uploaded")
        if report.hidden_uploaded:
            summary += f" · {report.hidden_uploaded} hidden (uploaded per DB)"
        self.query_one("#sync-summary", Static).update(summary)
        table = self.query_one("#sync-table", SelectableTable)
        table.selected.clear()
        table.set_rows(self._sync_rows())
        self.refresh_tables()

    @on(Button.Pressed, "#sync-queue")
    def _sync_queue_pressed(self) -> None:
        self._sync_upload(queue_only=True)

    @on(Button.Pressed, "#sync-upload")
    def _sync_upload_pressed(self) -> None:
        self._sync_upload(queue_only=False)

    def _sync_upload(self, queue_only: bool) -> None:
        table = self.query_one("#sync-table", SelectableTable)
        posts = [self.sync_posts[k] for k in table.target_keys() if k in self.sync_posts]
        if not posts:
            self.notify("Select missing posts first (space / a).", severity="warning")
            return

        def enqueue() -> None:
            ids = self.ctx.sync.enqueue(posts)
            for pid in ids:
                self.sync_posts.pop(pid, None)
            table.set_rows(self._sync_rows())
            self.refresh_tables()

        if queue_only:
            enqueue()
            self.notify(f"Queued {len(posts)} post(s). The daemon (or u in Queue) will upload them.")
        else:
            self._confirm_upload([p.id for p in posts], after=enqueue)

    def _sync_rows(self):
        for p in self.sync_posts.values():
            yield p.id, [p.id, status_text(self.sync_statuses.get(p.id)), f"@{p.author_username}",
                         p.media_summary, (p.text or "").replace("\n", " ")[:80]]
