"""Passing Batches (Tools menu, added 2026-11-06) -- passings grouped into
weekly release batches per town, with Organic sales for each calendar week,
as a share of the whole batch.

A batch is every passing a town released in one Monday-Sunday week. Week 1 is
that release week and each later week is the next Monday-Sunday (by request,
so a batch only ever has one running week). A late-week release therefore has
a short Week 1, and more of its early sales land in Week 2. A week is final
once its Sunday has passed. Status compares the running week, the current
calendar week, with the release line. Organic is parcel_map's rule: every
quoted order that isn't a door knocker's. Orders link to passings by property
ID. A few passings share one, so a sale goes to the first passing released on
that property, and is never counted twice.
"""

import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

import db
import parcel_map
import passings_data
from queries import internal_rep_codes

DAYS_BACK = 90
RELEASE_LINE = 2.0  # percent of the batch in a week
MIN_BATCH = 50      # below this, one sale swings the rate across the line
REFRESH_SECONDS = 1800

_PASSINGS_QUERY = (
    f"{passings_data._BASE_CTE} SELECT PlaceID, BecameAvailable, Municipality, City, PropertyID "
    "FROM passings_base WHERE BecameAvailable IS NOT NULL"
)
_ORDERS_QUERY = """
SELECT LTRIM(RTRIM(Property_ID)), ReferralData, CAST(InsertDate AS date)
FROM [PlanetWeb].[dbo].[FTTPFormData]
WHERE QuoteID IS NOT NULL AND NULLIF(LTRIM(RTRIM(Property_ID)), '') IS NOT NULL
"""

_lock = threading.Lock()
_state = {"data": None, "loaded_at": 0.0}


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def week_start(day):
    return day - timedelta(days=day.weekday())


def _town(municipality, city):
    name = next((n.strip() for n in (municipality, city) if isinstance(n, str) and n.strip()), "Unknown")
    return name.title()


def _batch_row(town, monday, place_ids, weekly_sales, today):
    passings = len(place_ids)
    weeks = []
    for week in range((today - monday).days // 7 + 1):
        sales = weekly_sales[week]
        weeks.append({
            "number": week + 1,
            "sales": sales,
            "rate": sales / passings * 100,
            "final": monday + timedelta(days=week * 7 + 6) < today,
        })

    running = weeks[-1]
    if passings < MIN_BATCH:
        status = "small"
    else:
        status = "under" if running["rate"] < RELEASE_LINE else "above"

    return {
        "name": f"{town} Batch Week {monday.strftime('%b')} {monday.day}",
        "town": town,
        "week_of": monday,
        "passings": passings,
        "place_ids": place_ids,
        "organic_sales": sum(week["sales"] for week in weeks),
        "organic_rate": sum(week["rate"] for week in weeks),
        "weeks": weeks,
        "status": status,
        "running": running,
    }


def build_batches(passings, organic_sales, today):
    """Batches newest first, from [(PlaceID, BecameAvailable, Municipality,
    City), ...] and {place_id: ["YYYY-MM-DD", ...]}. Whole batches only: the
    window starts on the Monday of the week DAYS_BACK ago."""
    cutoff = week_start(today - timedelta(days=DAYS_BACK))
    releases = {}
    weekly_sales = {}
    for place_id, became_available, municipality, city in passings:
        released = _as_date(became_available)
        if released is None or not cutoff <= released <= today:
            continue
        monday = week_start(released)
        key = (_town(municipality, city), monday)
        releases.setdefault(key, []).append(place_id)
        counts = weekly_sales.setdefault(key, Counter())
        for sale in organic_sales.get(place_id, []):
            sold = date.fromisoformat(sale)
            if sold >= released:
                counts[(sold - monday).days // 7] += 1

    batches = [
        _batch_row(town, monday, releases[(town, monday)], weekly_sales[(town, monday)], today)
        for town, monday in releases
    ]
    batches.sort(key=lambda b: (b["week_of"], b["passings"]), reverse=True)
    return batches


def sales_by_passing(passings, sales_by_property):
    """{place_id: sale dates} from [(PlaceID, BecameAvailable, Municipality,
    City, PropertyID), ...] and {property_id: sale dates}. Each property's
    sales go to its first-released passing (lowest PlaceID on a tie)."""
    first_passing = {}
    for place_id, became_available, _municipality, _city, property_id in passings:
        property_id = property_id.strip() if isinstance(property_id, str) else ""
        if not property_id:
            continue
        rank = (_as_date(became_available), place_id)
        if property_id not in first_passing or rank < first_passing[property_id]:
            first_passing[property_id] = rank
    return {
        rank[1]: sales_by_property[property_id]
        for property_id, rank in first_passing.items()
        if property_id in sales_by_property
    }


def find_batch(batches, town, week):
    """The batch for a town and its release Monday ("YYYY-MM-DD"), or None."""
    return next((b for b in batches if b["town"] == town and b["week_of"].isoformat() == week), None)


def summarize(batches):
    statuses = Counter(batch["status"] for batch in batches)
    return {
        "batches": len(batches),
        "passings": sum(batch["passings"] for batch in batches),
        "under": statuses["under"],
        "above": statuses["above"],
        "week_columns": max((len(batch["weeks"]) for batch in batches), default=0),
    }


def _fetch():
    conn = db.get_planetweb_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(parcel_map._REP_TOKENS_QUERY)
        rep_codes = internal_rep_codes(row[0] for row in cursor.fetchall())
        cursor.execute(_ORDERS_QUERY)
        sales_by_property = parcel_map.organic_sale_dates(cursor.fetchall(), rep_codes)
        cursor.execute(_PASSINGS_QUERY)
        passings = [tuple(row) for row in cursor.fetchall()]
        return [row[:4] for row in passings], sales_by_passing(passings, sales_by_property)
    finally:
        conn.close()


def load_batches():
    """(batches, error). Reads PlanetWeb every REFRESH_SECONDS. If a refresh
    fails, the last good data is used with the error."""
    error = None
    with _lock:
        if _state["data"] is None or time.time() - _state["loaded_at"] >= REFRESH_SECONDS:
            try:
                _state["data"] = _fetch()
                _state["loaded_at"] = time.time()
            except Exception as exc:
                print(f"[passing_batches] Could not load batches: {exc}")
                error = "Could not load the latest passings."
        data = _state["data"]
    if data is None:
        return None, error
    return build_batches(data[0], data[1], passings_data._today()), error
