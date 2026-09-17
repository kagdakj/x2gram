"""Rich TUI interface for Instagram Auto Uploader.

Focused on upload control: toggling auto-upload, selecting queue items, and batch uploading.
"""

import asyncio
import os
import sys
import time
from typing import Any, Dict, List, Set

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn

from .config import AppConfig
from .engine import AutoUploaderEngine
from .models import Post


class UploadTUI:
    """Terminal User Interface for controlling uploads and queue state."""

    def __init__(self, config_path: str = "config.yaml"):
        self.console = Console()
        self.config_path = config_path
        self.config = AppConfig.load(self.config_path)
        self.engine = AutoUploaderEngine(self.config)
        self.selected_ids: Set[str] = set()
        self.daemon_task: asyncio.Task | None = None
        self.running_daemon = False

    def clear_screen(self):
        """Clear console screen."""
        self.console.clear()

    def render_header(self) -> Panel:
        """Render top dashboard status panel."""
        stats = self.engine.state.get_stats()
        pending = self.engine.state.queue_size("pending")
        done = self.engine.state.queue_size("done")
        failed = self.engine.state.queue_size("failed")
        daemon_status = "[bold green]● RUNNING[/bold green]" if self.running_daemon else "[bold yellow]○ STOPPED[/bold yellow]"

        min_d = getattr(self.config.service, "post_delay_min", 15)
        max_d = getattr(self.config.service, "post_delay_max", max(min_d, getattr(self.config.service, "post_delay", 30)))
        delay_info = f"{min_d}s - {max_d}s (Random)"

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="right", ratio=1)

        grid.add_row(
            f"Auto-Upload Daemon: {daemon_status}",
            f"Queue: [cyan]{pending}[/cyan] pending | [green]{done}[/green] done | [red]{failed}[/red] failed",
            f"Random Delay: [magenta]{delay_info}[/magenta]",
        )

        return Panel(
            grid,
            title="[bold blue]📸 Instagram Auto Uploader — Upload Control Center[/bold blue]",
            border_style="blue",
        )

    def render_queue_table(self, pending_rows: List[Dict[str, Any]]) -> Table:
        """Render table of pending queue items with selection checkboxes."""
        table = Table(expand=True, header_style="bold magenta", border_style="dim")
        table.add_column("#", style="dim", width=4)
        table.add_column("Sel", width=5, justify="center")
        table.add_column("Post ID", style="bold cyan", width=20)
        table.add_column("Author", width=16)
        table.add_column("Media", width=8, justify="center")
        table.add_column("Source", width=10)
        table.add_column("URL / Link", style="dim")

        if not pending_rows:
            table.add_row("-", "-", "Queue is empty", "-", "-", "-", "Run 'Seed' [4] to fetch posts from Twitter.")
            return table

        for idx, row in enumerate(pending_rows, 1):
            pid = row["post_id"]
            checked = "[bold green][x][/bold green]" if pid in self.selected_ids else "[dim][ ][/dim]"
            
            author = "unknown"
            media_cnt = "1"
            url = f"https://x.com/i/status/{pid}"

            try:
                post = self.engine._post_from_json(row["post_json"])
                author = f"@{post.author_username}"
                media_cnt = str(len(post.media_items))
                url = post.url or f"https://x.com/{post.author_username}/status/{pid}"
            except Exception:
                pass

            table.add_row(
                str(idx),
                checked,
                pid,
                author,
                media_cnt,
                row["source_name"],
                url,
            )

        return table

    def render_menu(self):
        """Render interactive command prompt instructions."""
        self.console.print("\n[bold yellow]Actions:[/bold yellow]")
        self.console.print(" [bold green][1][/bold green] Toggle Auto-Upload Daemon  | [bold cyan][2][/bold cyan] Select / Checkmark Queue Items")
        self.console.print(" [bold magenta][3][/bold magenta] Upload Selected Now          | [bold blue][4][/bold blue] Seed Queue (Fetch from X)")
        self.console.print(" [bold white][5][/bold white] View Upload History          | [bold red][C][/bold red] Clear Queue  | [bold dim][Q][/bold dim] Quit")

    def run_menu_loop(self):
        """Main TUI synchronous command loop."""
        while True:
            self.clear_screen()
            self.console.print(self.render_header())

            pending_rows = self.engine.state.pop_pending(limit=100)
            self.console.print(self.render_queue_table(pending_rows))
            self.render_menu()

            choice = Prompt.ask("\nEnter option", default="1").strip().lower()

            if choice in ("1", "t", "toggle"):
                self.toggle_daemon()
            elif choice in ("2", "sel", "s"):
                self.handle_select(pending_rows)
            elif choice in ("3", "u", "upload"):
                self.handle_upload_selected(pending_rows)
            elif choice in ("4", "seed", "r", "fetch"):
                self.handle_seed()
            elif choice in ("5", "h", "history"):
                self.handle_history()
            elif choice in ("c", "clear"):
                if Confirm.ask("Clear all pending queue items?"):
                    count = self.engine.state.clear_queue(status="pending")
                    self.selected_ids.clear()
                    self.console.print(f"[green]Cleared {count} queue items.[/green]")
                    time.sleep(1)
            elif choice in ("q", "exit", "quit"):
                self.console.print("[yellow]Exiting TUI...[/yellow]")
                if self.running_daemon:
                    self.engine.stop()
                break

    def toggle_daemon(self):
        """Toggle continuous background daemon on/off."""
        if self.running_daemon:
            self.engine.stop()
            self.running_daemon = False
            self.console.print("[yellow]Auto-upload daemon STOPPED.[/yellow]")
        else:
            self.running_daemon = True
            self.console.print("[green]Auto-upload daemon STARTED in background...[/green]")
            asyncio.run(self.engine.check())
        time.sleep(1)

    def handle_select(self, pending_rows: List[Dict[str, Any]]):
        """Handle selection / checkmarking of queue items."""
        if not pending_rows:
            self.console.print("[yellow]Queue is empty. Nothing to select.[/yellow]")
            time.sleep(1)
            return

        self.console.print("\n[bold]Select items to upload:[/bold]")
        self.console.print("  • Type indices like '1,3,5' or '1-5'")
        self.console.print("  • Type 'all' / 'a' to select all")
        self.console.print("  • Type 'none' / 'n' to clear selection")

        inp = Prompt.ask("Selection").strip().lower()
        if inp in ("none", "n", "clear"):
            self.selected_ids.clear()
            return

        if inp in ("all", "a", "*"):
            self.selected_ids = {row["post_id"] for row in pending_rows}
            return

        # Parse range/numbers
        parts = inp.split(",")
        for part in parts:
            part = part.strip()
            if "-" in part:
                sub = part.split("-")
                if len(sub) == 2 and sub[0].isdigit() and sub[1].isdigit():
                    s, e = int(sub[0]), int(sub[1])
                    for i in range(min(s, e), max(s, e) + 1):
                        if 1 <= i <= len(pending_rows):
                            self.selected_ids.add(pending_rows[i - 1]["post_id"])
            elif part.isdigit():
                val = int(part)
                if 1 <= val <= len(pending_rows):
                    pid = pending_rows[val - 1]["post_id"]
                    if pid in self.selected_ids:
                        self.selected_ids.remove(pid)  # Toggle off
                    else:
                        self.selected_ids.add(pid)     # Toggle on

    def handle_upload_selected(self, pending_rows: List[Dict[str, Any]]):
        """Upload selected items or prompt if none selected."""
        if not self.selected_ids:
            if not pending_rows:
                self.console.print("[yellow]Queue is empty.[/yellow]")
                time.sleep(1)
                return
            if Confirm.ask("No items checked. Upload oldest pending post now?"):
                self.selected_ids.add(pending_rows[0]["post_id"])
            else:
                return

        target_ids = [row["post_id"] for row in pending_rows if row["post_id"] in self.selected_ids]
        if not target_ids:
            self.console.print("[yellow]Selected items no longer in queue.[/yellow]")
            time.sleep(1)
            return

        self.console.print(f"\n🚀 [bold green]Starting upload of {len(target_ids)} selected post(s)...[/bold green]")

        # Process selected items
        async def upload_batch():
            for pid in target_ids:
                row = next((r for r in pending_rows if r["post_id"] == pid), None)
                if not row:
                    continue
                try:
                    post = self.engine._post_from_json(row["post_json"])
                except Exception as e:
                    self.console.print(f"[red]Error loading post {pid}: {e}[/red]")
                    continue

                self.console.print(f"\n[cyan]Uploading post {post.id} (Author: @{post.author_username})...[/cyan]")
                res = await self.engine.process_and_upload(post)
                if res.success:
                    self.console.print(f"[bold green]✅ Uploaded successfully! Media ID: {res.media_id}[/bold green]")
                    self.engine.state.mark_queue_status(pid, "done")
                else:
                    self.console.print(f"[bold red]❌ Upload failed: {res.error_message}[/bold red]")
                    self.engine.state.mark_queue_status(pid, "failed")

                if pid in self.selected_ids:
                    self.selected_ids.remove(pid)

        asyncio.run(upload_batch())
        Prompt.ask("\nPress Enter to continue...")

    def handle_seed(self):
        """Fetch posts from Twitter and populate upload queue."""
        self.console.print("\n🌱 [bold cyan]Seeding queue from Twitter (fetching backlog)...[/bold cyan]")
        
        async def run_seed():
            res = await self.engine.seed()
            self.console.print(f"\n[bold green]Seed Complete:[/bold green] Fetched {res['fetched']} posts | Queued {res['queued']} new posts | Total pending: {res['pending']}")

        asyncio.run(run_seed())
        Prompt.ask("\nPress Enter to continue...")

    def handle_history(self):
        """View uploaded post history."""
        history = self.engine.state.get_seen_posts_details(limit=30)
        self.clear_screen()
        self.console.print("[bold blue]📜 Recent Upload History[/bold blue]\n")

        table = Table(expand=True, header_style="bold magenta", border_style="dim")
        table.add_column("#", width=4)
        table.add_column("Post ID", width=20)
        table.add_column("Status", width=10)
        table.add_column("Seen At", width=20)
        table.add_column("URL", style="dim")

        for idx, row in enumerate(history, 1):
            url = row.get("url") or f"https://x.com/i/status/{row['post_id']}"
            table.add_row(str(idx), row["post_id"], row["status"], str(row["seen_at"]), url)

        self.console.print(table)
        Prompt.ask("\nPress Enter to return...")


def main():
    tui = UploadTUI()
    tui.run_menu_loop()


if __name__ == "__main__":
    main()
