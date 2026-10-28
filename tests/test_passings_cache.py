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
    # PlaceID, Email, EmailInsertDate, FullAddress, Street, City, State, Zip, Avail, BecameAvailable, Units, Biz, ClassName, TypeLabel, Phone
    (1, "a@x.com", datetime(2026, 1, 5), "1 Main St", "Main St", "NEWARK", "NJ", "07101", 1, datetime(2026, 10, 4), 1, 0, "Residential", None, "973-555-0101"),
    (2, None, None, "2 Main St", "Main St", "NEWARK", "NJ", "07101", 3, datetime(2026, 10, 5), 2.0, 0, "Residential", None, None),
    (3, "none", datetime(2026, 1, 5), "3 Oak Ave", "Oak Ave", "TRENTON", "NJ", "08601", 1, datetime(2026, 10, 10), None, 0, "Office", "Commercial", None),
    (4, "b@y.com", datetime(2026, 2, 1), "4 Elm Rd", "Elm Rd", "Trenton", "NJ", "08601", 1, None, 4, 1, "Retail", "Commercial", ""),
    (5, "  ", None, "5 Pine Ln", "Pine Ln", "ALBANY", "NY", "12201", 3, datetime(2026, 9, 1), 1, 0, None, None, None),
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
    # The Became Available range scopes every KPI: places 1, 2, 3.
    assert k["total_passings"] == 3
    assert k["new_passings"] == 3
    assert k["total_leads"] == 1
    assert k["residential_leads"] == 1 and k["commercial_hot_leads"] == 0 and k["commercial_field_targets"] == 1
    assert k["available_now"] == 2 and k["pre_order"] == 1
    assert k["actionable_total"] == 2 and k["actionable_with_activity"] == 1
    everything = pc.get_kpis({}, activity_place_ids=[])
    assert everything["total_passings"] == 5 and everything["actionable_total"] == 3


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
        (10, 2, "new@lead.com", datetime(2026, 10, 20), 0, "(201) 555-0102"),   # first email for place 2
        (11, 1, "old@x.com", datetime(2025, 1, 1), 0, "000"),        # older than place 1's email -> ignored
        (12, 5, None, datetime(2026, 10, 20), 1, None),             # business flag only
        (13, 999, "x@x.com", datetime(2026, 10, 20), 1, None),      # not a passing -> ignored
    ])
    assert updated is not df
    by_id = updated.set_index("PlaceID")
    assert by_id.loc[2, "EmailAddress"] == "new@lead.com" and by_id.loc[2, "_residential"]
    assert by_id.loc[1, "EmailAddress"] == "a@x.com"
    assert by_id.loc[5, "PrequalAsBusiness"] == 1
    assert by_id.loc[2, "PhoneNumber"] == "(201) 555-0102"  # comes with the newer email
    assert by_id.loc[1, "PhoneNumber"] == "973-555-0101"
    assert pc.apply_form_rows(df, [(14, 999, "x@x.com", datetime(2026, 10, 20), 0, None)]) is df


def test_filter_options_are_distinct_and_sorted():
    options = pc.get_filter_options()
    assert options["state"] == ["NJ", "NY"]
    assert options["city"] == ["ALBANY", "NEWARK", "TRENTON", "Trenton"]


def test_residential_marks_move_commercial_places_out_of_commercial_categories():
    marks = {"residential_ids": {3, 4}}  # 3 = field target (email 'none'), 4 = hot lead with an email
    page = lambda cat: ids(pc.get_page(dict(marks, category=cat), "", "place_id", "asc", 1))
    assert page("commercial_hot_lead") == []
    assert page("commercial_field_target") == []
    assert page("residential_lead") == [1, 4]   # 4 has a valid email
    assert page("none") == [2, 3, 5]
    assert ids(pc.get_page(dict(marks, property_type="Commercial"), "", "place_id", "asc", 1)) == []
    k = pc.get_kpis(marks, activity_place_ids=[])
    assert k["commercial_hot_leads"] == 0 and k["commercial_field_targets"] == 0
    assert k["residential_leads"] == 2 and k["actionable_total"] == 2
    # Prequal-as-Business itself is what the customer submitted -- unchanged.
    assert ids(pc.get_page(dict(marks, prequal_only=True), "", "place_id", "asc", 1)) == [4]
    # The shared snapshot is untouched; unmarked queries see it as before.
    assert ids(pc.get_page({"category": "commercial_hot_lead"}, "", "place_id", "asc", 1)) == [4]


