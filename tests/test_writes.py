from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import load
from mfp_connector import db, writes
from mfp_connector.client import AuthExpired, UpstreamError
from mfp_connector.parse import parse_diary_report

TODAY = date(2026, 1, 3)
NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
BANANA = {
    "id": "98000210537901", "version": "59104990978037", "description": "Banana (One)", "brand_name": "",
    "deleted": False, "verified": False,
    "nutritional_contents": {"energy": {"unit": "calories", "value": 105}, "protein": 1, "carbohydrates": 27,
                             "fat": 0.4, "fiber": 3, "sodium": 1, "sugar": 14},
    "serving_sizes": [
        {"id": "s118", "value": 118, "unit": "g", "nutrition_multiplier": 1},
        {"id": "s1g", "value": 1, "unit": "g", "nutrition_multiplier": 0.00847457627118644},
    ],
}


class FakeClient:
    def __init__(self, fail=None):
        self.added, self.deleted, self.fail = [], [], fail or {}

    def food(self, food_id):
        if food_id != BANANA["id"]:
            raise UpstreamError(f"GET /api/services/foods/{food_id}: HTTP 404")
        return BANANA

    def add_food_entries(self, items):
        if "add" in self.fail:
            raise self.fail["add"]
        self.added.extend(items)
        return {"items": [{"id": f"new-{len(self.added)}"}]}

    def delete_entry(self, entry_id):
        if "delete" in self.fail:
            raise self.fail["delete"]
        self.deleted.append(entry_id)


@pytest.fixture
def synced(conn):
    db.replace_diary_range(conn, date(2026, 1, 1), TODAY,
                           parse_diary_report(load("diary_report.json"), date(2026, 1, 1), TODAY))
    return conn


def add(**kw):
    return {"food_id": BANANA["id"], "serving_size_id": "s118", "servings": 1, "meal": "Snacks", **kw}


def test_draft_writes_nothing_and_lists_changes(synced):
    c = FakeClient()
    d = writes.draft(synced, c, [add(servings=2)], [{"entry_id": "e3"}], today=TODAY, now=NOW)
    assert c.added == [] and c.deleted == []
    assert d["changes"][0] == "1. ADD 2026-01-03 Snacks: Banana (One), 2 × 118 g = 210 kcal [P 2g, C 54g, F 0.8g]"
    assert d["changes"][1].startswith("2. REMOVE 2026-01-01 Snacks: Peanut butter")
    assert d["kcal_change_by_date"] == {"2026-01-03": 210.0, "2026-01-01": -90.0}
    assert "confirm" in d["next_step"]


def test_serving_multiplier_from_mfp_not_caller(synced):
    d = writes.draft(synced, FakeClient(), [add(serving_size_id="s1g", servings=50)], [], today=TODAY, now=NOW)
    assert "50 × 1 g = 44.5 kcal" in d["changes"][0]


@pytest.mark.parametrize("item,msg", [
    (add(serving_size_id="bogus"), "isn't one of this food's sizes"),
    (add(meal="Brunch"), "meal must be"),
    (add(date="2025-12-01"), "outside the allowed window"),
    (add(date="2026-01-05"), "outside the allowed window"),
    (add(food_id="../admin"), "bad food_id"),
    (add(servings=0), "servings must be"),
    (add(servings=40), "per-item limit"),
])
def test_draft_rejects_bad_adds(synced, item, msg):
    with pytest.raises(writes.WriteError, match=msg):
        writes.draft(synced, FakeClient(), [item], [], today=TODAY, now=NOW)


def test_draft_rejects_unknown_or_bad_entries(synced):
    with pytest.raises(writes.WriteError, match="isn't in the synced diary"):
        writes.draft(synced, FakeClient(), [], [{"entry_id": "nope"}], today=TODAY, now=NOW)
    with pytest.raises(writes.WriteError, match="bad entry_id"):
        writes.draft(synced, FakeClient(), [], [{"entry_id": "a/../b"}], today=TODAY, now=NOW)
    with pytest.raises(writes.WriteError, match="nothing to change"):
        writes.draft(synced, FakeClient(), [], [], today=TODAY, now=NOW)


