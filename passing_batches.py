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
that property, and is never counted twice. The Monthly toggle does the same
by calendar month: a batch is a town's release month and Month 1 is that month.
The status line stays 2%, of the month in that view. The Include External toggle adds
door knockers' orders back, and every rate, status and the plateau follow.
"""

import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

import db
import parcel_map
import passings_data
from queries import internal_rep_codes

RELEASE_LINE = 2.0  # percent of the batch in a week
MIN_BATCH = 50      # below this, one sale swings the rate across the line
REFRESH_SECONDS = 1800
PAGE_SIZE = 50      # all-time batches run to 288 weeks, too big for one table

# Plateau: the Parcel Map curve's rule (static/js/parcel_map.js), on this
# tool's rate. The plateau is the average of `periods`, and needs `min_periods`
# of them. Decay ends at the first period whose `run`-period average sits
# within `band` of the plateau.
PLATEAU_RULES = {
    False: {"periods": range(53, 106), "min_periods": 26, "run": 13, "band": 0.05},  # weeks
    True: {"periods": range(13, 26), "min_periods": 6, "run": 3, "band": 0.2},       # months
}

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
_state = {"data": None, "loaded_at": 0.0, "built": {}, "built_key": None}


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def week_start(day):
    return day - timedelta(days=day.weekday())


def period_start(day, monthly=False):
    return day.replace(day=1) if monthly else week_start(day)


def _period_index(start, day, monthly):
    """Whole periods from the period beginning `start` to the one holding `day`."""
    if monthly:
        return (day.year - start.year) * 12 + day.month - start.month
    return (day - start).days // 7


def _nth_period_start(start, count, monthly):
    if monthly:
        months = start.month - 1 + count
        return date(start.year + months // 12, months % 12 + 1, 1)
    return start + timedelta(days=count * 7)


def _town(municipality, city):
    name = next((n.strip() for n in (municipality, city) if isinstance(n, str) and n.strip()), "Unknown")
    return name.title()


def _batch_row(town, start, place_ids, period_sales, today, monthly=False):
    passings = len(place_ids)
    weeks = []
    for week in range(_period_index(start, today, monthly) + 1):
        sales = period_sales[week]
        weeks.append({
            "number": week + 1,
            "sales": sales,
            "rate": sales / passings * 100,
            "final": _nth_period_start(start, week + 1, monthly) <= today,
        })

    # Week names repeat across years, so older batches carry theirs.
    year = "" if start.year == today.year else f", {start.year}"
    running = weeks[-1]
    if passings < MIN_BATCH:
        status = "small"
    else:
        status = "under" if running["rate"] < RELEASE_LINE else "above"

    return {
        "name": (
            f"{town} Batch {start.strftime('%b %Y')}" if monthly
            else f"{town} Batch Week {start.strftime('%b')} {start.day}{year}"
        ),
        "town": town,
        "week_of": start,
        "passings": passings,
        "place_ids": place_ids,
        "total_sales": sum(week["sales"] for week in weeks),
        "total_rate": sum(week["rate"] for week in weeks),
        "weeks": weeks,
        "status": status,
        "running": running,
    }


def build_batches(passings, sales, today, monthly=False):
    """Batches newest first, from [(PlaceID, BecameAvailable, Municipality,
    City), ...] and {place_id: ["YYYY-MM-DD", ...]}, all time."""
    releases = {}
    period_sales = {}
    for place_id, became_available, municipality, city in passings:
        released = _as_date(became_available)
        if released is None or released > today:
            continue
        start = period_start(released, monthly)
        key = (_town(municipality, city), start)
        releases.setdefault(key, []).append(place_id)
        counts = period_sales.setdefault(key, Counter())
        for sale in sales.get(place_id, []):
            sold = date.fromisoformat(sale)
            if sold >= released:
                counts[_period_index(start, sold, monthly)] += 1

    batches = [
        _batch_row(town, start, releases[(town, start)], period_sales[(town, start)], today, monthly)
        for town, start in releases
    ]
    batches.sort(key=lambda b: (b["week_of"], b["passings"]), reverse=True)
    return batches


def all_sale_dates(order_rows):
    """organic_sale_dates() with door knockers' orders kept."""
    sales = {}
    for property_id, _referral_code, sale_date in order_rows:
        sales.setdefault(property_id, []).append(str(sale_date)[:10])
    return sales


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
    }


def plateau(batches, monthly=False):
    """{"rate", "decay_ends", "first", "last"} from every finished period of
    every batch, or None without enough history. A period's rate is its sales
    over the passings of the batches that finished it, so big batches weigh
    more."""
    rule = PLATEAU_RULES[monthly]
    sales, passings = Counter(), Counter()
    for batch in batches:
        for week in batch["weeks"]:
            if week["final"]:
                sales[week["number"]] += week["sales"]
                passings[week["number"]] += batch["passings"]

    weeks = [week for week in rule["periods"] if passings[week]]
    if len(weeks) < rule["min_periods"]:
        return None
    rate = sum(sales[week] for week in weeks) / sum(passings[week] for week in weeks) * 100

    rates = [sales[week] / passings[week] * 100 if passings[week] else None for week in range(1, rule["periods"].stop)]
    decay_ends = None
    for start in range(len(rates) - rule["run"] + 1):
        run = rates[start:start + rule["run"]]
        if None in run:
            continue
        if abs(sum(run) / rule["run"] - rate) <= rule["band"] and abs(run[0] - rate) <= rule["band"] * 3:
            decay_ends = start + 1
            break
    return {"rate": rate, "decay_ends": decay_ends, "first": rule["periods"].start, "last": rule["periods"].stop - 1}


def _fetch():
    conn = db.get_planetweb_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(parcel_map._REP_TOKENS_QUERY)
        rep_codes = internal_rep_codes(row[0] for row in cursor.fetchall())
        cursor.execute(_ORDERS_QUERY)
        orders = cursor.fetchall()
        cursor.execute(_PASSINGS_QUERY)
        passings = [tuple(row) for row in cursor.fetchall()]
        return (
            [row[:4] for row in passings],
            sales_by_passing(passings, parcel_map.organic_sale_dates(orders, rep_codes)),
            sales_by_passing(passings, all_sale_dates(orders)),
        )
    finally:
        conn.close()


def load_batches(include_external=False, monthly=False):
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
        today = passings_data._today()
        if _state["built_key"] != (_state["loaded_at"], today):
            _state["built"] = {}
            _state["built_key"] = (_state["loaded_at"], today)
        key = (include_external, monthly)
        if key not in _state["built"]:
            _state["built"][key] = build_batches(data[0], data[2 if include_external else 1], today, monthly)
        return _state["built"][key], error
