"""CLI: python -m mfp_connector {backfill,nightly,today,status,serve,scheduler}"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone

from . import alerts, cookie, db
from .client import MFPClient
from .config import ConfigError, Settings, _secret
from .sync import Syncer, SyncFailed


def _syncer(settings: Settings) -> Syncer:
    client = MFPClient(settings.cookie, settings.user_agent, settings.username)
    return Syncer(db.connect(settings.db_path), client, settings.tz, after_run=alerts.hook(settings))


def _status(settings: Settings) -> dict:
    conn = db.connect(settings.db_path)
    one = lambda q: conn.execute(q).fetchone()  # noqa: E731
    days = one("SELECT MIN(date), MAX(date), COUNT(*), SUM(entry_count > 0) FROM diary_days")
    w = one("SELECT MIN(date), MAX(date), COUNT(*) FROM weights")
    good = db.last_run(conn, ("today", "nightly"), ok_only=True)
    recent = conn.execute(
        "SELECT id, started_at, scope, ok, error_kind, error FROM sync_runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    return {
        "diary_days": {"first": days[0], "last": days[1], "fetched": days[2], "with_entries": days[3]},
        "diary_entries": one("SELECT COUNT(*) FROM diary_entries")[0],
        "weights": {"first": w[0], "last": w[1], "count": w[2]},
        "last_good_refresh": good["started_at"] if good else None,
        "recent_runs": [dict(r) for r in recent],
    }


def _serve(settings: Settings, host: str, port: int) -> int:
    import os

    import uvicorn

    from .server import build_asgi

    token = _secret("MFP_MCP_TOKEN")
    if not token:
        print("config error: MFP_MCP_TOKEN (or MFP_MCP_TOKEN_FILE) must be set", file=sys.stderr)
        return 2
    hosts = [h.strip() for h in os.environ.get("MFP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",") if h.strip()]
    try:
        app = build_asgi(settings, token, hosts, _secret("MFP_ADMIN_TOKEN"))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    # access_log off: uvicorn would log the path, which may carry the token.
    uvicorn.run(app, host=host, port=port, access_log=False, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def _next_run(now: datetime, at: str, tz) -> datetime:
    hh, mm = map(int, at.split(":"))
    local = now.astimezone(tz)
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= local:
        target = (local + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return target


def _scheduler(at: str) -> int:
    log = logging.getLogger("mfp_connector.scheduler")

    def nightly() -> None:
        try:
            settings = Settings.from_env()  # re-read secrets: a rotated cookie needs no restart
            syncer = _syncer(settings)
            try:
                log.info("nightly done: %s", syncer.nightly())
            finally:
                syncer.conn.close()
        except (SyncFailed, ConfigError) as e:
            log.error("%s", e)
        # Also evaluate alerts directly, so missing config or a dead scheduler loop still
        # surfaces as "stale" even when no sync run happened.
        s = Settings.from_env(require_auth=False)
        if s.alert_url:
            conn = db.connect(s.db_path)
            try:
                alerts.check(conn, alerts.Notifier(s.alert_url), cookie_info=cookie.info(s.db_path),
                             admin_url=s.admin_url)
            finally:
                conn.close()

    settings = Settings.from_env(require_auth=False)
    conn = db.connect(settings.db_path)
    last = db.last_run(conn, ("nightly",), ok_only=True)
    conn.close()
    if last is None or datetime.now(timezone.utc) - datetime.fromisoformat(last["started_at"]) > timedelta(hours=24):
        log.info("no successful nightly in 24h; running now")
        nightly()
    while True:
        target = _next_run(datetime.now(timezone.utc), at, settings.tz)
        log.info("next nightly at %s", target.isoformat())
        while (remaining := (target - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(min(remaining, 300))
        nightly()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mfp_connector")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="fetch a historical range (31-day chunks, newest first)")
    b.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    b.add_argument("--to", dest="end", type=date.fromisoformat)
    sub.add_parser("nightly", help="re-fetch the last 7 days, weights and goals")
    t = sub.add_parser("today", help="refresh today (rate-limited)")
    t.add_argument("--force", action="store_true")
    sub.add_parser("status", help="print sync status as JSON")
    sv = sub.add_parser("serve", help="run the MCP server (streamable HTTP)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sc = sub.add_parser("scheduler", help="run nightly sync every day at a fixed local time")
    sc.add_argument("--at", default="03:30", help="local time HH:MM (MFP_TZ)")
    sub.add_parser("alert-test", help="send a test notification to MFP_ALERT_URL")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # urllib3 debug logging would print request headers, i.e. the cookie.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        settings = Settings.from_env(require_auth=args.cmd not in ("status", "serve", "alert-test"))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "status":
        print(json.dumps(_status(settings), indent=2))
        return 0
    if args.cmd == "alert-test":
        if not settings.alert_url:
            print("MFP_ALERT_URL (or MFP_ALERT_URL_FILE) is not set", file=sys.stderr)
            return 2
        ok = alerts.Notifier(settings.alert_url).send(
            "MFP connector test", "Alerts are working.", click=settings.admin_url)
        print("sent" if ok else "failed (see log)")
        return 0 if ok else 1
    if args.cmd == "serve":
        return _serve(settings, args.host, args.port)
    if args.cmd == "scheduler":
        return _scheduler(args.at)
    syncer = _syncer(settings)
    try:
        if args.cmd == "backfill":
            result = syncer.backfill(args.start, args.end)
        elif args.cmd == "nightly":
            result = syncer.nightly()
        else:
            result = syncer.refresh_today(force=args.force)
    except SyncFailed as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