def test_confirm_applies_once_and_logs(synced):
    c = FakeClient()
    d = writes.draft(synced, c, [add()], [{"entry_id": "e3"}], today=TODAY, now=NOW)
    r = writes.confirm(synced, c, d["draft_id"], now=NOW + timedelta(minutes=1))
    assert r["status"] == "applied" and r["dates"] == ["2026-01-01", "2026-01-03"]
    assert c.added == [{"type": "food_entry", "date": "2026-01-03",
                        "food": {"id": BANANA["id"], "version": BANANA["version"]}, "servings": 1.0,
                        "meal_position": 3, "serving_size": {"value": 118, "unit": "g", "nutrition_multiplier": 1}}]
    assert c.deleted == ["e3"]
    assert r["results"][0]["entry_id"] == "new-1"
    log = synced.execute("SELECT action, ok FROM write_log ORDER BY id").fetchall()
    assert [tuple(x) for x in log] == [("add", 1), ("remove", 1)]
    with pytest.raises(writes.WriteError, match="already applied"):
        writes.confirm(synced, c, d["draft_id"], now=NOW + timedelta(minutes=2))
    assert len(c.added) == 1


def test_confirm_rejects_unknown_and_expired(synced):
    c = FakeClient()
    with pytest.raises(writes.WriteError, match="unknown"):
        writes.confirm(synced, c, "deadbeef", now=NOW)
    d = writes.draft(synced, c, [add()], [], today=TODAY, now=NOW)
    with pytest.raises(writes.WriteError, match="expired"):
        writes.confirm(synced, c, d["draft_id"], now=NOW + timedelta(minutes=16))
    assert c.added == []


def test_hourly_limit_refuses_whole_draft(synced):
    c = FakeClient()
    for i in range(3):
        d = writes.draft(synced, c, [add()] * 5, [], today=TODAY, now=NOW)
        writes.confirm(synced, c, d["draft_id"], now=NOW + timedelta(minutes=i))
    assert len(c.added) == 15
    d = writes.draft(synced, c, [add()], [], today=TODAY, now=NOW + timedelta(minutes=5))
    assert d["writes_left_this_hour"] == 0
    with pytest.raises(writes.WriteError, match="hourly write limit"):
        writes.confirm(synced, c, d["draft_id"], now=NOW + timedelta(minutes=6))
    d = writes.draft(synced, c, [add()], [], today=TODAY, now=NOW + timedelta(minutes=61))
    assert writes.confirm(synced, c, d["draft_id"], now=NOW + timedelta(minutes=62))["status"] == "applied"


def test_too_many_items_in_one_draft(synced):
    with pytest.raises(writes.WriteError, match="at most 15"):
        writes.draft(synced, FakeClient(), [add()] * 16, [], today=TODAY, now=NOW)


def test_auth_failure_stops_and_marks_rest_skipped(synced):
    c = FakeClient(fail={"add": AuthExpired("x")})
    d = writes.draft(synced, c, [add(), add()], [], today=TODAY, now=NOW)
    r = writes.confirm(synced, c, d["draft_id"], now=NOW)
    assert r["status"] == "failed"
    assert [x.get("error") for x in r["results"]] == ["x", "skipped after earlier failure"]
    assert synced.execute("SELECT COUNT(*) FROM write_log WHERE ok = 0").fetchone()[0] == 2


def test_partial_failure(synced):
    c = FakeClient(fail={"delete": UpstreamError("HTTP 500")})
    d = writes.draft(synced, c, [add()], [{"entry_id": "e3"}], today=TODAY, now=NOW)
    r = writes.confirm(synced, c, d["draft_id"], now=NOW)
    assert r["status"] == "partial" and [x["ok"] for x in r["results"]] == [True, False]


def test_food_search_formatting_prefers_history(synced):
    from mfp_connector.queries import food_search
    other = {**BANANA, "id": "1", "description": "Porridge oats", "brand_name": "Generic"}
    out = food_search(synced, [{"item": BANANA}, {"item": other}], limit=5)["results"]
    assert [r["name"] for r in out] == ["Porridge oats", "Banana (One)"]  # logged before → first
    assert out[0]["times_you_logged_it"] == 1
    assert out[1]["servings"][0] == {"serving_size_id": "s118", "size": "118 g", "kcal": 105.0, "protein": 1.0,
                                     "carbs": 27.0, "fat": 0.4, "fibre": 3.0, "sodium": 1.0, "sugar": 14.0}


def test_attach_new_entry_ids_by_diffing(synced):
    before = writes.entry_ids(synced, ["2026-01-03"])
    synced.execute(
        "INSERT INTO diary_entries (entry_id, date, meal, food_name, is_quick_add, nutrients_json, fetched_at) "
        "VALUES ('fresh-uuid', '2026-01-03', 'Snacks', 'Banana (One)', 0, '{}', 'now')")
    result = {"draft_id": "d1", "dates": ["2026-01-03"], "results": [
        {"ok": True, "change": "ADD 2026-01-03 Snacks: Banana (One), 1 × 118 g = 105 kcal", "entry_id": None}]}
    writes.attach_new_entry_ids(synced, result, before)
    assert result["results"][0]["entry_id"] == "fresh-uuid"
