"""The /admin/cookie page: cookie status, the update form, and a project guide.

Everything interpolated into the page goes through html.escape. The page never
shows the cookie or any token.
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

SESSION_DAYS = 30  # observed lifetime of an MFP session cookie

_LABELS = {
    "ok": ("good", "Valid"),
    "auth_expired": ("bad", "Expired"),
    "blocked": ("warn", "Blocked by Cloudflare"),
    "upstream": ("warn", "MFP error"),
    "missing": ("bad", "Not set"),
    "unknown": ("warn", "Unknown"),
}


def _when(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "never"
    t = datetime.fromisoformat(iso).astimezone(tz)
    return t.strftime("%a %d %b, %H:%M")


def _pill(kind: str, detail: str = "") -> str:
    cls, label = _LABELS.get(kind, ("warn", kind))
    extra = f' <span class="muted">{html.escape(detail)}</span>' if detail else ""
    return f'<span class="pill {cls}">{label}</span>{extra}'


def _status_block(st: dict | None, tz: ZoneInfo) -> str:
    if st is None:
        return '<p class="muted">Status unavailable.</p>'
    search_detail = "search needs a fresher cookie than syncing" if st["search"] == "auth_expired" else ""
    age = st.get("cookie_age_days")
    if age is None:
        cookie_line = "From the server's Docker secret (age unknown)"
    else:
        left = SESSION_DAYS - age
        expiry = f"likely expires in ~{left:.0f} days" if left > 0 else "probably expired by age"
        cookie_line = f"Saved {_when(st.get('cookie_saved_at'), tz)} ({age:.0f} days ago) · {expiry}"
    overall = "good" if st["data"] == "ok" and st["search"] == "ok" else \
              "bad" if st["data"] in ("auth_expired", "missing") else "warn"
    headline = {"good": "Everything is working",
                "warn": "Partly working",
                "bad": "Cookie needs refreshing"}[overall]
    err = ""
    if st.get("last_error"):
        err = f'<tr><th>Last sync error</th><td>{html.escape(st["last_error"])}</td></tr>'
    return f"""
<div class="status {overall}">
  <div class="headline"><span class="dot"></span>{headline}</div>
  <table>
    <tr><th>Diary &amp; weight sync</th><td>{_pill(st["data"])}</td></tr>
    <tr><th>Food search &amp; logging</th><td>{_pill(st["search"], search_detail)}</td></tr>
    <tr><th>Cookie</th><td>{html.escape(cookie_line)}</td></tr>
    <tr><th>Last successful sync</th><td>{html.escape(_when(st.get("last_successful_sync"), tz))}</td></tr>
    {err}
  </table>
  <form method="get" class="inline"><input type="hidden" name="check" value="1">
    <span class="muted">Checked {html.escape(_when(st.get("checked_at"), tz))}</span>
    <button type="submit" class="secondary">Check now</button></form>
