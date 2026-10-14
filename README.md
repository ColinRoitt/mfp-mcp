# MFP → Claude connector

A vibecoded bridge from MyFitnessPal to claude.ai. A sync job copies your food diary, weight readings and daily goals into SQLite. An MCP server (streamable HTTP) exposes them as tools that a claude.ai custom connector can call. It can also search MFP's food database and add or remove diary entries, but only through a draft that you see and approve (see [Changing the diary](#changing-the-diary)).

```
MFP web API ◀── mfp-sync (nightly 03:30) ───┐
     ▲                                      ▼
     └── mfp-mcp (today refresh, ≤1/5 min) ─▶ ./data/mfp.db
                    │
claude.ai ─▶ reverse proxy (TLS) ─▶ mfp-mcp :8000   (/mcp, /admin/cookie, /healthz)
```

## Contents

- [Tools](#tools)
- [Changing the diary](#changing-the-diary)
- [Deploying](#deploying)
- [Nginx Proxy Manager](#nginx-proxy-manager)
- [claude.ai connector](#claudeai-connector)
- [Refreshing the cookie](#refreshing-the-cookie-roughly-monthly)
- [Alerts](#alerts)
- [Operations](#operations)
- [Reference](#reference)
- [Local development](#local-development)
- [Security](#security)

## Tools

| Tool | What it returns |
|---|---|
| `get_today` | Refreshes today first (at most once every 5 min), then returns today's entries by meal, totals, the day's goal and what's remaining |
| `get_diary(from_date, to_date, detail)` | Each day's status and totals; `detail="items"` adds every entry (max 31 days) |
| `get_weights(from_date, to_date)` | One reading per day, plus a trailing 7-calendar-day mean |
| `get_trend(from_date, to_date)` | OLS with Newey–West HAC SEs, Theil–Sen with 95% CI, and Mann–Kendall, in lb/day and lb/week; needs at least 8 days with readings |
| `get_coverage(from_date, to_date, partial_fraction)` | Unlogged, partial and unsynced days, plus Quick Add usage |
| `find_food(query, limit)` | Foods you've logged before, with counts, dates and average nutrients |
| `sync_status` | Last good sync, cookie health and age, data ranges and recent errors |
| `search_foods(query, limit)` | MFP food database search: `food_id`, serving sizes with per-serving kcal and macros; foods you've logged before come first |
| `draft_diary_changes(add, remove)` | Validates adds and removes against MFP and returns a numbered list and a `draft_id`. **Writes nothing.** |
| `confirm_diary_changes(draft_id)` | **The only tool that writes to MFP.** Applies a draft, then re-syncs the days it touched |

Dates are `YYYY-MM-DD` and ranges include both ends.

**Conventions the analysis skill can rely on:**
- **Units:** weight in lb; energy in kcal; protein, carbs, fat, fibre and sugar in g; sodium in mg.
- **Day status:**
  - `logged` means it has entries.
  - `unlogged` means synced but empty: missing data, not zero intake, so its totals are `null`.
  - `not_synced` means never fetched.
- **Null nutrients** mean MFP didn't record them, for example a calories-only Quick Add. They're never 0.
- **Quick Add** entries have `is_quick_add: true`. They're detected by MFP's "Quick Add" food description.
- **Partial days:** a logged day is "partial" when its kcal is below `partial_fraction` (default 0.5) × that weekday's kcal goal.
- **Freshness:** every response has a `freshness` block with the last successful sync, a `stale` flag (no good sync for 26 h) and human-readable `warnings`.
- **`get_trend`:** the series has one point per day (the mean of that day's readings), and x is days since the first reading. P-values use the normal approximation. Mann–Kendall isn't corrected for autocorrelation, so its p-value is optimistic for daily weights. The results were cross-checked against statsmodels and scipy.

## Changing the diary

Every change takes two steps, and the second needs your click:

1. Claude searches (`search_foods`) and calls `draft_diary_changes`. The server checks each item against MFP: the food must exist, the serving size must be one of that food's real sizes (Claude can't supply its own multiplier), and an entry to remove must exist in the synced diary. It then stores a draft and returns a numbered list such as `1. ADD 2026-10-12 Snacks: Banana (One), 1 × 118 g = 105 kcal [P 1g, C 27g, F 0.4g]`. Nothing has been written yet.
2. Claude shows you the list and asks. Only after you say yes does it call `confirm_diary_changes`.

**One-time setup in claude.ai:** go to Customize → Connectors → this connector, and set `confirm_diary_changes` to **Needs approval**. Never choose "Always allow". With that set, nothing reaches MFP without your click on the approval prompt, whatever Claude does. The tool instructions also tell Claude to show the list and wait, but the approval setting is what actually enforces it.

**Server-side limits:**
- At most 15 writes per rolling hour (adds and removes combined); a draft that would go over is refused whole.
- At most 15 items per draft.
- Drafts expire after 15 minutes and can be confirmed only once.
- Dates from 14 days ago to tomorrow.
- No single added item over 3,000 kcal.
- Writes are never retried, so a timeout can't add the same food twice.

Every write is recorded in the `write_log` table. To see recent ones, run `sqlite3 data/mfp.db "SELECT at, description, ok, error FROM write_log ORDER BY id DESC LIMIT 20"`.

**Search needs a fully valid session.** It stops working sooner than the nightly sync when the cookie ages. When it fails, the error says so; refresh the cookie at `/admin/cookie`.

## Deploying

You need Docker with the compose plugin, and an existing external Docker network called `proxy` that Nginx Proxy Manager is attached to.

The repo has no git remote. To copy the committed files (never secrets or data) from your dev machine to the server:

```sh
ssh root@<vps> mkdir -p mfp-mcp
git archive HEAD | ssh root@<vps> 'tar -x -C ~/mfp-mcp'
```

Then on the server:

```sh
cd ~/mfp-mcp
cp .env.example .env              # set MFP_USER (and PUID/PGID if not 1000)
mkdir -p data secrets && chmod 700 secrets

# Tokens: one for the MCP endpoint (goes into claude.ai), one for the cookie page
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/mfp_mcp_token
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/mfp_admin_token
: > secrets/mfp_alert_url         # optional: ntfy topic URL or Discord webhook, see Alerts

# MFP session: the value of the __Secure-next-auth.session-token cookie (see "Refreshing the cookie"),
# and your browser's User-Agent. One line each.
nano secrets/mfp_cookie
nano secrets/mfp_ua

chown -R 1000:1000 data secrets && chmod 600 secrets/*     # must match PUID/PGID
docker compose build

docker compose run --rm sync today --force               # 1. check MFP doesn't block this server's IP
docker compose run --rm sync backfill --from 2020-01-01  # 2. one-off history from when you started logging; safe to re-run
docker compose up -d                                     # 3. server + nightly scheduler
docker compose run --rm sync status
```

If step 1 fails with `[blocked]`, Cloudflare is challenging the server's IP. The options are switching the client to `curl_cffi`, or running `sync` somewhere else and copying `data/mfp.db` across. Instead of backfilling, you can also copy an existing `data/mfp.db` into `./data/`.

**If the parent directory is itself a git repo** (for example a home directory tracked in git), the project's own `.gitignore` keeps `secrets/`, `data/` and `.env` out of it. Check with `git -C ~ check-ignore -v mfp-mcp/secrets/mfp_cookie`.

### Updating a running deployment

```sh
# on the dev machine, after committing
git archive HEAD | ssh root@<vps> 'tar -x -C ~/mfp-mcp'
ssh root@<vps> 'cd ~/mfp-mcp && docker compose up -d --build'
```

This leaves `.env`, `secrets/` and `data/` alone, because they're not in the archive. If a release adds a new file under `secrets/`, create it before `up`; compose refuses to start if one is missing. Schema changes are applied automatically: tables are created with `IF NOT EXISTS` on startup.

## Nginx Proxy Manager

Add a proxy host:

| Field | Value |
|---|---|
| Domain Names | `mfp.example.com` |
| Scheme / Forward Hostname / Port | `http` / `mfp-mcp` / `8000` |
| Block Common Exploits | on |
| Websockets Support | not needed |
| SSL | a Let's Encrypt certificate (or an existing wildcard one), Force SSL, HTTP/2 |
| Advanced → Custom Nginx Configuration | `access_log off;` |

The `access_log off;` line keeps the MCP token out of NPM's logs when it's sent in the URL.

To check it works, run these from anywhere:

```sh
curl https://mfp.example.com/healthz              # {"ok":true}, no auth needed, no data
curl -i -X POST https://mfp.example.com/mcp       # 401
```

If your domain is proxied through Cloudflare and the connector fails while `/healthz` still works from your browser, check Cloudflare's firewall and bot events for the host, and add a rule that skips bot checks for it.

## claude.ai connector

Go to **Customize → Connectors → Add custom connector**, and choose **No sign-in** for Authentication. The token is in `secrets/mfp_mcp_token`. The server accepts it in either form:

1. **Request headers.** This option is in beta and only some accounts have it, so check whether your dialog shows a **Request headers** section.
   - URL: `https://mfp.example.com/mcp`
   - Header `authorization` with the value `Bearer <token>`. Include `Bearer `, because Claude sends the value exactly as entered. Alternatively, use `x-api-key` with the bare token.
2. **Secret in the URL.** This works on any account.
   - URL: `https://mfp.example.com/mcp/<token>`

You can't edit auth settings after adding a connector. To change the token, update the secret, run `docker compose up -d --force-recreate mcp`, then remove the connector and add it again.

## Refreshing the cookie (roughly monthly)

MFP's API only needs one cookie, `__Secure-next-auth.session-token`. It seems to last about 30 days from when your browser received it. MFP won't renew it for a server (its session endpoint rejects the token and tells the browser to delete it), so a fresh one has to come from a logged-in browser. You'll get an alert when it's 25 days old and again when it expires.

1. Log in on myfitnesspal.com in a desktop browser.
2. Open DevTools → **Storage** (Firefox) or **Application** (Chrome) → Cookies → `https://www.myfitnesspal.com`, and copy the value of `__Secure-next-auth.session-token`.
3. Open `https://mfp.example.com/admin/cookie`, and paste the value along with your admin token (`secrets/mfp_admin_token`; let your password manager remember it).

The server checks the cookie against MFP before saving it, then refreshes today. Both containers use it straight away, with no restart. A whole Cookie header also works; only the session token is kept. Or use curl:

```sh
curl -X POST https://mfp.example.com/admin/cookie -H 'content-type: application/json' \
  -d '{"admin_token": "<admin token>", "cookie": "<session token value>"}'
```

The same page shows a **status card**. It says whether the cookie currently works for syncing and for food search (search needs a fresher session), when it was saved, its estimated expiry (about 30 days), and the last successful sync. It also includes a short guide to the project. The check is cached for 10 minutes and re-runs straight after a save; "Check now" can run it at most once a minute. For scripts, `GET /admin/cookie?status` returns the same information as JSON. Neither shows the cookie or any token.

The saved cookie is stored in `data/mfp_cookie` (mode 600, with a `.json` file next to it recording when it was saved), and it takes precedence over `secrets/mfp_cookie`. After 5 wrong admin tokens in 10 minutes, the page locks for 10 minutes.

## Alerts

`secrets/mfp_alert_url` holds an ntfy topic URL (`https://ntfy.sh/<long-random-topic>`, or your own ntfy server) or a Discord webhook URL. An empty file means alerts are off. For ntfy, subscribe to the topic in the ntfy app.

| Alert | When |
|---|---|
| MFP cookie expired | the latest `today`/`nightly` run failed with `auth_expired`; the message links to `/admin/cookie` |
| MFP sync blocked | the latest run failed with a Cloudflare challenge |
| MFP data is stale | no successful sync for 26 hours (any other cause) |
| MFP cookie expires soon | the saved cookie is 25 or more days old; sent once per cookie |
| MFP sync recovered | the first good run after any of the first three |

Each problem alerts once, then reminds you at most every 24 hours. Alert state lives in the `alerts` table, so the two containers never double-send. Delivery failures are logged and retried at the next check. Messages never include diary or weight data, but anyone with the topic name can read them, so keep it unguessable.

To test alerts, run `docker compose run --rm sync alert-test`. A changed URL is picked up after `docker compose up -d --force-recreate`.

## Operations

| Task | Command (in `~/mfp-mcp`) |
|---|---|
| Status summary | `docker compose run --rm sync status` |
| Logs | `docker logs mfp-mcp` / `docker logs mfp-sync` |
| Force a refresh of today | `docker compose run --rm sync today --force` |
| Re-sync the last 7 days now | `docker compose run --rm sync nightly` |
| Re-fetch a past range | `docker compose run --rm sync backfill --from 2026-01-01 --to 2026-03-31` |
| Restart | `docker compose restart` |
| Back up | copy `data/mfp.db` (for a consistent copy while it's running: `sqlite3 data/mfp.db ".backup mfp-backup.db"`) |

**Troubleshooting**

| Symptom | Likely cause |
|---|---|
| `auth_expired` in `sync_status` / alerts | The cookie expired. Refresh it at `/admin/cookie`. |
| `blocked` | Cloudflare challenged the VPS's requests to MFP. Retry later; if it persists, see Deploying. |
| `suspicious_empty` | MFP returned no entries for a multi-day range where data exists. Nothing was deleted; it usually clears on the next run. |
| `parse` | MFP changed its response format. Compare a fresh response with `tests/fixtures/`. |
| claude.ai can't connect but `/healthz` works | A CDN or WAF (such as Cloudflare) in front of the host, a wrong token in the connector URL or header, or a `Host` mismatch (`MFP_PUBLIC_HOST`). |
| `get_today` says `skipped` | Normal: it refreshed less than 5 minutes ago. |

## Reference

### Commands

`python -m mfp_connector <command>` (in Docker: `docker compose run --rm sync <command>`):

| Command | Does |
|---|---|
| `backfill --from DATE [--to DATE]` | Fetches the diary in 31-day chunks, newest first, plus weights and goals |
| `nightly` | Re-fetches the last 7 days (past days get edited), weights and goals |
| `today [--force]` | Refreshes today and weights; limited to once per 5 min unless `--force` |
| `status` | Prints a JSON summary |
| `serve [--host] [--port]` | Runs the MCP server (the `mcp` container) |
| `scheduler [--at HH:MM]` | Runs `nightly` daily at that local time (the `sync` container) |
| `alert-test` | Sends a test notification |

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MFP_USER` | (required) | MFP username |
| `MFP_TZ` | `UTC` | IANA timezone that defines "today" and the scheduler time |
| `MFP_COUNTRY` | `US` | Region of MFP's food database used by `search_foods` |
| `MFP_DB` | `data/mfp.db` (`/data/mfp.db` in Docker) | SQLite path; the saved cookie lives next to it |
| `MFP_PUBLIC_HOST` | (required in compose) | Public hostname, e.g. `mfp.example.com`. The only `Host` accepted on `/mcp`; also used for links in alerts and on the admin page |
| `MFP_NIGHTLY_AT` | `03:30` | Scheduler time (compose) |
| `PUID` / `PGID` | `1000` | Container user; must own `data/` and `secrets/` |
| `MFP_COOKIE`, `MFP_UA`, `MFP_MCP_TOKEN`, `MFP_ADMIN_TOKEN`, `MFP_ALERT_URL` | | Secrets. Each also has a `*_FILE` form, which compose uses to point at `/run/secrets/*` |
| `MFP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*` | Read by `serve`; compose sets it from `MFP_PUBLIC_HOST` |
| `MFP_COOKIE_OVERRIDE` | `<db dir>/mfp_cookie` | Where `/admin/cookie` saves the cookie |

### MFP endpoints used (from the phase 0 spike)

| Data | Endpoint | Notes |
|---|---|---|
| Diary | `POST /api/services/diary/report` | A read-only report query. MFP returns HTTP 500 for ranges near a year, so the sync uses 31-day chunks. Entries have stable UUIDs, and their nutrients are already multiplied by the number of servings. |
| Weight | `GET /api/user-measurements/measurements` | Real readings, paged with `offset`/`limit`. (`/reports/results/progress/1/…` is forward-filled and in stone, so it isn't used.) |
| Goals | `GET /api/services/diary/nutrient_goals` | One goal per weekday |
| Food search | `GET /api/nutrition?query=…&resource_type=foods&country_code=US` | Needs a fully valid session, the NextAuth CSRF cookie (from `/api/auth/csrf`) and a `Referer` header; a stale session gets `404 {"items":[]}` |
| Food lookup | `GET /api/services/foods/{id}` | Gives the authoritative serving sizes and version for an add |
| Add entry | `POST /api/services/diary` `{items:[{type:"food_entry", date, food:{id,version}, servings, meal_position, serving_size}]}` | Write; never retried |
| Delete entry | `DELETE /api/services/diary/{entry_id}` | Write; never retried |
| Auth check | `GET /api/user-measurements/measurements?limit=1` | Used by `/admin/cookie` |

An invalid session gets `400 … Missing HTTP header: Authorization`, which the sync classifies as `auth_expired`. The printable-diary HTML page is a client-rendered shell with no diary data in it.

### Database (`data/mfp.db`)

| Table / view | Contents |
|---|---|
| `diary_entries` | One row per food entry: `entry_id`, `date`, `meal`, `food_name`, `brand`, `servings`, `serving_size`, `kcal`, `protein`, `carbs`, `fat`, `fibre`, `sodium`, `sugar`, `is_quick_add`, `logged_at`, and `nutrients_json` (every nutrient MFP returned) |
| `diary_days` | Every date fetched, with its entry count, which is how `unlogged` is told apart from `not_synced` |
| `daily_totals` (view) | Per-day sums, Quick Add count and kcal, and entries missing macros |
| `weights` | `measurement_id`, `date`, `weight_lb` |
| `goals` | The current goal per weekday |
| `sync_runs` | Every sync attempt: `scope`, `ok`, `error_kind`, `error` |
| `alerts` | Alert state |
| `drafts` | Pending and applied diary-change drafts |
| `write_log` | Every write sent to MFP, with its result |

Each sync replaces whole days in one transaction, so edits and deletions in MFP are picked up. An empty response can never wipe more than one stored day.

### Code layout

| Module | Role |
|---|---|
| `client.py` | HTTP client and error classification (`auth_expired` / `blocked` / `upstream`) |
| `parse.py` | JSON → typed records |
| `db.py` | Schema and atomic writes |
| `sync.py` | Backfill, nightly and today refresh; records each run in `sync_runs` |
| `queries.py` / `stats.py` | Tool logic and trend statistics |
| `server.py` | MCP server, auth middleware and `/admin/cookie` |
| `cookie.py` | Cookie normalising and the saved-cookie override |
| `admin_page.py` | The `/admin/cookie` page: status card, update form and guide |
| `alerts.py` | ntfy/Discord notifications |
| `writes.py` | The draft and confirm flow, write limits and audit log |
| `__main__.py` | CLI and scheduler |

`mfp_spike.py` is the original phase 0 probe, kept for reference.

## Local development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                 # no network access
# .env with MFP_COOKIE / MFP_UA / MFP_USER for running the CLI against MFP:
set -a; . ./.env; set +a
.venv/bin/python -m mfp_connector status
MFP_MCP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
  .venv/bin/python -m mfp_connector serve --host 127.0.0.1 --port 8765
```

`tests/test_real_fixtures.py` also runs against real MFP responses in `spike-out/` when that folder exists. It's git-ignored and holds personal data, so never commit it. The committed fixtures in `tests/fixtures/` are synthetic.

## Security

- Every request needs the MCP token except `/healthz` (no data) and `/admin/cookie`. The admin page requires its own admin token, sent in the POST body, and won't accept the MCP token. Tokens are compared in constant time and must be at least 32 characters.
- `/mcp` rejects any `Host` other than `MFP_PUBLIC_HOST` (DNS-rebinding protection).
- Logs redact the path token and never print headers or cookies. Uvicorn's own access log is off, and so is NPM's access log for this host.
- Containers run as non-root with a read-only root filesystem, no capabilities and `no-new-privileges`. Only `mfp-mcp` is on the `proxy` network, and no host ports are published.
- Only `confirm_diary_changes` writes to MFP. It's annotated as destructive, should be set to "Needs approval" in claude.ai, and is rate-limited and logged. Every other tool is marked read-only. Whoever holds the MCP token can draft changes, and confirm them unless your claude.ai approval setting stops it, so treat the token as write access.

## Roadmap

- A browser extension that pushes the session cookie to `/admin/cookie` automatically
- Blood pressure readings
- An in-chat confirmation card (MCP App) with an "Add" button that Claude can't press
