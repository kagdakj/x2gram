"""Core orchestration service for Instagram Auto Uploader."""

import asyncio
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
            else:
                logger.error("Upload failed for post %s: %s", post.id, result.error_message)
                self.state.increment_stat("errors")

            return result

        finally:
            self._cleanup_post_temp_dir(post_dir)

    async def check(self) -> Dict[str, Any]:
        """Perform one poll across all sources and process new posts."""
        async with self._check_lock:
            all_candidate_posts: List[Post] = []

            for source in self.sources:
                try:
                    posts = source.fetch_new_posts()
                    all_candidate_posts.extend(posts)
                except Exception as e:
                    logger.error("Source '%s' fetch error: %s", source.name, e)
                    self.state.increment_stat("errors")

            if not all_candidate_posts:
                return {"total": 0, "found": 0, "uploaded": 0, "baseline": False}

            # Handle initial baseline setup
            # Note: baseline skip only applies to remote sources (e.g. Twitter backlog).
            # Local folder drag-and-drop posts are always uploaded immediately.
            if not self.state.is_initialized() and self.config.service.initial_skip:
                remote_ids = [p.id for p in all_candidate_posts if p.source_name != "local"]
                if remote_ids:
                    self.state.mark_seen_batch(remote_ids, source="baseline")
                    self.state.increment_stat("checked", len(remote_ids))
                    logger.info("Baseline initialized with %d remote posts. Old remote posts will not be uploaded.", len(remote_ids))
                self.state.set_initialized(True)

            # Find unseen posts
            unseen_posts = [p for p in all_candidate_posts if not self.state.is_seen(p.id)]

            # Process oldest first
            unseen_posts.reverse()

            uploaded_count = 0
            for post in unseen_posts:
                self.state.increment_stat("checked")
                result = await self.process_and_upload(post)
                if result.success:
                    uploaded_count += 1

                # Small breather between consecutive posts
                await asyncio.sleep(1.0)

            return {
                "total": len(all_candidate_posts),
                "found": len(unseen_posts),
                "uploaded": uploaded_count,
                "baseline": False,
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
