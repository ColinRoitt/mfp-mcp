"""Settings from environment variables or Docker secrets.

A cookie saved through the admin endpoint (see cookie.py) overrides MFP_COOKIE.
Each secret can be given directly (``MFP_COOKIE``) or as a file path
(``MFP_COOKIE_FILE``, e.g. ``/run/secrets/mfp_cookie``). Secrets are excluded
from ``repr`` so they never end up in logs or tracebacks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .cookie import load_override, override_path


class ConfigError(RuntimeError):
    pass


def _secret(name: str) -> str | None:
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError as e:
            raise ConfigError(f"cannot read {name}_FILE: {e.strerror}") from None
    return os.environ.get(name) or None


@dataclass(frozen=True)
class Settings:
    username: str
    db_path: Path
    tz: ZoneInfo
    cookie: str | None = field(default=None, repr=False)
    user_agent: str | None = field(default=None, repr=False)
    alert_url: str | None = field(default=None, repr=False)
    public_host: str | None = None
    country: str = "US"  # MFP food database region for search

    @property
    def admin_url(self) -> str | None:
        return f"https://{self.public_host}/admin/cookie" if self.public_host else None

    @classmethod
    def from_env(cls, require_auth: bool = True) -> "Settings":
        db_path = Path(os.environ.get("MFP_DB", "data/mfp.db"))
        # A cookie saved via the admin endpoint wins over the env/Docker secret.
        cookie = load_override(override_path(db_path)) or _secret("MFP_COOKIE")
        user_agent = _secret("MFP_UA")
        if require_auth and not (cookie and user_agent):
            raise ConfigError("MFP_COOKIE and MFP_UA (or their *_FILE variants) must be set")
        username = os.environ.get("MFP_USER")
        if not username:
            raise ConfigError("MFP_USER must be set")
        return cls(
            username=username,
            db_path=db_path,
            tz=ZoneInfo(os.environ.get("MFP_TZ", "UTC")),
            cookie=cookie,
            user_agent=user_agent,
            alert_url=_secret("MFP_ALERT_URL"),
            public_host=os.environ.get("MFP_PUBLIC_HOST") or None,
            country=os.environ.get("MFP_COUNTRY", "US").upper(),
        )
