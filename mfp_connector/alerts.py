"""Push alerts when the sync breaks (expired cookie, Cloudflare block, stale
data), when the cookie is getting old, and when things recover.

Alerts fire on state changes, with a reminder at most once a day while a
problem persists. State lives in the ``alerts`` table so the two containers
don't double-send. Messages never contain diary or weight data.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from . import db

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=26)
REMIND_EVERY = timedelta(hours=24)
COOKIE_WARN_DAYS = 25  # MFP sessions appear to last ~30 days from issue


class Notifier:
    """POSTs to an ntfy topic URL (or any plain-text webhook) or a Discord webhook."""

    def __init__(self, url: str, *, session: requests.Session | None = None, timeout: float = 10):
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.discord = "discord.com/api/webhooks/" in url or "discordapp.com/api/webhooks/" in url

    def send(self, title: str, message: str, *, priority: str = "default", click: str | None = None) -> bool:
        try:
            if self.discord:
                body = f"**{title}**\n{message}" + (f"\n{click}" if click else "")
                r = self.session.post(self.url, json={"content": body}, timeout=self.timeout)
            else:
                headers = {"Title": title, "Priority": priority, "Tags": "warning" if priority != "default" else "white_check_mark"}
                if click:
                    headers["Click"] = click
                r = self.session.post(self.url, data=message.encode(), headers=headers, timeout=self.timeout)
            if r.status_code >= 300:
                log.error("alert delivery failed: HTTP %s", r.status_code)
                return False
            return True
        except requests.RequestException as e:
            log.error("alert delivery failed: %s", type(e).__name__)
            return False


def _problems(conn: sqlite3.Connection, now: datetime, cookie_info: dict | None, admin_url: str | None) -> dict:
    fix = f" Update it at {admin_url}" if admin_url else ""
    out: dict[str, tuple[str, str, str | None]] = {}
    last = conn.execute(
        "SELECT started_at, scope, ok, error_kind FROM sync_runs WHERE ok IS NOT NULL "
        "AND scope IN ('today', 'nightly') ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if last is not None and not last["ok"]:
        if last["error_kind"] == "auth_expired":
            out["auth_expired"] = ("MFP cookie expired",
                                   "MyFitnessPal rejected the saved session, so the diary isn't syncing." + fix, None)
        elif last["error_kind"] == "blocked":
            out["blocked"] = ("MFP sync blocked", "Cloudflare challenged the last MyFitnessPal sync.", None)
    good = db.last_run(conn, ("today", "nightly"), ok_only=True)
    if not out and (good is None or now - datetime.fromisoformat(good["started_at"]) > STALE_AFTER):
        out["stale"] = ("MFP data is stale",
                        "No successful MyFitnessPal sync in over 26 hours. Check `sync_status`.", None)
    if cookie_info and (cookie_info.get("age_days") or 0) >= COOKIE_WARN_DAYS:
        out["cookie_age"] = ("MFP cookie expires soon",
                             f"The saved session is {cookie_info['age_days']:.0f} days old and will likely expire "
                             f"within a few days.{fix}", cookie_info.get("updated_at"))
    return out


def check(
    conn: sqlite3.Connection,
    notifier: Notifier,
    *,
    cookie_info: dict | None = None,
    admin_url: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Evaluate alert conditions and send what's due. Returns titles sent."""
    now = now or datetime.now(timezone.utc)
    problems = _problems(conn, now, cookie_info, admin_url)
    sent: list[str] = []
    stamp = now.isoformat(timespec="seconds")
    with db._tx(conn):
        rows = {r["kind"]: r for r in conn.execute("SELECT * FROM alerts")}
        for kind, (title, message, detail) in problems.items():
            row = rows.get(kind)
            if kind == "cookie_age":
                due = row is None or not row["active"] or row["detail"] != detail  # once per saved cookie
            else:
                due = row is None or not row["active"] or (
                    now - datetime.fromisoformat(row["last_sent_at"]) >= REMIND_EVERY)
            if due and notifier.send(title, message, priority="high", click=admin_url):
                sent.append(title)
                conn.execute(
                    "INSERT INTO alerts (kind, active, first_at, last_sent_at, detail) VALUES (?, 1, ?, ?, ?) "
                    "ON CONFLICT(kind) DO UPDATE SET active = 1, last_sent_at = excluded.last_sent_at, "
                    "detail = excluded.detail, first_at = CASE WHEN alerts.active THEN alerts.first_at "
                    "ELSE excluded.first_at END",
                    (kind, stamp, stamp, detail),
                )
        resolved = [k for k, r in rows.items() if r["active"] and k not in problems]
        for kind in resolved:
            conn.execute("UPDATE alerts SET active = 0 WHERE kind = ?", (kind,))
        if any(k != "cookie_age" for k in resolved) and notifier.send(
                "MFP sync recovered", "MyFitnessPal syncing is working again."):
            sent.append("MFP sync recovered")
    for title in sent:
        log.info("alert sent: %s", title)
    return sent


def hook(settings) -> Callable[[sqlite3.Connection], None] | None:
    """Syncer.after_run callback for these settings, or None when alerts aren't configured."""
    if not settings.alert_url:
        return None
    from . import cookie

    notifier = Notifier(settings.alert_url)

    def after_run(conn: sqlite3.Connection) -> None:
        check(conn, notifier, cookie_info=cookie.info(settings.db_path), admin_url=settings.admin_url)

    return after_run
