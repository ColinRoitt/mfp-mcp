from datetime import date

import pytest

from conftest import load
from mfp_connector.parse import (
    ParseError,
    parse_diary_report,
    parse_goals,
    parse_measurements,
)

JAN1, JAN3 = date(2026, 1, 1), date(2026, 1, 3)


def test_diary_report_covers_every_requested_date():
    out = parse_diary_report(load("diary_report.json"), JAN1, JAN3)
    assert list(out) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert out["2026-01-02"] == []


def test_entry_fields_use_scaled_entry_nutrients():
    oats = parse_diary_report(load("diary_report.json"), JAN1, JAN3)["2026-01-01"][0]
    assert oats.entry_id == "e1"
    assert oats.meal == "Breakfast"
    assert oats.food_name == "Porridge oats"
    assert oats.servings == 1.5  # float32 noise from MFP rounded off
    assert oats.serving_size == "80 g"
    assert oats.kcal == 225.3  # entry-level value, not the food's per-serving 150
    assert oats.protein == 7.5
    assert not oats.is_quick_add


def test_quick_add_flagged_and_untracked_nutrients_stay_null():
    entries = parse_diary_report(load("diary_report.json"), JAN1, JAN3)
    qa = entries["2026-01-01"][1]
    assert qa.is_quick_add
    assert qa.kcal == 600
    assert qa.protein is None and qa.carbs is None and qa.fat is None
    assert entries["2026-01-03"][0].is_quick_add


def test_deleted_entries_skipped_and_nbsp_normalised():
    day = parse_diary_report(load("diary_report.json"), JAN1, JAN3)["2026-01-01"]
    assert [e.entry_id for e in day] == ["e1", "e2", "e3"]
    assert day[2].food_name == "Peanut butter - Smooth"


def test_kilojoules_converted():
    payload = load("diary_report.json")
    payload[1]["food_entries"][0]["nutritional_contents"]["energy"] = {"unit": "kilojoules", "value": 418.4}
    assert parse_diary_report(payload, JAN1, JAN3)["2026-01-03"][0].kcal == 100


@pytest.mark.parametrize("payload", [{"error": "x"}, [{"food_entries": []}], ["nope"]])
def test_bad_diary_shapes_rejected(payload):
    with pytest.raises(ParseError):
        parse_diary_report(payload, JAN1, JAN3)


def test_day_outside_requested_range_rejected():
    with pytest.raises(ParseError, match="outside"):
        parse_diary_report(load("diary_report.json"), JAN1, date(2026, 1, 2))


def test_measurements_keep_weight_only():
    w = parse_measurements(load("measurements.json")["items"])
    assert [(r.date, r.weight_lb) for r in w] == [("2026-01-03", 180.0), ("2026-01-02", 181.4)]


@pytest.mark.parametrize("unit,value,lb", [("kilograms", 100, 220.46), ("stone", 22.5, 315.0)])
def test_weight_units_converted(unit, value, lb):
    [r] = parse_measurements([{"id": "x", "type": "Weight", "value": value, "date": "2026-01-01", "unit": unit}])
    assert r.weight_lb == lb


def test_unknown_weight_unit_rejected():
    with pytest.raises(ParseError):
        parse_measurements([{"id": "x", "type": "Weight", "value": 1, "date": "2026-01-01", "unit": "furlongs"}])


def test_goals_per_weekday():
    goals = {g.day_of_week: g for g in parse_goals(load("nutrient_goals.json"))}
    assert goals["monday"].kcal == 2000
    assert goals["saturday"].kcal == 2300
    assert goals["monday"].fibre == 30


def test_goals_need_all_weekdays():
    payload = load("nutrient_goals.json")
    payload["daily_goals"].pop()
    with pytest.raises(ParseError):
        parse_goals(payload)
