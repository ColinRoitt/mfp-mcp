"""Sync orchestration: backfill, nightly, and rate-limited refresh of today.

Every run is recorded in ``sync_runs`` with a classified error, so consumers can
report staleness instead of silently serving old data.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import db
from .client import MFPClient, MFPError
from .parse import ParseError, parse_diary_report, parse_goals, parse_measurements

log = logging.getLogger(__name__)

CHUNK_DAYS = 31
NIGHTLY_DAYS = 7
TODAY_MIN_INTERVAL = 300


def error_kind(e: Exception) -> str:
    if isinstance(e, MFPError):
        return e.kind
    if isinstance(e, ParseError):
        return "parse"
    if isinstance(e, db.SuspiciousEmpty):
        return "suspicious_empty"
    return "internal"


class SyncFailed(RuntimeError):
    def __init__(self, scope: str, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__(f"{scope} failed: " + "; ".join(f"[{k}] {m}" for k, m in errors))


class Syncer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: MFPClient,
        tz: ZoneInfo,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        pause: float = 2.0,
        after_run: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.conn = conn
        self.client = client
        self.tz = tz
        self.now = now
        self.sleep = sleep
        self.pause = pause
        self.after_run = after_run

    def local_today(self) -> date:
        return self.now().astimezone(self.tz).date()

    # -- building blocks -------------------------------------------------

    def _sync_diary(self, start: date, end: date) -> dict:
        """Fetch [start, end] in CHUNK_DAYS windows, newest first, committing each window."""
        days = entries = 0
        chunk_end = end
        while chunk_end >= start:
            chunk_start = max(chunk_end - timedelta(days=CHUNK_DAYS - 1), start)
            if days:
                self.sleep(self.pause)
            payload = self.client.diary_report(chunk_start, chunk_end)
            parsed = parse_diary_report(payload, chunk_start, chunk_end)
            n = db.replace_diary_range(self.conn, chunk_start, chunk_end, parsed)
            log.info("diary %s..%s: %d entries", chunk_start, chunk_end, n)
            days += len(parsed)
            entries += n
            chunk_end = chunk_start - timedelta(days=1)
        return {"diary_days": days, "diary_entries": entries}

    def _sync_weights(self) -> dict:
        n = db.replace_weights(self.conn, parse_measurements(self.client.measurements()))
        log.info("weights: %d readings", n)
        return {"weights": n}

    def _sync_goals(self) -> dict:
        n = db.replace_goals(self.conn, parse_goals(self.client.nutrient_goals(self.local_today())))
        log.info("goals: %d weekdays", n)
        return {"goals": n}

    def _run(self, scope: str, parts: list[tuple[str, Callable[[], dict]]]) -> dict:
        """Run each part, continuing past failures; record one sync_runs row."""
        run_id = db.start_run(self.conn, scope, self.now().isoformat(timespec="seconds"))
        detail: dict = {}
        errors: list[tuple[str, str]] = []
        for name, fn in parts:
            try:
                detail.update(fn())
            except Exception as e:  # noqa: BLE001 - classified and recorded below
                kind = error_kind(e)
                log.error("%s/%s failed [%s]: %s", scope, name, kind, e)
                errors.append((kind, f"{name}: {e}"))
                if kind in ("auth_expired", "blocked"):
                    break  # every later request would fail the same way
            except BaseException:
                # Ctrl-C / container stop: don't leave the run looking like it's still going.
                db.finish_run(self.conn, run_id, False, error_kind="interrupted",
                              error=f"{name}: interrupted", detail=detail)
                raise
        db.finish_run(
            self.conn,
            run_id,
            not errors,
            error_kind=errors[0][0] if errors else None,
            error="; ".join(m for _, m in errors) or None,
            detail=detail,
        )
        if self.after_run is not None:
            try:
                self.after_run(self.conn)
            except Exception:  # noqa: BLE001 - alerting must never break a sync
                log.exception("after-run hook failed")
        if errors:
            raise SyncFailed(scope, errors)
        return detail

    # -- public operations -----------------------------------------------

    def backfill(self, start: date, end: date | None = None) -> dict:
        end = end or self.local_today()
        return self._run(f"backfill:{start}..{end}", [
            ("diary", lambda: self._sync_diary(start, end)),
            ("weights", self._sync_weights),
            ("goals", self._sync_goals),
        ])

    def nightly(self) -> dict:
        today = self.local_today()
        return self._run("nightly", [
            ("diary", lambda: self._sync_diary(today - timedelta(days=NIGHTLY_DAYS - 1), today)),
            ("weights", self._sync_weights),
            ("goals", self._sync_goals),
        ])

    def refresh_today(self, min_interval: int = TODAY_MIN_INTERVAL, force: bool = False) -> dict:
        """Re-fetch today's diary and weights, at most once per ``min_interval`` seconds.

        The limit counts any attempt (failed ones too) so a dead cookie can't
        cause a request per tool call. Returns ``{"skipped": True, ...}`` when limited.
        """
        if not force:
            last = db.last_run(self.conn, ("today", "nightly"))
            if last is not None:
                age = (self.now() - datetime.fromisoformat(last["started_at"])).total_seconds()
                if age < min_interval:
                    return {"skipped": True, "last_run_id": last["id"], "seconds_since": int(age)}
        today = self.local_today()
        parts = [("diary", lambda: self._sync_diary(today, today)), ("weights", self._sync_weights)]
        if self.conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] < 7:
            parts.append(("goals", self._sync_goals))
        return self._run("today", parts)
