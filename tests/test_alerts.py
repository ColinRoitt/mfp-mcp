from datetime import datetime, timedelta, timezone

import requests

from mfp_connector import alerts, db

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
ADMIN_URL = "https://mfp.example.com/admin/cookie"


class FakeNotifier:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send(self, title, message, *, priority="default", click=None):
        self.sent.append((title, message, click))
        return self.ok


def run(conn, scope, ok, at, kind=None):
    rid = db.start_run(conn, scope, at.isoformat(timespec="seconds"))
    db.finish_run(conn, rid, ok, error_kind=kind)


def check(conn, n, at, cookie_info=None):
    return alerts.check(conn, n, cookie_info=cookie_info, admin_url=ADMIN_URL, now=at)


def test_healthy_sends_nothing(conn):
    n = FakeNotifier()
    run(conn, "nightly", True, T0)
    assert check(conn, n, T0 + timedelta(hours=1)) == [] and n.sent == []


def test_expired_cookie_alerts_once_reminds_daily_then_recovers(conn):
    n = FakeNotifier()
    run(conn, "nightly", True, T0)
    run(conn, "today", False, T0 + timedelta(hours=1), "auth_expired")
    assert check(conn, n, T0 + timedelta(hours=1)) == ["MFP cookie expired"]
    assert ADMIN_URL in n.sent[0][1] and n.sent[0][2] == ADMIN_URL
    run(conn, "today", False, T0 + timedelta(hours=2), "auth_expired")
    assert check(conn, n, T0 + timedelta(hours=2)) == []  # no repeat within a day
    assert check(conn, n, T0 + timedelta(hours=26)) == ["MFP cookie expired"]  # daily reminder
    run(conn, "today", True, T0 + timedelta(hours=27))
    assert check(conn, n, T0 + timedelta(hours=27)) == ["MFP sync recovered"]
    assert check(conn, n, T0 + timedelta(hours=28)) == []


def test_auth_alert_suppresses_stale_duplicate(conn):
    n = FakeNotifier()
    run(conn, "nightly", False, T0, "auth_expired")
    assert check(conn, n, T0 + timedelta(days=3)) == ["MFP cookie expired"]


def test_stale_without_failures(conn):
    n = FakeNotifier()
    run(conn, "nightly", True, T0)
    assert check(conn, n, T0 + timedelta(hours=30)) == ["MFP data is stale"]


def test_backfill_failures_do_not_alert(conn):
    n = FakeNotifier()
    run(conn, "nightly", True, T0)
    run(conn, "backfill:2015-01-01..2026-10-05", False, T0 + timedelta(minutes=5), "upstream")
    assert check(conn, n, T0 + timedelta(hours=1)) == []


def test_cookie_age_warns_once_per_cookie(conn):
    n = FakeNotifier()
    run(conn, "nightly", True, T0)
    old = {"source": "admin_override", "updated_at": "2026-09-08T10:00:00+00:00", "age_days": 27.1}
    assert check(conn, n, T0, old) == ["MFP cookie expires soon"]
    run(conn, "nightly", True, T0 + timedelta(days=2))
    assert check(conn, n, T0 + timedelta(days=2), {**old, "age_days": 29.1}) == []
    fresh = {"source": "admin_override", "updated_at": T0.isoformat(), "age_days": 0}
    assert check(conn, n, T0 + timedelta(days=2), fresh) == []  # cleared silently, no "recovered"


def test_failed_delivery_is_retried_next_time(conn):
    n = FakeNotifier(ok=False)
    run(conn, "nightly", False, T0, "blocked")
    assert check(conn, n, T0) == []
    n.ok = True
    assert check(conn, n, T0 + timedelta(minutes=5)) == ["MFP sync blocked"]


class FakeSession:
    def __init__(self, status=200, exc=None):
        self.calls, self.status, self.exc = [], status, exc

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        r = requests.Response()
        r.status_code = self.status
        return r


def test_ntfy_format():
    s = FakeSession()
    assert alerts.Notifier("https://ntfy.sh/topic", session=s).send("T", "M", priority="high", click=ADMIN_URL)
    url, kw = s.calls[0]
    assert kw["data"] == b"M" and kw["headers"]["Title"] == "T" and kw["headers"]["Click"] == ADMIN_URL


def test_discord_format():
    s = FakeSession(status=204)
    assert alerts.Notifier("https://discord.com/api/webhooks/1/abc", session=s).send("T", "M")
    assert s.calls[0][1]["json"] == {"content": "**T**\nM"}


def test_delivery_errors_never_raise():
    assert not alerts.Notifier("https://ntfy.sh/x", session=FakeSession(exc=requests.ConnectionError())).send("T", "M")
    assert not alerts.Notifier("https://ntfy.sh/x", session=FakeSession(status=500)).send("T", "M")


def test_sync_hook_failure_does_not_break_sync(conn):
    from datetime import date
    from zoneinfo import ZoneInfo

    from mfp_connector.sync import Syncer
    from test_sync import FakeClient, raw_entries

    def boom(_):
        raise RuntimeError("alert backend down")
    s = Syncer(conn, FakeClient(raw_entries()), ZoneInfo("UTC"), now=lambda: T0, sleep=lambda _: None, after_run=boom)
    assert s.backfill(date(2026, 1, 1), date(2026, 1, 3))["diary_entries"] == 4
