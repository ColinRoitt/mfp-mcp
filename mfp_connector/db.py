"""SQLite storage. All replace_* functions are atomic: a failed fetch or parse
never leaves a half-updated day, and an implausibly empty response never wipes
existing data."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import astuple
from datetime import date, datetime, timezone
from pathlib import Path

from .parse import DailyGoal, DiaryEntry, WeightReading, date_range

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS diary_entries (
    entry_id       TEXT PRIMARY KEY,
    date           TEXT NOT NULL,
    meal           TEXT NOT NULL,
    meal_position  INTEGER,
    food_name      TEXT NOT NULL,
    brand          TEXT,
    servings       REAL,
    serving_size   TEXT,
    kcal           REAL,
    protein        REAL,
    carbs          REAL,
    fat            REAL,
    fibre          REAL,
    sodium         REAL,
    sugar          REAL,
    is_quick_add   INTEGER NOT NULL,
    consumed_at    TEXT,
    logged_at      TEXT,
    nutrients_json TEXT NOT NULL,
    fetched_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS diary_entries_date ON diary_entries(date);

-- One row per date we have fetched, including days with no entries, so
-- "unlogged" can be told apart from "never synced".
CREATE TABLE IF NOT EXISTS diary_days (
    date        TEXT PRIMARY KEY,
    entry_count INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weights (
    measurement_id TEXT PRIMARY KEY,
    date           TEXT NOT NULL,
    weight_lb      REAL NOT NULL,
    source         TEXT NOT NULL,
    updated_at     TEXT,
    fetched_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS weights_date ON weights(date);

CREATE TABLE IF NOT EXISTS goals (
    day_of_week TEXT PRIMARY KEY,
    valid_from  TEXT,
    valid_to    TEXT,
    kcal        REAL,
    protein     REAL,
    carbs       REAL,
    fat         REAL,
    fibre       REAL,
    sodium      REAL,
    sugar       REAL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    scope       TEXT NOT NULL,
    ok          INTEGER,            -- NULL while running
    error_kind  TEXT,
    error       TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS sync_runs_scope ON sync_runs(scope, started_at);

-- Alert state, so alerts fire on changes rather than on every run.
CREATE TABLE IF NOT EXISTS alerts (
    kind         TEXT PRIMARY KEY,
    active       INTEGER NOT NULL,
    first_at     TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    detail       TEXT
);

-- Diary write drafts (see writes.py) and an audit log of every write to MFP.
CREATE TABLE IF NOT EXISTS drafts (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status     TEXT NOT NULL,      -- pending | applying | applied | partial | failed | expired
    actions    TEXT NOT NULL       -- JSON list of validated add/remove actions
);
CREATE TABLE IF NOT EXISTS write_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    draft_id    TEXT NOT NULL,
    action      TEXT NOT NULL,     -- add | remove
    date        TEXT NOT NULL,
    entry_id    TEXT,
    food_id     TEXT,
    description TEXT NOT NULL,
    ok          INTEGER,           -- NULL while in flight
    error       TEXT
);
CREATE INDEX IF NOT EXISTS write_log_at ON write_log(at);

-- Totals are NULL (not 0) for fetched days with no entries: unlogged, not fasting.
CREATE VIEW IF NOT EXISTS daily_totals AS
SELECT
    d.date,
    d.fetched_at,
    COUNT(e.entry_id)                                        AS entry_count,
    ROUND(SUM(e.kcal), 1)                                    AS kcal,
    ROUND(SUM(e.protein), 1)                                 AS protein,
    ROUND(SUM(e.carbs), 1)                                   AS carbs,
    ROUND(SUM(e.fat), 1)                                     AS fat,
    ROUND(SUM(e.fibre), 1)                                   AS fibre,
    ROUND(SUM(e.sodium), 1)                                  AS sodium,
    ROUND(SUM(e.sugar), 1)                                   AS sugar,
    COALESCE(SUM(e.is_quick_add), 0)                         AS quick_add_count,
    ROUND(SUM(CASE WHEN e.is_quick_add THEN e.kcal END), 1)  AS quick_add_kcal,
    COALESCE(SUM(e.entry_id IS NOT NULL
        AND (e.protein IS NULL OR e.carbs IS NULL OR e.fat IS NULL)), 0)
                                                             AS entries_missing_macros
FROM diary_days d
LEFT JOIN diary_entries e ON e.date = d.date
GROUP BY d.date;
"""


