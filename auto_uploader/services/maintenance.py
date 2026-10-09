"""One-off maintenance tasks: connection checks, caption repair, reset."""

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..config import AppConfig
from ..models import Post
from ..store import Store
from .events import EventBus
from .sync import match_media_to_post


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class RepairReport:
    repaired: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    stopped_reason: Optional[str] = None


class MaintenanceService:
    def __init__(self, config: AppConfig, store: Store, events: EventBus,
                 uploader_factory: Callable, sources_factory: Callable, plugins_factory: Callable):
        self.config = config
        self.store = store
        self.events = events
        self._uploader = uploader_factory
        self._sources = sources_factory
        self._plugins = plugins_factory

    # ── doctor ────────────────────────────────────────────────────────────

    def check_connections(self) -> List[CheckResult]:
        results = []
        for source in self._sources():
            try:
                ok = source.test_connection()
                results.append(CheckResult(f"source:{source.name}", ok, "" if ok else "see log for details"))
            except Exception as e:
                results.append(CheckResult(f"source:{source.name}", False, str(e)))
        try:
            ok = self._uploader().test_connection()
            results.append(CheckResult("instagram", ok, "" if ok else "login failed, see log"))
        except Exception as e:
            results.append(CheckResult("instagram", False, str(e)))
        return results

    def check_environment(self) -> List[CheckResult]:
        """Catch the classic 'wrong virtualenv' trap: an old instagrapi fails every login."""
        import sys
        from importlib.metadata import PackageNotFoundError, version

        results = [CheckResult(f"python: {sys.executable}", True)]
        try:
            from instagrapi import config as ig_config

            try:
                installed = version("instagrapi")
            except PackageNotFoundError:
                installed = "?"
            session_app = self._uploader().app_version_of(self._uploader()._load_settings())
            detail = f"instagrapi {installed}, app {ig_config.DEFAULT_APP_VERSION}"
            ok = True
            if session_app and session_app not in ig_config.APP_SETTINGS:
                ok, detail = False, (f"{detail}: saved session uses {session_app}, unknown to this instagrapi "
                                     f"(wrong virtualenv, or upgrade instagrapi)")
            results.append(CheckResult(detail, ok, detail if not ok else ""))
        except ImportError as e:
            results.append(CheckResult("instagrapi", False, str(e)))
        return results

    def check_config(self) -> List[CheckResult]:
        cfg = self.config
        results = [
            CheckResult("instagram credentials",
                        bool(cfg.instagram.session_id or (cfg.instagram.username and cfg.instagram.password)
                             or Path(cfg.instagram.session_file).is_file()),
                        "need session_id, username+password, or a saved session file"),
        ]
        if cfg.twitter.enabled:
            results.append(CheckResult("twitter cookies",
                                       bool(cfg.twitter.username and cfg.twitter.auth_token and cfg.twitter.ct0),
                                       "need twitter.username, auth_token and ct0"))
        if not (cfg.twitter.enabled or cfg.local.enabled):
            results.append(CheckResult("sources", False, "no source enabled (twitter.enabled / local.enabled)"))
        return results

    # ── caption repair ────────────────────────────────────────────────────

    def repair_captions(self, limit: int = 30, force: bool = False, delay: float = 4,
                        progress: Optional[Callable[[str], None]] = None) -> RepairReport:
        """Re-apply generated captions to recent Instagram posts that lost theirs."""
        say = progress or (lambda msg: self.events.emit("repair.progress", msg))
        report = RepairReport()
        uploader = self._uploader()
        plugins = self._plugins()

        medias = uploader.list_recent_media(limit)
        timeline = self.store.upload_timeline()
        say(f"Inspecting {len(medias)} Instagram post(s)...")

        fetched: Optional[Dict[str, Post]] = None
        for media in medias:
            if media.caption.strip() and not force:
                continue

            post_id = match_media_to_post(media, timeline)
            if not post_id:
                say(f"IG {media.code}: no matching post (no link in caption, no upload within 30s).")
                report.skipped += 1
                continue

            record = self.store.get(post_id)
            post = record.post() if record else None
            if post is None:
                if fetched is None:
                    fetched = self._fetch_twitter(max(40, limit))
                post = fetched.get(post_id)
            if post is None:
                say(f"IG {media.code}: matched {post_id}, but its text is unavailable.")
                report.skipped += 1
                continue

            caption = plugins.apply_caption_pipeline(post.caption or post.text, post)
            try:
                uploader.edit_caption(media.pk, caption)
                report.repaired += 1
                say(f"IG {media.code}: caption restored from {post_id}.")
            except Exception as e:
                report.errors.append(f"{media.code}: {e}")
                say(f"IG {media.code}: edit failed: {e}")
                if "feedback_required" in str(e).lower():
                    report.stopped_reason = "Instagram action block; wait 15-30 minutes."
                    break
            time.sleep(delay)
        return report

    def _fetch_twitter(self, limit: int) -> Dict[str, Post]:
        source = next((s for s in self._sources() if s.name == "twitter"), None)
        if source is None:
            return {}
        try:
            return {p.id: p for p in source.fetch_new_posts(limit=limit)}
        except Exception as e:
            self.events.emit("repair.progress", f"Could not fetch X posts: {e}", level="warning")
            return {}

    # ── reset ─────────────────────────────────────────────────────────────

    def reset(self, restore_files: bool = False, logout: bool = False) -> List[str]:
        """Forget upload history + baseline. The session is kept unless `logout` is set."""
        lines = [f"Forgot {self.store.reset_history()} uploaded/skipped record(s); queue kept."]

        session_file = Path(self.config.instagram.session_file)
        if logout and session_file.is_file():
            # The session also holds the device fingerprint: a new one looks like a new phone
            # to Instagram, so keep a copy and only do this when switching accounts.
            backup = session_file.with_suffix(".json.removed")
            session_file.rename(backup)
            lines.append(f"Removed Instagram session (kept a copy at {backup}).")
        elif session_file.is_file():
            lines.append("Kept the Instagram session and its device fingerprint (use --logout to drop it).")

        if restore_files:
            lines.append(f"Restored {self.restore_processed_files()} item(s) into '{self.config.local.watch_dir}'.")
        return lines

    def restore_processed_files(self) -> int:
        proc_dir = Path(self.config.local.processed_dir)
        watch_dir = Path(self.config.local.watch_dir)
        watch_dir.mkdir(parents=True, exist_ok=True)
        if not proc_dir.is_dir():
            return 0

        count = 0
        for day_dir in [d for d in proc_dir.iterdir() if d.is_dir()]:
            for item in list(day_dir.iterdir()):
                dest = watch_dir / item.name
                if dest.exists():
                    dest = watch_dir / f"{item.stem}_restored{item.suffix}"
                shutil.move(str(item), str(dest))
                count += 1
            try:
                day_dir.rmdir()
            except OSError:
                pass
        return count
