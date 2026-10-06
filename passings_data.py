"""Passings & Leads -- PlanetWeb data access (README.md "Passings & Leads").

Reads directly from PlanetWeb SQL Server's [dbo].[View_Places] and
[dbo].[FTTPFormData] via db.get_planetweb_connection() -- the SAME
connection every other PlanetWeb query in this app already uses. Read-only,
like every other source-database query in this app: nothing here ever
executes anything but SELECT.

The base row shape (which PlaceIDs qualify as a Passing, how the email/
Prequal-as-Business/BecameAvailable columns are derived) is copied verbatim
from the existing campaign-list stored procedure the user provided,
`dbo.SP_CampaignEmailListBuilder_24`:

    WHERE p.ID IS NOT NULL
      AND p.As_AvailabilityID IN (1, 3)
      AND p.USPSPropertyAddress IS NOT NULL
      AND p.IsPlanetImproved = 1

    EmailAddress      -- TOP 1 most recent non-blank FTTPFormData.EmailAddress
                         for the PlaceID, newest InsertDate first.
    PrequalAsBusiness -- EXISTS a FTTPFormData row for this PlaceID with
                         IsBusiness = 1 (the SP's #TempTableFormData).
    BecameAvailable   -- COALESCE(As_DateFirstAvailability1,
                         As_DateFirstAvailability3, As_AvailabilityModifiedDate,
                         PP_UpdatedDate, PP_InsertDate) -- same fallback
                         chain as the SP's AuxVar1 CASE expression, kept as
                         a real date/datetime (not the SP's formatted
                         display string) since this app filters, buckets,
                         and sorts on it -- see spec "AuxVar1" section.

This module never loads the whole PlanetWeb result set into memory or into
the browser -- every list/table view is server-side paginated
(OFFSET/FETCH), every KPI/chart is a SQL aggregate query, and every filter/
search value is a parameterized `?` placeholder, same conventions as
marketing_data.py (the most recent precedent for "browse a raw PlanetWeb
source view with server-side paging").

Classification (is this row a Lead? which of the three prospecting
categories?) is never reimplemented here by hand -- every SQL predicate
that depends on it (category filters, KPI SUM(CASE...) columns, the
commercial-property LIKE clauses) is built FROM
passings_classification.COMMERCIAL_PROPERTY_KEYWORDS/is_valid_email() so
there is exactly one keyword list and one "what counts as a valid email"
rule in this codebase, not a second copy drifting in SQL. passings_data.py
also calls passings_classification.classify() in Python on each already-
fetched page of rows, purely to attach display fields (badge labels,
primary action) -- that call never affects which rows are IN the page or
how many total rows there are; the SQL predicates above already guarantee
that.

Cross-database filters (Has Activity/No Activity, Called, Visited, Rep,
Last Activity date range) can't be a single SQL join -- the activity log
lives in a separate Postgres database (appdb, see passings_activity_store.py),
not on this SQL Server. app.py resolves those filters into a place_id
allow-/deny-list via passings_activity_store first, then passes them in
here as `place_id_in`/`place_id_not_in` -- see _membership_sql() below for
how that becomes a chunked, parameterized `PlaceID IN (...)` clause (SQL
Server caps a query at ~2100 parameters, so large ID sets are chunked and
OR'd/AND'd together rather than sent as one clause)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import db
import passings_classification

# Same reasoning as sales_metrics.py's/marketing_metrics.py's own
# EASTERN_TZ/_today(): the app server's clock runs in UTC, but "today" for
# a time-range control must anchor to Eastern local time or the window can
# silently drift by a day right around midnight UTC. Defined locally
# (rather than importing sales_metrics) to keep this module's only
# dependency on the DataFrame-based pipeline at zero, same as
# marketing_metrics.py already does.
EASTERN_TZ = ZoneInfo("America/New_York")


def _today():
    return datetime.now(EASTERN_TZ).date()


PAGE_SIZE = 50

TIME_RANGE_OPTIONS = [
    ("7d", "7 Days"),
    ("30d", "30 Days"),
    ("90d", "90 Days"),
    ("ytd", "YTD"),
    ("1y", "1 Year"),
    ("all_time", "All Time"),
    ("custom", "Custom Range"),
]

_CHUNK_SIZE = 1900  # stays under SQL Server's ~2100 query-parameter ceiling


def resolve_time_range(range_key, custom_start=None, custom_end=None):
    """(start_date, end_date) for a TIME_RANGE_OPTIONS key -- both None for
    all_time/unrecognized (no AuxVar1 restriction). custom_start/custom_end
    are 'YYYY-MM-DD' strings straight from the query string; an unparsable
    or partial custom range also falls back to (None, None) -- "no
    restriction" is always a safe degradation, never a crash."""
    today = _today()
    if range_key == "7d":
        return today - timedelta(days=7), today
    if range_key == "30d":
        return today - timedelta(days=30), today
    if range_key == "90d":
        return today - timedelta(days=90), today
    if range_key == "ytd":
        return date(today.year, 1, 1), today
    if range_key == "1y":
        return today - timedelta(days=365), today
    if range_key == "custom":
        try:
            start = datetime.strptime(custom_start, "%Y-%m-%d").date() if custom_start else None
            end = datetime.strptime(custom_end, "%Y-%m-%d").date() if custom_end else None
        except (TypeError, ValueError):
            return None, None
        return start, end
    return None, None


