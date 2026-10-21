"""Tests for passings_cache.py -- the in-memory Passings & Leads snapshot.
Each check pins the cache to the semantics of its passings_data.py SQL
counterpart (filters, KPI definitions, SQL Server NULL ordering and
DATEDIFF(WEEK) bucketing, the latest-email/business-flag rules) using a
small hand-built snapshot -- no database needed.

Run with: pytest tests/test_passings_cache.py -v
"""

from datetime import date, datetime

import pandas as pd
import pytest

import passings_cache as pc

ROWS = [
    # PlaceID, Email, EmailInsertDate, FullAddress, Street, City, State, Zip, Avail, BecameAvailable, Units, Biz, ClassName, TypeLabel
    (1, "a@x.com", datetime(2026, 1, 5), "1 Main St", "Main St", "NEWARK", "NJ", "07101", 1, datetime(2026, 10, 4), 1, 0, "Residential", None),
    (2, None, None, "2 Main St", "Main St", "NEWARK", "NJ", "07101", 3, datetime(2026, 10, 5), 2.0, 0, "Residential", None),
    (3, "none", datetime(2026, 1, 5), "3 Oak Ave", "Oak Ave", "TRENTON", "NJ", "08601", 1, datetime(2026, 10, 10), None, 0, "Office", "Commercial"),
    (4, "b@y.com", datetime(2026, 2, 1), "4 Elm Rd", "Elm Rd", "Trenton", "NJ", "08601", 1, None, 4, 1, "Retail", "Commercial"),
    (5, "  ", None, "5 Pine Ln", "Pine Ln", "ALBANY", "NY", "12201", 3, datetime(2026, 9, 1), 1, 0, None, None),
]


@pytest.fixture(autouse=True)
def snapshot():
    df = pd.DataFrame.from_records(ROWS, columns=pc._SNAPSHOT_COLUMNS)
    pc._state["df"] = pc._prepare(df)
    yield
    pc._state["df"] = None


def ids(page):
    return [r["PlaceID"] for r in page["rows"]]


def test_categories_match_the_classification_tree():
    page = lambda cat: ids(pc.get_page({"category": cat}, "", "place_id", "asc", 1))
    assert page("residential_lead") == [1]
    assert page("commercial_hot_lead") == [4]
    assert page("commercial_field_target") == [3]
    assert page("none") == [2, 5]  # missing, blank, and 'none' emails are not leads


def test_city_filter_is_case_insensitive_like_sql_server():
    assert ids(pc.get_page({"city": "trenton"}, "", "place_id", "asc", 1)) == [3, 4]


def test_search_matches_any_column_case_insensitively():
    assert ids(pc.get_page({}, "OAK", "place_id", "asc", 1)) == [3]
    assert ids(pc.get_page({}, "b@y", "place_id", "asc", 1)) == [4]
    assert ids(pc.get_page({}, "5", "place_id", "asc", 1)) == [5]  # PlaceID / address


def test_activity_allow_and_deny_lists():
    assert ids(pc.get_page({"place_id_in": [1, 3]}, "", "place_id", "asc", 1)) == [1, 3]
    assert ids(pc.get_page({"place_id_in": []}, "", "place_id", "asc", 1)) == []
    assert ids(pc.get_page({"place_id_not_in": [1, 3]}, "", "place_id", "asc", 1)) == [2, 4, 5]


def test_became_available_range_is_inclusive_and_excludes_nulls():
    filters = {"became_from": date(2026, 10, 4), "became_to": date(2026, 10, 5)}
    assert ids(pc.get_page(filters, "", "place_id", "asc", 1)) == [1, 2]


def test_sort_puts_nulls_first_ascending_and_last_descending():
    assert ids(pc.get_page({}, "", "became_available", "asc", 1))[0] == 4
    assert ids(pc.get_page({}, "", "became_available", "desc", 1))[-1] == 4
    assert ids(pc.get_page({}, "", "city", "asc", 1)) == [5, 1, 2, 3, 4]  # case-insensitive, PlaceID tiebreak


def test_rows_come_back_as_plain_python_values():
    row = pc.get_page({"place_id_in": [2]}, "", "place_id", "asc", 1)["rows"][0]
    assert row["AddressUnits"] == 2 and isinstance(row["AddressUnits"], int)
    assert row["EmailAddress"] is None
    assert isinstance(row["BecameAvailable"], datetime)
    assert row["category"] == "none"


def test_kpis():
    k = pc.get_kpis({"became_from": date(2026, 10, 1), "became_to": date(2026, 10, 31)},
                    activity_place_ids=[3, 99])
    assert k["total_passings"] == 5          # footprint ignores the time range
    assert k["new_passings"] == 3            # 1, 2, 3 became available in October
    assert k["total_leads"] == 2
    assert k["residential_leads"] == 1 and k["commercial_hot_leads"] == 1 and k["commercial_field_targets"] == 1
    assert k["available_now"] == 3 and k["pre_order"] == 2
    assert k["actionable_total"] == 3 and k["actionable_with_activity"] == 1


def test_week_buckets_follow_sql_servers_sunday_boundary():
    buckets = pc.get_passings_over_time({}, "", "week")["buckets"]
    by_bucket = {b["bucket"]: b["new_passings"] for b in buckets}
    # Sun 10/4, Mon 10/5 and Sat 10/10 all land in the bucket of Mon 10/5
    # (a Sunday rolls forward to the next Monday, like DATEDIFF(WEEK)).
    assert by_bucket["2026-10-05T00:00:00"] == 3
    assert by_bucket["2026-08-31T00:00:00"] == 1  # Tue 9/1
    assert buckets[-1]["cumulative_passings"] == 4


def test_month_buckets_and_market_breakdown():
    months = pc.get_passings_over_time({}, "", "month")["buckets"]
    assert [(b["bucket"], b["new_passings"]) for b in months] == [("2026-09-01", 1), ("2026-10-01", 3)]
    markets = pc.get_market_breakdown({}, "", "state")["markets"]
    assert markets[0] == {"market": "NJ", "passings": 4, "leads": 2, "lead_penetration_pct": 50.0}


def test_incremental_form_rows_add_newer_emails_and_business_flags():
    df = pc._state["df"]
    updated = pc.apply_form_rows(df, [
        (10, 2, "new@lead.com", datetime(2026, 10, 20), 0),   # first email for place 2
        (11, 1, "old@x.com", datetime(2025, 1, 1), 0),        # older than place 1's email -> ignored
        (12, 5, None, datetime(2026, 10, 20), 1),             # business flag only
        (13, 999, "x@x.com", datetime(2026, 10, 20), 1),      # not a passing -> ignored
    ])
    assert updated is not df
    by_id = updated.set_index("PlaceID")
    assert by_id.loc[2, "EmailAddress"] == "new@lead.com" and by_id.loc[2, "_residential"]
    assert by_id.loc[1, "EmailAddress"] == "a@x.com"
    assert by_id.loc[5, "PrequalAsBusiness"] == 1
    assert pc.apply_form_rows(df, [(14, 999, "x@x.com", datetime(2026, 10, 20), 0)]) is df


def test_filter_options_are_distinct_and_sorted():
    options = pc.get_filter_options()
    assert options["state"] == ["NJ", "NY"]
    assert options["city"] == ["ALBANY", "NEWARK", "TRENTON", "Trenton"]
