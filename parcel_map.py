"""Parcel Map (Tools menu, added 2026-11-05) -- every NJ tax parcel outline
with passings highlighted.

Parcel outlines are not stored here: static/js/parcel_map.js reads only the
parcels in view, straight from the state's public service (NJ_PARCELS_URL,
about 3.5M parcels). The boundaries are not legal property lines. This
module serves the other half, the NJ passing points, using the same
passings_base definition as /passings. A point inside a parcel marks that
parcel as a passing (199 of 200 sampled points fell inside a parcel).

Each point also carries its Organic sale dates, for the cards above the map.
Organic = every quoted order that isn't a door knocker's, all time. The
dashboard's own sales data can't be reused: it starts at 2026-01-01, and
passings go back to 2021.

Points also carry the day door knockers reached their town, which ends a
passing's clean window on the Organic curve (see the Territory Release Timing
report). That day is the town's first knocker order from PROGRAM_START on.
This reproduces the report's Hopatcong arrival, 2026-03-16.
"""

import threading
import time

import db
import passings_data
from queries import INTERNAL_REP_TOKEN_MARKER, internal_rep_codes, is_door_knocker_code

NJ_PARCELS_URL = (
    "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/"
    "Parcels_Composite_NJ_WM/FeatureServer/0/query"
)
REFRESH_SECONDS = 1800
# Doorknocking began here, so knocker-coded orders before it are not arrivals.
PROGRAM_START = "2026-02-27"

_POINTS_QUERY = (
    f"{passings_data._BASE_CTE} SELECT PlaceID, Latitude, Longitude, ReleaseDate, Municipality FROM passings_base "
    "WHERE State = 'NJ' AND Latitude IS NOT NULL AND Longitude IS NOT NULL"
)

_ORDERS_QUERY = """
SELECT NJPR_PlaceID, ReferralData, CAST(InsertDate AS date)
FROM [PlanetWeb].[dbo].[FTTPFormData]
WHERE QuoteID IS NOT NULL AND NJPR_PlaceID IS NOT NULL
"""
_REP_TOKENS_QUERY = f"""
SELECT DISTINCT SourceToken
FROM [PlanetWeb].[dbo].[FTTPFormData]
WHERE InsertByEmployeeID IS NOT NULL AND SourceToken LIKE '{INTERNAL_REP_TOKEN_MARKER}%'
"""

# Pre-2026 promo codes that look like a knocker's first initial + last name.
# queries.is_door_knocker_code() was only tuned against 2026 orders.
OLD_PROMO_CODES = frozenset({
    "welcometofranklin",
    "welcomefranklin",
    "doorhanger",
    "onefreemonth",
    "planetadcode",
    "thisisawonkycode",
    "blackfriday",
    "thankyou",
    "greenremedies",
    "nocode",
})

_lock = threading.Lock()
_state = {"points": None, "loaded_at": 0.0}


def _not_knockers(rep_codes):
    return frozenset(rep_codes) | OLD_PROMO_CODES


def _town_key(town):
    return town.strip().upper() if isinstance(town, str) and town.strip() else None


def rep_arrivals(order_rows, rep_codes, town_by_place):
    """{town: "YYYY-MM-DD"}, the first knocker order on or after PROGRAM_START
    in each town, from [(PlaceID, ReferralData, sale date), ...]."""
    not_knockers = _not_knockers(rep_codes)
    arrivals = {}
    for place_id, referral_code, sale_date in order_rows:
        town = town_by_place.get(place_id)
        day = str(sale_date)[:10]
        if town is None or day < PROGRAM_START or not is_door_knocker_code(referral_code, not_knockers):
            continue
        if town not in arrivals or day < arrivals[town]:
            arrivals[town] = day
    return arrivals


def first_knocker_orders(order_rows, rep_codes):
    """{place_id: "YYYY-MM-DD"}, each place's first knocker order. With the
    curve's after-knockers view on, a passing leaves the curve here: a house
    a knocker sold can't become an Organic order."""
    not_knockers = _not_knockers(rep_codes)
    firsts = {}
    for place_id, referral_code, sale_date in order_rows:
        if is_door_knocker_code(referral_code, not_knockers):
            day = str(sale_date)[:10]
            if place_id not in firsts or day < firsts[place_id]:
                firsts[place_id] = day
    return firsts


def organic_sale_dates(order_rows, rep_codes):
    """{place_id: ["YYYY-MM-DD", ...]}, one date per Organic sale, from
    [(PlaceID, ReferralData, sale date), ...]."""
    not_knockers = _not_knockers(rep_codes)
    sales = {}
    for place_id, referral_code, sale_date in order_rows:
        if not is_door_knocker_code(referral_code, not_knockers):
            sales.setdefault(place_id, []).append(str(sale_date)[:10])
    return sales


def to_points(rows, organic_sales, arrivals, knocker_orders):
    """[(PlaceID, lat, lng, became_available, town), ...] from the database ->
    JSON-ready [[place_id, lat, lng, "YYYY-MM-DD" or None, [sale dates],
    knocker arrival "YYYY-MM-DD" or None, first knocker order "YYYY-MM-DD" or
    None], ...]. Drops rows without a usable location."""
    points = []
    for place_id, lat, lng, became_available, town in rows:
        if place_id is None or not lat or not lng:
            continue
        available = became_available.date().isoformat() if became_available else None
        points.append([
            int(place_id), round(float(lat), 6), round(float(lng), 6), available,
            organic_sales.get(place_id, []), arrivals.get(_town_key(town)), knocker_orders.get(place_id),
        ])
    return points


def _fetch_points():
    conn = db.get_planetweb_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(_REP_TOKENS_QUERY)
        rep_codes = internal_rep_codes(row[0] for row in cursor.fetchall())
        cursor.execute(_ORDERS_QUERY)
        orders = cursor.fetchall()
        cursor.execute(_POINTS_QUERY)
        rows = cursor.fetchall()
        town_by_place = {row[0]: _town_key(row[4]) for row in rows}
        return to_points(
            rows,
            organic_sale_dates(orders, rep_codes),
            rep_arrivals(orders, rep_codes, town_by_place),
            first_knocker_orders(orders, rep_codes),
        )
    finally:
        conn.close()


def load_points():
    """(points, error). Refreshes every REFRESH_SECONDS. If a refresh
    fails, the last good points are returned with the error."""
    with _lock:
        if _state["points"] is not None and time.time() - _state["loaded_at"] < REFRESH_SECONDS:
            return _state["points"], None
        try:
            _state["points"] = _fetch_points()
            _state["loaded_at"] = time.time()
            return _state["points"], None
        except Exception as exc:
            print(f"[parcel_map] Could not load passing points: {exc}")
            return _state["points"], "Could not load passings."
