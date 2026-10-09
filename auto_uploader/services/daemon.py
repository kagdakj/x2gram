"""Background loop: poll sources, then upload from the queue."""

import asyncio
import logging
import time
from typing import Optional

from ..config import ServiceConfig
from ..models import PostStatus
from ..store import Store
from .events import EventBus
from .ingest import IngestService
from .policy import UploadPolicy
from .publisher import Publisher

logger = logging.getLogger("auto_uploader.daemon")

MIN_SLEEP = 15


class Daemon:
    def __init__(self, config: ServiceConfig, store: Store, ingest: IngestService,
                 publisher: Publisher, policy: UploadPolicy, events: EventBus):
        self.config = config
        self.store = store
        self.ingest = ingest
        self.publisher = publisher
        self.policy = policy
        self.events = events
        self.state = "stopped"          # stopped | fetching | uploading | sleeping | stopping
        self.next_cycle_at: Optional[float] = None
        self._last_fetch = 0.0
        self._stop: Optional[asyncio.Event] = None

    @property
    def running(self) -> bool:
        return self.state != "stopped"

    def _set_state(self, state: str) -> None:
        self.state = state
        self.events.emit("daemon.state", f"Daemon {state}", state=state)

    async def run(self, once: bool = False) -> None:
        if self.running:
            raise RuntimeError("Daemon is already running.")
        self._stop = asyncio.Event()
        recovered = self.store.recover_stale_uploads()
        if recovered:
            self.events.emit("daemon.recovered", f"{recovered} interrupted upload(s) marked failed; check Instagram before retrying.", level="warning")

        try:
            first = True
            while not self._stop.is_set():
                await self.run_cycle(force_fetch=first)
                first = False
                if once or self._stop.is_set():
                    break
                wait = self._sleep_seconds()
                self.next_cycle_at = time.time() + wait
                self._set_state("sleeping")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.next_cycle_at = None
            self._set_state("stopped")

    def stop(self) -> None:
        if self._stop is not None and self.running:
            self._set_state("stopping")
            self._stop.set()
            self.publisher.request_stop()

    async def run_cycle(self, force_fetch: bool = True) -> None:
        # While draining a backlog the loop wakes up often; don't hammer the sources.
        if force_fetch or time.time() - self._last_fetch >= max(MIN_SLEEP, self.config.check_interval):
            self._set_state("fetching")
            self._last_fetch = time.time()
            try:
                await self.ingest.fetch()
            except Exception as e:
                logger.exception("Fetch cycle failed")
                self.events.emit("daemon.error", f"Fetch failed: {e}", level="error")

        if self._stop is not None and self._stop.is_set():
            return
        if self.store.counts()[PostStatus.QUEUED] == 0:
            return

        self._set_state("uploading")
        try:
            result = await self.publisher.upload_next(limit=max(1, self.config.uploads_per_cycle))
            if result.uploaded or result.failed:
                self.events.emit(
                    "daemon.cycle",
                    f"Cycle done: {len(result.uploaded)} uploaded, {len(result.failed)} failed, "
                    f"{self.store.counts()[PostStatus.QUEUED]} still queued.",
                )
        except Exception as e:
            logger.exception("Upload cycle failed")
            self.events.emit("daemon.error", f"Upload cycle failed: {e}", level="error")

    def _sleep_seconds(self) -> float:
        interval = max(MIN_SLEEP, self.config.check_interval)
        if self.store.counts()[PostStatus.QUEUED] == 0:
            return interval
        next_allowed = self.policy.next_allowed_at()
        if next_allowed is None:
            return interval
        # Wake up early if the queue could move before the next regular poll.
        return max(MIN_SLEEP, min(interval, next_allowed - time.time()))
