"""Tests for hubspot_logic.py -- owner matching, call parsing, date
ranges, time buckets, formatting. Pure functions; no database or HubSpot.

Run with: pytest tests/test_hubspot_logic.py -v
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import hubspot_logic as logic


def owner(owner_id, email=None, first=None, last=None):
    return {"id": owner_id, "email": email, "firstName": first, "lastName": last}


USERS = [
    {"id": 1, "email": "jack.jacinto@planet.net", "sales_rep_id": 10},
    {"id": 2, "email": "Maria.Lopez@planet.net", "sales_rep_id": 20},
    {"id": 3, "email": "no.rep@planet.net", "sales_rep_id": None},
]
REPS = [
    {"id": 10, "name": "Jack Jacinto"},
    {"id": 20, "name": "Maria Lopez"},
    {"id": 30, "name": "Sam Smith"},
    {"id": 40, "name": "Chris Lee"},
    {"id": 41, "name": "chris  LEE"},
    {"id": 50, "name": "José O'Brien"},
    {"id": 60, "name": "No Rep"},
]


# ---------------- matching ----------------

def test_exact_email_match_uses_users_rep():
    result = logic.match_owners([owner("1", "jack.jacinto@planet.net", "J", "J")], USERS, REPS)["1"]
    assert result == {"dashboard_user_id": 1, "sales_rep_id": 10, "method": "email_exact", "status": "matched"}


def test_case_insensitive_email_match():
    result = logic.match_owners([owner("2", "  maria.lopez@PLANET.net ")], USERS, REPS)["2"]
    assert result["method"] == "email_normalized"
    assert result["sales_rep_id"] == 20
    assert result["status"] == "matched"


def test_unique_name_match_when_no_email_match():
    result = logic.match_owners([owner("3", "sam@other.com", "Sam", "Smith")], USERS, REPS)["3"]
    assert result == {"dashboard_user_id": None, "sales_rep_id": 30, "method": "name", "status": "matched"}


def test_name_match_folds_accents_and_punctuation():
    result = logic.match_owners([owner("4", None, "Jose", "OBrien")], USERS, REPS)["4"]
    assert result["sales_rep_id"] == 50


def test_name_equal_to_two_reps_is_ambiguous_not_matched():
    result = logic.match_owners([owner("5", None, "Chris", "Lee")], USERS, REPS)["5"]
    assert result["sales_rep_id"] is None
    assert result["status"] == "ambiguous"


def test_two_owners_with_same_name_are_both_ambiguous():
    results = logic.match_owners([owner("6", "a@x.com", "Sam", "Smith"), owner("7", "b@x.com", "Sam", "Smith")], USERS, REPS)
    assert results["6"]["sales_rep_id"] is None and results["6"]["status"] == "ambiguous"
    assert results["7"]["sales_rep_id"] is None and results["7"]["status"] == "ambiguous"


def test_name_match_onto_rep_already_matched_by_email_is_ambiguous():
    results = logic.match_owners(
        [owner("8", "jack.jacinto@planet.net"), owner("9", "jj@personal.com", "Jack", "Jacinto")], USERS, REPS,
    )
    assert results["8"]["sales_rep_id"] == 10
    assert results["9"]["sales_rep_id"] is None
    assert results["9"]["status"] == "ambiguous"


def test_email_user_without_rep_falls_through_to_name_and_keeps_user():
    result = logic.match_owners([owner("10", "no.rep@planet.net", "No", "Rep")], USERS, REPS)["10"]
    assert result["dashboard_user_id"] == 3
    assert result["sales_rep_id"] == 60
    assert result["method"] == "name"


def test_no_match_is_unmatched():
    result = logic.match_owners([owner("11", "nobody@x.com", "Nobody", "Here")], USERS, REPS)["11"]
    assert result == {"dashboard_user_id": None, "sales_rep_id": None, "method": "none", "status": "unmatched"}


def test_owner_with_no_name_or_email_is_unmatched():
    result = logic.match_owners([owner("12")], USERS, REPS)["12"]
    assert result["status"] == "unmatched"


# ---------------- parsing ----------------

def test_parse_call_full_record():
    row = logic.parse_call({
        "id": "123",
        "properties": {
            "hubspot_owner_id": "77",
            "hs_timestamp": "2026-10-15T14:32:05.120Z",
            "hs_call_direction": "OUTBOUND",
            "hs_call_duration": "204000",
            "hs_call_disposition": "f240bbac-87c9-4f6e-bf70-924b57d47db7",
            "hs_call_status": "COMPLETED",
            "hs_call_to_number": "+15551234567",
            "hs_lastmodifieddate": "1760538725120",
        },
    })
    assert row["hubspot_call_id"] == "123"
    assert row["call_timestamp"] == datetime(2026, 10, 15, 14, 32, 5, 120000, tzinfo=timezone.utc)
    assert row["duration_ms"] == 204000
    assert row["hubspot_modified_at"] == datetime.fromtimestamp(1760538725.120, tz=timezone.utc)


def test_parse_call_missing_properties_stay_none():
    row = logic.parse_call({"id": "5", "properties": {"hs_call_duration": "", "hs_call_title": "  "}})
    assert row["duration_ms"] is None
    assert row["title"] is None
    assert row["call_timestamp"] is None
    assert row["direction"] is None


def test_parse_call_without_id_has_no_id():
    assert logic.parse_call({"properties": {}})["hubspot_call_id"] is None


# ---------------- connected ----------------

def test_connected_ids_from_labels_plus_default(monkeypatch):
    monkeypatch.delenv("HUBSPOT_CONNECTED_OUTCOMES", raising=False)
    ids = logic.connected_disposition_ids({"custom-1": "Connected", "x": "No answer"})
    assert ids == {"custom-1", logic.DEFAULT_CONNECTED_DISPOSITION_ID}


def test_connected_override(monkeypatch):
    monkeypatch.setenv("HUBSPOT_CONNECTED_OUTCOMES", "Spoke with decision maker")
    ids = logic.connected_disposition_ids({"a": "Spoke with decision maker", "b": "Connected"})
    assert ids == {"a"}


# ---------------- date ranges ----------------

TODAY = date(2026, 10, 16)


@pytest.mark.parametrize("key,expected", [
    ("today", (date(2026, 10, 16), date(2026, 10, 16))),
    ("yesterday", (date(2026, 10, 15), date(2026, 10, 15))),
    ("last_7_days", (date(2026, 10, 10), date(2026, 10, 16))),
    ("last_30_days", (date(2026, 9, 17), date(2026, 10, 16))),
    ("last_90_days", (date(2026, 7, 19), date(2026, 10, 16))),
    ("this_month", (date(2026, 10, 1), date(2026, 10, 16))),
    ("last_month", (date(2026, 9, 1), date(2026, 9, 30))),
])
def test_resolve_date_ranges(key, expected):
    assert logic.resolve_date_range(key, None, None, TODAY) == expected


def test_last_month_in_january():
    assert logic.resolve_date_range("last_month", None, None, date(2026, 1, 5)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_custom_range_validation():
    assert logic.resolve_date_range("custom", "2026-01-01", "2026-01-31", TODAY) == (date(2026, 1, 1), date(2026, 1, 31))
    with pytest.raises(ValueError):
        logic.resolve_date_range("custom", "2026-02-01", "2026-01-01", TODAY)
    with pytest.raises(ValueError):
        logic.resolve_date_range("custom", "garbage", "2026-01-01", TODAY)
    with pytest.raises(ValueError):
        logic.resolve_date_range("bogus", None, None, TODAY)


# ---------------- buckets ----------------

def test_daily_buckets_zero_filled():
    counts = {datetime(2026, 10, 2): 4}
    rows = logic.fill_buckets(counts, date(2026, 10, 1), date(2026, 10, 3), "daily")
    assert [r["count"] for r in rows] == [0, 4, 0]
    assert rows[1]["label"] == "Oct 2"


def test_hourly_buckets_cover_whole_days():
    rows = logic.fill_buckets({datetime(2026, 10, 1, 13): 2}, date(2026, 10, 1), date(2026, 10, 1), "hourly")
    assert len(rows) == 24
    assert rows[13]["count"] == 2
    assert rows[13]["label"] == "Oct 1, 1 PM"


def test_weekly_buckets_start_monday():
    rows = logic.fill_buckets({}, date(2026, 10, 1), date(2026, 10, 16), "weekly")
    assert [r["start"][:10] for r in rows] == ["2026-09-28", "2026-10-05", "2026-10-12"]


def test_monthly_quarterly_yearly_buckets():
    monthly = logic.fill_buckets({datetime(2026, 2, 1): 9}, date(2025, 12, 15), date(2026, 2, 3), "monthly")
    assert [r["label"] for r in monthly] == ["Dec 2025", "Jan 2026", "Feb 2026"]
    assert monthly[2]["count"] == 9
    quarterly = logic.fill_buckets({}, date(2025, 11, 1), date(2026, 4, 1), "quarterly")
    assert [r["label"] for r in quarterly] == ["Q4 2025", "Q1 2026", "Q2 2026"]
    yearly = logic.fill_buckets({datetime(2025, 1, 1): 3}, date(2024, 6, 1), date(2026, 1, 1), "yearly")
    assert [(r["label"], r["count"]) for r in yearly] == [("2024", 0), ("2025", 3), ("2026", 0)]


def test_too_many_hourly_buckets_rejected():
    with pytest.raises(ValueError):
        logic.fill_buckets({}, date(2020, 1, 1), date(2026, 1, 1), "hourly")


# ---------------- formatting ----------------

def test_duration_formatting():
    assert logic.format_duration_ms(204000) == "3m 24s"
    assert logic.format_duration_ms(32000) == "0m 32s"
    assert logic.format_duration_hms(3_723_000) == "1h 2m 3s"
    assert logic.format_duration_hms(0) == "0m 0s"
    assert logic.format_duration_ms(None) is None
    assert logic.humanize_status("NO_ANSWER") == "No Answer"


# ---------------- range-driven grouping ----------------

@pytest.mark.parametrize("key,expected", [
    ("last_6_months", (date(2026, 4, 17), date(2026, 10, 16))),
    ("last_12_months", (date(2025, 10, 17), date(2026, 10, 16))),
    ("this_year", (date(2026, 1, 1), date(2026, 10, 16))),
])
def test_new_preset_ranges(key, expected):
    assert logic.resolve_date_range(key, None, None, TODAY) == expected


def test_months_back_clamps_short_months():
    # Aug 31 minus 6 months -> Feb has no 31st; start the day after Feb 28.
    assert logic.resolve_date_range("last_6_months", None, None, date(2026, 8, 31))[0] == date(2026, 3, 1)


@pytest.mark.parametrize("key,scale", [
    ("today", "hourly"), ("yesterday", "hourly"),
    ("last_7_days", "daily"), ("last_30_days", "daily"), ("this_month", "daily"), ("last_month", "daily"),
    ("last_90_days", "weekly"), ("last_6_months", "weekly"),
    ("last_12_months", "monthly"), ("this_year", "monthly"),
])
def test_preset_auto_scale(key, scale):
    start, end = logic.resolve_date_range(key, None, None, TODAY)
    assert logic.resolve_time_scale("auto", key, start, end)[0] == scale


@pytest.mark.parametrize("days,scale", [(1, "hourly"), (2, "hourly"), (3, "daily"), (62, "daily"),
                                        (63, "weekly"), (200, "weekly"), (201, "monthly"), (731, "quarterly"), (2000, "yearly")])
def test_custom_auto_scale_by_length(days, scale):
    end = date(2026, 10, 16)
    start = end - timedelta(days=days - 1)
    assert logic.auto_time_scale("custom", start, end) == scale


def test_auto_choice_is_always_allowed():
    end = date(2026, 10, 16)
    for days in range(1, 2500, 7):
        start = end - timedelta(days=days - 1)
        scale, allowed = logic.resolve_time_scale("auto", "custom", start, end)
        assert scale in allowed
    # This Month on the 1st is a 1-day range, but its preset is Daily.
    scale, allowed = logic.resolve_time_scale("auto", "this_month", date(2026, 10, 1), date(2026, 10, 1))
    assert scale == "daily" and "daily" in allowed


def test_manual_scale_must_fit_range():
    assert logic.resolve_time_scale("weekly", "last_30_days", date(2026, 9, 17), date(2026, 10, 16))[0] == "weekly"
    with pytest.raises(ValueError):
        logic.resolve_time_scale("hourly", "last_90_days", date(2026, 7, 19), date(2026, 10, 16))
    with pytest.raises(ValueError):
        logic.resolve_time_scale("yearly", "today", TODAY, TODAY)
    with pytest.raises(ValueError):
        logic.resolve_time_scale("minutely", "today", TODAY, TODAY)


def test_safe_https_url_and_contact_url():
    assert logic.safe_https_url(" https://api-na1.hubspot.com/recording/x ") == "https://api-na1.hubspot.com/recording/x"
    for bad in ["javascript:alert(1)", "http://x.com/a", "/relative", "https://a b", "", None]:
        assert logic.safe_https_url(bad) is None
    assert logic.hubspot_contact_url("app-eu1.hubspot.com", "99", "5") == "https://app-eu1.hubspot.com/contacts/99/record/0-1/5"
    assert logic.hubspot_contact_url("evil.com", "99", "5") == "https://app.hubspot.com/contacts/99/record/0-1/5"
    assert logic.hubspot_contact_url(None, None, "5") is None


def test_contact_search_url():
    assert (logic.hubspot_contact_search_url("app.hubspot.com", "99", "a+b@x.com")
            == "https://app.hubspot.com/contacts/99/objects/0-1/views/all/list?query=a%2Bb%40x.com")
    assert logic.hubspot_contact_search_url("evil.com", "99", "a@x.com").startswith("https://app.hubspot.com/")
    assert logic.hubspot_contact_search_url("app.hubspot.com", None, "a@x.com") is None


def test_format_hours_minutes():
    assert logic.format_hours_minutes(22320) == "6h 12m"
    assert logic.format_hours_minutes(900) == "15m"
