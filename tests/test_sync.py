from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import load
from mfp_connector.client import AuthExpired, UpstreamError
from mfp_connector.sync import CHUNK_DAYS, Syncer, SyncFailed

LOCAL = ZoneInfo("Asia/Tokyo")  # UTC+9, no DST


class FakeClient:
    def __init__(self, diary=None, fail=None):
        self.diary = diary or {}  # date str -> list of raw entries
        self.fail = fail or {}
        self.calls = []

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail[name]

    def diary_report(self, start, end):
        self.calls.append(("diary", start, end))
        self._maybe_fail("diary")
        days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        return [{"date": d, "food_entries": self.diary[d]} for d in days if self.diary.get(d)]

    def measurements(self):
        self.calls.append(("measurements",))
        self._maybe_fail("measurements")
        return load("measurements.json")["items"]

    def nutrient_goals(self, day):
        self.calls.append(("goals", day))
        self._maybe_fail("goals")
        return load("nutrient_goals.json")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def raw_entries():
    return {d["date"]: d["food_entries"] for d in load("diary_report.json")}


def make(conn, client, at=datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)):
    clock = Clock(at)
    return Syncer(conn, client, LOCAL, now=clock, sleep=lambda _: None), clock


def test_backfill_chunks_newest_first_and_records_run(conn):
    client = FakeClient(raw_entries())
    s, _ = make(conn, client)
    result = s.backfill(date(2025, 11, 1))
    diary_calls = [c for c in client.calls if c[0] == "diary"]
    assert diary_calls[0][2] == date(2026, 1, 3)
    assert all((end - start).days < CHUNK_DAYS for _, start, end in diary_calls)
    assert diary_calls[-1][1] == date(2025, 11, 1)
    assert result["diary_entries"] == 4 and result["weights"] == 2 and result["goals"] == 7
    run = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert run["ok"] == 1 and run["scope"] == "backfill:2025-11-01..2026-01-03"


def test_nightly_fetches_last_7_local_days(conn):
    client = FakeClient(raw_entries())
    # 23:30 UTC on 30 Jun is already 1 Jul in Tokyo
    s, _ = make(conn, client, datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc))
    s.nightly()
    assert ("diary", date(2026, 6, 25), date(2026, 7, 1)) in client.calls


def test_partial_failure_recorded_and_other_parts_still_run(conn):
    client = FakeClient(raw_entries(), fail={"measurements": UpstreamError("GET /m: HTTP 500")})
    s, _ = make(conn, client)
    with pytest.raises(SyncFailed):
        s.nightly()
    assert ("goals", date(2026, 1, 3)) in client.calls
    run = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert run["ok"] == 0 and run["error_kind"] == "upstream" and "weights" in run["error"]
    assert conn.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0] == 4  # diary part still committed


def test_auth_expiry_stops_run_early(conn):
    client = FakeClient(fail={"diary": AuthExpired("POST /r: session not accepted")})
    s, _ = make(conn, client)
    with pytest.raises(SyncFailed):
        s.nightly()
    assert [c[0] for c in client.calls] == ["diary"]
    assert conn.execute("SELECT error_kind FROM sync_runs").fetchone()[0] == "auth_expired"


def test_refresh_today_rate_limited_including_failures(conn):
    client = FakeClient(raw_entries(), fail={"diary": UpstreamError("x")})
    s, clock = make(conn, client)
    with pytest.raises(SyncFailed):
        s.refresh_today()
    clock.t += timedelta(seconds=120)
    assert s.refresh_today()["skipped"] is True
    client.fail = {}
    clock.t += timedelta(seconds=200)
    result = s.refresh_today()
    assert result["diary_entries"] == 1
    assert ("diary", date(2026, 1, 3), date(2026, 1, 3)) in client.calls


def test_refresh_today_force_bypasses_limit(conn):
    client = FakeClient(raw_entries())
    s, _ = make(conn, client)
    s.refresh_today()
    assert s.refresh_today(force=True).get("skipped") is None


def test_interrupted_run_is_closed_out(conn):
    client = FakeClient(fail={"diary": KeyboardInterrupt()})
    s, _ = make(conn, client)
    with pytest.raises(KeyboardInterrupt):
        s.nightly()
    run = conn.execute("SELECT ok, error_kind FROM sync_runs").fetchone()
    assert (run["ok"], run["error_kind"]) == (0, "interrupted")


def test_refresh_today_fetches_goals_only_when_missing(conn):
    client = FakeClient(raw_entries())
    s, clock = make(conn, client)
    s.refresh_today()
    assert any(c[0] == "goals" for c in client.calls)
    client.calls.clear()
    s.refresh_today(force=True)
    assert not any(c[0] == "goals" for c in client.calls)
