"""Wires config, store and services together (lazily, so cheap commands stay fast)."""

from functools import cached_property
from typing import List, Optional

from .config import AppConfig
from .plugins.base import PluginManager
from .services import (
    Daemon, EventBus, IngestService, MaintenanceService, Publisher, SyncService, UploadPolicy,
)
from .services.media import MediaStager
from .sources.base import BaseSource
from .store import Store
from .uploader.instagram import InstagramUploader


class AppContext:
    def __init__(self, config: AppConfig, config_path: str = "config.yaml"):
        self.config = config
        self.config_path = config_path
        self.events = EventBus()
        self.store = Store(config.service.db_path)

    @classmethod
    def load(cls, config_path: str = "config.yaml") -> "AppContext":
        return cls(AppConfig.load(config_path), config_path)

    # ── adapters ──────────────────────────────────────────────────────────

    @cached_property
    def sources(self) -> List[BaseSource]:
        from .sources.local_folder import LocalFolderSource
        from .sources.twitter_cli import TwitterCliSource

        sources: List[BaseSource] = []
        if self.config.twitter.enabled:
            sources.append(TwitterCliSource(self.config.twitter.model_dump()))
        if self.config.local.enabled:
            sources.append(LocalFolderSource(self.config.local.model_dump()))
        return sources

    @property
    def twitter(self) -> Optional[BaseSource]:
        return next((s for s in self.sources if s.name == "twitter"), None)

    @cached_property
    def uploader(self) -> InstagramUploader:
        return InstagramUploader(self.config.instagram.model_dump())

    @cached_property
    def plugins(self) -> PluginManager:
        return PluginManager(self.config.plugins)

    # ── services ──────────────────────────────────────────────────────────

    @cached_property
    def policy(self) -> UploadPolicy:
        return UploadPolicy(self.config.service, self.store)

    @cached_property
    def stager(self) -> MediaStager:
        return MediaStager(self.config.service.temp_dir)

    @cached_property
    def ingest(self) -> IngestService:
        return IngestService(self.store, self.sources, self.events, self.config.service.initial_skip)

    @cached_property
    def publisher(self) -> Publisher:
        return Publisher(
            store=self.store,
            uploader=self.uploader,
            plugins=lambda: self.plugins,
            sources=self.sources,
            policy=self.policy,
            stager=self.stager,
            events=self.events,
        )

    @cached_property
    def sync(self) -> SyncService:
        return SyncService(self.store, self.uploader, self.twitter, self.events)

    @cached_property
    def maintenance(self) -> MaintenanceService:
        return MaintenanceService(
            self.config, self.store, self.events,
            uploader_factory=lambda: self.uploader,
            sources_factory=lambda: self.sources,
            plugins_factory=lambda: self.plugins,
        )

    @cached_property
    def daemon(self) -> Daemon:
        return Daemon(self.config.service, self.store, self.ingest, self.publisher, self.policy, self.events)