class SuspiciousEmpty(RuntimeError):
    """MFP returned nothing where we hold data; refusing to wipe it."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


class _tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK (connection runs in autocommit mode)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def replace_diary_range(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    entries_by_date: dict[str, list[DiaryEntry]],
    fetched_at: str | None = None,
) -> int:
    """Replace every day in [start, end] with the fetched entries. Returns entry count."""
    fetched_at = fetched_at or utcnow()
    days = date_range(start, end)
    if set(entries_by_date) != set(days):
        raise ValueError("entries_by_date must cover exactly the requested range")
    total = sum(len(v) for v in entries_by_date.values())
    with _tx(conn):
        existing = conn.execute(
            "SELECT COUNT(*) FROM diary_entries WHERE date BETWEEN ? AND ?", (days[0], days[-1])
        ).fetchone()[0]
        if total == 0 and existing > 0 and len(days) > 1:
            raise SuspiciousEmpty(
                f"MFP returned no entries for {days[0]}..{days[-1]} but {existing} are stored"
            )
        conn.execute("DELETE FROM diary_entries WHERE date BETWEEN ? AND ?", (days[0], days[-1]))
        conn.executemany(
            "INSERT INTO diary_entries VALUES (" + ",".join("?" * 20) + ")",
            [(*astuple(e), fetched_at) for d in days for e in entries_by_date[d]],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO diary_days VALUES (?, ?, ?)",
            [(d, len(entries_by_date[d]), fetched_at) for d in days],
        )
    return total


def replace_weights(conn: sqlite3.Connection, readings: list[WeightReading], fetched_at: str | None = None) -> int:
    fetched_at = fetched_at or utcnow()
    with _tx(conn):
        existing = conn.execute("SELECT COUNT(*) FROM weights").fetchone()[0]
        if not readings and existing:
            raise SuspiciousEmpty(f"MFP returned no weights but {existing} are stored")
        conn.execute("DELETE FROM weights")
        conn.executemany(
            "INSERT INTO weights VALUES (?, ?, ?, ?, ?, ?)",
            [(*astuple(r), fetched_at) for r in readings],
        )
    return len(readings)


def replace_goals(conn: sqlite3.Connection, goals: list[DailyGoal], fetched_at: str | None = None) -> int:
    fetched_at = fetched_at or utcnow()
    with _tx(conn):
        conn.execute("DELETE FROM goals")
        conn.executemany(
            "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*astuple(g), fetched_at) for g in goals],
        )
    return len(goals)


def start_run(conn: sqlite3.Connection, scope: str, started_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (started_at, scope) VALUES (?, ?)", (started_at or utcnow(), scope)
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    ok: bool,
    *,
    error_kind: str | None = None,
    error: str | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at=?, ok=?, error_kind=?, error=?, detail=? WHERE id=?",
        (utcnow(), int(ok), error_kind, error, json.dumps(detail) if detail else None, run_id),
    )


def last_run(conn: sqlite3.Connection, scopes: tuple[str, ...], ok_only: bool = False) -> sqlite3.Row | None:
    q = f"SELECT * FROM sync_runs WHERE scope IN ({','.join('?' * len(scopes))})"
    if ok_only:
        q += " AND ok = 1"
    return conn.execute(q + " ORDER BY started_at DESC, id DESC LIMIT 1", scopes).fetchone()
