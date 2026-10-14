import json
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from conftest import load
from mfp_connector import cookie, db
from mfp_connector.config import ConfigError, Settings
from mfp_connector.parse import parse_diary_report
from mfp_connector.server import build_asgi

TOKEN = "t" * 40
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k in ("MFP_COOKIE", "MFP_UA", "MFP_COOKIE_FILE", "MFP_UA_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MFP_USER", "someone")
    from datetime import date
    path = tmp_path / "mfp.db"
    conn = db.connect(path)
    db.replace_diary_range(conn, date(2026, 1, 1), date(2026, 1, 3),
                           parse_diary_report(load("diary_report.json"), date(2026, 1, 1), date(2026, 1, 3)))
    conn.close()
    settings = Settings(username="someone", db_path=path, tz=ZoneInfo("UTC"))
    app = build_asgi(settings, TOKEN, ["testserver", "mfp.example.com"])
    with TestClient(app) as c:
        yield c


def rpc(method, params=None, id=1):
    return json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def call(client, path, method, params=None, headers=None):
    return client.post(path, content=rpc(method, params), headers={**HEADERS, **(headers or {})})


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("path,headers", [
    ("/mcp", {}),
    ("/mcp", {"authorization": "Bearer wrong"}),
    ("/mcp", {"authorization": TOKEN}),  # missing Bearer scheme
    ("/mcp/wrong", {}),
    ("/mcp/wrong", {"authorization": f"Bearer {TOKEN}"}),  # wrong path token not rescued by header
    ("/other", {"authorization": f"Bearer {TOKEN}"}),
])
def test_rejects_without_secret(client, path, headers):
    assert call(client, path, "tools/list", headers=headers).status_code == 401


@pytest.mark.parametrize("path,headers", [
    ("/mcp", {"authorization": f"Bearer {TOKEN}"}),
    ("/mcp", {"x-api-key": TOKEN}),
    (f"/mcp/{TOKEN}", {}),
])
def test_accepts_secret_and_lists_tools(client, path, headers):
    r = call(client, path, "tools/list", headers=headers)
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"get_today", "get_diary", "get_weights", "get_trend", "get_coverage",
                          "find_food", "sync_status", "search_foods", "draft_diary_changes",
                          "confirm_diary_changes"}
    # confirm_diary_changes is the only tool that changes MFP
    writers = {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]}
    assert writers == {"confirm_diary_changes"}
    assert tools["confirm_diary_changes"]["annotations"]["destructiveHint"] is True


def test_wrong_host_rejected(client):
    r = call(client, "/mcp", "tools/list", headers={"authorization": f"Bearer {TOKEN}", "host": "evil.example"})
    assert r.status_code == 421


def test_tool_call_returns_structured_diary(client):
    r = call(client, "/mcp", "tools/call",
             {"name": "get_diary", "arguments": {"from_date": "2026-01-01", "to_date": "2026-01-02"}},
             headers={"authorization": f"Bearer {TOKEN}"})
    result = r.json()["result"]
    assert not result.get("isError")
    days = result["structuredContent"]["days"]
    assert [d["status"] for d in days] == ["logged", "unlogged"]
    assert result["structuredContent"]["freshness"]["stale"] is True


def test_tool_argument_errors_are_tool_errors(client):
    r = call(client, "/mcp", "tools/call",
             {"name": "get_diary", "arguments": {"from_date": "2026-01-05", "to_date": "2026-01-01"}},
             headers={"authorization": f"Bearer {TOKEN}"})
    result = r.json()["result"]
    assert result["isError"] and "before" in result["content"][0]["text"]


def test_get_today_without_cookie_reports_refresh_failure(client):
    r = call(client, "/mcp", "tools/call", {"name": "get_today", "arguments": {}},
             headers={"authorization": f"Bearer {TOKEN}"})
    out = r.json()["result"]["structuredContent"]
    assert out["refresh"] == {"ok": False, "error_kind": "config", "error": out["refresh"]["error"]}


def test_short_token_refused(tmp_path):
    s = Settings(username="x", db_path=tmp_path / "x.db", tz=ZoneInfo("UTC"))
    with pytest.raises(ConfigError):
        build_asgi(s, "short", ["testserver"])


# -- admin cookie endpoint ---------------------------------------------------

ADMIN = "a" * 40
SESSION = "fake-session-token-" + "B" * 200


