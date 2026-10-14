"""MFP session cookie handling.

MFP's API authenticates with the NextAuth session token alone, so that is all we
keep. A cookie set through the admin endpoint is written next to the database
and takes precedence over the Docker secret, so both containers pick it up
without a restart.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SESSION_COOKIE = "__Secure-next-auth.session-token"
_TOKEN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")


class CookieError(ValueError):
    pass


def normalise(raw: str) -> str:
    """Accept a bare session-token value or a full Cookie header; return a Cookie header
    holding only the session token (including NextAuth's .0/.1 chunks if present)."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    if "=" in raw:
        keep = [p.strip() for p in raw.split(";") if p.strip().split("=", 1)[0].startswith(SESSION_COOKIE)]
        if not keep:
            raise CookieError(f"no {SESSION_COOKIE} cookie found in what was pasted")
        return "; ".join(sorted(keep))
    if not 100 <= len(raw) <= 20000 or not set(raw) <= _TOKEN_CHARS:
        raise CookieError("that doesn't look like a session token value")
    return f"{SESSION_COOKIE}={raw}"


def override_path(db_path: Path) -> Path:
    return Path(os.environ.get("MFP_COOKIE_OVERRIDE", Path(db_path).parent / "mfp_cookie"))


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def load_override(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def save_override(path: Path, cookie: str, source: str = "admin") -> dict:
    """Atomically write the cookie (mode 600) plus a small metadata file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source}
    for target, content in ((path, cookie + "\n"), (_meta_path(path), json.dumps(meta))):
        tmp = target.with_name(target.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)
    return meta


def info(db_path: Path, now: datetime | None = None) -> dict:
    """Where the active cookie comes from and how old it is (no cookie contents)."""
    path = override_path(db_path)
    if load_override(path) is None:
        return {"source": "docker_secret", "updated_at": None, "age_days": None}
    try:
        meta = json.loads(_meta_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    age = None
    if meta.get("updated_at"):
        now = now or datetime.now(timezone.utc)
        age = round((now - datetime.fromisoformat(meta["updated_at"])).total_seconds() / 86400, 1)
    return {"source": "admin_override", "updated_at": meta.get("updated_at"), "age_days": age}
