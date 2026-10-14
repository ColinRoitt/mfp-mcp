"""MCP server (streamable HTTP) over the synced SQLite database.

Reads come from the local database. Diary writes go through a two-step
draft_diary_changes → confirm_diary_changes flow (see writes.py); the confirm
tool is the only one that changes MFP and should be set to "Needs approval".

Auth is a shared secret, accepted either as a header
(``Authorization: Bearer <token>`` or ``X-API-Key: <token>``) or as the last
path segment (``/mcp/<token>``) for clients that can't send custom headers.

``/admin/cookie`` (form + JSON) replaces the MFP session cookie. It has its own
admin token, sent in the request body, so the MCP token alone can't change it.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import admin_page, alerts, cookie, db, queries, writes
from .client import MFPClient, MFPError
from .config import ConfigError, Settings
from .sync import Syncer, SyncFailed

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
ADMIN_PATH = "/admin/cookie"
PUBLIC_PATHS = ("/healthz", ADMIN_PATH)  # the admin route checks its own token
MIN_TOKEN_LENGTH = 32
MAX_ADMIN_BODY = 32 * 1024
STATUS_TTL = 600          # cache the public cookie-status check for 10 minutes...
STATUS_MIN_INTERVAL = 60  # ...and let "Check now" re-run it at most once a minute
DateStr = Annotated[str, Field(description="Date, YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")]
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
READ_ONLY_REMOTE = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITES_MFP = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


class AddItem(BaseModel):
    food_id: str = Field(description="food_id from search_foods")
    serving_size_id: str | None = Field(None, description="serving_size_id from search_foods (default: the food's first size)")
    servings: float = Field(1, gt=0, le=writes.MAX_SERVINGS, description="Number of that serving size")
    meal: Literal["Breakfast", "Lunch", "Dinner", "Snacks"]
    date: str | None = Field(None, description="YYYY-MM-DD; default today. Allowed: 14 days back to tomorrow")


class RemoveItem(BaseModel):
    entry_id: str = Field(description="entry_id from get_diary(detail='items') or get_today")


INSTRUCTIONS = """\
The user's MyFitnessPal food diary and weight history, synced into a local database, plus food search and
confirmed diary edits.
Units: weight lb; energy kcal; protein, carbs, fat, fibre and sugar g; sodium mg. Days have a status: "logged", "unlogged" (synced, nothing logged) or "not_synced".
Unlogged days are missing data, not zero intake. Null nutrients mean "not recorded", not zero.
Quick Add entries (is_quick_add) are calorie/macro-only entries without a food.
Every response includes `freshness`; mention any warnings in it to the user.

Adding or removing diary entries ALWAYS takes two steps:
1. search_foods to find foods, then draft_diary_changes. This writes nothing and returns a numbered list.
2. Show that list to the user verbatim and ask them to confirm. Only after they explicitly agree, call
   confirm_diary_changes with the draft_id. Never confirm a draft the user hasn't seen and approved, and
   never confirm in the same response that created the draft. If they want changes, make a new draft.
Prefer foods the user has logged before (times_you_logged_it) and verified foods. At most 15 writes per hour.
"""


class SecretAuth:
    """ASGI middleware: require the shared secret on everything except PUBLIC_PATHS."""

    def __init__(self, app, token: str, mcp_path: str = MCP_PATH):
        self.app = app
        self._token = token.encode()
        self._prefix = mcp_path + "/"
        self._mcp_path = mcp_path

    def _match(self, candidate: str | bytes | None) -> bool:
        if not candidate:
            return False
        if isinstance(candidate, str):
            candidate = candidate.encode()
        return hmac.compare_digest(candidate, self._token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        authz = headers.get(b"authorization", b"")
        ok = (authz[:7].lower() == b"bearer " and self._match(authz[7:].strip())) or self._match(
            headers.get(b"x-api-key")
        )
        path = scope["path"]
        if path.startswith(self._prefix):
            token, _, rest = path[len(self._prefix):].partition("/")
            if not self._match(token):
                ok = False  # a wrong path token is never rescued by a header
            else:
                ok = True
                new_path = self._mcp_path + ("/" + rest if rest else "")
                scope = {**scope, "path": new_path, "raw_path": new_path.encode()}
        elif path != self._mcp_path:
            ok = False
        if not ok:
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


class AccessLog:
    """Request log that never prints the path token or headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = {}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            path = scope["path"]
            if path.startswith(MCP_PATH + "/"):
                path = MCP_PATH + "/<redacted>"
            log.info("%s %s -> %s", scope["method"], path, status.get("code", "-"))


