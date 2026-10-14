"""Diary writes via draft → confirm.

``draft`` validates everything against MFP (real foods, real serving sizes,
existing entries), computes nutrients and stores a short-lived draft. Nothing is
written to MFP. ``confirm`` applies a pending draft, once, within a rolling
hourly write limit, logging every write to ``write_log``.

The confirm tool is meant to be set to "Needs approval" in claude.ai, so a human
click is required between seeing the list and anything reaching MFP.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import db
from .client import MFPClient, MFPError

MEALS = ("Breakfast", "Lunch", "Dinner", "Snacks")
MAX_WRITES_PER_HOUR = 15
MAX_ITEMS_PER_DRAFT = 15
MAX_SERVINGS = 2000      # gram-based sizes need large counts (e.g. 30 × 1 g)
MAX_ITEM_KCAL = 3000     # sanity cap per added item
DRAFT_TTL = timedelta(minutes=15)
PAST_DAYS = 14  # writes allowed from today-14 to today+1
NUTRIENT_KEYS = {"kcal": None, "protein": "protein", "carbs": "carbohydrates", "fat": "fat",
                 "fibre": "fiber", "sodium": "sodium", "sugar": "sugar"}

_FOOD_ID = re.compile(r"^\d{1,25}$")
_ENTRY_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")


class WriteError(ValueError):
    """Rejected request; the message is shown to the caller."""


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def meal_position(meal: str) -> int:
    for i, name in enumerate(MEALS):
        if meal.strip().lower() == name.lower():
            return i
    raise WriteError(f"meal must be one of {', '.join(MEALS)}")


def _check_date(d: str | None, today: date) -> str:
    if d is None:
        return today.isoformat()
    try:
        day = date.fromisoformat(d)
    except ValueError:
        raise WriteError(f"bad date {d!r}; use YYYY-MM-DD") from None
    if not today - timedelta(days=PAST_DAYS) <= day <= today + timedelta(days=1):
        raise WriteError(f"date {d} is outside the allowed window ({PAST_DAYS} days back to tomorrow)")
    return day.isoformat()


def _kcal(nc: dict) -> float | None:
    e = nc.get("energy")
    if isinstance(e, dict) and e.get("value") is not None:
        v = float(e["value"])
        return v / 4.184 if str(e.get("unit", "")).startswith("kilojoule") else v
    return None


def scaled_nutrients(nc: dict, multiplier: float, servings: float) -> dict:
    out = {}
    for key, src in NUTRIENT_KEYS.items():
        v = _kcal(nc) if key == "kcal" else nc.get(src)
        out[key] = None if v is None else round(float(v) * multiplier * servings, 1)
    return out


def serving_label(ss: dict) -> str:
    return f"{float(ss.get('value') or 0):g} {ss.get('unit', '')}".strip()


def _fmt_add(a: dict) -> str:
    n = a["nutrients"]
    macros = ", ".join(f"{k[0].upper()} {n[k]:g}g" for k in ("protein", "carbs", "fat") if n[k] is not None)
    qty = f"{a['servings']:g} × {a['serving_label']}"
    brand = f" ({a['brand']})" if a.get("brand") else ""
    return f"ADD {a['date']} {a['meal']}: {a['food']}{brand}, {qty} = {n['kcal']:g} kcal" + (f" [{macros}]" if macros else "")


def _fmt_remove(r: dict) -> str:
    kcal = f" ({r['kcal']:g} kcal)" if r.get("kcal") is not None else ""
    return f"REMOVE {r['date']} {r['meal']}: {r['food']}{kcal}"


def draft(
    conn: sqlite3.Connection,
    client: MFPClient,
    adds: list[dict],
    removes: list[dict],
    *,
    today: date,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    if not adds and not removes:
        raise WriteError("nothing to change")
    if len(adds) + len(removes) > MAX_ITEMS_PER_DRAFT:
        raise WriteError(f"at most {MAX_ITEMS_PER_DRAFT} changes per draft")

    actions: list[dict] = []
    foods: dict[str, dict] = {}
    for a in adds:
        food_id = str(a.get("food_id", "")).strip()
        if not _FOOD_ID.match(food_id):
            raise WriteError(f"bad food_id {food_id!r}")
        servings = float(a.get("servings", 1))
        if not 0 < servings <= MAX_SERVINGS:
            raise WriteError(f"servings must be between 0 and {MAX_SERVINGS}")
        meal = MEALS[meal_position(str(a.get("meal", "")))]
        day = _check_date(a.get("date"), today)
        if food_id not in foods:
            foods[food_id] = client.food(food_id)
        food = foods[food_id]
        if food.get("deleted"):
            raise WriteError(f"food {food_id} has been deleted in MFP")
        sizes = food.get("serving_sizes") or []
        want = a.get("serving_size_id")
        ss = next((s for s in sizes if str(s.get("id")) == str(want)), None) if want else (sizes[0] if sizes else None)
        if ss is None:
            options = "; ".join(f"{s.get('id')} = {serving_label(s)}" for s in sizes[:12])
            raise WriteError(f"serving_size_id {want!r} isn't one of this food's sizes: {options}")
        nutrients = scaled_nutrients(food.get("nutritional_contents") or {},
                                     float(ss.get("nutrition_multiplier") or 1), servings)
        if (nutrients["kcal"] or 0) > MAX_ITEM_KCAL:
            raise WriteError(f"{servings:g} × {serving_label(ss)} of {food.get('description')!r} is "
                             f"{nutrients['kcal']:g} kcal, over the {MAX_ITEM_KCAL} kcal per-item limit; check the amount")
        actions.append({
            "action": "add",
            "date": day,
            "meal": meal,
            "meal_position": meal_position(meal),
            "food_id": str(food["id"]),
            "food_version": str(food.get("version") or food["id"]),
            "food": (food.get("description") or "").strip(),
            "brand": (food.get("brand_name") or "").strip() or None,
            "servings": servings,
            "serving_size": {k: ss.get(k) for k in ("value", "unit", "nutrition_multiplier")},
            "serving_label": serving_label(ss),
            "nutrients": nutrients,
        })
    for r in removes:
        entry_id = str(r.get("entry_id", "")).strip()
        if not _ENTRY_ID.match(entry_id):
            raise WriteError(f"bad entry_id {entry_id!r}")
        row = conn.execute("SELECT date, meal, food_name, kcal FROM diary_entries WHERE entry_id = ?",
                           (entry_id,)).fetchone()
        if row is None:
            raise WriteError(f"entry {entry_id} isn't in the synced diary (call get_diary with detail='items')")
        _check_date(row["date"], today)
        if any(x["action"] == "remove" and x["entry_id"] == entry_id for x in actions):
            raise WriteError(f"entry {entry_id} listed twice")
        actions.append({"action": "remove", "entry_id": entry_id, "date": row["date"], "meal": row["meal"],
                        "food": row["food_name"], "kcal": row["kcal"]})

    draft_id = secrets.token_hex(4)
    expires = now + DRAFT_TTL
    conn.execute("INSERT INTO drafts (id, created_at, expires_at, status, actions) VALUES (?, ?, ?, 'pending', ?)",
                 (draft_id, _iso(now), _iso(expires), json.dumps(actions)))

    by_date: dict[str, float] = {}
    for x in actions:
        delta = (x["nutrients"]["kcal"] or 0) if x["action"] == "add" else -(x.get("kcal") or 0)
        by_date[x["date"]] = round(by_date.get(x["date"], 0) + delta, 1)
    return {
        "draft_id": draft_id,
        "expires_at": _iso(expires),
        "changes": [f"{i}. " + (_fmt_add(x) if x["action"] == "add" else _fmt_remove(x))
                    for i, x in enumerate(actions, 1)],
        "kcal_change_by_date": by_date,
        "writes_left_this_hour": MAX_WRITES_PER_HOUR - writes_in_last_hour(conn, now),
        "next_step": "Show the numbered list to the user exactly as above and ask them to confirm. "
                     "Only call confirm_diary_changes after they explicitly say yes; if they want changes, "
                     "make a new draft.",
    }


def writes_in_last_hour(conn: sqlite3.Connection, now: datetime) -> int:
    return conn.execute("SELECT COUNT(*) FROM write_log WHERE at > ?",
                        (_iso(now - timedelta(hours=1)),)).fetchone()[0]


def confirm(conn: sqlite3.Connection, client: MFPClient, draft_id: str, *, now: datetime | None = None) -> dict:
    """Apply a pending draft. Returns per-item results and the dates touched."""
    now = now or datetime.now(timezone.utc)
    with db._tx(conn):
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            raise WriteError("unknown draft_id")
        if row["status"] != "pending":
            raise WriteError(f"draft already {row['status']}; make a new draft")
        if datetime.fromisoformat(row["expires_at"]) < now:
            conn.execute("UPDATE drafts SET status = 'expired' WHERE id = ?", (draft_id,))
            raise WriteError("draft expired; make a new draft and show it to the user again")
        actions = json.loads(row["actions"])
        used = writes_in_last_hour(conn, now)
        if used + len(actions) > MAX_WRITES_PER_HOUR:
            raise WriteError(f"hourly write limit: {used} of {MAX_WRITES_PER_HOUR} used, "
                             f"this draft needs {len(actions)}; try again later")
        conn.execute("UPDATE drafts SET status = 'applying' WHERE id = ?", (draft_id,))
        # Reserve the writes up front so a concurrent confirm can't exceed the limit.
        for x in actions:
            conn.execute(
                "INSERT INTO write_log (at, draft_id, action, date, entry_id, food_id, description, ok) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (_iso(now), draft_id, x["action"], x["date"], x.get("entry_id"), x.get("food_id"),
                 _fmt_add(x) if x["action"] == "add" else _fmt_remove(x)),
            )
    log_ids = [r[0] for r in conn.execute("SELECT id FROM write_log WHERE draft_id = ? ORDER BY id", (draft_id,))]

    results = []
    for log_id, x in zip(log_ids, actions):
        try:
            if x["action"] == "add":
                resp = client.add_food_entries([{
                    "type": "food_entry",
                    "date": x["date"],
                    "food": {"id": x["food_id"], "version": x["food_version"]},
                    "servings": x["servings"],
                    "meal_position": x["meal_position"],
                    "serving_size": x["serving_size"],
                }])
                new_id = None
                if isinstance(resp, dict) and isinstance(resp.get("items"), list) and resp["items"]:
                    new_id = resp["items"][0].get("id")
                conn.execute("UPDATE write_log SET ok = 1, entry_id = ? WHERE id = ?", (new_id, log_id))
                results.append({"ok": True, "change": _fmt_add(x), "entry_id": new_id})
            else:
                client.delete_entry(x["entry_id"])
                conn.execute("UPDATE write_log SET ok = 1 WHERE id = ?", (log_id,))
                results.append({"ok": True, "change": _fmt_remove(x)})
        except MFPError as e:
            conn.execute("UPDATE write_log SET ok = 0, error = ? WHERE id = ?", (f"[{e.kind}] {e}", log_id))
            results.append({"ok": False, "change": _fmt_add(x) if x["action"] == "add" else _fmt_remove(x),
                            "error": str(e), "error_kind": e.kind})
            if e.kind in ("auth_expired", "blocked"):
                for rest_id, rest in list(zip(log_ids, actions))[len(results):]:
                    conn.execute("UPDATE write_log SET ok = 0, error = 'skipped' WHERE id = ?", (rest_id,))
                    results.append({"ok": False, "change": _fmt_add(rest) if rest["action"] == "add"
                                    else _fmt_remove(rest), "error": "skipped after earlier failure"})
                break
    status = "applied" if all(r["ok"] for r in results) else "partial" if any(r["ok"] for r in results) else "failed"
    conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
    return {"draft_id": draft_id, "status": status, "results": results,
            "dates": sorted({x["date"] for x in actions})}


def draft_dates(conn: sqlite3.Connection, draft_id: str) -> list[str]:
    row = conn.execute("SELECT actions FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return sorted({x["date"] for x in json.loads(row["actions"])}) if row else []


def entry_ids(conn: sqlite3.Connection, dates: list[str]) -> set[str]:
    if not dates:
        return set()
    q = f"SELECT entry_id FROM diary_entries WHERE date IN ({','.join('?' * len(dates))})"
    return {r[0] for r in conn.execute(q, dates)}


def attach_new_entry_ids(conn: sqlite3.Connection, result: dict, before: set[str]) -> None:
    """MFP's add response doesn't carry the new entry's id; find it by diffing the re-synced days."""
    new = [dict(r) for r in conn.execute(
        f"SELECT entry_id, date, meal, food_name FROM diary_entries WHERE date IN "
        f"({','.join('?' * len(result['dates']))})", result["dates"]) if r[0] not in before]
    for res in result["results"]:
        if not res["ok"] or res.get("entry_id") or not res["change"].startswith("ADD "):
            continue
        for cand in new:
            if res["change"].startswith(f"ADD {cand['date']} {cand['meal']}: {cand['food_name']}"):
                res["entry_id"] = cand["entry_id"]
                conn.execute("UPDATE write_log SET entry_id = ? WHERE draft_id = ? AND description = ? "
                             "AND entry_id IS NULL", (cand["entry_id"], result["draft_id"], res["change"]))
                new.remove(cand)
                break
