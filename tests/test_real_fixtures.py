"""Invariants over real responses captured in spike-out/ (git-ignored; skipped when absent)."""
import json
from datetime import date

import pytest

from conftest import SPIKE_OUT
from mfp_connector import db
from mfp_connector.parse import parse_diary_report, parse_goals, parse_measurements

reports = sorted(SPIKE_OUT.glob("diary_report_*_*.json"))
pytestmark = pytest.mark.skipif(not reports, reason="no real fixtures in spike-out/")


def _range(path):
    _, start, end = path.stem.rsplit("_", 2)
    return date.fromisoformat(start), date.fromisoformat(end)


@pytest.mark.parametrize("path", reports, ids=lambda p: p.stem)
def test_real_diary_reports_parse_and_store(path, conn):
    start, end = _range(path)
    raw = json.loads(path.read_text())
    parsed = parse_diary_report(raw, start, end)
    entries = [e for day in parsed.values() for e in day]
    assert len(entries) == sum(len(d["food_entries"]) for d in raw)
    assert all(e.kcal is not None for e in entries)
    assert len({e.entry_id for e in entries}) == len(entries)
    assert db.replace_diary_range(conn, start, end, parsed) == len(entries)


def test_real_reports_contain_quick_adds():
    flagged = [
        e for p in reports for day in parse_diary_report(json.loads(p.read_text()), *_range(p)).values()
        for e in day if e.is_quick_add
    ]
    assert flagged and all(e.food_name.lower().startswith("quick add") for e in flagged)


def test_real_measurements():
    path = SPIKE_OUT / "meas.json"
    readings = parse_measurements(json.loads(path.read_text())["items"])
    assert readings and all(50 < r.weight_lb < 800 for r in readings)


def test_real_goals():
    goals = parse_goals(json.loads((SPIKE_OUT / "nutrient_goals.json").read_text()))
    assert all(g.kcal and g.kcal > 0 for g in goals)