def test_hubspot_call_filters_cover_leads_only():
    stats = {"b@y.com": {"calls": 4, "last_call_at": None, "last_call_date": date(2026, 10, 20)}}
    page = lambda **f: ids(pc.get_page(dict(f, hubspot_call_stats=stats), "", "place_id", "asc", 1))
    assert page() == [1, 2, 3, 4, 5]  # inactive filters leave everything in
    assert page(hubspot_called=True) == [4]
    assert page(hubspot_called=False) == [1]  # leads with no calls -- never non-leads
    assert page(hubspot_max_calls=0) == [1]  # 0 = never called (leads only)
    assert page(hubspot_max_calls=3) == [1]
    assert page(hubspot_max_calls=4) == [1, 4]
    assert page(hubspot_last_call_from=date(2026, 10, 20), hubspot_last_call_to=date(2026, 10, 20)) == [4]
    assert page(hubspot_last_call_from=date(2026, 10, 21)) == []
    # Email matching ignores case and surrounding spaces.
    upper = {"A@X.COM".lower(): {"calls": 1, "last_call_at": None, "last_call_date": date(2026, 10, 1)}}
    assert ids(pc.get_page({"hubspot_called": True, "hubspot_call_stats": upper}, "", "place_id", "asc", 1)) == [1]


def test_has_phone_filter():
    # Only place 1 has a usable number; "" / None show NA on the card.
    assert ids(pc.get_page({"has_phone": True}, "", "place_id", "asc", 1)) == [1]
    assert ids(pc.get_page({"has_phone": False}, "", "place_id", "asc", 1)) == [2, 3, 4, 5]


def test_pas_place_url():
    from queries import pas_place_url
    assert pas_place_url(204428) == "https://pas.planet.net/Places/Detail?place_id=204428#Summary"
    assert pas_place_url(204428.0) == "https://pas.planet.net/Places/Detail?place_id=204428#Summary"
    assert pas_place_url(None) is None and pas_place_url(float("nan")) is None


def test_order_status_best_order_per_place_wins():
    orders = pd.DataFrame.from_records([
        # ID, PlaceID, SubscriberUUID, StartDate, StatusID
        (10, 1, "UUID-A", datetime(2026, 9, 1), 8),      # billed in Vision -> completed despite Ready job
        (11, 1, None, datetime(2026, 12, 1), 8),
        (20, 3, None, datetime(2026, 10, 26), 8),        # install date today -> Needs Attention
        (30, 4, None, datetime(2026, 11, 2), 4),         # future -> Install Scheduled
        (31, 4, None, datetime(2026, 9, 2), 8),
        (40, 5, None, None, None),                       # no fiber job -> Not Scheduled
        (50, 6, None, datetime(2026, 8, 1), 16),         # only a cancelled job
        (60, 7, None, datetime(2025, 3, 1), 6),          # job Complete, not billed -> completed
    ], columns=["ID", "PlaceID", "SubscriberUUID", "StartDate", "StatusID"])
    statuses = pc.compute_order_statuses(orders, {"uuid-a"}, date(2026, 10, 26))
    assert statuses.to_dict() == {
        1: pc.ORDER_COMPLETED, 3: pc.ORDER_NEEDS_ATTENTION, 4: pc.ORDER_SCHEDULED,
        5: pc.ORDER_NOT_SCHEDULED, 6: pc.ORDER_CANCELLED, 7: pc.ORDER_COMPLETED,
    }
    page = lambda status: ids(pc.get_page({"order_status": status, "order_statuses": statuses}, "", "place_id", "asc", 1))
    assert page(pc.ORDER_NEEDS_ATTENTION) == [3]
    assert page(pc.NO_ORDER) == [2]  # passing 2 has no order at all
    assert page(pc.ORDER_COMPLETED) == [1]


def test_lead_phone_number_is_formatted_or_na():
    rows = {r["PlaceID"]: r for r in pc.get_page({}, "", "place_id", "asc", 1)["rows"]}
    assert (rows[1]["phone_display"], rows[1]["phone_tel"]) == ("(973) 555-0101", "+19735550101")
    assert rows[4]["phone_display"] is None  # blank -> the card shows NA
    import passings_data
    assert passings_data.format_phone("1 (973) 555-0101") == ("(973) 555-0101", "+19735550101")
    assert passings_data.format_phone("none") == (None, None)
    assert passings_data.format_phone("ripray232@aol.com") == (None, None)
    assert passings_data.format_phone("000-000-0000") == (None, None)
    assert passings_data.format_phone("9735790325ext.1203") == ("9735790325ext.1203", "97357903251203")
