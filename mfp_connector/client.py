"""Read-only HTTP client for the MyFitnessPal web API.

Authenticates with a Cookie header + User-Agent copied from a logged-in browser.
Reads are GETs plus the diary report query (a POST that only reads). The only
writes are add_food_entries and delete_entry, used by the confirmed-draft flow
in writes.py; they are never retried, so a timeout can't double-add. Errors are
classified so callers can tell "cookie expired" from "Cloudflare blocked us"
from "MFP is having a bad day".
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable

import requests

BASE_URL = "https://www.myfitnesspal.com"
MEASUREMENTS_PAGE = 200


class MFPError(Exception):
    kind = "upstream"


class AuthExpired(MFPError):
    kind = "auth_expired"


class Blocked(MFPError):
    kind = "blocked"


class UpstreamError(MFPError):
    kind = "upstream"


def _looks_like_challenge(resp: requests.Response, head: str) -> bool:
    return (
        resp.headers.get("cf-mitigated", "").lower() == "challenge"
        or "just a moment" in head
        or "cf_chl" in head
        or "challenge-platform" in head
    )


def check_response(resp: requests.Response, what: str, *, allow_empty: bool = False) -> Any:
    """Return the decoded JSON body (None for an allowed empty 2xx), or raise a classified MFPError."""
    status = resp.status_code
    ctype = resp.headers.get("content-type", "").lower()
    is_json = "json" in ctype
    head = "" if is_json else resp.text[:5000].lower()

    if not is_json and _looks_like_challenge(resp, head):
        raise Blocked(f"{what}: Cloudflare challenge (HTTP {status})")
    if 300 <= status < 400:
        location = resp.headers.get("location", "")
        if "login" in location:
            raise AuthExpired(f"{what}: redirected to login")
        raise UpstreamError(f"{what}: unexpected redirect (HTTP {status})")
    if status in (401, 403):
        raise AuthExpired(f"{what}: HTTP {status}")
    if status == 400 and is_json and "authorization" in resp.text[:1000].lower():
        # What MFP returns when the session cookie is missing or no longer valid.
        raise AuthExpired(f"{what}: session not accepted (missing Authorization)")
    if allow_empty and status in (200, 201, 202, 204) and not resp.content.strip():
        return None
    if status not in (200, 201):
        raise UpstreamError(f"{what}: HTTP {status}")
    if not is_json:
        if "/account/login" in head or "log in" in head:
            raise AuthExpired(f"{what}: got login page instead of JSON")
        raise UpstreamError(f"{what}: expected JSON, got {ctype or 'no content-type'}")
    try:
        return resp.json()
    except ValueError:
        raise UpstreamError(f"{what}: invalid JSON body") from None


class MFPClient:
    def __init__(
        self,
        cookie: str,
        user_agent: str,
        username: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 60,
        retries: int = 2,
        backoff: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.username = username
        self._session = session or requests.Session()
        self._headers = {
            "Cookie": cookie,
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
        self._csrf_cookie: str | None = None

    def _request(
        self, method: str, path: str, *, params=None, json_body=None, headers=None,
        retry: bool = True, allow_empty: bool = False,
    ) -> Any:
        what = f"{method} {path}"
        last: MFPError | None = None
        attempts = self._retries + 1 if retry else 1
        for attempt in range(attempts):
            if attempt:
                self._sleep(self._backoff * attempt)
            try:
                resp = self._session.request(
                    method,
                    BASE_URL + path,
                    headers={**self._headers, **(headers or {})},
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as e:
                last = UpstreamError(f"{what}: {type(e).__name__}")
                continue
            try:
                return check_response(resp, what, allow_empty=allow_empty)
            except UpstreamError as e:
                if resp.status_code < 500:
                    raise
                last = e
        assert last is not None
        raise last

    def diary_report(self, start: date, end: date) -> Any:
        """Food diary for an inclusive date range. MFP 500s on a full year; keep it to ~31 days."""
        return self._request(
            "POST",
            "/api/services/diary/report",
            json_body={
                "username": self.username,
                "show_food_diary": 1,
                "show_exercise_diary": 0,
                "show_food_notes": 0,
                "show_exercise_notes": 0,
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )

    def measurements(self) -> list[dict]:
        """All body measurements (weight etc.), following offset/limit pagination."""
        items: list[dict] = []
        while True:
            page = self._request(
                "GET",
                "/api/user-measurements/measurements",
                params={"offset": len(items), "limit": MEASUREMENTS_PAGE},
            )
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise UpstreamError("measurements: unexpected response shape")
            items.extend(page["items"])
            if not page.get("has_more") or not page["items"]:
                return items

    def _browser_headers(self, referer: str) -> dict:
        """NextAuth CSRF cookie + Referer, which /api/nutrition requires (seen 2026-10-12)."""
        if self._csrf_cookie is None:
            what = "GET /api/auth/csrf"
            try:
                resp = self._session.get(BASE_URL + "/api/auth/csrf", headers=self._headers,
                                         timeout=self._timeout, allow_redirects=False)
            except requests.RequestException as e:
                raise UpstreamError(f"{what}: {type(e).__name__}") from None
            check_response(resp, what)
            jar = [c for c in resp.cookies if "csrf" in c.name]
            if not jar:
                raise UpstreamError(f"{what}: no CSRF cookie returned")
            self._csrf_cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        return {"Cookie": f"{self._headers['Cookie']}; {self._csrf_cookie}", "Referer": referer}

    def search_foods(self, query: str, max_items: int = 25, country_code: str = "US") -> list[dict]:
        """MFP food database search. Needs a fully valid session; a stale one gets 404 {"items": []}."""
        path = "/api/nutrition"
        headers = self._browser_headers(f"{BASE_URL}/food/calorie-chart-nutrition-facts")
        try:
            resp = self._session.get(
                BASE_URL + path, headers={**self._headers, **headers}, timeout=self._timeout,
                allow_redirects=False,
                params={"query": query, "page": 1, "max_items": max_items,
                        "country_code": country_code, "resource_type": "foods"},
            )
        except requests.RequestException as e:
            raise UpstreamError(f"GET {path}: {type(e).__name__}") from None
        if resp.status_code == 404 and "json" in resp.headers.get("content-type", ""):
            raise AuthExpired("food search: MFP refused the session (search needs a fresh cookie)")
        body = check_response(resp, f"GET {path}")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise UpstreamError("food search: unexpected response shape")
        return body["items"]

    def food(self, food_id: str) -> dict:
        body = self._request("GET", f"/api/services/foods/{food_id}")
        if isinstance(body, dict) and isinstance(body.get("items"), list) and body["items"]:
            body = body["items"][0]
        if not isinstance(body, dict) or not body.get("id"):
            raise UpstreamError(f"food {food_id}: unexpected response shape")
        return body

    def add_food_entries(self, items: list[dict]) -> Any:
        """WRITE: add food entries. Not retried."""
        return self._request("POST", "/api/services/diary", json_body={"items": items},
                             headers=self._browser_headers(f"{BASE_URL}/food/diary"),
                             retry=False, allow_empty=True)

    def delete_entry(self, entry_id: str) -> None:
        """WRITE: delete one diary entry. Not retried."""
        self._request("DELETE", f"/api/services/diary/{entry_id}",
                      headers=self._browser_headers(f"{BASE_URL}/food/diary"),
                      retry=False, allow_empty=True)

    def check_auth(self) -> None:
        """Cheapest authenticated read; raises AuthExpired/Blocked/UpstreamError on failure."""
        page = self._request("GET", "/api/user-measurements/measurements", params={"limit": 1})
        if not isinstance(page, dict) or "items" not in page:
            raise UpstreamError("auth check: unexpected response shape")

    def nutrient_goals(self, day: date) -> Any:
        return self._request(
            "GET",
            "/api/services/diary/nutrient_goals",
            params={"date": day.isoformat(), "username": self.username},
        )
