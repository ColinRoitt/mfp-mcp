"""Read-only queries backing the MCP tools. Every function takes a connection and
returns JSON-serialisable dicts; nothing here touches the network."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import db, stats
from .parse import date_range

NUTRIENTS = ("kcal", "protein", "carbs", "fat", "fibre", "sodium", "sugar")
MAX_ITEM_DAYS = 31
STALE_AFTER = timedelta(hours=26)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class QueryError(ValueError):
    """Bad tool arguments; the message is shown to the caller."""


def parse_range(start: str, end: str, max_days: int | None = None) -> tuple[date, date]:
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise QueryError("dates must be YYYY-MM-DD") from None
    if e < s:
        raise QueryError("'to' is before 'from'")
    if max_days and (e - s).days + 1 > max_days:
        raise QueryError(f"range too long for this request (max {max_days} days)")
    return s, e


def _r(v, nd=1):
    return None if v is None else round(v, nd)


# -- freshness -------------------------------------------------------------

def freshness(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """How current the data is. Attached to every tool response."""
    now = now or datetime.now(timezone.utc)
    good = conn.execute(
        "SELECT started_at, scope FROM sync_runs WHERE ok = 1 ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    last = conn.execute(
        "SELECT started_at, scope, ok, error_kind, error FROM sync_runs "
        "WHERE ok IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    out = {
        "last_successful_sync": good["started_at"] if good else None,
        "stale": good is None or now - datetime.fromisoformat(good["started_at"]) > STALE_AFTER,
        "last_attempt": None,
        "warnings": [],
    }
    if last:
        out["last_attempt"] = {"at": last["started_at"], "scope": last["scope"], "ok": bool(last["ok"])}
        if not last["ok"]:
            out["last_attempt"].update(error_kind=last["error_kind"], error=last["error"])
            if last["error_kind"] == "auth_expired":
                out["warnings"].append("MFP cookie has expired: data is not being updated until it is refreshed.")
            elif last["error_kind"] == "blocked":
                out["warnings"].append("MFP (Cloudflare) blocked the last sync attempt.")
            else:
                out["warnings"].append(f"Last sync attempt failed ({last['error_kind']}).")
    if out["stale"]:
        out["warnings"].append(
            "No successful sync in the last 26 hours; recent days may be missing or out of date."
            if good else "No successful sync has ever run."
        )
    return out


# -- diary -------------------------------------------------------------------

def _totals_row(r) -> dict:
    return {
        "entry_count": r["entry_count"],
        **{n: r[n] for n in NUTRIENTS},
        "quick_add_count": r["quick_add_count"],
        "quick_add_kcal": r["quick_add_kcal"],
        "entries_missing_macros": r["entries_missing_macros"],
    }


def _entry(r) -> dict:
    return {
        "entry_id": r["entry_id"],
        "meal": r["meal"],
        "food": r["food_name"],
        "brand": r["brand"],
        "servings": r["servings"],
        "serving_size": r["serving_size"],
        **{n: _r(r[n]) for n in NUTRIENTS},
        "is_quick_add": bool(r["is_quick_add"]),
        "logged_at": r["logged_at"],
    }


def diary(conn: sqlite3.Connection, start: str, end: str, detail: str = "totals") -> dict:
    if detail not in ("totals", "items"):
        raise QueryError("detail must be 'totals' or 'items'")
    s, e = parse_range(start, end, MAX_ITEM_DAYS if detail == "items" else None)
    totals = {r["date"]: r for r in conn.execute(
        "SELECT * FROM daily_totals WHERE date BETWEEN ? AND ?", (s.isoformat(), e.isoformat()))}
    items: dict[str, list] = {}
    if detail == "items":
        for r in conn.execute(
            "SELECT * FROM diary_entries WHERE date BETWEEN ? AND ? "
            "ORDER BY date, meal_position, logged_at, entry_id", (s.isoformat(), e.isoformat())
        ):
            items.setdefault(r["date"], []).append(_entry(r))
    days = []
    for d in date_range(s, e):
        t = totals.get(d)
        if t is None:
            day = {"date": d, "status": "not_synced"}
        else:
            day = {"date": d, "status": "logged" if t["entry_count"] else "unlogged",
                   "fetched_at": t["fetched_at"], **_totals_row(t)}
            if detail == "items":
                day["entries"] = items.get(d, [])
        days.append(day)
    return {"from": s.isoformat(), "to": e.isoformat(), "detail": detail, "days": days,
            "freshness": freshness(conn)}


def goal_for(conn: sqlite3.Connection, d: date) -> dict | None:
    r = conn.execute("SELECT * FROM goals WHERE day_of_week = ?", (WEEKDAYS[d.weekday()],)).fetchone()
    return None if r is None else {n: r[n] for n in NUTRIENTS}


def today(conn: sqlite3.Connection, d: date, refresh: dict | None = None) -> dict:
    day = diary(conn, d.isoformat(), d.isoformat(), "items")["days"][0]
    goal = goal_for(conn, d)
    by_meal: dict[str, list] = {}
    for item in day.get("entries", []):
        by_meal.setdefault(item["meal"], []).append(item)
    out = {
        "date": d.isoformat(),
        "status": day["status"],
        "totals": {n: day.get(n) for n in NUTRIENTS} if day["status"] != "not_synced" else None,
        "quick_add_count": day.get("quick_add_count", 0),
        "meals": by_meal,
        "goal": goal,
        "remaining": None,
        "refresh": refresh,
        "freshness": freshness(conn),
    }
    if goal:
        out["remaining"] = {
            n: _r(goal[n] - (day.get(n) or 0)) if goal[n] is not None else None for n in NUTRIENTS
        }
    return out


def coverage(conn: sqlite3.Connection, start: str, end: str, partial_fraction: float = 0.5) -> dict:
    """Unlogged, partial and unsynced days plus Quick Add usage.

    A day is "partial" when logged kcal is below ``partial_fraction`` × that
    weekday's kcal goal (needs goals synced).
    """
    s, e = parse_range(start, end)
    days = diary(conn, start, end)["days"]
    unlogged, unsynced, partial, qa_days = [], [], [], []
    logged = qa_count = 0
    qa_kcal = total_kcal = 0.0
    for day in days:
        if day["status"] == "not_synced":
            unsynced.append(day["date"])
            continue
        if day["status"] == "unlogged":
            unlogged.append(day["date"])
            continue
        logged += 1
        total_kcal += day["kcal"] or 0
        if day["quick_add_count"]:
            qa_count += day["quick_add_count"]
            qa_kcal += day["quick_add_kcal"] or 0
            qa_days.append({"date": day["date"], "count": day["quick_add_count"], "kcal": day["quick_add_kcal"]})
        goal = goal_for(conn, date.fromisoformat(day["date"]))
        if goal and goal["kcal"] and (day["kcal"] or 0) < partial_fraction * goal["kcal"]:
            partial.append({"date": day["date"], "kcal": day["kcal"], "goal_kcal": goal["kcal"]})
    n = len(days)
    return {
        "from": s.isoformat(),
        "to": e.isoformat(),
        "days": n,
        "logged_days": logged,
        "logged_fraction": round(logged / n, 3) if n else None,
        "unlogged_dates": unlogged,
        "not_synced_dates": unsynced,
        "partial_days": partial,
        "partial_rule": f"kcal < {partial_fraction} x weekday goal",
        "quick_add": {
            "entries": qa_count,
            "days": qa_days,
            "kcal": round(qa_kcal, 1),
            "share_of_logged_kcal": round(qa_kcal / total_kcal, 3) if total_kcal else None,
        },
        "freshness": freshness(conn),
    }


def find_food(conn: sqlite3.Connection, query: str, limit: int = 20) -> dict:
    q = query.strip()
    if len(q) < 2:
        raise QueryError("query must be at least 2 characters")
    limit = max(1, min(limit, 100))
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        """
        SELECT food_name, brand, serving_size, is_quick_add,
               COUNT(*) AS times_logged, MIN(date) AS first_logged, MAX(date) AS last_logged,
               AVG(servings) AS avg_servings, AVG(kcal) AS avg_kcal, AVG(protein) AS avg_protein,
               AVG(carbs) AS avg_carbs, AVG(fat) AS avg_fat
        FROM diary_entries
        WHERE food_name LIKE ? ESCAPE '\\' OR brand LIKE ? ESCAPE '\\'
        GROUP BY food_name, brand, serving_size, is_quick_add
        ORDER BY times_logged DESC, last_logged DESC
        LIMIT ?
        """,
        (like, like, limit),
    ).fetchall()
    return {
        "query": q,
        "matches": [
            {
                "food": r["food_name"], "brand": r["brand"], "serving_size": r["serving_size"],
                "is_quick_add": bool(r["is_quick_add"]), "times_logged": r["times_logged"],
                "first_logged": r["first_logged"], "last_logged": r["last_logged"],
                "avg_servings": _r(r["avg_servings"], 2),
                "avg_per_entry": {"kcal": _r(r["avg_kcal"]), "protein": _r(r["avg_protein"]),
                                  "carbs": _r(r["avg_carbs"]), "fat": _r(r["avg_fat"])},
            }
            for r in rows
        ],
    }


# -- weight ------------------------------------------------------------------

def _daily_weights(conn: sqlite3.Connection, s: date, e: date) -> list[tuple[str, float, int]]:
    """(date, mean lb, n readings) per day with readings."""
    return [
        (r[0], r[1], r[2]) for r in conn.execute(
            "SELECT date, AVG(weight_lb), COUNT(*) FROM weights WHERE date BETWEEN ? AND ? "
            "GROUP BY date ORDER BY date", (s.isoformat(), e.isoformat()))
    ]


def weights(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """Readings plus a trailing 7-calendar-day mean (over days that have a reading)."""
    s, e = parse_range(start, end)
    # include 6 days before `from` so the first rolling means use a full window
    daily = _daily_weights(conn, s - timedelta(days=6), e)
    by_date = {date.fromisoformat(d): w for d, w, _ in daily}
    readings = []
    for d, w, n in daily:
        dd = date.fromisoformat(d)
        if dd < s:
            continue
        window = [v for k, v in by_date.items() if dd - timedelta(days=6) <= k <= dd]
        readings.append({
            "date": d, "weight_lb": round(w, 2), "readings_that_day": n,
            "rolling_7d_mean_lb": round(sum(window) / len(window), 2),
            "rolling_7d_days": len(window),
        })
    return {"from": s.isoformat(), "to": e.isoformat(), "unit": "lb", "readings": readings,
            "freshness": freshness(conn)}


def trend(conn: sqlite3.Connection, start: str, end: str) -> dict:
    s, e = parse_range(start, end)
    daily = _daily_weights(conn, s, e)
    if len(daily) < 8:
        raise QueryError(f"need at least 8 days with weight readings in range, found {len(daily)}")
    d0 = date.fromisoformat(daily[0][0])
    x = np.array([(date.fromisoformat(d) - d0).days for d, _, _ in daily], dtype=float)
    y = np.array([w for _, w, _ in daily], dtype=float)
    ts = stats.theil_sen(x, y)
    ols = stats.ols_hac(x, y)
    return {
        "from": s.isoformat(),
        "to": e.isoformat(),
        "unit": "lb",
        "n_days": len(daily),
        "first_date": daily[0][0],
        "last_date": daily[-1][0],
        "span_days": int(x[-1]),
        "ols_hac": stats.weekly(ols),
        "theil_sen": stats.weekly(ts),
        "mann_kendall": stats.mann_kendall(y),
        "fitted_change_lb": {
            "ols": round(ols["slope_per_day"] * x[-1], 2),
            "theil_sen": round(ts["slope_per_day"] * x[-1], 2),
        },
        "notes": [
            "x = days since first_date; one point per day (mean of that day's readings).",
            "OLS SEs are Newey-West HAC (Bartlett kernel, lag floor(4(n/100)^(2/9)), n/(n-2) correction).",
            "P-values and CIs use the normal approximation.",
            "Mann-Kendall has no autocorrelation correction, so its p-value is optimistic for daily weights.",
        ],
        "freshness": freshness(conn),
    }


# -- status ------------------------------------------------------------------

def sync_status(conn: sqlite3.Connection, cookie_info: dict | None = None) -> dict:
    one = lambda q: conn.execute(q).fetchone()  # noqa: E731
    days = one("SELECT MIN(date), MAX(date), COUNT(*), SUM(entry_count > 0) FROM diary_days")
    w = one("SELECT MIN(date), MAX(date), COUNT(*) FROM weights")
    errors = conn.execute(
        "SELECT started_at, scope, error_kind, error FROM sync_runs WHERE ok = 0 "
        "ORDER BY started_at DESC LIMIT 10").fetchall()
    last = db.last_run(conn, ("today", "nightly"))
    fr = freshness(conn)
    if last is not None and last["ok"] == 0 and last["error_kind"] == "auth_expired":
        cookie = "expired"
    elif fr["last_successful_sync"] and not fr["stale"]:
        cookie = "ok"
    else:
        cookie = "unknown"
    return {
        "cookie_health": cookie,
        "cookie": cookie_info,
        "freshness": fr,
        "diary": {"first_date": days[0], "last_date": days[1], "days_synced": days[2],
                  "days_with_entries": days[3] or 0,
                  "entries": one("SELECT COUNT(*) FROM diary_entries")[0]},
        "weights": {"first_date": w[0], "last_date": w[1], "readings": w[2]},
        "goals_synced": one("SELECT COUNT(*) FROM goals")[0] == 7,
        "recent_errors": [dict(r) for r in errors],
    }


# -- food search -------------------------------------------------------------

def food_search(conn: sqlite3.Connection, raw_items: list, limit: int = 10) -> dict:
    """Shape MFP search results for the model, marking foods already in the user's diary."""
    from .writes import scaled_nutrients, serving_label

    history = {
        (r[0].lower(), (r[1] or "").lower()): (r[2], r[3])
        for r in conn.execute(
            "SELECT food_name, brand, COUNT(*), MAX(date) FROM diary_entries GROUP BY food_name, brand")
    }
    out = []
    for raw in raw_items:
        f = raw.get("item", raw) if isinstance(raw, dict) else None
        if not f or not f.get("id") or f.get("deleted"):
            continue
        name = (f.get("description") or "").strip()
        brand = (f.get("brand_name") or "").strip() or None
        nc = f.get("nutritional_contents") or {}
        times, last = history.get((name.lower(), (brand or "").lower()), (0, None))
        out.append({
            "food_id": str(f["id"]),
            "name": name,
            "brand": brand,
            "verified": bool(f.get("verified")),
            "times_you_logged_it": times,
            "last_logged": last,
            "servings": [
                {"serving_size_id": str(ss.get("id")), "size": serving_label(ss),
                 **scaled_nutrients(nc, float(ss.get("nutrition_multiplier") or 1), 1)}
                for ss in (f.get("serving_sizes") or [])[:8]
            ],
        })
        if len(out) >= limit:
            break
    # Foods the user already eats first, then MFP's order.
    out.sort(key=lambda x: -x["times_you_logged_it"])
    return {"results": out, "note": "Nutrients are per 1 serving of each size. Pass food_id and "
            "serving_size_id to draft_diary_changes."}