</div>"""


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">
<title>MFP connector</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--accent:#2557d6;
--good:#1f7a3a;--good-bg:#e3f4e8;--warn:#8a5a00;--warn-bg:#fdf1d8;--bad:#a12222;--bad-bg:#fbe3e3}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--fg:#e8e8e3;--muted:#9b9b94;--line:#33332f;
--accent:#7aa2ff;--good:#7fd69a;--good-bg:#173323;--warn:#f0c46a;--warn-bg:#352a12;--bad:#ff8f8f;--bad-bg:#3a1717}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:46rem;margin:0 auto;padding:2rem 16px 4rem}
h1{font-size:1.6rem;margin:0 0 .25rem}h2{font-size:1.15rem;margin:2.2rem 0 .6rem}
p,li{color:var(--fg)}.muted{color:var(--muted);font-size:.9rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.1rem 1.2rem;margin-top:1rem}
.status{border-left:5px solid var(--warn)}.status.good{border-left-color:var(--good)}.status.bad{border-left-color:var(--bad)}
.status{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.2rem;margin-top:1.2rem}
.headline{font-weight:600;font-size:1.1rem;display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem}
.dot{width:.7rem;height:.7rem;border-radius:50%;background:var(--warn)}
.good .dot{background:var(--good)}.bad .dot{background:var(--bad)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:.35rem 0;vertical-align:top;border-top:1px solid var(--line)}
th{font-weight:500;color:var(--muted);width:42%;padding-right:1rem}
.pill{display:inline-block;padding:.05rem .55rem;border-radius:999px;font-size:.85rem;font-weight:600}
.pill.good{background:var(--good-bg);color:var(--good)}.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.bad{background:var(--bad-bg);color:var(--bad)}
.msg{padding:.75rem 1rem;border-radius:8px;margin-top:1rem}.msg.ok{background:var(--good-bg);color:var(--good)}
.msg.err{background:var(--bad-bg);color:var(--bad)}
label{display:block;margin-top:.9rem;font-weight:600}
input,textarea{width:100%;padding:.55rem;font:14px ui-monospace,monospace;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg)}textarea{height:7rem}
button{margin-top:1rem;padding:.55rem 1.2rem;font-size:1rem;border:0;border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}
button.secondary{margin:0 0 0 .75rem;padding:.3rem .8rem;font-size:.85rem;background:transparent;color:var(--accent);
border:1px solid var(--line)}
form.inline{display:flex;align-items:center;justify-content:flex-end;margin-top:.6rem}
code{font:13px ui-monospace,monospace;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:0 .25rem;
overflow-wrap:anywhere}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:.75rem;overflow-x:auto;font-size:13px}
ol,ul{padding-left:1.3rem}li{margin:.25rem 0}
details{margin-top:.5rem}summary{cursor:pointer;font-weight:600}
</style></head><body><main>
<h1>MFP &rarr; Claude connector</h1>
<p class="muted">Your MyFitnessPal diary and weights, available to Claude as a custom connector.</p>

{status}
{message}

<div class="card">
<h2 style="margin-top:0">Update the MFP cookie</h2>
<ol>
<li>Log in on <a href="https://www.myfitnesspal.com" rel="noreferrer">myfitnesspal.com</a> in a desktop browser.</li>
<li>Open DevTools &rarr; <b>Storage</b> (Firefox) or <b>Application</b> (Chrome) &rarr; Cookies &rarr;
<code>https://www.myfitnesspal.com</code>, and copy the value of <code>__Secure-next-auth.session-token</code>.</li>
<li>Paste it below with your admin token. It's checked against MFP before it's saved, and takes effect straight away.</li>
</ol>
<form method="post" autocomplete="off">
<label for="t">Admin token</label><input id="t" name="admin_token" type="password" autocomplete="current-password" required>
<label for="c">Session cookie</label><textarea id="c" name="cookie" required spellcheck="false"
 placeholder="eyJhbGciOi… (a whole Cookie header also works)"></textarea>
<button type="submit">Check &amp; save</button>
</form>
</div>

<h2>What it does</h2>
<p>A sync job on this server copies your MyFitnessPal food diary, weight readings and daily goals into a local
database. It does this nightly at 03:30, and also refreshes today whenever Claude asks. Claude reaches that data
through an MCP server, which you add to claude.ai as a custom connector. Claude can also search MFP's food
database and add or remove diary entries, but only after showing you the list and getting your approval.</p>

<h2>What Claude can do</h2>
<table>
<tr><th><code>get_today</code></th><td>Today's food by meal, totals, goal and what's remaining (refreshes first)</td></tr>
<tr><th><code>get_diary</code></th><td>Daily totals or every entry for a date range, with logged / unlogged / not-synced status</td></tr>
<tr><th><code>get_weights</code></th><td>Weigh-ins (lb) with a 7-day rolling mean</td></tr>
<tr><th><code>get_trend</code></th><td>Weight trend: OLS with HAC errors, Theil&ndash;Sen and Mann&ndash;Kendall, per week</td></tr>
<tr><th><code>get_coverage</code></th><td>Unlogged and partial days, and Quick Add use</td></tr>
<tr><th><code>find_food</code></th><td>Search foods you've logged before</td></tr>
<tr><th><code>search_foods</code></th><td>Search MFP's food database; foods you eat come first</td></tr>
<tr><th><code>draft_diary_changes</code></th><td>Prepares adds and removes as a numbered list. Writes nothing</td></tr>
<tr><th><code>confirm_diary_changes</code></th><td><b>The only tool that changes your diary.</b> Applies an approved draft</td></tr>
<tr><th><code>sync_status</code></th><td>Sync health, cookie age and recent errors</td></tr>
</table>
<p class="muted">Ask in plain English, for example "how am I doing today?", "what's my weight trend this
month?", or "add a banana and a cortado to breakfast".</p>

<h2>How it works</h2>
<pre>MyFitnessPal &larr;&rarr; sync &amp; MCP server (this VPS, Docker) &rarr; SQLite
                         &uarr;
claude.ai &rarr; Cloudflare &rarr; Nginx Proxy Manager (TLS) &rarr; /mcp</pre>
<ul>
<li><b>Data source:</b> MFP's own web API, signed in with your browser's session cookie. That's why it needs a
fresh cookie roughly monthly; MFP doesn't let a server renew it.</li>
<li><b>Diary changes:</b> two steps. Claude drafts a list, you approve it, and only then
<code>confirm_diary_changes</code> writes. Serving sizes come from MFP itself, so Claude can't invent amounts.</li>
<li><b>Limits:</b> at most 15 writes per hour and 15 items per draft. Drafts expire after 15 minutes and work only once.
Dates from 14 days back to tomorrow, and no single item over 3,000 kcal. Every write is logged.</li>
<li><b>Freshness:</b> every answer says when data was last synced, and warns if it's stale or the cookie has expired.</li>
</ul>

<h2>Setting it up in Claude</h2>
<ol>
<li>In claude.ai, go to <b>Customize &rarr; Connectors &rarr; Add custom connector</b>, and choose <b>No sign-in</b>.</li>
<li>Use the URL <code>https://{host}/mcp/&lt;MCP token&gt;</code>. If your dialog has <b>Request headers</b>, you can instead
use <code>https://{host}/mcp</code> with the header <code>authorization: Bearer &lt;MCP token&gt;</code>.</li>
<li>Open the connector's tool list, and set <code>confirm_diary_changes</code> to <b>Needs approval</b>. Never
choose "Always allow" for it. Your click on the approval prompt is what stops anything reaching MFP without you.</li>
<li>If tools are missing after an update, disconnect and reconnect the connector, then start a new chat.</li>
</ol>
<details><summary>Server setup (for redeploying)</summary>
<p>It runs as two containers from <code>compose.yaml</code>: <code>mfp-mcp</code> (server, on the <code>proxy</code>
network) and <code>mfp-sync</code> (nightly sync). Secrets are files in <code>secrets/</code>: the MFP cookie and
User-Agent, the MCP token, the admin token, and an optional alert URL. Data lives in <code>data/mfp.db</code>.
Full steps, operations and troubleshooting are in the project README.</p>
<pre>docker compose run --rm sync backfill --from 2020-01-01   # one-off history
docker compose up -d                                     # server + nightly sync
docker compose run --rm sync status                      # health summary</pre>
</details>

<h2>Alerts</h2>
<p>If an alert URL is set (ntfy or Discord), you'll get a push when: the cookie expires; the cookie is 25 days old;
syncing is blocked or stale; or syncing recovers. Each problem alerts once, then at most daily.</p>

<h2>Privacy &amp; security</h2>
<ul>
<li>This page never shows your cookie or tokens. The status check is cached and rate-limited.</li>
<li>Every other endpoint needs the MCP token. This page needs the separate admin token to change anything, and
locks for 10 minutes after 5 wrong attempts.</li>
<li>Your diary data stays in the database on this server. Alerts never include food or weight data.</li>
</ul>
</main></body></html>
"""


def render(status: dict | None, tz: ZoneInfo, host: str | None, message: str = "", ok: bool | None = None) -> str:
    box = "" if not message else f'<div class="msg {"ok" if ok else "err"}">{html.escape(message)}</div>'
    return (PAGE.replace("{status}", _status_block(status, tz))
                .replace("{message}", box)
                .replace("{host}", html.escape(host or "your-host")))
