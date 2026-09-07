"""Core orchestration service for Instagram Auto Uploader."""

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from .config import AppConfig
from .models import MediaItem, MediaType, Post, UploadResult
from .plugins.base import PluginManager
from .sources.base import BaseSource
from .sources.local_folder import LocalFolderSource
from .sources.twitter_cli import TwitterCliSource
from .state import StateStore
from .uploader.base import BaseUploader
from .uploader.instagram import InstagramUploader

logger = logging.getLogger("auto_uploader.engine")


class AutoUploaderEngine:
    """Orchestrates sources, plugins, state, and uploaders."""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig.load()
        self.state = StateStore(db_path=self.config.service.db_path)
        self.plugin_manager = PluginManager(self.config.plugins)

        # Setup sources
        self.sources: List[BaseSource] = []
        if self.config.twitter.enabled:
            self.sources.append(TwitterCliSource(self.config.twitter.model_dump()))
        if self.config.local.enabled:
            self.sources.append(LocalFolderSource(self.config.local.model_dump()))

        # Setup uploader
        self.uploader: BaseUploader = InstagramUploader(self.config.instagram.model_dump())

        # Temporary files root
        self.temp_root = Path(self.config.service.temp_dir)
        self.temp_root.mkdir(parents=True, exist_ok=True)

        self._check_lock = asyncio.Lock()
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None

    def cleanup_temp_all(self) -> None:
        """Remove any leftover temp media directories."""
        if self.temp_root.is_dir():
            shutil.rmtree(self.temp_root, ignore_errors=True)
            self.temp_root.mkdir(parents=True, exist_ok=True)

    def _create_post_temp_dir(self, post_id: str) -> Path:
        """Create an isolated temporary folder for one post."""
        post_dir = self.temp_root / f"post_{post_id}_{int(time.time())}"
        post_dir.mkdir(parents=True, exist_ok=True)
        return post_dir

    def _cleanup_post_temp_dir(self, post_dir: Optional[Path]) -> None:
        """Safely delete post temporary directory."""
        if post_dir and post_dir.is_dir():
            try:
                shutil.rmtree(post_dir, ignore_errors=True)
            except Exception:
                pass

    async def _download_media_item(
        self,
        session: aiohttp.ClientSession,
        media_item: MediaItem,
        target_dir: Path,
        index: int,
    ) -> bool:
        """Download remote media item to local file."""
        if media_item.local_path and media_item.local_path.is_file():
            return True

        if not media_item.url:
            return False

        try:
            async with session.get(media_item.url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning("HTTP %d downloading %s", resp.status, media_item.url)
                    return False

                data = await resp.read()
                if not data:
                    return False

                # Infer extension
                content_type = resp.headers.get("Content-Type", "").lower()
                ext = ".jpg"
                if "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                elif "gif" in content_type:
                    ext = ".gif"
                elif "mp4" in content_type or "video" in content_type:
                    ext = ".mp4"
                else:
                    path_ext = os.path.splitext(urlparse(str(resp.url)).path)[1].lower()
                    if path_ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
                        ext = path_ext

                filename = f"media_{index}{ext}"
                local_file = target_dir / filename
                with open(local_file, "wb") as f:
                    f.write(data)

                media_item.local_path = local_file
                media_item.original_path = local_file
                media_item.content_type = content_type
                return True

        except Exception as e:
            logger.error("Download failed for %s: %s", media_item.url, e)
            return False

    async def download_post_media(self, post: Post, post_dir: Path) -> None:
        """Prepare all media attachments in post_dir."""
        if not post.has_media:
            return

        remote_items = []
        for idx, media in enumerate(post.media_items):
            if media.local_path and media.local_path.is_file():
                # Copy local file into isolated post_dir for non-destructive processing
                dest = post_dir / media.local_path.name
                if media.local_path.resolve() != dest.resolve():
                    shutil.copyfile(media.local_path, dest)
                media.local_path = dest
            elif media.url:
                remote_items.append((idx, media))

        if remote_items:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as session:
                tasks = [
                    self._download_media_item(session, media, post_dir, idx)
                    for idx, media in remote_items
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

    async def process_and_upload(self, post: Post) -> UploadResult:
        """Download, run through plugin pipeline, and upload to Instagram."""
        post_dir = self._create_post_temp_dir(post.id)

        try:
            # 1. Download/stage media
            await self.download_post_media(post, post_dir)

            # Filter out media that failed downloading
            valid_media = [m for m in post.media_items if m.is_valid_local]
            if not valid_media:
                return UploadResult(
                    success=False,
                    error_message="No valid media could be obtained for this post.",
                )
            post.media_items = valid_media

            # 2. Run plugins pipeline (anime crop, caption formatting, watermark)
            post = self.plugin_manager.apply_post_pipeline(post)

            # 3. Post delay if specified
            delay = self.config.service.post_delay
            if delay > 0:
                logger.info("Applying post delay (%d seconds)...", delay)
                await asyncio.sleep(delay)

            # 4. Upload to Instagram
            result = self.uploader.upload_post(post)

            # 5. Notify plugins of result
            self.plugin_manager.notify_upload_complete(post, result)

            # 6. Notify source of result (e.g. move local files to processed/)
            for source in self.sources:
                if source.name == post.source_name:
                    try:
                        source.on_post_uploaded(post, result)
                    except Exception as e:
                        logger.error("Source '%s' on_post_uploaded failed: %s", source.name, e)

            if result.success:
                logger.info("Successfully uploaded post %s (media_id: %s)", post.id, result.media_id)
                self.state.mark_seen(post.id, post.source_name, status="uploaded")
                self.state.increment_stat("published")

                # If caption was stripped, increase delay for subsequent posts
                if result.extra.get("caption_stripped"):
                    old_delay = self.config.service.post_delay
                    new_delay = max(60, old_delay * 2)
                    self.config.service.post_delay = new_delay
                    logger.warning(
                        "⚠️ Caption was stripped by Instagram. Increasing post_delay from %ds to %ds "
                        "to reduce spam detection risk.",
                        old_delay, new_delay,
                    )
                    # Extra cooldown before next post
                    logger.info("Applying extra cooldown (60s) after caption stripping...")
                    await asyncio.sleep(60)
            else:
                logger.error("Upload failed for post %s: %s", post.id, result.error_message)
                self.state.increment_stat("errors")

            return result

        finally:
            self._cleanup_post_temp_dir(post_dir)

    # ── Post serialization for queue ───────────────────────────────────────

    @staticmethod
    def _post_to_json(post: Post) -> str:
        """Serialize a Post to JSON string for queue storage."""
        media_dicts = []
        for m in post.media_items:
            media_dicts.append({
                "url": m.url,
                "media_type": m.media_type.value,
                "width": m.width,
                "height": m.height,
            })
        return json.dumps({
            "id": post.id,
            "source_name": post.source_name,
            "author_username": post.author_username,
            "author_name": post.author_name,
            "text": post.text,
            "caption": post.caption,
            "url": post.url,
            "is_repost": post.is_repost,
            "created_at": post.created_at,
            "media_items": media_dicts,
            "raw_data": post.raw_data,
        }, ensure_ascii=False)

    @staticmethod
    def _post_from_json(json_str: str) -> Post:
        """Deserialize a Post from queue JSON string."""
        d = json.loads(json_str)
        media_items = []
        for m in d.get("media_items", []):
            media_items.append(MediaItem(
                url=m.get("url"),
                media_type=MediaType(m.get("media_type", "photo")),
                width=m.get("width"),
                height=m.get("height"),
            ))
        return Post(
            id=d["id"],
            source_name=d.get("source_name", "twitter"),
            author_username=d.get("author_username", "unknown"),
            author_name=d.get("author_name", "Unknown"),
            text=d.get("text", ""),
            caption=d.get("caption", ""),
            url=d.get("url"),
            media_items=media_items,
            is_repost=d.get("is_repost", False),
            created_at=d.get("created_at"),
            raw_data=d.get("raw_data", {}),
        )

    # ── Queue management ──────────────────────────────────────────────────

    def _enqueue_new_posts(self, posts: List[Post]) -> int:
        """Filter unseen posts with media and add to queue. Returns count added."""
        items = []
        for p in posts:
            if not p.has_media:
                continue
            if self.state.is_seen(p.id):
                continue
            if self.state.queue_has(p.id):
                continue
            items.append((p.id, p.source_name, self._post_to_json(p)))
        return self.state.enqueue_batch(items)

    async def seed(self) -> Dict[str, Any]:
        """One-time deep fetch: pull initial_load posts and populate queue with all unseen."""
        all_posts: List[Post] = []
        for source in self.sources:
            try:
                if source.name == "twitter":
                    limit = getattr(self.config.twitter, "initial_load", 200) or 200
                    posts = source.fetch_new_posts(limit=limit)
                else:
                    posts = source.fetch_new_posts()
                all_posts.extend(posts)
            except Exception as e:
                logger.error("Source '%s' seed fetch error: %s", source.name, e)

        added = self._enqueue_new_posts(all_posts)
        pending = self.state.queue_size("pending")
        logger.info("Seed complete: fetched %d posts, queued %d new, %d total pending.", len(all_posts), added, pending)
        return {"fetched": len(all_posts), "queued": added, "pending": pending}

    async def check(self) -> Dict[str, Any]:
        """Two-phase check: 1) light poll → queue new posts, 2) pop queue → upload."""
        async with self._check_lock:
            # ── Phase 1: Poll sources lightly, queue new posts ──
            all_candidate_posts: List[Post] = []

            for source in self.sources:
                try:
                    if source.name == "twitter":
                        poll_limit = getattr(self.config.twitter, "poll_limit", 20) or 20
                        posts = source.fetch_new_posts(limit=poll_limit)
                    else:
                        posts = source.fetch_new_posts()
                    all_candidate_posts.extend(posts)
                except Exception as e:
                    logger.error("Source '%s' fetch error: %s", source.name, e)
                    self.state.increment_stat("errors")

            # Handle initial baseline setup
            if not self.state.is_initialized() and self.config.service.initial_skip:
                remote_ids = [p.id for p in all_candidate_posts if p.source_name != "local"]
                if remote_ids:
                    self.state.mark_seen_batch(remote_ids, source="baseline")
                    self.state.increment_stat("checked", len(remote_ids))
                    logger.info("Baseline initialized with %d remote posts.", len(remote_ids))
                self.state.set_initialized(True)
                return {"total": len(all_candidate_posts), "found": 0, "uploaded": 0, "queued": 0, "baseline": True}

            # Queue any new posts discovered
            newly_queued = self._enqueue_new_posts(all_candidate_posts)
            if newly_queued > 0:
                logger.info("Queued %d new post(s) for upload.", newly_queued)

            # ── Phase 2: Pop from queue and upload ──
            pending_count = self.state.queue_size("pending")
            if pending_count == 0:
                return {"total": len(all_candidate_posts), "found": 0, "uploaded": 0, "queued": newly_queued, "baseline": False}

            # Pop one at a time for rate-limit compliance
            pending_rows = self.state.pop_pending(limit=10)
            uploaded_count = 0

            for row in pending_rows:
                post_id = row["post_id"]
                post_json = row["post_json"]

                # Skip if already seen (uploaded between seed and now)
                if self.state.is_seen(post_id):
                    self.state.mark_queue_status(post_id, "done")
                    continue

                # Rate limit checks
                if self.config.service.rate_limit_enabled:
                    today_uploads = self.state.get_uploads_in_last_hours(24)
                    if today_uploads >= self.config.service.max_posts_per_day:
                        logger.warning(
                            "🛑 Daily upload limit reached (%d/%d). Stopping.",
                            today_uploads, self.config.service.max_posts_per_day,
                        )
                        break

                    last_upload = self.state.get_last_upload_time()
                    if last_upload:
                        elapsed = time.time() - last_upload
                        min_interval = self.config.service.min_post_interval
                        if elapsed < min_interval:
                            logger.info(
                                "⏳ Min interval not met (%ds/%ds). Next upload in %ds.",
                                int(elapsed), min_interval, int(min_interval - elapsed),
                            )
                            break

                # Deserialize and upload
                try:
                    post = self._post_from_json(post_json)
                except Exception as e:
                    logger.error("Failed to deserialize queued post %s: %s", post_id, e)
                    self.state.mark_queue_status(post_id, "failed")
                    continue

                self.state.increment_stat("checked")
                result = await self.process_and_upload(post)

                if result.success:
                    uploaded_count += 1
                    self.state.mark_queue_status(post_id, "done")
                else:
                    self.state.mark_queue_status(post_id, "failed")

                await asyncio.sleep(1.0)

            remaining = self.state.queue_size("pending")
            logger.info("Check complete: %d uploaded, %d still pending in queue.", uploaded_count, remaining)

            return {
                "total": len(all_candidate_posts),
                "found": len(pending_rows),
                "uploaded": uploaded_count,
                "queued": newly_queued,
                "pending": remaining,
                "baseline": False,
            }

    async def sync_missing_posts(
        self,
        twitter_limit: int = 50,
        ig_limit: int = 50,
        dry_run: bool = False,
        selected_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Compare actual Instagram posts against X reposts and upload missing ones."""
        import re
        import sqlite3
        from datetime import datetime, timezone

        # 1. Ensure Instagram client and fetch actual media
        uploader = self.uploader
        if not hasattr(uploader, "_ensure_client"):
            raise RuntimeError("Uploader does not support Instagram client access.")

        cl = uploader._ensure_client()
        user_id = cl.user_id_from_username(self.config.instagram.username)
        logger.info("Fetching up to %d recent Instagram posts for @%s...", ig_limit, self.config.instagram.username)
        medias = cl.user_medias(user_id, amount=ig_limit)

        ig_tweet_ids = set()
        for m in medias:
            cap = m.caption_text or ""
            matches = re.findall(r"(?:twitter\.com|x\.com)/[^/\s]+/status/(\d+)", cap)
            for tid in matches:
                ig_tweet_ids.add(tid)

        # Fallback: Correlate with DB seen_posts timestamp matching for any Instagram post without parsed URL
        db_posts = []
        try:
            con = sqlite3.connect(self.config.service.db_path)
            cur = con.cursor()
            db_posts = cur.execute("SELECT post_id, seen_at FROM seen_posts WHERE source='twitter'").fetchall()
            con.close()
        except Exception:
            db_posts = []

        if db_posts:
            for m in medias:
                cap = m.caption_text or ""
                matches = re.findall(r"(?:twitter\.com|x\.com)/[^/\s]+/status/(\d+)", cap)
                if not matches:
                    m_ts = m.taken_at.timestamp()
                    for pid, seen_at_str in db_posts:
                        try:
                            dt = datetime.strptime(seen_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            if abs(m_ts - dt.timestamp()) < 25:
                                ig_tweet_ids.add(pid)
                                break
                        except Exception:
                            pass

        logger.info("Found %d posts on Instagram, identified %d associated X tweet IDs.", len(medias), len(ig_tweet_ids))

        # 2. Fetch X reposts from Twitter source
        twitter_source = next((s for s in self.sources if s.name == "twitter"), None)
        if not twitter_source:
            raise RuntimeError("Twitter source is not enabled or configured.")

        logger.info("Fetching up to %d reposts from Twitter @%s...", twitter_limit, self.config.twitter.username)
        tweets = twitter_source.fetch_new_posts(limit=twitter_limit)

        # 3. Compare to find missing reposts that have media
        missing_posts = [
            t for t in tweets
            if t.id not in ig_tweet_ids and t.has_media
        ]

        # Upload in chronological order (oldest first)
        missing_posts.reverse()

        logger.info(
            "Comparison complete: %d X reposts checked, %d missing from Instagram.",
            len(tweets),
            len(missing_posts),
        )

        posts_to_upload = missing_posts
        if selected_ids is not None:
            sel_set = set(selected_ids)
            posts_to_upload = [p for p in missing_posts if p.id in sel_set]

        uploaded_count = 0
        failed_count = 0

        if not dry_run:
            for post in posts_to_upload:
                logger.info("Uploading missing post %s (Author: %s)...", post.id, post.author_username)
                result = await self.process_and_upload(post)
                if result.success:
                    uploaded_count += 1
                else:
                    failed_count += 1
                    logger.error("Failed uploading missing post %s: %s", post.id, result.error_message)

        return {
            "ig_posts_scanned": len(medias),
            "ig_tweet_ids_found": len(ig_tweet_ids),
            "twitter_posts_scanned": len(tweets),
            "missing_posts": missing_posts,
            "posts_to_upload": posts_to_upload,
            "uploaded_count": uploaded_count,
            "failed_count": failed_count,
            "dry_run": dry_run,
        }

    async def run_loop(self) -> None:
        """Background continuous checking loop."""
        self._running = True
        logger.info("Service loop started (interval: %d sec)", self.config.service.check_interval)

        while self._running:
            try:
                await self.check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in service loop: %s", e, exc_info=True)
                self.state.increment_stat("errors")

            try:
                await asyncio.sleep(max(15, self.config.service.check_interval))
            except asyncio.CancelledError:
                break

        logger.info("Service loop ended.")

    def start(self) -> None:
        """Start async loop in asyncio event loop."""
        try:
            asyncio.run(self.run_loop())
        except KeyboardInterrupt:
            logger.info("Service stopped by user.")
        finally:
            self.cleanup_temp_all()

    def stop(self) -> None:
        """Signal service loop to stop."""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
