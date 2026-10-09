"""Pull posts from sources and put the new ones into the store."""

import asyncio
from dataclasses import dataclass, field
from typing import List, Sequence

from ..models import Post, PostStatus
from ..sources.base import BaseSource
from ..store import Store
from .events import EventBus


@dataclass
class FetchResult:
    fetched: int = 0
    new_posts: List[Post] = field(default_factory=list)
    queued: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    baseline: bool = False


class IngestService:
    def __init__(self, store: Store, sources: Sequence[BaseSource], events: EventBus, initial_skip: bool = True):
        self.store = store
        self.sources = sources
        self.events = events
        self.initial_skip = initial_skip

    async def fetch(self, deep: bool = False, dry_run: bool = False) -> FetchResult:
        """Poll every source once.

        - regular poll: on the very first run (initial_skip) remote posts are stored as
          `skipped` so the account's existing history isn't mass-uploaded.
        - deep fetch: the user explicitly wants the backlog, so everything new is queued
          and the baseline is considered done.
        """
        result = FetchResult()
        self.events.emit("fetch.started", f"Fetching from {len(self.sources)} source(s){' (deep)' if deep else ''}...")

        posts: List[Post] = []
        for source in self.sources:
            try:
                batch = await asyncio.to_thread(source.fetch, deep)
            except Exception as e:
                msg = f"Source '{source.name}' fetch failed: {e}"
                result.errors.append(msg)
                self.store.increment_stat("errors")
                self.events.emit("fetch.error", msg, level="error")
                continue
            posts.extend(batch)

        result.fetched = len(posts)
        candidates = [p for p in posts if p.has_media]
        known = self.store.known_ids(p.id for p in candidates)
        seen_in_batch = set()
        for p in candidates:
            if p.id not in known and p.id not in seen_in_batch:
                seen_in_batch.add(p.id)
                result.new_posts.append(p)

        if dry_run:
            self.events.emit("fetch.done", f"Dry run: {len(result.new_posts)} new post(s) found.")
            return result

        source_by_name = {s.name: s for s in self.sources}
        baseline = self.initial_skip and not deep and not self.store.is_initialized()
        to_skip = []
        to_queue = result.new_posts
        if baseline:
            to_skip = [p for p in result.new_posts if getattr(source_by_name.get(p.source_name), "is_remote", True)]
            to_queue = [p for p in result.new_posts if p not in to_skip]
            result.baseline = True

        result.skipped = self.store.add_posts(to_skip, PostStatus.SKIPPED)
        result.queued = self.store.add_posts(to_queue, PostStatus.QUEUED)
        self.store.increment_stat("checked", len(result.new_posts))
        if not self.store.is_initialized():
            self.store.set_initialized(True)

        if baseline:
            message = f"First run: {result.skipped} existing post(s) marked as skipped, {result.queued} queued."
        else:
            message = f"Fetched {result.fetched}, queued {result.queued} new post(s)."
        self.events.emit("fetch.done", message, level="success" if result.queued else "info",
                         queued=result.queued, skipped=result.skipped)
        return result
