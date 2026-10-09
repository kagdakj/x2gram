"""Upload pacing: daily limits, minimum interval, random delays, action-block backoff.

The "human-like" extras (active hours, interval jitter, daily cap jitter, rest days) are
all opt-in: with the default config this behaves exactly like a plain rate limiter.
"""

import random
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

from ..config import ServiceConfig
from ..store import Store

MAX_BACKOFF = 8.0


def parse_active_hours(value: str) -> Optional[Tuple[int, int]]:
    """'10:00-23:00' -> (600, 1380) in minutes since midnight. None when unset/invalid."""
    if not value or "-" not in value:
        return None
    start, _, end = value.partition("-")
    try:
        sh, sm = (int(p) for p in start.strip().split(":"))
        eh, em = (int(p) for p in end.strip().split(":"))
    except ValueError:
        return None
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    if not (0 <= start_min < 1440 and 0 < end_min <= 1440) or start_min == end_min:
        return None
    return start_min, end_min


class UploadPolicy:
    def __init__(self, config: ServiceConfig, store: Store):
        self.config = config
        self.store = store
        self.delay_multiplier = 1.0
        self.paused_until: float = 0.0
        self.pause_reason: str = ""
        self._interval_factor = self._new_interval_factor()

    # ── human-like schedule helpers (no-ops unless configured) ────────────

    def _new_interval_factor(self) -> float:
        jitter = max(0.0, self.config.interval_jitter)
        return 1.0 + random.uniform(0.0, jitter)

    def _day_random(self, when: datetime) -> random.Random:
        """Deterministic per-day randomness: restarts don't reshuffle today's plan."""
        return random.Random(f"{when:%Y-%m-%d}")

    def is_rest_day(self, when: Optional[datetime] = None) -> bool:
        probability = max(0.0, min(1.0, self.config.rest_day_probability))
        if probability <= 0:
            return False
        return self._day_random(when or datetime.now()).random() < probability

    def daily_cap(self, when: Optional[datetime] = None) -> int:
        when = when or datetime.now()
        if self.is_rest_day(when):
            return 0
        cap = self.config.max_posts_per_day
        jitter = max(0, self.config.daily_limit_jitter)
        if jitter:
            cap += self._day_random(when).randint(-jitter, jitter)
        return max(0, cap)

    def active_window(self) -> Optional[Tuple[int, int]]:
        return parse_active_hours(self.config.active_hours)

    def _within_active_hours(self, when: datetime) -> bool:
        window = self.active_window()
        if window is None:
            return True
        start, end = window
        minutes = when.hour * 60 + when.minute
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end  # window wraps past midnight

    def _next_window_start(self, when: datetime) -> float:
        """Epoch seconds of the next time the active window opens."""
        window = self.active_window()
        if window is None:
            return when.timestamp()
        start, _ = window
        candidate = when.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if candidate <= when:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    # ── gatekeeping ───────────────────────────────────────────────────────

    def blocked_reason(self, now: Optional[float] = None) -> Optional[str]:
        """Why an upload may not start right now, or None if it may."""
        now = now or time.time()
        if self.paused_until > now:
            return f"paused {int(self.paused_until - now)}s more ({self.pause_reason})"

        if not self.config.rate_limit_enabled:
            return None

        moment = datetime.fromtimestamp(now)
        if not self._within_active_hours(moment):
            return f"outside active hours ({self.config.active_hours})"
        if self.is_rest_day(moment):
            return "rest day (no uploads today)"

        used = self.store.uploads_since(24)
        cap = self.daily_cap(moment)
        if used >= cap:
            return f"daily limit reached ({used}/{cap})"

        last = self.store.last_upload_time()
        if last is not None:
            wait = self.effective_interval() - (now - last)
            if wait > 0:
                return f"min interval not met, next upload in {int(wait)}s"
        return None

    def effective_interval(self) -> float:
        return self.config.min_post_interval * self._interval_factor

    def next_allowed_at(self, now: Optional[float] = None) -> Optional[float]:
        """Earliest time an upload could start (None = not today, e.g. daily cap hit)."""
        now = now or time.time()
        candidates = [now]
        if self.paused_until > now:
            candidates.append(self.paused_until)
        if self.config.rate_limit_enabled:
            moment = datetime.fromtimestamp(now)
            if self.is_rest_day(moment) or self.store.uploads_since(24) >= self.daily_cap(moment):
                return None
            if not self._within_active_hours(moment):
                candidates.append(self._next_window_start(moment))
            last = self.store.last_upload_time()
            if last is not None:
                candidates.append(last + self.effective_interval())
        return max(candidates)

    # ── pacing ────────────────────────────────────────────────────────────

    def delay_range(self) -> tuple:
        lo = self.config.post_delay_min * self.delay_multiplier
        hi = max(lo, self.config.post_delay_max * self.delay_multiplier)
        return lo, hi

    def random_delay(self) -> float:
        lo, hi = self.delay_range()
        return random.uniform(lo, hi) if hi > 0 else 0.0

    # ── feedback from Instagram ───────────────────────────────────────────

    def on_caption_stripped(self) -> None:
        """Instagram silently removed a caption: slow down future uploads."""
        self.delay_multiplier = min(MAX_BACKOFF, self.delay_multiplier * 2)
        self.pause(60, "caption stripped cooldown")

    def on_success(self) -> None:
        self.delay_multiplier = max(1.0, self.delay_multiplier * 0.75)
        self._interval_factor = self._new_interval_factor()  # next gap differs from this one

    def on_action_block(self) -> None:
        self.pause(self.config.action_block_cooldown, "Instagram action block")

    def pause(self, seconds: float, reason: str) -> None:
        self.paused_until = max(self.paused_until, time.time() + seconds)
        self.pause_reason = reason

    def resume(self) -> None:
        self.paused_until = 0.0
        self.pause_reason = ""