def auto_granularity(start, end):
    """day/week/month bucket size for a time-series chart, picked by range
    length so "7 Days" isn't one bar and "All Time" isn't thousands."""
    if start is None or end is None:
        return "month"
    days = (end - start).days
    if days <= 45:
        return "day"
    if days <= 210:
        return "week"
    return "month"


# ============================================================
# Base CTE -- the ONE place the SP's row shape is reproduced. Every query
# function below opens with "WITH passings_base AS (_BASE_CTE) SELECT ...".
#
# PropertyTypeLabel is computed HERE, once, as a real column (rather than
# a LIKE predicate re-parameterized at every call site) specifically
# because a single query below (get_kpis()) needs "is this row commercial"
# up to four times -- re-emitting/re-parameterizing that predicate that
# many times is exactly how a param-count/order mismatch bug creeps in.
# One column, referenced by plain equality/IS NULL everywhere downstream,
# makes that whole bug class impossible. Keywords come from
# passings_classification.COMMERCIAL_PROPERTY_KEYWORDS -- fixed,
# developer-authored constants (never user input), so they're safe to
# inline as SQL string literals (still defensively quote-escaped).
# ============================================================

def _property_type_case_sql():
    when_clauses = []
    for label, keywords in passings_classification.COMMERCIAL_PROPERTY_KEYWORDS:
        escaped_label = label.replace("'", "''")
        keyword_checks = " OR ".join(
            "LOWER(p.ClassName) LIKE '%{}%'".format(keyword.replace("'", "''"))
            for keyword in keywords
        )
        when_clauses.append(f"WHEN ({keyword_checks}) THEN '{escaped_label}'")
    return "CASE " + " ".join(when_clauses) + " ELSE NULL END"


