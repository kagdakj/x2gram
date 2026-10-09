import os

from auto_uploader.config import AppConfig, ServiceConfig


def test_legacy_post_delay_maps_to_min():
    cfg = ServiceConfig(post_delay=40)
    assert cfg.post_delay_min == 40
    assert cfg.post_delay_max == 60
    assert ServiceConfig(post_delay=90).post_delay_max == 90


def test_env_file_and_empty_values(tmp_path, monkeypatch):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TWITTER_USERNAME", "INSTAGRAM_USERNAME", "CHECK_INTERVAL")}
    monkeypatch.setattr(os, "environ", env)
    (tmp_path / "config.yaml").write_text("instagram:\n  username: from_yaml\n")
    (tmp_path / ".env").write_text("INSTAGRAM_USERNAME=\nTWITTER_USERNAME=from_env\nCHECK_INTERVAL=120\n")
    cfg = AppConfig.load(str(tmp_path / "config.yaml"), env_file=str(tmp_path / ".env"))
    assert cfg.instagram.username == "from_yaml"  # empty .env value doesn't wipe it
    assert cfg.twitter.username == "from_env"
    assert cfg.service.check_interval == 120
