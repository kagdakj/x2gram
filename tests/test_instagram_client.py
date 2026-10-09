"""Session/device handling of the Instagram adapter (no network: instagrapi.Client is faked)."""

import json

import pytest

from auto_uploader.uploader.instagram import InstagramUploader

CURRENT_APP = "449.0.0.52.84"   # what the fake instagrapi considers up to date
OLD_APP = "428.0.0.47.67"

SAVED = {
    "uuids": {"uuid": "keep-me", "phone_id": "phone-1"},
    "device_settings": {"manufacturer": "Google/google", "model": "Pixel 8 Pro", "app_version": OLD_APP},
    "user_agent": f"Instagram {OLD_APP} Android",
    "cookies": {"sessionid": "expired"},
}


class FakeClient:
    instances = []

    def __init__(self):
        self.settings = {"uuids": {"uuid": "fresh-device"},
                         "device_settings": {"model": "Random Phone", "app_version": CURRENT_APP}}
        self.delay_range = None
        self.logins = []
        self.locale = self.country = self.country_code = self.tz = None
        self.timeline_ok = True
        self.edits = []
        self.media_caption = ""
        FakeClient.instances.append(self)

    # session
    def set_settings(self, settings):
        self.settings = json.loads(json.dumps(settings))

    def load_settings(self, path, override_app_version=False):
        with open(path) as f:
            self.set_settings(json.load(f))
        if override_app_version:
            # instagrapi keeps uuids/hardware and swaps in its current app profile
            self.settings["device_settings"]["app_version"] = CURRENT_APP
            self.set_user_agent()
        return self.settings

    def set_user_agent(self, user_agent="", reset=False):
        app = self.settings["device_settings"].get("app_version", CURRENT_APP)
        self.settings["user_agent"] = f"Instagram {app} Android"
        return True

    def get_settings(self):
        return self.settings

    def dump_settings(self, path):
        with open(path, "w") as f:
            json.dump(self.settings, f)

    def set_device(self, device, reset=False):
        self.settings["device_settings"] = device

    def set_proxy(self, proxy):
        self.proxy = proxy

    def set_locale(self, value):
        self.locale = value

    def set_country(self, value):
        self.country = value

    def set_country_code(self, value):
        self.country_code = value

    def set_timezone_offset(self, value):
        self.tz = value

    # api
    def get_timeline_feed(self):
        if not self.timeline_ok:
            raise RuntimeError("login_required")
        return {"ok": True}

    def login(self, username=None, password=None, relogin=False, verification_code=""):
        self.logins.append({"relogin": relogin})
        self.settings.setdefault("cookies", {})["sessionid"] = "fresh"
        return True

    def media_edit(self, media_id, caption):
        self.edits.append((media_id, caption))

    def media_info(self, pk):
        return type("M", (), {"caption_text": self.media_caption})()


@pytest.fixture
def fake_instagrapi(monkeypatch):
    import instagrapi

    FakeClient.instances = []
    monkeypatch.setattr(instagrapi, "Client", FakeClient)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return FakeClient


def make_uploader(tmp_path, **overrides):
    config = {"username": "u", "password": "p", "session_file": str(tmp_path / "session.json"),
              "request_delay_min": 2, "request_delay_max": 5}
    config.update(overrides)
    return InstagramUploader(config)


def test_valid_session_is_reused_without_login(tmp_path, fake_instagrapi):
    session = tmp_path / "session.json"
    session.write_text(json.dumps(SAVED))
    uploader = make_uploader(tmp_path)

    client = uploader._ensure_client()
    assert client.logins == []                              # no login call
    assert client.settings["uuids"]["uuid"] == "keep-me"
    assert client.delay_range == [2, 5]                     # request pacing enabled
    assert uploader._ensure_client() is client              # cached


def test_expired_session_relogins_with_same_device(tmp_path, fake_instagrapi):
    """Regression: a fresh Client() used to give Instagram a brand new phone."""
    session = tmp_path / "session.json"
    session.write_text(json.dumps(SAVED))
    uploader = make_uploader(tmp_path)
    fake_instagrapi.instances.clear()

    original_init = fake_instagrapi.__init__

    def init(self):
        original_init(self)
        self.timeline_ok = False

    fake_instagrapi.__init__ = init
    client = uploader._ensure_client()
    fake_instagrapi.__init__ = original_init

    assert client.logins == [{"relogin": True}]
    assert client.settings["uuids"]["uuid"] == "keep-me"    # device preserved
    assert client.settings["device_settings"]["model"] == "Pixel 8 Pro"
    assert session.is_file() and (tmp_path / "session.json.bak").is_file()
    assert json.loads(session.read_text())["uuids"]["uuid"] == "keep-me"


def test_outdated_app_version_is_refreshed_and_saved(tmp_path, fake_instagrapi):
    """Regression: Instagram refuses logins from an app version it considers out of date."""
    session = tmp_path / "session.json"
    session.write_text(json.dumps(SAVED))
    uploader = make_uploader(tmp_path)

    client = uploader._ensure_client()
    assert client.settings["device_settings"]["app_version"] == CURRENT_APP
    assert client.settings["user_agent"] == f"Instagram {CURRENT_APP} Android"
    assert client.settings["uuids"]["uuid"] == "keep-me"          # same phone
    assert client.settings["device_settings"]["model"] == "Pixel 8 Pro"

    stored = json.loads(session.read_text())
    assert stored["device_settings"]["app_version"] == CURRENT_APP   # written back
    assert json.loads((tmp_path / "session.json.bak").read_text())["device_settings"]["app_version"] == OLD_APP


def test_pinned_app_version_must_be_known(tmp_path, fake_instagrapi):
    uploader = make_uploader(tmp_path, app_version="1.2.3.4.5")
    with pytest.raises(RuntimeError, match="Unknown instagram.app_version"):
        uploader._ensure_client()


def test_configured_device_and_locale_are_applied_without_session(tmp_path, fake_instagrapi):
    uploader = make_uploader(tmp_path, device={"model": "Pixel 7"}, locale="ko_KR",
                             country="KR", country_code=82, timezone_offset=32400)
    client = uploader._ensure_client()
    assert client.settings["device_settings"] == {"model": "Pixel 7"}
    assert (client.locale, client.country, client.country_code, client.tz) == ("ko_KR", "KR", 82, 32400)
    assert client.logins == [{"relogin": False}]


def test_caption_repair_attempts_are_limited(tmp_path, fake_instagrapi):
    uploader = make_uploader(tmp_path, caption_repair_attempts=1, caption_repair_delay=0)
    client = uploader._ensure_client()

    assert uploader._repair_caption(client, "123", "hello") is False   # media_info stays empty
    assert len(client.edits) == 1                                      # was 3 retries before

    client.media_caption = "hello"
    assert uploader._repair_caption(client, "123", "hello") is True

    uploader.caption_repair_attempts = 0
    client.edits.clear()
    assert uploader._repair_caption(client, "123", "hello") is False
    assert client.edits == []