@pytest.fixture
def admin(tmp_path, monkeypatch):
    from mfp_connector.client import MFPClient
    monkeypatch.setenv("MFP_USER", "someone")
    monkeypatch.setenv("MFP_UA", "UA")
    monkeypatch.setenv("MFP_DB", str(tmp_path / "mfp.db"))
    for k in ("MFP_COOKIE", "MFP_COOKIE_FILE", "MFP_UA_FILE", "MFP_COOKIE_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)
    checked = []
    monkeypatch.setattr(MFPClient, "check_auth", lambda self: checked.append(self._headers["Cookie"]))
    from mfp_connector.server import App
    monkeypatch.setattr(App, "refresh_today", lambda self, force=False: {"ok": True, "skipped": False})
    # status checks call MFP; count them instead
    status_calls = []
    monkeypatch.setattr(MFPClient, "search_foods", lambda self, q, max_items=1, country_code="US": status_calls.append(q) or [])
    settings = Settings(username="someone", db_path=tmp_path / "mfp.db", tz=ZoneInfo("UTC"))
    app = build_asgi(settings, TOKEN, ["testserver"], admin_token=ADMIN)
    with TestClient(app) as c:
        c.checked = checked
        c.status_calls = status_calls
        c.tmp = tmp_path
        yield c


def test_admin_form_served_without_mcp_token(admin):
    r = admin.get("/admin/cookie")
    assert r.status_code == 200 and "admin_token" in r.text and r.headers["cache-control"] == "no-store"


def test_admin_rejects_wrong_token_and_mcp_token(admin):
    for t in ("nope", TOKEN):
        r = admin.post("/admin/cookie", json={"admin_token": t, "cookie": SESSION})
        assert r.status_code == 403
    assert admin.checked == [] and not (admin.tmp / "mfp_cookie").exists()


def test_admin_locks_after_repeated_failures(admin):
    for _ in range(5):
        admin.post("/admin/cookie", json={"admin_token": "nope", "cookie": SESSION})
    r = admin.post("/admin/cookie", json={"admin_token": ADMIN, "cookie": SESSION})
    assert r.status_code == 429


def test_admin_saves_validated_cookie(admin):
    r = admin.post("/admin/cookie", json={"admin_token": ADMIN, "cookie": f"a=1; {cookie.SESSION_COOKIE}={SESSION}"})
    body = r.json()
    assert r.status_code == 200 and body["ok"], body
    assert admin.checked == [f"{cookie.SESSION_COOKIE}={SESSION}"]
    assert (admin.tmp / "mfp_cookie").read_text().strip() == f"{cookie.SESSION_COOKIE}={SESSION}"
    assert "refresh" in body


def test_admin_form_post_returns_html(admin):
    r = admin.post("/admin/cookie", data={"admin_token": ADMIN, "cookie": SESSION}, headers={"accept": "text/html"})
    assert r.status_code == 200 and "Saved." in r.text


def test_admin_does_not_save_rejected_cookie(admin, monkeypatch):
    from mfp_connector.client import AuthExpired, MFPClient

    def reject(self):
        raise AuthExpired("x")
    monkeypatch.setattr(MFPClient, "check_auth", reject)
    r = admin.post("/admin/cookie", json={"admin_token": ADMIN, "cookie": SESSION})
    assert r.status_code == 400 and "rejected" in r.json()["error"]
    assert not (admin.tmp / "mfp_cookie").exists()


def test_admin_rejects_junk_and_oversized(admin):
    assert admin.post("/admin/cookie", json={"admin_token": ADMIN, "cookie": "a=1"}).status_code == 400
    big = {"admin_token": ADMIN, "cookie": "x" * 40000}
    assert admin.post("/admin/cookie", json=big).status_code == 413


def test_admin_disabled_without_token(client):
    assert client.get("/admin/cookie").status_code == 404


def test_admin_token_must_differ_from_mcp_token(tmp_path):
    s = Settings(username="x", db_path=tmp_path / "x.db", tz=ZoneInfo("UTC"))
    with pytest.raises(ConfigError):
        build_asgi(s, TOKEN, ["testserver"], admin_token=TOKEN)



def test_admin_page_shows_status_and_guide(admin):
    r = admin.get("/admin/cookie")
    assert "Cookie needs refreshing" in r.text  # no cookie configured in this fixture
    assert "Not set" in r.text and "confirm_diary_changes" in r.text and "Needs approval" in r.text
    assert admin.status_calls == []  # nothing to check without a cookie


def test_status_after_save_is_rechecked_and_valid(admin):
    r = admin.post("/admin/cookie", data={"admin_token": ADMIN, "cookie": SESSION}, headers={"accept": "text/html"})
    assert "Saved." in r.text and "Everything is working" in r.text
    assert admin.status_calls == ["apple"]
    st = admin.get("/admin/cookie?status").json()
    assert st["data"] == "ok" and st["search"] == "ok" and st["cookie_age_days"] == 0
    assert SESSION not in r.text and ADMIN not in r.text


def test_status_check_is_cached_and_rate_limited(admin):
    admin.post("/admin/cookie", json={"admin_token": ADMIN, "cookie": SESSION})
    admin.get("/admin/cookie?status")
    for _ in range(3):
        admin.get("/admin/cookie")
        admin.get("/admin/cookie?check=1")  # "Check now" within a minute: still cached
    assert admin.status_calls == ["apple"]  # one real check; everything after is served from the cache


def test_expired_search_shown_as_partial(admin, monkeypatch):
    from mfp_connector.client import AuthExpired, MFPClient

    def expired(self, q, max_items=1, country_code="US"):
        raise AuthExpired("x")
    monkeypatch.setattr(MFPClient, "search_foods", expired)
    r = admin.post("/admin/cookie", data={"admin_token": ADMIN, "cookie": SESSION}, headers={"accept": "text/html"})
    assert "Partly working" in r.text and "search needs a fresher cookie" in r.text