_BASE_CTE = f"""
WITH latest_email AS (
    -- Was a per-row correlated subquery (SELECT TOP 1 ... WHERE
    -- NJPR_PlaceID = p.ID ORDER BY InsertDate DESC) evaluated once for
    -- EVERY qualifying View_Places row -- fine for a selective category
    -- (e.g. Commercial Hot Leads, ~2.5k rows) but a genuine N+1 query
    -- against FTTPFormData (221k rows) for a majority-share category
    -- (Residential Leads/Commercial Field Targets, ~58k rows each) --
    -- tens of thousands of individual correlated lookups, which is what
    -- was actually hanging the page (fixed 2026-08-21; the correlated
    -- version had a supporting index on NJPR_PlaceID and was still this
    -- slow purely from lookup COUNT, not a missing index). Same
    -- semantics (most recent InsertDate among rows with a real,
    -- non-blank email, per PlaceID), computed ONCE as a single windowed
    -- pass over FTTPFormData instead of once per View_Places row.
    SELECT NJPR_PlaceID, EmailAddress
    FROM (
        SELECT
            fd.NJPR_PlaceID,
            fd.EmailAddress,
            ROW_NUMBER() OVER (PARTITION BY fd.NJPR_PlaceID ORDER BY fd.InsertDate DESC) AS rn
        FROM [PlanetWeb].[dbo].[FTTPFormData] fd
        WHERE fd.NJPR_PlaceID IS NOT NULL
          AND fd.EmailAddress IS NOT NULL
          AND LTRIM(RTRIM(fd.EmailAddress)) <> ''
    ) ranked
    WHERE rn = 1
),
business_flag AS (
    -- Was a per-row correlated EXISTS subquery -- same N+1 problem and
    -- same fix as latest_email above: one DISTINCT scan over
    -- FTTPFormData instead of one EXISTS check per View_Places row.
    SELECT DISTINCT NJPR_PlaceID
    FROM [PlanetWeb].[dbo].[FTTPFormData]
    WHERE NJPR_PlaceID IS NOT NULL AND IsBusiness = 1
),
passings_base AS (
    SELECT
        p.ID AS PlaceID,
        le.EmailAddress AS EmailAddress,
        ISNULL(p.USPSPropertyAddress, p.PropertyLocation) AS FullAddress,
        ISNULL(p.USPSPropertyStreetName + ' ' + p.USPSPropertyStreetSuffix, p.PropertyLocation) AS Street,
        ISNULL(p.USPSPropertyCity, p.PropertyCity) AS City,
        ISNULL(p.USPSPropertyState, p.PropertyState) AS State,
        ISNULL(p.USPSPropertyZip, p.PropertyZip) AS ZipCode,
        p.As_AvailabilityID AS AvailabilityID,
        COALESCE(
            p.As_DateFirstAvailability1,
            p.As_DateFirstAvailability3,
            p.As_AvailabilityModifiedDate,
            p.PP_UpdatedDate,
            p.PP_InsertDate
        ) AS BecameAvailable,
        p.Counter_AddressUnits AS AddressUnits,
        CASE WHEN bf.NJPR_PlaceID IS NOT NULL THEN 1 ELSE 0 END AS PrequalAsBusiness,
        p.ClassName AS ClassName,
        {_property_type_case_sql()} AS PropertyTypeLabel
    FROM [PlanetWeb].[dbo].[View_Places] p
    LEFT JOIN latest_email le ON le.NJPR_PlaceID = p.ID
    LEFT JOIN business_flag bf ON bf.NJPR_PlaceID = p.ID
    WHERE p.ID IS NOT NULL
      AND p.As_AvailabilityID IN (1, 3)
      AND p.USPSPropertyAddress IS NOT NULL
      AND p.IsPlanetImproved = 1
)
"""

_EMAIL_VALID_SQL = (
    "(EmailAddress IS NOT NULL AND LTRIM(RTRIM(EmailAddress)) <> '' "
    "AND LOWER(LTRIM(RTRIM(EmailAddress))) <> 'none')"
)

_COMMERCIAL_LABELS = {label for label, _keywords in passings_classification.COMMERCIAL_PROPERTY_KEYWORDS}


def _property_type_sql(label):
    """(sql, params) for the Property Type filter -- a plain equality
    against the PropertyTypeLabel column computed in the base CTE, or
    (None, []) for an unrecognized label."""
    if label not in _COMMERCIAL_LABELS:
        return None, []
    return "PropertyTypeLabel = ?", [label]


def _category_sql(category):
    """(sql, params) implementing the exact classification tree in
    passings_classification.classify() as a SQL predicate. No params
    needed -- PropertyTypeLabel/PrequalAsBusiness/EmailAddress are all
    plain columns from the base CTE."""
    if category == "commercial_hot_lead":
        return "PrequalAsBusiness = 1", []
    if category == "commercial_field_target":
        return "(PrequalAsBusiness = 0 AND PropertyTypeLabel IS NOT NULL)", []
    if category == "residential_lead":
        return f"(PrequalAsBusiness = 0 AND PropertyTypeLabel IS NULL AND {_EMAIL_VALID_SQL})", []
    if category == "none":
        return f"(PrequalAsBusiness = 0 AND PropertyTypeLabel IS NULL AND NOT {_EMAIL_VALID_SQL})", []
    return None, []


def _membership_sql(column, ids, negate=False):
    """(sql, params) for `column IN (...)` / `column NOT IN (...)` over a
    (possibly large) id list, chunked to stay under SQL Server's ~2100
    query-parameter ceiling. Positive membership chunks are OR'd (in ANY
    chunk); negated membership chunks are AND'd (in NO chunk) -- see this
    module's docstring. `ids=None`/empty returns (None, []) -- "no
    restriction from this predicate", the caller just omits it."""
    if not ids:
        return None, []
    ids = sorted({int(i) for i in ids})
    chunks = [ids[i:i + _CHUNK_SIZE] for i in range(0, len(ids), _CHUNK_SIZE)]
    joiner = " AND " if negate else " OR "
    op = "NOT IN" if negate else "IN"
    parts, params = [], []
    for chunk in chunks:
        placeholders = ", ".join("?" for _ in chunk)
        parts.append(f"{column} {op} ({placeholders})")
        params.extend(chunk)
    sql = "(" + joiner.join(parts) + ")"
    return sql, params


