"""Tests for parcel_map.py -- the NJ passing points behind the Parcel Map.

Run with: pytest tests/test_parcel_map.py -v
"""

from datetime import date, datetime
from decimal import Decimal

import parcel_map


def test_to_points_converts_database_values():
    rows = [(12, Decimal("40.9512345678"), Decimal("-74.6612345678"), datetime(2025, 3, 9, 14, 30), " Sparta ")]
    points = parcel_map.to_points(rows, {12: ["2025-04-01"]}, {"SPARTA": "2026-05-04"}, {12: "2026-06-01"})
    assert points == [[12, 40.951235, -74.661235, "2025-03-09", ["2025-04-01"], "2026-05-04", "2026-06-01"]]


def test_to_points_keeps_a_passing_with_no_date():
    rows = [(12, Decimal("40.9"), Decimal("-74.6"), None, None)]
    assert parcel_map.to_points(rows, {}, {}, {}) == [[12, 40.9, -74.6, None, [], None, None]]


def test_to_points_drops_rows_without_a_location():
    day = datetime(2025, 3, 9)
    rows = [(1, None, Decimal("-74.6"), day, "A"), (2, Decimal("40.9"), None, day, "A"), (3, Decimal("0"), Decimal("0"), day, "A"), (None, Decimal("40.9"), Decimal("-74.6"), day, "A")]
    assert parcel_map.to_points(rows, {}, {}, {}) == []


def test_organic_sales_leave_out_door_knockers_only():
    day = date(2026, 3, 9)
    rows = [
        (1, None, day),               # no code: rep or website order
        (1, "welcometoplanet", day),  # promo
        (1, "iponce", day),           # Issac Ponce: internal, not a knocker
        (1, "dzupkay", day),          # internal rep's own name code
        (1, "tharshbarger", day),     # door knocker
        (2, "mritchie", day),         # listed door knocker
    ]
    assert parcel_map.organic_sale_dates(rows, {"dzupkay"}) == {1: ["2026-03-09"] * 4}


def test_old_promo_codes_are_not_door_knockers():
    rows = [(1, "welcometofranklin", date(2023, 5, 1)), (1, "DoorHanger", "2023-06-02")]
    assert parcel_map.organic_sale_dates(rows, set()) == {1: ["2023-05-01", "2023-06-02"]}


def test_rep_arrival_is_the_first_knocker_order_of_the_program():
    towns = {1: "SPARTA", 2: "SPARTA", 3: "NEWTON"}
    rows = [
        (1, "mritchie", date(2024, 4, 17)),  # before doorknocking began
        (1, "mritchie", date(2026, 5, 9)),
        (2, "mritchie", date(2026, 5, 4)),
        (3, None, date(2026, 3, 1)),         # not a knocker order
        (9, "mritchie", date(2026, 3, 1)),   # not a passing
    ]
    assert parcel_map.rep_arrivals(rows, set(), towns) == {"SPARTA": "2026-05-04"}


def test_first_knocker_order_per_place():
    rows = [
        (1, "mritchie", date(2026, 6, 2)),
        (1, "mritchie", date(2026, 5, 4)),
        (1, None, date(2026, 1, 1)),  # Organic, ignored
        (2, "iponce", date(2026, 3, 1)),  # internal rep, ignored
    ]
    assert parcel_map.first_knocker_orders(rows, set()) == {1: "2026-05-04"}


def test_points_use_the_passings_definition_in_nj_only():
    assert "p.IsPlanetImproved = 1" in parcel_map._POINTS_QUERY
    assert "State = 'NJ'" in parcel_map._POINTS_QUERY


def test_failed_refresh_keeps_last_good_points(monkeypatch):
    monkeypatch.setitem(parcel_map._state, "points", [[1, 40.9, -74.6]])
    monkeypatch.setitem(parcel_map._state, "loaded_at", 0.0)

    def fail():
        raise RuntimeError("database down")

    monkeypatch.setattr(parcel_map, "_fetch_points", fail)
    assert parcel_map.load_points() == ([[1, 40.9, -74.6]], "Could not load passings.")
