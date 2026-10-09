import time
from datetime import datetime

from auto_uploader.config import ServiceConfig
from auto_uploader.services.policy import UploadPolicy, parse_active_hours
from auto_uploader.store import Store


def make_policy(tmp_path, **overrides):
    cfg = ServiceConfig(rate_limit_enabled=True, max_posts_per_day=5, min_post_interval=600, **overrides)
    return UploadPolicy(cfg, Store(str(tmp_path / "p.db")))


def at(hour, minute=0):
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def test_defaults_behave_like_plain_rate_limiter(tmp_path):
    policy = make_policy(tmp_path)
    assert policy.active_window() is None
    assert policy.is_rest_day() is False
    assert policy.daily_cap() == 5
    assert policy.effective_interval() == 600
    assert policy.blocked_reason(at(3)) is None        # any hour is fine


def test_active_hours_block_and_schedule(tmp_path):
    policy = make_policy(tmp_path, active_hours="10:00-23:00")
    assert policy.active_window() == (600, 1380)
    assert policy.blocked_reason(at(12)) is None
    assert "outside active hours" in policy.blocked_reason(at(3))

    next_at = policy.next_allowed_at(at(3))
    assert datetime.fromtimestamp(next_at).hour == 10
    assert next_at > at(3)
    # after the window, the next slot is tomorrow morning
    assert datetime.fromtimestamp(policy.next_allowed_at(at(23, 30))).hour == 10


def test_active_hours_wrapping_midnight(tmp_path):
    policy = make_policy(tmp_path, active_hours="22:00-02:00")
    assert policy.blocked_reason(at(23)) is None
    assert policy.blocked_reason(at(1)) is None
    assert policy.blocked_reason(at(12)) is not None


def test_interval_jitter_varies_between_uploads(tmp_path):
    policy = make_policy(tmp_path, interval_jitter=0.8)
    seen = set()
    for _ in range(20):
        policy.on_success()
        interval = policy.effective_interval()
        assert 600 <= interval <= 600 * 1.8
        seen.add(round(interval))
    assert len(seen) > 1


def test_daily_cap_jitter_is_stable_per_day(tmp_path):
    policy = make_policy(tmp_path, daily_limit_jitter=1)
    day = datetime(2026, 9, 20, 12, 0)
    caps = {policy.daily_cap(day) for _ in range(5)}
    assert len(caps) == 1 and 4 <= caps.pop() <= 6
    other_days = {policy.daily_cap(day.replace(day=d)) for d in range(1, 25)}
    assert len(other_days) > 1


def test_rest_day_blocks_everything(tmp_path):
    policy = make_policy(tmp_path, rest_day_probability=1.0)
    assert policy.daily_cap() == 0
    assert "rest day" in policy.blocked_reason()
    assert policy.next_allowed_at() is None


def test_parse_active_hours_rejects_garbage():
    assert parse_active_hours("") is None
    assert parse_active_hours("10:00") is None
    assert parse_active_hours("abc-def") is None
    assert parse_active_hours("10:00-10:00") is None
    assert parse_active_hours("09:30-17:45") == (570, 1065)