def _build_where(filters, search):
    """Returns (sql, params) ANDing every active filter/search value on
    top of the base CTE's own WHERE (already baked in). `filters` keys are
    all optional -- see passings.html/app.py for the full set. Mirrors
    marketing_data._build_where()'s shape: every value is a `?` parameter,
    never string-interpolated, regardless of source."""
    filters = filters or {}
    clauses = ["1 = 1"]
    params = []

    category = filters.get("category")
    if category:
        sql, category_params = _category_sql(category)
        if sql:
            clauses.append(sql)
            params.extend(category_params)

    if filters.get("prequal_only"):
        clauses.append("PrequalAsBusiness = 1")

    property_type = filters.get("property_type")
    if property_type:
        sql, type_params = _property_type_sql(property_type)
        if sql:
            clauses.append(sql)
            params.extend(type_params)

    has_lead = filters.get("has_lead")
    if has_lead is True:
        clauses.append(_EMAIL_VALID_SQL)
    elif has_lead is False:
        clauses.append(f"NOT {_EMAIL_VALID_SQL}")

    city = filters.get("city")
    if city:
        clauses.append("City = ?")
        params.append(city)

    state = filters.get("state")
    if state:
        clauses.append("State = ?")
        params.append(state)

    zip_code = filters.get("zip")
    if zip_code:
        clauses.append("ZipCode = ?")
        params.append(zip_code)

    availability_id = filters.get("availability_id")
    if availability_id in (1, 3):
        clauses.append("AvailabilityID = ?")
        params.append(availability_id)

    became_from = filters.get("became_from")
    if became_from:
        clauses.append("BecameAvailable >= ?")
        params.append(became_from)
    became_to = filters.get("became_to")
    if became_to:
        # Inclusive of the whole `became_to` day -- compare against the
        # start of the NEXT day, same convention marketing_metrics.py's
        # fetch_leads() already uses for an inclusive end date.
        clauses.append("BecameAvailable < ?")
        params.append(became_to + timedelta(days=1))

    place_id_in = filters.get("place_id_in")
    if place_id_in is not None:
        sql, id_params = _membership_sql("PlaceID", place_id_in, negate=False)
        if sql:
            clauses.append(sql)
            params.extend(id_params)
        else:
            # An empty allow-list means nothing can match -- e.g. "Called"
            # filter with zero call activity logged anywhere yet.
            clauses.append("1 = 0")

    place_id_not_in = filters.get("place_id_not_in")
    if place_id_not_in:
        sql, id_params = _membership_sql("PlaceID", place_id_not_in, negate=True)
        if sql:
            clauses.append(sql)
            params.extend(id_params)

    if search:
        like_term = f"%{search}%"
        search_clauses = [
            "FullAddress LIKE ?",
            "Street LIKE ?",
            "City LIKE ?",
            "ZipCode LIKE ?",
            "EmailAddress LIKE ?",
            "CAST(PlaceID AS NVARCHAR(20)) LIKE ?",
        ]
        clauses.append("(" + " OR ".join(search_clauses) + ")")
        params.extend([like_term] * len(search_clauses))

    return " AND ".join(clauses), params


_SORT_COLUMNS = {
    "address": "FullAddress",
    "city": "City",
    "state": "State",
    "zip": "ZipCode",
    "email": "EmailAddress",
    "availability": "AvailabilityID",
    "units": "AddressUnits",
    "became_available": "BecameAvailable",
    "place_id": "PlaceID",
}


def _sort_sql(sort_key, sort_dir):
    column = _SORT_COLUMNS.get(sort_key, "BecameAvailable")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    # PlaceID as a tiebreaker keeps pagination stable across pages when
    # many rows share the same sort value (e.g. many rows with the same
    # BecameAvailable date).
    return f"ORDER BY {column} {direction}, PlaceID ASC"


def _connect_and_run(build_query, empty_result):
    """Shared connect/execute/error-handling shell -- every public
    function below hands this a `build_query(cursor)` callback and gets
    back either its return value or `empty_result` on any failure, with
    the same sanitized-error/console-log behavior marketing_data.py
    already established. Centralizing this means every new query function
    added here doesn't need to hand-roll its own try/except/finally."""
    conn = None
    try:
        conn = db.get_planetweb_connection()
        cursor = conn.cursor()
        return build_query(cursor)
    except Exception as exc:
        print("=" * 60)
        print("PASSINGS & LEADS QUERY FAILED")
        print("=" * 60)
        print(f"Server:   {db.PLANETWEB_HOST}")
        print(f"Database: {db.PLANETWEB_DATABASE}")
        print(f"Error:    {exc}")
        print("=" * 60)
        if isinstance(empty_result, dict):
            result = dict(empty_result)
            result["error"] = db.sanitize_error(exc)
            return result
        return empty_result
    finally:
        if conn is not None:
            conn.close()


