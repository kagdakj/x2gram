"""Compare what is on Instagram with the X reposts and find what's missing."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..models import Post, PostStatus
from ..sources.base import BaseSource
from ..store import Store
from ..uploader.instagram import InstagramMedia
from .events import EventBus

TWEET_URL_RE = re.compile(r"(?:twitter\.com|x\.com)/[^/\s]+/status/(\d+)")
TIMESTAMP_TOLERANCE = 30  # seconds between our upload record and Instagram's taken_at


def tweet_ids_in_caption(caption: str) -> List[str]:
    return TWEET_URL_RE.findall(caption or "")


def match_media_to_post(media: InstagramMedia, timeline: Sequence[Tuple[str, float]]) -> Optional[str]:
    """Tweet id for an Instagram post: caption link first, then closest upload timestamp."""
    ids = tweet_ids_in_caption(media.caption)
    if ids:
        return ids[0]
    best, best_diff = None, float(TIMESTAMP_TOLERANCE)
    for post_id, uploaded_at in timeline:
        if uploaded_at is None:
            continue
        diff = abs(media.taken_at - uploaded_at)
        if diff < best_diff:
            best, best_diff = post_id, diff
    return best


@dataclass
class SyncReport:
    ig_scanned: int = 0
    x_scanned: int = 0
    ig_tweet_ids: Set[str] = field(default_factory=set)
    marked_uploaded: int = 0
    missing: List[Post] = field(default_factory=list)
    hidden_uploaded: int = 0   # uploaded per our DB but not in the scanned IG window
    statuses: Dict[str, PostStatus] = field(default_factory=dict)


class SyncService:
    def __init__(self, store: Store, uploader, twitter: Optional[BaseSource], events: EventBus):
        self.store = store
        self.uploader = uploader
        self.twitter = twitter
        self.events = events

    async def scan(self, ig_limit: int = 50, x_limit: int = 50, include_uploaded: bool = False) -> SyncReport:
        if self.twitter is None:
            raise RuntimeError("Twitter source is not enabled in config.")

        report = SyncReport()
        self.events.emit("sync.started", f"Scanning {ig_limit} Instagram posts and {x_limit} X reposts...")

        medias = await asyncio.to_thread(self.uploader.list_recent_media, ig_limit)
        timeline = self.store.upload_timeline("twitter")
        for media in medias:
            for tid in tweet_ids_in_caption(media.caption):
                report.ig_tweet_ids.add(tid)
            matched = match_media_to_post(media, timeline)
            if matched:
                report.ig_tweet_ids.add(matched)
        report.ig_scanned = len(medias)

        tweets = await asyncio.to_thread(self.twitter.fetch_new_posts, x_limit)
        report.x_scanned = len(tweets)
        with_media = [t for t in tweets if t.has_media]

        # Anything already on Instagram is recorded as uploaded so it never gets re-posted.
        on_ig = [t for t in with_media if t.id in report.ig_tweet_ids]
        report.marked_uploaded = self.store.mark_found_on_instagram(t.id for t in on_ig)
        report.marked_uploaded += self.store.add_posts(on_ig, PostStatus.UPLOADED)

        for tweet in reversed(with_media):  # oldest first
            if tweet.id in report.ig_tweet_ids:
                continue
            record = self.store.get(tweet.id)
            status = record.status if record else None
            if status == PostStatus.UPLOADING:
                continue
            if status == PostStatus.UPLOADED and not include_uploaded:
                report.hidden_uploaded += 1
                continue
            if status:
                report.statuses[tweet.id] = status
            report.missing.append(tweet)

        self.events.emit(
            "sync.done",
            f"Sync scan: {len(report.missing)} missing post(s) "
            f"({report.ig_scanned} IG / {report.x_scanned} X scanned).",
            level="success",
        )
        return report

    def enqueue(self, posts: Iterable[Post]) -> List[str]:
        """Make the given posts uploadable (new -> queued, failed/skipped -> re-queued)."""
        posts = list(posts)
        self.store.add_posts(posts, PostStatus.QUEUED)
        ids = [p.id for p in posts]
        self.store.requeue(ids)
        return ids