class FailureGuard:
    """Lock the admin endpoint for a while after repeated bad tokens."""

    def __init__(self, limit: int = 5, window: float = 600, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._failures: list[float] = []
        self._lock = threading.Lock()

    def _prune(self):
        cutoff = self.clock() - self.window
        self._failures = [t for t in self._failures if t > cutoff]

    def locked(self) -> bool:
        with self._lock:
            self._prune()
            return len(self._failures) >= self.limit

    def fail(self) -> None:
        with self._lock:
            self._failures.append(self.clock())


async def _read_fields(request: Request) -> dict | None:
    if int(request.headers.get("content-length") or 0) > MAX_ADMIN_BODY:
        return None
    body = await request.body()
    if len(body) > MAX_ADMIN_BODY:
        return None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


class App:
    """Holds settings and serialises on-demand refreshes."""

    def __init__(self, settings: Settings, admin_token: str | None = None):
        self.settings = settings
        self.admin_token = admin_token
        self.admin_guard = FailureGuard()
        self._refresh_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status: dict | None = None
        self._status_at = 0.0

    def conn(self):
        return db.connect(self.settings.db_path)

    def refresh_today(self, force: bool = False) -> dict:
        """Rate-limited refresh. Never raises: failures are reported in the result."""
        with self._refresh_lock:
            try:
                # Re-read secrets each time so a rotated cookie needs no restart.
                s = Settings.from_env()
            except ConfigError as e:
                return {"ok": False, "error_kind": "config", "error": str(e)}
            conn = self.conn()
            try:
                syncer = Syncer(conn, MFPClient(s.cookie, s.user_agent, s.username), s.tz, pause=0,
                                after_run=alerts.hook(s))
                result = syncer.refresh_today(force=force)
                if result.get("skipped"):
                    return {"ok": True, "skipped": True,
                            "reason": f"refreshed {result['seconds_since']}s ago (limit: once per 5 min)"}
                return {"ok": True, "skipped": False}
            except SyncFailed as e:
                return {"ok": False, "error_kind": e.errors[0][0], "error": str(e)}
            finally:
                conn.close()

    def cookie_status(self, refresh: bool = False, force: bool = False) -> dict:
        """Is the cookie valid for syncing and for search? Cached; this is shown on a public page."""
        with self._status_lock:
            age = time.monotonic() - self._status_at
            if self._status is not None and not force and (
                    age < STATUS_MIN_INTERVAL or (age < STATUS_TTL and not refresh)):
                return self._status
            try:
                s = Settings.from_env(require_auth=False)
            except ConfigError:
                s = self.settings
            st = {"data": "missing", "search": "missing"}
            if s.cookie and s.user_agent:
                client = MFPClient(s.cookie, s.user_agent, s.username, retries=0, timeout=15)
                for key, check in (("data", client.check_auth),
                                   ("search", lambda: client.search_foods("apple", max_items=1,
                                                                          country_code=s.country))):
                    try:
                        check()
                        st[key] = "ok"
                    except MFPError as e:
                        st[key] = e.kind
            info = cookie.info(s.db_path)
            conn = self.conn()
            try:
                fr = queries.freshness(conn)
            finally:
                conn.close()
            last = fr.get("last_attempt") or {}
            st.update(
                cookie_saved_at=info["updated_at"],
                cookie_age_days=info["age_days"],
                last_successful_sync=fr["last_successful_sync"],
                last_error=f"{last['error_kind']} at {last['at']}" if last and not last.get("ok") else None,
                checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self._status, self._status_at = st, time.monotonic()
            return st

    def update_cookie(self, raw: str) -> dict:
        """Validate a pasted cookie against MFP, then persist it and refresh today."""
        try:
            new = cookie.normalise(raw)
        except cookie.CookieError as e:
            return {"ok": False, "error": str(e)}
        try:
            s = Settings.from_env(require_auth=False)
        except ConfigError as e:
            return {"ok": False, "error": str(e)}
        if not s.user_agent:
            return {"ok": False, "error": "MFP_UA is not configured on the server"}
        try:
            MFPClient(new, s.user_agent, s.username, retries=0).check_auth()
        except MFPError as e:
            msg = "MFP rejected this cookie (expired, or copied from a logged-out session)" \
                if e.kind == "auth_expired" else str(e)
            return {"ok": False, "error_kind": e.kind, "error": msg}
        meta = cookie.save_override(cookie.override_path(s.db_path), new)
        log.info("MFP cookie updated via admin endpoint")
        return {"ok": True, "updated_at": meta["updated_at"], "refresh": self.refresh_today(force=True)}

    def client(self) -> MFPClient:
        try:
            s = Settings.from_env()
        except ConfigError as e:
            raise ToolError(f"server is missing MFP credentials: {e}") from None
        return MFPClient(s.cookie, s.user_agent, s.username, retries=1)

    def confirm(self, draft_id: str) -> dict:
        """Apply a draft, then re-sync the touched days so reads reflect the change."""
        with self._refresh_lock:
            conn = self.conn()
            try:
                client = self.client()
                draft_dates = writes.draft_dates(conn, draft_id)
                before = writes.entry_ids(conn, draft_dates)
                result = writes.confirm(conn, client, draft_id)
                s = Settings.from_env()
                syncer = Syncer(conn, client, s.tz, pause=0)
                resync_errors = []
                for d in result["dates"]:
                    day = datetime.fromisoformat(d).date()
                    try:
                        syncer._sync_diary(day, day)
                    except Exception as e:  # noqa: BLE001 - report, don't fail the confirmed write
                        resync_errors.append(f"{d}: {e}")
                if not resync_errors:
                    writes.attach_new_entry_ids(conn, result, before)
                result["days_after"] = [
                    {k: v for k, v in day.items() if k in ("date", "status", "entry_count", "kcal", "protein",
                                                           "carbs", "fat", "entries")}
                    for day in queries.diary(conn, result["dates"][0], result["dates"][-1], "items")["days"]
                    if day["date"] in result["dates"]
                ]
                if resync_errors:
                    result["resync_warnings"] = resync_errors
                return result
            finally:
                conn.close()

    def local_today(self):
        return datetime.now(timezone.utc).astimezone(self.settings.tz).date()


def build_mcp(app: App) -> MCPServer:
    mcp = MCPServer(name="mfp", title="MyFitnessPal diary (read-only)", instructions=INSTRUCTIONS, version="0.2.0")

    async def run(fn, *args) -> dict:
        def work():
            conn = app.conn()
            try:
                return fn(conn, *args)
            finally:
                conn.close()
        try:
            return await anyio.to_thread.run_sync(work)
        except queries.QueryError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ_ONLY)
    async def get_today() -> dict[str, Any]:
        """Today's diary so far (by meal), totals, the day's goal and what's remaining.
        Refreshes today's data from MyFitnessPal first (at most once per 5 minutes)."""
        refresh = await anyio.to_thread.run_sync(app.refresh_today)
        return await run(queries.today, app.local_today(), refresh)

    @mcp.tool(annotations=READ_ONLY)
    async def get_diary(
        from_date: DateStr,
        to_date: DateStr,
        detail: Annotated[Literal["totals", "items"], Field(
            description="'totals' = daily totals only; 'items' = also every food entry (max 31 days)")] = "totals",
    ) -> dict[str, Any]:
        """Food diary for an inclusive date range: per-day status, nutrient totals and Quick Add counts,
        optionally with every entry."""
        return await run(queries.diary, from_date, to_date, detail)

    @mcp.tool(annotations=READ_ONLY)
    async def get_weights(from_date: DateStr, to_date: DateStr) -> dict[str, Any]:
        """Weight readings (lb) in a date range, one per day, with a trailing 7-day rolling mean."""
        return await run(queries.weights, from_date, to_date)

    @mcp.tool(annotations=READ_ONLY)
    async def get_trend(from_date: DateStr, to_date: DateStr) -> dict[str, Any]:
        """Weight trend over a date range: OLS slope with Newey-West HAC SEs, Theil-Sen slope with CI,
        and a Mann-Kendall test. Slopes in lb/day and lb/week. Needs >= 8 days with readings."""
        return await run(queries.trend, from_date, to_date)

    @mcp.tool(annotations=READ_ONLY)
    async def get_coverage(
        from_date: DateStr,
        to_date: DateStr,
        partial_fraction: Annotated[float, Field(
            ge=0, le=1, description="A logged day is 'partial' if kcal < this fraction of that day's goal")] = 0.5,
    ) -> dict[str, Any]:
        """Logging completeness for a range: unlogged, partial and unsynced days, and Quick Add usage."""
        return await run(queries.coverage, from_date, to_date, partial_fraction)

    @mcp.tool(annotations=READ_ONLY)
    async def find_food(
        query: Annotated[str, Field(min_length=2, description="Case-insensitive text to match in food name or brand")],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Search foods previously logged, with how often, when, and average nutrients per entry."""
        return await run(queries.find_food, query, limit)

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    async def search_foods(
        query: Annotated[str, Field(min_length=2, max_length=100, description="Food to search for, e.g. 'banana'")],
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> dict[str, Any]:
        """Search MyFitnessPal's food database. Returns food_id and serving sizes (with per-serving kcal and
        macros) to use with draft_diary_changes. Foods the user has logged before are listed first."""
        def work():
            raw = app.client().search_foods(query, max_items=max(limit * 2, 10), country_code=app.settings.country)
            conn = app.conn()
            try:
                return queries.food_search(conn, raw, limit)
            finally:
                conn.close()
        try:
            return await anyio.to_thread.run_sync(work)
        except MFPError as e:
            hint = f" Refresh the cookie at {app.settings.admin_url}." if e.kind == "auth_expired" and app.settings.admin_url else ""
            raise ToolError(f"MFP search failed ({e.kind}): {e}.{hint}") from None

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    async def draft_diary_changes(
        add: Annotated[list[AddItem], Field(max_length=writes.MAX_ITEMS_PER_DRAFT)] = [],  # noqa: B006
        remove: Annotated[list[RemoveItem], Field(max_length=writes.MAX_ITEMS_PER_DRAFT)] = [],  # noqa: B006
    ) -> dict[str, Any]:
        """Step 1 of 2 for changing the diary. Validates foods/servings/entries against MyFitnessPal and returns
        a numbered list of changes plus a draft_id. WRITES NOTHING. Show the list to the user and wait for their
        explicit confirmation before calling confirm_diary_changes."""
        def work():
            conn = app.conn()
            try:
                return writes.draft(conn, app.client(), [a.model_dump() for a in add],
                                    [r.model_dump() for r in remove], today=app.local_today())
            finally:
                conn.close()
        try:
            return await anyio.to_thread.run_sync(work)
        except (writes.WriteError, MFPError) as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=WRITES_MFP)
    async def confirm_diary_changes(
        draft_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}$", description="draft_id from draft_diary_changes")],
    ) -> dict[str, Any]:
        """Step 2 of 2: WRITES TO MYFITNESSPAL. Applies a draft the user has seen and explicitly approved in this
        conversation. Never call this without that approval. Each draft can be confirmed once, within 15 minutes."""
        try:
            return await anyio.to_thread.run_sync(app.confirm, draft_id)
        except writes.WriteError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=READ_ONLY)
    async def sync_status() -> dict[str, Any]:
        """Sync health: last successful sync, cookie health, data ranges and recent errors."""
        return await run(queries.sync_status, cookie.info(app.settings.db_path))

    @mcp.custom_route(ADMIN_PATH, methods=["GET", "POST"], include_in_schema=False)
    async def admin_cookie(request: Request):
        as_html = "text/html" in request.headers.get("accept", "")

        async def page(message: str = "", ok: bool | None = None, status: int = 200, *, refresh=False, force=False):
            st = await anyio.to_thread.run_sync(lambda: app.cookie_status(refresh=refresh, force=force))
            return HTMLResponse(admin_page.render(st, app.settings.tz, app.settings.public_host, message, ok),
                                status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

        async def reply(status: int, body: dict):
            if as_html:
                return await page(body.get("message") or body.get("error", ""), body.get("ok"), status,
                                  force=bool(body.get("ok")))
            return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

        if not app.admin_token:
            return PlainTextResponse("not found", status_code=404)
        if request.method == "GET":
            if "status" in request.query_params:  # JSON status for scripts/monitoring
                st = await anyio.to_thread.run_sync(app.cookie_status)
                return JSONResponse(st, headers={"Cache-Control": "no-store"})
            return await page(refresh="check" in request.query_params)
        if app.admin_guard.locked():
            return await reply(429, {"ok": False, "error": "too many failed attempts; try again later"})
        fields = await _read_fields(request)
        if fields is None:
            return await reply(413, {"ok": False, "error": "request too large"})
        given = fields.get("admin_token", "").encode()
        if not hmac.compare_digest(given, app.admin_token.encode()):
            app.admin_guard.fail()
            log.warning("admin endpoint: bad token")
            return await reply(403, {"ok": False, "error": "wrong admin token"})
        result = await anyio.to_thread.run_sync(app.update_cookie, fields.get("cookie", ""))
        if result["ok"]:
            refresh = result["refresh"]
            result["message"] = "Saved. MFP accepted the cookie" + (
                " and today's data was refreshed." if refresh.get("ok") else
                f", but the follow-up refresh failed: {refresh.get('error')}")
        return await reply(200 if result["ok"] else 400, result)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request):
        return JSONResponse({"ok": True})

    return mcp


def build_asgi(settings: Settings, token: str, allowed_hosts: list[str], admin_token: str | None = None):
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"MFP_MCP_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
    if admin_token is not None:
        if len(admin_token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"MFP_ADMIN_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
        if hmac.compare_digest(admin_token.encode(), token.encode()):
            raise ConfigError("MFP_ADMIN_TOKEN must differ from MFP_MCP_TOKEN")
    db.connect(settings.db_path).close()  # create schema up front
    mcp = build_mcp(App(settings, admin_token))
    starlette_app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=["https://claude.ai", "https://claude.com"]
            + [f"http://{h}" for h in allowed_hosts] + [f"https://{h}" for h in allowed_hosts],
        ),
    )
    return AccessLog(SecretAuth(starlette_app, token))