_ROW_COLUMNS = [
    "PlaceID", "EmailAddress", "FullAddress", "Street", "City", "State", "ZipCode",
    "AvailabilityID", "BecameAvailable", "AddressUnits", "PrequalAsBusiness", "ClassName",
]


def _row_to_dict(row):
    record = dict(zip(_ROW_COLUMNS, row))
    classification = passings_classification.classify({
        "email": record["EmailAddress"],
        "prequal_as_business": record["PrequalAsBusiness"],
        "class_name": record["ClassName"],
    })
    record.update(classification)
    record["availability_label"] = passings_classification.availability_label(record["AvailabilityID"])
    return record


def get_filter_options(filters=None):
    """Distinct City/State/ZipCode/Property Type values for the filter
    dropdowns, scoped to the base qualifying-Passing population (NOT
    cross-filtered by the other active filters -- same convention
    marketing_data.get_filter_options()/app.py's unique_sorted_values()
    already use for their own dropdowns). Returns an empty dict (never
    raises) on failure -- the page still renders, only the dropdowns go
    empty."""
    empty = {"city": [], "state": [], "property_type": []}

    def run(cursor):
        options = dict(empty)
        cursor.execute(f"{_BASE_CTE} SELECT DISTINCT City FROM passings_base WHERE City IS NOT NULL AND City <> '' ORDER BY City")
        options["city"] = [row[0] for row in cursor.fetchall()]
        cursor.execute(f"{_BASE_CTE} SELECT DISTINCT State FROM passings_base WHERE State IS NOT NULL AND State <> '' ORDER BY State")
        options["state"] = [row[0] for row in cursor.fetchall()]
        options["property_type"] = [label for label, _keywords in passings_classification.COMMERCIAL_PROPERTY_KEYWORDS]
        return options

    return _connect_and_run(run, empty)


