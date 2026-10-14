#!/usr/bin/env python3
"""
MFP phase 0 spike: can the server fetch diary + weight data with a browser cookie?

Reads secrets from env only (never hard-code them):
  MFP_COOKIE   full Cookie header copied from browser DevTools
  MFP_UA       the User-Agent of that same browser
  MFP_USER     MFP username

Writes raw responses to ./spike-out/ for building the parser later.
Does not write to MFP. Read-only GETs only.
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

COOKIE = os.environ.get("MFP_COOKIE")
UA = os.environ.get("MFP_UA")
USER = os.environ.get("MFP_USER")
if not COOKIE or not UA or not USER:
    sys.exit("Set MFP_COOKIE, MFP_UA and MFP_USER in the environment first.")

today = date.today()
start = today - timedelta(days=7)
TARGETS = {
    "diary": (
        f"https://www.myfitnesspal.com/reports/printable-diary/{USER}"
        f"?from={start}&to={today}&show_food_diary=1"
    ),
    "weight": (
        "https://www.myfitnesspal.com/api/services/reports/results/progress/1/365"
        "?report_name="
    ),
}
HEADERS = {"Cookie": COOKIE, "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
OUT = Path("spike-out")
OUT.mkdir(exist_ok=True)


def classify(name, status, final_url, ctype, body):
    """Best-guess verdict on what came back."""
    low = body[:5000].lower()
    if status in (403, 503) or "just a moment" in low or "cf-challenge" in low or "cf_chl" in low:
        return "BLOCKED (Cloudflare challenge)"
    if "/account/login" in final_url or "/login" in final_url:
        return "LOGGED OUT (cookie invalid/expired)"
    if status != 200:
        return f"FAIL (HTTP {status})"
    if name == "weight":
        try:
            data = json.loads(body)
        except ValueError:
            return "FAIL (not JSON)"
        n = len(data.get("data", data) if isinstance(data, dict) else data)
        return f"OK (JSON, ~{n} top-level items)"
    if name == "diary":
        hits = low.count("<tr")
        return "OK (HTML, looks like a diary)" if "food" in low and hits > 5 else "UNSURE (inspect HTML)"
    return "UNKNOWN"


def fetch_requests(url):
    import requests
    r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    return r.status_code, r.url, r.headers.get("content-type", ""), r.text


def fetch_curl_cffi(url):
    from curl_cffi import requests as creq  # pip install curl_cffi
    r = creq.get(url, headers=HEADERS, timeout=30, impersonate="chrome")
    return r.status_code, r.url, r.headers.get("content-type", ""), r.text


def run(client_name, fetcher):
    print(f"\n== client: {client_name} ==")
    all_ok = True
    for name, url in TARGETS.items():
        try:
            status, final_url, ctype, body = fetcher(url)
        except Exception as e:  # noqa: BLE001
            print(f"  {name:7} ERROR {type(e).__name__}: {e}")
            all_ok = False
            continue
        verdict = classify(name, status, final_url, ctype, body)
        ext = "json" if "json" in ctype else "html"
        (OUT / f"{client_name}-{name}.{ext}").write_text(body, encoding="utf-8")
        print(f"  {name:7} HTTP {status}  {len(body):>8} bytes  {ctype[:30]:30}  -> {verdict}")
        all_ok &= verdict.startswith("OK")
    return all_ok


if __name__ == "__main__":
    if run("requests", fetch_requests):
        print("\nPlain requests works. Simplest path confirmed.")
        sys.exit(0)
    try:
        ok = run("curl_cffi", fetch_curl_cffi)
    except ImportError:
        print("\ncurl_cffi not installed; skipping TLS-impersonation fallback.")
        ok = False
    print("\ncurl_cffi works: use it in the sync worker." if ok
          else "\nBoth failed: next step is Playwright with a persistent profile.")
    sys.exit(0 if ok else 1)
