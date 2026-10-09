"""The only place where posts get uploaded."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..models import Post, PostStatus, UploadResult
from ..plugins.base import PluginManager
from ..sources.base import BaseSource
from ..store import Store
from ..uploader.base import BaseUploader
from .events import EventBus
from .media import MediaStager
from .policy import UploadPolicy

logger = logging.getLogger("auto_uploader.publisher")

# Errors that mean "stop uploading for a while", not "retry the next post".
ACTION_BLOCK_MARKERS = (
    "feedback_required", "challenge_required", "please wait a few minutes",
    "429", "too many requests", "rate limit", "login_required",
    "your version of instagram is out of date",
)


@dataclass
class BatchResult:
    uploaded: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    stopped_reason: Optional[str] = None

    @property
    def not_started(self) -> int:
        return len(self.skipped)


class Publisher:
    def __init__(
        self,
        store: Store,
        uploader: BaseUploader,
        plugins: Callable[[], PluginManager],
        sources: Sequence[BaseSource],
        policy: UploadPolicy,
        stager: MediaStager,
        events: EventBus,
    ):
        self.store = store
        self.uploader = uploader
        self._plugins_factory = plugins
        self.sources = {s.name: s for s in sources}
        self.policy = policy
        self.stager = stager
        self.events = events
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock_obj: Optional[asyncio.Lock] = None
        self._stop_obj: Optional[asyncio.Event] = None
        self.current_post_id: Optional[str] = None

    def _primitives(self) -> None:
        # CLI commands may call asyncio.run() several times; primitives are loop-bound.
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._lock_obj = asyncio.Lock()
            self._stop_obj = asyncio.Event()

    @property
    def _lock(self) -> asyncio.Lock:
        self._primitives()
        return self._lock_obj

    @property
    def _stop(self) -> asyncio.Event:
        self._primitives()
        return self._stop_obj

    @property
    def plugins(self) -> PluginManager:
        return self._plugins_factory()

    @property
    def busy(self) -> bool:
        return bool(self._lock_obj and self._lock_obj.locked())

    def request_stop(self) -> None:
        """Stop after the current upload (pending delays are cut short)."""
        if self._stop_obj is not None:
            self._stop_obj.set()

    def preview_caption(self, post: Post) -> str:
        return self.plugins.apply_caption_pipeline(post.caption or post.text, post)

    # ── public entry points ───────────────────────────────────────────────

    async def upload_next(self, limit: int = 1) -> BatchResult:
        """Upload the oldest queued posts (daemon path)."""
        ids = [r.post_id for r in self.store.next_queued(limit)]
        return await self.upload(ids, respect_limits=True, delay_first=True)

    async def upload(
        self,
        post_ids: Sequence[str],
        respect_limits: bool = True,
        delay_first: bool = False,
    ) -> BatchResult:
        """Upload the given stored posts in order.

        respect_limits=False skips the daily cap / interval check (manual "force"),
        but action-block pauses and random delays between posts always apply.
        """
        result = BatchResult()
        if not post_ids:
            return result

        if self._lock.locked():
            self.events.emit("upload.waiting", "Another upload is in progress; waiting...")

        async with self._lock:
            self._stop.clear()
            for index, post_id in enumerate(post_ids):
                if self._stop.is_set():
                    result.stopped_reason = "stopped"
                    self._skip_rest(result, post_ids[index:], "stopped")
                    break

                record = self.store.get(post_id)
                if record is None or record.status not in (PostStatus.QUEUED, PostStatus.FAILED, PostStatus.SKIPPED):
                    reason = "not found" if record is None else f"status is {record.status.value}"
                    result.skipped[post_id] = reason
                    continue

                blocked = self.policy.blocked_reason()
                paused = self.policy.paused_until > time.time()
                if blocked and (respect_limits or paused):
                    result.stopped_reason = blocked
                    self.events.emit("upload.blocked", f"Upload paused: {blocked}", level="warning")
                    self._skip_rest(result, post_ids[index:], blocked)
                    break

                if index > 0 or delay_first:
                    delay = self.policy.random_delay()
                    if delay > 0:
                        self.events.emit("upload.delay", f"Waiting {delay:.0f}s before next upload...",
                                         post_id=post_id, seconds=delay)
                        if await self._sleep(delay):
                            result.stopped_reason = "stopped"
                            self._skip_rest(result, post_ids[index:], "stopped")
                            break

                outcome = await self._upload_one(post_id)
                if outcome.success:
                    result.uploaded.append(post_id)
                else:
                    result.failed[post_id] = outcome.error_message or "unknown error"
                    if self._is_action_block(outcome.error_message):
                        self.policy.on_action_block()
                        result.stopped_reason = "Instagram action block"
                        self.events.emit(
                            "upload.blocked",
                            f"Instagram action block detected. Pausing uploads for "
                            f"{self.policy.config.action_block_cooldown // 60} min.",
                            level="error",
                        )
                        self._skip_rest(result, post_ids[index + 1:], "action block")
                        break
        return result

    # ── internals ─────────────────────────────────────────────────────────

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless stop is requested. Returns True if stopped."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _skip_rest(result: BatchResult, ids: Sequence[str], reason: str) -> None:
        for pid in ids:
            result.skipped.setdefault(pid, reason)

    @staticmethod
    def _is_action_block(error: Optional[str]) -> bool:
        text = (error or "").lower()
        return any(marker in text for marker in ACTION_BLOCK_MARKERS)

    async def _upload_one(self, post_id: str) -> UploadResult:
        record = self.store.get(post_id)
        post = record.post() if record else None
        if post is None:
            error = "Stored post has no payload (re-fetch it from the source)."
            self.store.mark_failed(post_id, error)
            self.events.emit("upload.failed", f"{post_id}: {error}", post_id=post_id, level="error")
            return UploadResult(success=False, error_message=error)

        self.current_post_id = post_id
        self.store.mark_uploading(post_id)
        self.store.increment_stat("checked")
        self.events.emit("upload.started", f"Uploading {post_id} (@{post.author_username}, {post.media_summary})...",
                         post_id=post_id)

        post_dir = self.stager.create_dir(post_id)
        try:
            await self.stager.stage(post, post_dir)
            post.media_items = [m for m in post.media_items if m.is_valid_local]
            if not post.media_items:
                result = UploadResult(success=False, error_message="No valid media could be obtained for this post.")
            else:
                self.events.emit("upload.processing", f"Processing media for {post_id}...", post_id=post_id)
                post = await asyncio.to_thread(self.plugins.apply_post_pipeline, post)
                self.events.emit("upload.sending", f"Sending {post_id} to Instagram...", post_id=post_id)
                result = await asyncio.to_thread(self.uploader.upload_post, post)
        except Exception as e:
            logger.exception("Upload pipeline crashed for %s", post_id)
            result = UploadResult(success=False, error_message=str(e))
        finally:
            self.stager.cleanup(post_dir)
            self.current_post_id = None

        self._notify(post, result)

        if result.success:
            self.store.mark_uploaded(post_id, result.media_id)
            self.store.increment_stat("published")
            self.policy.on_success()
            self.events.emit("upload.done", f"Uploaded {post_id} (media {result.media_id}).",
                             post_id=post_id, level="success")
            if result.extra.get("caption_stripped"):
                self.policy.on_caption_stripped()
                self.events.emit(
                    "upload.caption_stripped",
                    f"Instagram stripped the caption of {post_id}. Slowing down "
                    f"(delay x{self.policy.delay_multiplier:g}). Fix later with `tools fix-captions`.",
                    post_id=post_id, level="warning",
                )
        else:
            self.store.mark_failed(post_id, result.error_message or "unknown error")
            self.store.increment_stat("errors")
            self.events.emit("upload.failed", f"Upload failed for {post_id}: {result.error_message}",
                             post_id=post_id, level="error")
        return result

    def _notify(self, post: Post, result: UploadResult) -> None:
        try:
            self.plugins.notify_upload_complete(post, result)
        except Exception:
            logger.exception("Plugin notification failed")
        source = self.sources.get(post.source_name)
        if source:
            try:
                source.on_post_uploaded(post, result)
            except Exception:
                logger.exception("Source '%s' on_post_uploaded failed", source.name)