def get_page(filters, search, sort_key, sort_dir, page):
    """Paginated, classified rows for the card/table view. Returns a dict,
    always with every key present (never raises), same contract as
    marketing_data.get_marketing_form_data()."""
    where_sql, params = _build_where(filters, search)
    empty = {"ok": False, "error": None, "rows": [], "total": 0, "page": 1, "total_pages": 1, "page_size": PAGE_SIZE}

    def run(cursor):
        cursor.execute(f"{_BASE_CTE} SELECT COUNT(*) FROM passings_base WHERE {where_sql}", params)
        total = int(cursor.fetchone()[0] or 0)
        total_pages = max(1, -(-total // PAGE_SIZE))
        clamped_page = min(max(1, page), total_pages)
        offset = (clamped_page - 1) * PAGE_SIZE

        order_sql = _sort_sql(sort_key, sort_dir)
        select_list = ", ".join(f"[{c}]" for c in _ROW_COLUMNS)
        cursor.execute(
            f"{_BASE_CTE} SELECT {select_list} FROM passings_base WHERE {where_sql} "
            f"{order_sql} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
            params + [offset, PAGE_SIZE],
        )
        rows = [_row_to_dict(row) for row in cursor.fetchall()]
        return {"ok": True, "error": None, "rows": rows, "total": total, "page": clamped_page,
                "total_pages": total_pages, "page_size": PAGE_SIZE}

    return _connect_and_run(run, empty)


def get_kpis(filters, search=None, activity_place_ids=None):
    """One aggregate pass over `filters`/`search` (deliberately EXCLUDING
    any became_from/became_to -- see below) for the footprint-level KPIs,
    plus a second small aggregate restricted to the AuxVar1 window for New
    Passings.

    Total Passings / Total Leads / Lead Penetration / Residential Leads /
    Commercial Hot Leads / Commercial Field Targets / Outreach Coverage are
    deliberately NOT scoped to the Global Time Range -- they describe "the
    whole current footprint under whatever non-time filters/search are
    active" (same convention dashboard.html's Records/Calendar sections
    already use: "independent of the page's period filter"), but they DO
    reflect the active search term, same as admin/marketing_form_data.html's
    "Total Rows ... matching current search/filters" KPI. New Passings is
    the one time-boxed KPI (spec #10: "became available DURING the
    currently selected time range") -- if every KPI were time-boxed, New
    Passings and Total Passings would be redundant under any non-"All
    Time" range.

    `activity_place_ids` (optional, from passings_activity_store.get_all_
    place_ids_with_activity()) is used only to compute Outreach Coverage's
    numerator -- see _membership_sql()'s docstring for why this can't be a
    single cross-database join."""
    footprint_filters = {k: v for k, v in (filters or {}).items() if k not in ("became_from", "became_to")}
    where_sql, params = _build_where(footprint_filters, search)

    time_filters = dict(footprint_filters)
    time_filters["became_from"] = (filters or {}).get("became_from")
    time_filters["became_to"] = (filters or {}).get("became_to")
    time_where_sql, time_params = _build_where(time_filters, search)

    empty = {
        "ok": False, "error": None,
        "total_passings": 0, "total_leads": 0, "lead_penetration_pct": 0.0,
        "new_passings": 0,
        "residential_leads": 0, "commercial_hot_leads": 0, "commercial_field_targets": 0,
        "available_now": 0, "pre_order": 0,
        "actionable_total": 0, "actionable_with_activity": 0, "outreach_coverage_pct": 0.0,
    }

    def run(cursor):
        residential_sql = f"(PrequalAsBusiness = 0 AND PropertyTypeLabel IS NULL AND {_EMAIL_VALID_SQL})"
        target_sql = "(PrequalAsBusiness = 0 AND PropertyTypeLabel IS NOT NULL)"
        actionable_sql = f"(PrequalAsBusiness = 1 OR PropertyTypeLabel IS NOT NULL OR {_EMAIL_VALID_SQL})"

        activity_membership_sql, activity_membership_params = _membership_sql("PlaceID", activity_place_ids)
        activity_case = f"AND {activity_membership_sql}" if activity_membership_sql else "AND 1 = 0"

        query = f"""
        {_BASE_CTE}
        SELECT
            COUNT(DISTINCT PlaceID),
            SUM(CASE WHEN {_EMAIL_VALID_SQL} THEN 1 ELSE 0 END),
            SUM(CASE WHEN {residential_sql} THEN 1 ELSE 0 END),
            SUM(CASE WHEN PrequalAsBusiness = 1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN {target_sql} THEN 1 ELSE 0 END),
            SUM(CASE WHEN AvailabilityID = 1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN AvailabilityID = 3 THEN 1 ELSE 0 END),
            SUM(CASE WHEN {actionable_sql} THEN 1 ELSE 0 END),
            SUM(CASE WHEN {actionable_sql} {activity_case} THEN 1 ELSE 0 END)
        FROM passings_base
        WHERE {where_sql}
        """
        # Param order must match `?` occurrence order in the query TEXT
        # above: activity_case sits inside the SELECT list (which comes
        # before WHERE in the rendered SQL), so its params go first.
        query_params = activity_membership_params + params
        cursor.execute(query, query_params)
        (total_passings, total_leads, residential_leads, commercial_hot_leads,
         commercial_field_targets, available_now, pre_order,
         actionable_total, actionable_with_activity) = cursor.fetchone()

        total_passings = int(total_passings or 0)
        total_leads = int(total_leads or 0)
        actionable_total = int(actionable_total or 0)
        actionable_with_activity = int(actionable_with_activity or 0)

        cursor.execute(f"{_BASE_CTE} SELECT COUNT(DISTINCT PlaceID) FROM passings_base WHERE {time_where_sql}", time_params)
        new_passings = int(cursor.fetchone()[0] or 0)

        return {
            "ok": True, "error": None,
            "total_passings": total_passings,
            "total_leads": total_leads,
            "lead_penetration_pct": (total_leads / total_passings * 100) if total_passings else 0.0,
            "new_passings": new_passings,
            "residential_leads": int(residential_leads or 0),
            "commercial_hot_leads": int(commercial_hot_leads or 0),
            "commercial_field_targets": int(commercial_field_targets or 0),
            "available_now": int(available_now or 0),
            "pre_order": int(pre_order or 0),
            "actionable_total": actionable_total,
            "actionable_with_activity": actionable_with_activity,
            "outreach_coverage_pct": (actionable_with_activity / actionable_total * 100) if actionable_total else 0.0,
        }

    return _connect_and_run(run, empty)


def _bucket_expr(granularity):
    if granularity == "day":
        return "CAST(BecameAvailable AS DATE)"
    if granularity == "week":
        return "DATEADD(WEEK, DATEDIFF(WEEK, 0, BecameAvailable), 0)"
    return "DATEFROMPARTS(YEAR(BecameAvailable), MONTH(BecameAvailable), 1)"


def get_passings_over_time(filters, search, granularity):
    """New Passings + New Leads per bucket (day/week/month, auto-picked by
    the caller via auto_granularity()), ordered oldest-first, plus a
    running cumulative total -- backs both "New Passings Over Time" and
    "Total Serviceable Passings Over Time" (cumulative) with one query,
    and "New Passings vs New Leads Over Time" (spec #14) from the same
    rows. `filters` should include became_from/became_to (the Global Time
    Range) -- see this module's docstring for why charts are always
    time-boxed even though most KPIs aren't."""
    where_sql, params = _build_where(filters, search)
    bucket_sql = _bucket_expr(granularity)
    empty = {"ok": False, "error": None, "buckets": []}

    def run(cursor):
        cursor.execute(
            f"""
            {_BASE_CTE}
            SELECT {bucket_sql} AS Bucket,
                   COUNT(DISTINCT PlaceID) AS NewPassings,
                   SUM(CASE WHEN {_EMAIL_VALID_SQL} THEN 1 ELSE 0 END) AS NewLeads
            FROM passings_base
            WHERE {where_sql} AND BecameAvailable IS NOT NULL
            GROUP BY {bucket_sql}
            ORDER BY Bucket ASC
            """,
            params,
        )
        rows = cursor.fetchall()
        cumulative = 0
        buckets = []
        for bucket, new_passings, new_leads in rows:
            new_passings = int(new_passings or 0)
            cumulative += new_passings
            buckets.append({
                "bucket": bucket.isoformat() if hasattr(bucket, "isoformat") else str(bucket),
                "new_passings": new_passings,
                "new_leads": int(new_leads or 0),
                "cumulative_passings": cumulative,
            })
        return {"ok": True, "error": None, "buckets": buckets}

    return _connect_and_run(run, empty)


def get_market_breakdown(filters, search, dimension="city", top_n=15):
    """Passings/Leads/Lead Penetration grouped by City or State -- backs
    both "Passings by Market" (sort by passings desc) and "Lead Penetration
    by Market" (sort by lead_penetration_pct asc, to surface "High Passings
    + Low Lead Penetration" markets per spec #13) from the same rows, so
    callers just re-sort the same list rather than issuing two queries."""
    column = "State" if dimension == "state" else "City"
    where_sql, params = _build_where(filters, search)
    empty = {"ok": False, "error": None, "markets": []}

    def run(cursor):
        cursor.execute(
            f"""
            {_BASE_CTE}
            SELECT TOP {int(top_n) if top_n else 1000} {column} AS Market,
                   COUNT(DISTINCT PlaceID) AS Passings,
                   SUM(CASE WHEN {_EMAIL_VALID_SQL} THEN 1 ELSE 0 END) AS Leads
            FROM passings_base
            WHERE {where_sql} AND {column} IS NOT NULL AND {column} <> ''
            GROUP BY {column}
            ORDER BY Passings DESC
            """,
            params,
        )
        markets = []
        for market, passings, leads in cursor.fetchall():
            passings = int(passings or 0)
            leads = int(leads or 0)
            markets.append({
                "market": market,
                "passings": passings,
                "leads": leads,
                "lead_penetration_pct": (leads / passings * 100) if passings else 0.0,
            })
        return {"ok": True, "error": None, "markets": markets}

    return _connect_and_run(run, empty)


# ============================================================
# Admin -> Data Tools -> "Passings & Leads Data" raw-row browser + the
# validation metrics at the top of that page (spec #25/#26).
# ============================================================

_ADMIN_COLUMNS = [
    ("PlaceID", "PlaceID (AuxVar5)"),
    ("EmailAddress", "Email Address"),
    ("FullAddress", "Full Address"),
    ("Street", "Street"),
    ("City", "City"),
    ("State", "State"),
    ("ZipCode", "Zip Code"),
    ("AvailabilityID", "Availability ID"),
    ("BecameAvailable", "Became Available (AuxVar1)"),
    ("AddressUnits", "Address Units (AuxVar2)"),
    ("PrequalAsBusiness", "Prequal as Business (AuxVar3)"),
    ("ClassName", "Property Type / ClassName (AuxVar4)"),
]


def get_admin_page(filters, search, page):
    """Same shape as get_page(), but returns every raw column (unclassified
    -- no derived category/label fields) for the Admin Data Tools raw-row
    browser, which exists specifically to let an admin see the source
    values themselves, not this app's interpretation of them."""
    where_sql, params = _build_where(filters, search)
    empty = {"ok": False, "error": None, "rows": [], "total": 0, "page": 1, "total_pages": 1, "page_size": PAGE_SIZE}

    def run(cursor):
        cursor.execute(f"{_BASE_CTE} SELECT COUNT(*) FROM passings_base WHERE {where_sql}", params)
        total = int(cursor.fetchone()[0] or 0)
        total_pages = max(1, -(-total // PAGE_SIZE))
        clamped_page = min(max(1, page), total_pages)
        offset = (clamped_page - 1) * PAGE_SIZE

        select_list = ", ".join(f"[{c}]" for c, _label in _ADMIN_COLUMNS)
        cursor.execute(
            f"{_BASE_CTE} SELECT {select_list} FROM passings_base WHERE {where_sql} "
            f"ORDER BY PlaceID ASC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
            params + [offset, PAGE_SIZE],
        )
        column_names = [c for c, _label in _ADMIN_COLUMNS]
        rows = [dict(zip(column_names, row)) for row in cursor.fetchall()]
        return {"ok": True, "error": None, "rows": rows, "total": total, "page": clamped_page,
                "total_pages": total_pages, "page_size": PAGE_SIZE}

    return _connect_and_run(run, empty)


def get_validation_metrics():
    """Rows Imported / Unique PlaceIDs / Duplicate PlaceIDs / Valid+Missing
    Emails / Prequal as Business Count / Commercial+Industrial+Church
    Property Counts / Missing AuxVar1+AuxVar4 -- spec #26, always over the
    full unfiltered qualifying-Passing population (this is a data-
    integrity check, not something an admin should be able to filter away
    by accident)."""
    empty = {
        "ok": False, "error": None,
        "rows_imported": 0, "unique_place_ids": 0, "duplicate_place_ids": 0,
        "valid_emails": 0, "missing_emails": 0, "prequal_as_business_count": 0,
        "commercial_count": 0, "industrial_count": 0, "church_religious_count": 0,
        "missing_auxvar1": 0, "missing_auxvar4": 0,
    }

    def run(cursor):
        commercial_sql, commercial_params = _property_type_sql("Commercial")
        industrial_sql, industrial_params = _property_type_sql("Industrial")
        church_sql, church_params = _property_type_sql("Church / Religious")

        cursor.execute(
            f"""
            {_BASE_CTE}
            SELECT
                COUNT(*),
                COUNT(DISTINCT PlaceID),
                SUM(CASE WHEN {_EMAIL_VALID_SQL} THEN 1 ELSE 0 END),
                SUM(CASE WHEN PrequalAsBusiness = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN {commercial_sql} THEN 1 ELSE 0 END),
                SUM(CASE WHEN {industrial_sql} THEN 1 ELSE 0 END),
                SUM(CASE WHEN {church_sql} THEN 1 ELSE 0 END),
                SUM(CASE WHEN BecameAvailable IS NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN ClassName IS NULL OR LTRIM(RTRIM(ClassName)) = '' THEN 1 ELSE 0 END)
            FROM passings_base
            """,
            commercial_params + industrial_params + church_params,
        )
        (rows_imported, unique_place_ids, valid_emails, prequal_count,
         commercial_count, industrial_count, church_count,
         missing_auxvar1, missing_auxvar4) = cursor.fetchone()

        rows_imported = int(rows_imported or 0)
        unique_place_ids = int(unique_place_ids or 0)
        valid_emails = int(valid_emails or 0)

        return {
            "ok": True, "error": None,
            "rows_imported": rows_imported,
            "unique_place_ids": unique_place_ids,
            "duplicate_place_ids": max(0, rows_imported - unique_place_ids),
            "valid_emails": valid_emails,
            "missing_emails": rows_imported - valid_emails,
            "prequal_as_business_count": int(prequal_count or 0),
            "commercial_count": int(commercial_count or 0),
            "industrial_count": int(industrial_count or 0),
            "church_religious_count": int(church_count or 0),
            "missing_auxvar1": int(missing_auxvar1 or 0),
            "missing_auxvar4": int(missing_auxvar4 or 0),
        }

    return _connect_and_run(run, empty)


ADMIN_COLUMNS = _ADMIN_COLUMNS
