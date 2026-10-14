from datetime import date

import pytest

from conftest import load
from mfp_connector import db
from mfp_connector.parse import parse_diary_report, parse_measurements

JAN1, JAN3 = date(2026, 1, 1), date(2026, 1, 3)


def _store(conn, payload=None, start=JAN1, end=JAN3):
    parsed = parse_diary_report(payload if payload is not None else load("diary_report.json"), start, end)
    return db.replace_diary_range(conn, start, end, parsed)


def test_daily_totals(conn):
    assert _store(conn) == 4
    rows = {r["date"]: dict(r) for r in conn.execute("SELECT * FROM daily_totals")}
    d1 = rows["2026-01-01"]
    assert d1["entry_count"] == 3
    assert d1["kcal"] == round(225.3 + 600 + 90, 1)
    assert d1["quick_add_count"] == 1
    assert d1["quick_add_kcal"] == 600
    assert d1["entries_missing_macros"] == 1
    # fetched but empty: unlogged, so totals are NULL rather than 0
    assert rows["2026-01-02"]["entry_count"] == 0
    assert rows["2026-01-02"]["kcal"] is None


def test_refetch_replaces_day_including_deletions(conn):
    _store(conn)
    payload = load("diary_report.json")
    payload[0]["food_entries"] = payload[0]["food_entries"][:1]  # two entries deleted in MFP
    _store(conn, payload)
    ids = [r[0] for r in conn.execute("SELECT entry_id FROM diary_entries ORDER BY entry_id")]
    assert ids == ["e1", "e5"]


def test_empty_response_does_not_wipe_multi_day_range(conn):
    _store(conn)
    with pytest.raises(db.SuspiciousEmpty):
        _store(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0] == 4


def test_empty_single_day_allowed(conn):
    _store(conn)
    _store(conn, [], JAN3, JAN3)
    assert conn.execute("SELECT COUNT(*) FROM diary_entries WHERE date='2026-01-03'").fetchone()[0] == 0


def test_failed_write_rolls_back(conn):
    _store(conn)
    parsed = parse_diary_report(load("diary_report.json"), JAN1, JAN3)
    parsed["2026-01-03"] = parsed["2026-01-03"] * 2  # duplicate primary key
    with pytest.raises(Exception):
        db.replace_diary_range(conn, JAN1, JAN3, parsed)
    assert conn.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0] == 4
    assert not conn.in_transaction


def test_weights_replace_and_guard(conn):
    readings = parse_measurements(load("measurements.json")["items"])
    assert db.replace_weights(conn, readings) == 2
    assert db.replace_weights(conn, readings[:1]) == 1
    with pytest.raises(db.SuspiciousEmpty):
        db.replace_weights(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM weights").fetchone()[0] == 1


def test_sync_runs(conn):
    run = db.start_run(conn, "nightly", "2026-01-01T00:00:00+00:00")
    assert db.last_run(conn, ("nightly",), ok_only=True) is None
    db.finish_run(conn, run, True, detail={"x": 1})
    assert db.last_run(conn, ("nightly",), ok_only=True)["id"] == run
