import os
import stat

import pytest

from mfp_connector import cookie
from mfp_connector.config import Settings

TOKEN = "fake-session-token-" + "A" * 200 + ".x-y_z"


@pytest.mark.parametrize("raw", [
    TOKEN,
    f"  {TOKEN}\n",
    f"{cookie.SESSION_COOKIE}={TOKEN}",
    f"Cookie: a=1; {cookie.SESSION_COOKIE}={TOKEN}; cf_clearance=zzz",
])
def test_normalise_keeps_only_session_token(raw):
    assert cookie.normalise(raw) == f"{cookie.SESSION_COOKIE}={TOKEN}"


def test_normalise_keeps_chunked_tokens():
    raw = f"{cookie.SESSION_COOKIE}.1=bbb; other=1; {cookie.SESSION_COOKIE}.0=aaa"
    assert cookie.normalise(raw) == f"{cookie.SESSION_COOKIE}.0=aaa; {cookie.SESSION_COOKIE}.1=bbb"


@pytest.mark.parametrize("raw", ["", "short", "a=1; b=2", TOKEN + " with spaces", "x" * 30000])
def test_normalise_rejects_junk(raw):
    with pytest.raises(cookie.CookieError):
        cookie.normalise(raw)


def test_override_wins_over_secret_and_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("MFP_DB", str(tmp_path / "mfp.db"))
    monkeypatch.setenv("MFP_USER", "someone")
    monkeypatch.setenv("MFP_UA", "UA")
    monkeypatch.setenv("MFP_COOKIE", "from-secret")
    monkeypatch.delenv("MFP_COOKIE_FILE", raising=False)
    monkeypatch.delenv("MFP_COOKIE_OVERRIDE", raising=False)
    assert Settings.from_env().cookie == "from-secret"
    assert cookie.info(tmp_path / "mfp.db")["source"] == "docker_secret"

    path = cookie.override_path(tmp_path / "mfp.db")
    cookie.save_override(path, "from-admin")
    assert Settings.from_env().cookie == "from-admin"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    info = cookie.info(tmp_path / "mfp.db")
    assert info["source"] == "admin_override" and info["age_days"] == 0
