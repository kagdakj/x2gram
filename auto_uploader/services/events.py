"""Tiny publish/subscribe bus so the CLI and TUI can follow what services do."""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("auto_uploader.events")


@dataclass
class Event:
    kind: str                    # e.g. "upload.started", "fetch.done", "daemon.sleeping"
    message: str
    post_id: Optional[str] = None
    level: str = "info"          # info | success | warning | error
    data: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


Listener = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: List[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def emit(
        self,
        kind: str,
        message: str,
        post_id: Optional[str] = None,
        level: str = "info",
        **data: Any,
    ) -> Event:
        event = Event(kind=kind, message=message, post_id=post_id, level=level, data=data)
        log_level = {"warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        logger.log(log_level, message)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("Event listener failed")
        return event
