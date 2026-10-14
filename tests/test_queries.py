from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import load
from mfp_connector import db, queries
from mfp_connector.parse import WeightReading, parse_diary_report, parse_goals

JAN1, JAN3 = date(2026, 1, 1), date(2026, 1, 3)  # 2026-01-01 is a Thursday


@pytest.fixture
def filled(conn):
    db.replace_diary_range(conn, JAN1, JAN3, parse_diary_report(load("diary_report.json"), JAN1, JAN3))
    db.replace_goals(conn, parse_goals(load("nutrient_goals.json")))
    readings = [
        WeightReading(f"m{i}", (JAN1 + timedelta(days=i)).isoformat(), 200 - 0.2 * i + (0.3 if i % 3 else 0), "mfp", None)
        for i in range(30) if i % 4 != 3  # some missing days
    ]
    db.replace_weights(conn, readings)
    run = db.start_run(conn, "nightly", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.finish_run(conn, run, True)
    return conn


def test_diary_statuses(filled):
    days = queries.diary(filled, "2025-12-31", "2026-01-03")["days"]
    assert [d["status"] for d in days] == ["not_synced", "logged", "unlogged", "logged"]
    assert days[1]["quick_add_count"] == 1 and days[1]["kcal"] == 915.3
    assert "entries" not in days[1]


def test_diary_items_ordered_by_meal(filled):
    day = queries.diary(filled, "2026-01-01", "2026-01-01", "items")["days"][0]
    assert [e["meal"] for e in day["entries"]] == ["Breakfast", "Dinner", "Snacks"]
    assert day["entries"][1]["is_quick_add"] is True and day["entries"][1]["protein"] is None


def test_items_range_limited(filled):
    with pytest.raises(queries.QueryError):
        queries.diary(filled, "2026-01-01", "2026-03-01", "items")


@pytest.mark.parametrize("a,b", [("2026-01-03", "2026-01-01"), ("yesterday", "2026-01-01")])
def test_bad_ranges(filled, a, b):
    with pytest.raises(queries.QueryError):
        queries.diary(filled, a, b)


def test_today_remaining_uses_weekday_goal(filled):
    t = queries.today(filled, JAN1, {"ok": True})
    assert t["goal"]["kcal"] == 2000
    assert t["remaining"]["kcal"] == round(2000 - 915.3, 1)
    assert set(t["meals"]) == {"Breakfast", "Dinner", "Snacks"}
    sat = queries.today(filled, JAN3, None)
    assert sat["goal"]["kcal"] == 2300


def test_coverage(filled):
    c = queries.coverage(filled, "2026-01-01", "2026-01-04")
    assert c["unlogged_dates"] == ["2026-01-02"]
    assert c["not_synced_dates"] == ["2026-01-04"]
    assert [p["date"] for p in c["partial_days"]] == ["2026-01-01", "2026-01-03"]
    assert c["quick_add"]["entries"] == 2


def test_find_food_escapes_wildcards(filled):
    assert queries.find_food(filled, "oats")["matches"][0]["food"] == "Porridge oats"
    assert queries.find_food(filled, "acme")["matches"][0]["brand"] == "Acme"
    assert queries.find_food(filled, "%%")["matches"] == []


def test_weights_rolling_mean(filled):
    w = queries.weights(filled, "2026-01-08", "2026-01-12")["readings"]
    assert all(r["rolling_7d_days"] <= 7 for r in w)
    first = w[0]
    # readings skip Jan 4, 8, 12: first in range is Jan 9, window Jan 3-9 has 5 days
    assert first["date"] == "2026-01-09" and first["rolling_7d_days"] == 5


def test_trend(filled):
    t = queries.trend(filled, "2026-01-01", "2026-01-31")
    assert t["ols_hac"]["slope_per_week"] == pytest.approx(-1.4, abs=0.15)
    assert t["mann_kendall"]["trend"] == "decreasing"
    assert t["theil_sen"]["ci95_per_week"][0] < t["theil_sen"]["slope_per_week"] < t["theil_sen"]["ci95_per_week"][1]


def test_trend_needs_data(filled):
    with pytest.raises(queries.QueryError):
        queries.trend(filled, "2026-01-01", "2026-01-05")


def test_freshness_flags_expired_cookie(filled):
    run = db.start_run(filled, "today", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.finish_run(filled, run, False, error_kind="auth_expired", error="diary: session not accepted")
    f = queries.freshness(filled)
    assert not f["stale"] and any("cookie" in w for w in f["warnings"])
    assert queries.sync_status(filled)["cookie_health"] == "expired"


def test_freshness_stale_when_old(conn):
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    db.finish_run(conn, db.start_run(conn, "nightly", old), True)
    assert queries.freshness(conn)["stale"] is True
