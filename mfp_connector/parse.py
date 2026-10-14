"""Pure functions turning MFP API JSON into typed records.

Nutrient values MFP didn't record stay ``None`` (e.g. a calories-only Quick Add
has no protein); they are never coerced to 0.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

KJ_PER_KCAL = 4.184
LB_PER = {"pounds": 1.0, "lbs": 1.0, "kilograms": 2.2046226218, "kg": 2.2046226218, "stone": 14.0}


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class DiaryEntry:
    entry_id: str
    date: str
    meal: str
    meal_position: int | None
    food_name: str
    brand: str | None
    servings: float | None
    serving_size: str | None
    kcal: float | None
    protein: float | None
    carbs: float | None
    fat: float | None
    fibre: float | None
    sodium: float | None
    sugar: float | None
    is_quick_add: bool
    consumed_at: str | None
    logged_at: str | None
    nutrients_json: str


@dataclass(frozen=True)
class WeightReading:
    measurement_id: str
    date: str
    weight_lb: float
    source: str
    updated_at: str | None


@dataclass(frozen=True)
class DailyGoal:
    day_of_week: str
    valid_from: str | None
    valid_to: str | None
    kcal: float | None
    protein: float | None
    carbs: float | None
    fat: float | None
    fibre: float | None
    sodium: float | None
    sugar: float | None


def _num(v: Any, ndigits: int = 3) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _kcal(energy: Any) -> float | None:
    if isinstance(energy, dict):
        value = _num(energy.get("value"), 6)
        if value is None:
            return None
        unit = (energy.get("unit") or "calories").lower()
        if unit.startswith("kilojoule"):
            value /= KJ_PER_KCAL
        elif unit != "calories":
            raise ParseError(f"unknown energy unit {unit!r}")
        return round(value, 3)
    return _num(energy)


def _iso_date(s: Any, what: str) -> str:
    try:
        return date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise ParseError(f"{what}: bad date {s!r}") from None


def is_quick_add(food: dict) -> bool:
    return (food.get("description") or "").strip().lower().startswith("quick add")


def parse_entry(e: dict) -> DiaryEntry:
    if not isinstance(e, dict) or not e.get("id"):
        raise ParseError("food entry without id")
    food = e.get("food") or {}
    nc = e.get("nutritional_contents") or {}
    ss = e.get("serving_size") or {}
    serving_size = None
    if ss.get("unit") is not None:
        value = _num(ss.get("value"), 4)
        serving_size = f"{value:g} {ss['unit']}" if value is not None else ss["unit"]
    return DiaryEntry(
        entry_id=str(e["id"]),
        date=_iso_date(e.get("date"), f"entry {e['id']}"),
        meal=e.get("meal_name") or "Unknown",
        meal_position=e.get("meal_position"),
        food_name=(food.get("description") or "(unnamed food)").replace("\xa0", " ").strip(),
        brand=(food.get("brand_name") or "").replace("\xa0", " ").strip() or None,
        servings=_num(e.get("servings"), 4),
        serving_size=serving_size,
        kcal=_kcal(nc.get("energy")),
        protein=_num(nc.get("protein")),
        carbs=_num(nc.get("carbohydrates")),
        fat=_num(nc.get("fat")),
        fibre=_num(nc.get("fiber")),
        sodium=_num(nc.get("sodium")),
        sugar=_num(nc.get("sugar")),
        is_quick_add=is_quick_add(food),
        consumed_at=e.get("consumed_at"),
        logged_at=e.get("logged_at"),
        nutrients_json=json.dumps(nc, sort_keys=True),
    )


def date_range(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def parse_diary_report(payload: Any, start: date, end: date) -> dict[str, list[DiaryEntry]]:
    """Entries keyed by every date in [start, end]; dates MFP omitted map to []."""
    if not isinstance(payload, list):
        raise ParseError(f"diary report: expected a list, got {type(payload).__name__}")
    out: dict[str, list[DiaryEntry]] = {d: [] for d in date_range(start, end)}
    for day in payload:
        if not isinstance(day, dict):
            raise ParseError("diary report: day is not an object")
        d = _iso_date(day.get("date"), "diary report day")
        if d not in out:
            raise ParseError(f"diary report: day {d} outside requested {start}..{end}")
        entries = day.get("food_entries") or []
        if not isinstance(entries, list):
            raise ParseError(f"diary report: food_entries for {d} is not a list")
        for e in entries:
            if e.get("deleted") or e.get("type", "food_entry") != "food_entry":
                continue
            entry = parse_entry(e)
            if entry.date != d:
                raise ParseError(f"diary report: entry {entry.entry_id} dated {entry.date} under {d}")
            out[d].append(entry)
    return out


def parse_measurements(items: Any) -> list[WeightReading]:
    if not isinstance(items, list):
        raise ParseError("measurements: expected a list")
    out = []
    for m in items:
        if (m.get("type") or "").lower() != "weight":
            continue
        unit = (m.get("unit") or "").lower()
        if unit not in LB_PER:
            raise ParseError(f"measurement {m.get('id')}: unknown weight unit {unit!r}")
        value = _num(m.get("value"), 4)
        if value is None or not m.get("id"):
            raise ParseError(f"measurement {m.get('id')}: missing id or value")
        out.append(WeightReading(
            measurement_id=str(m["id"]),
            date=_iso_date(m.get("date"), f"measurement {m['id']}"),
            weight_lb=round(value * LB_PER[unit], 2),
            source="mfp",
            updated_at=m.get("updated_at"),
        ))
    return out


def parse_goals(payload: Any) -> list[DailyGoal]:
    if not isinstance(payload, dict) or not isinstance(payload.get("daily_goals"), list):
        raise ParseError("nutrient goals: unexpected shape")
    out = []
    for g in payload["daily_goals"]:
        out.append(DailyGoal(
            day_of_week=str(g.get("day_of_week", "")).lower(),
            valid_from=payload.get("valid_from"),
            valid_to=payload.get("valid_to"),
            kcal=_kcal(g.get("energy")),
            protein=_num(g.get("protein")),
            carbs=_num(g.get("carbohydrates")),
            fat=_num(g.get("fat")),
            fibre=_num(g.get("fiber")),
            sodium=_num(g.get("sodium")),
            sugar=_num(g.get("sugar")),
        ))
    days = {g.day_of_week for g in out}
    if len(out) != 7 or len(days) != 7:
        raise ParseError(f"nutrient goals: expected 7 weekdays, got {sorted(days)}")
    return out
