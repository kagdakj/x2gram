"""Logging configuration for CLI and TUI modes."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level: str = "INFO", log_file: Optional[str] = None,
                  console: bool = True, extra_handler: Optional[logging.Handler] = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(file_handler)

    if console:
        from rich.logging import RichHandler
        root.addHandler(RichHandler(show_path=False, rich_tracebacks=True, log_time_format="%H:%M:%S"))

    if extra_handler:
        root.addHandler(extra_handler)

    # Third-party chatter
    for noisy in ("urllib3", "httpx", "instagrapi", "public_request", "private_request", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
