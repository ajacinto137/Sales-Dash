"""Marketing Form Data -- Admin Portal raw-data verification tool
(templates/admin/marketing_form_data.html, app.py's
admin_marketing_form_data()).

Reads directly from PlanetWeb SQL Server's [dbo].[View_FormDataAnalytics]
view via db.get_planetweb_connection() -- the SAME connection/credentials
(PLANETWEB_HOST/DATABASE/USERNAME/PASSWORD) every other PlanetWeb query in
this app already uses (see queries.py's MAIN_SALES_QUERY and friends).
No new environment variables, no new connection helper -- this view lives
on the identical server/database the app is already configured for.

Deliberately NOT part of data_store/ensure_data_loaded() (app.py) -- every
other source-database dataset in this app is pulled whole into a pandas
DataFrame on every page load. This view can be much larger and this page
is explicitly a raw-row browser, so it stays on its own real SQL-level
pagination (OFFSET/FETCH) with server-side search/filtering instead,
recomputed fresh only when this one route is hit. This is intentionally a
different pattern from main_sales/vision_packages/service_cancellations
(see app.py's dataframe_to_table()) -- do not "fix" this by merging it
into that pipeline; the whole point is to avoid loading tens of thousands
of marketing rows into memory on every request across the app.

Read-only, like every other source-database query in this app -- nothing
here ever executes anything but SELECT. Business logic (the base date
window, column list, sort order) matches the spec query verbatim; this
module only adds parameterized WHERE/OFFSET/FETCH on top for the admin's
search/filter/pagination controls -- it never changes what "a row in this
view" means."""

from datetime import date

import db

# Base window from the spec query -- 2026-03-01 through "right now" on the
# SQL Server itself (GETDATE()), never widened. Every additional filter
# below is ANDed on top of this, so it can only narrow the result set
# further, never escape this window.
BASE_START_DATE = date(2026, 3, 1)

PAGE_SIZE = 50

# (column, label) in the exact order/casing of the spec query -- this is
# the ONE place both the SELECT list and the table header labels come
# from, so they can't drift apart.
COLUMNS = [
    ("InsertDate", "Insert Date"),
    ("AvailabilityID", "Availability ID"),
    ("QuoteID", "Quote ID"),
    ("NJPR_municipalityName", "Municipality"),
    ("Zipcode", "Zipcode"),
    ("MarketingToken", "Marketing Token"),
    ("ReferralData", "Referral Data"),
    ("email_id", "Email ID"),
    ("uniqueURL", "Unique URL"),
    ("utm_source", "UTM Source"),
    ("utm_medium", "UTM Medium"),
    ("utm_campaign", "UTM Campaign"),
    ("utm_content", "UTM Content"),
    ("utm_term", "UTM Term"),
    ("utm_id", "UTM ID"),
    ("fbclid", "fbclid"),
    ("msclkid", "msclkid"),
    ("ndclid", "ndclid"),
    ("tw_source", "Twitter Source"),
    ("tw_adid", "Twitter Ad ID"),
    ("tw_campaign", "Twitter Campaign"),
    ("tw_kwdid", "Twitter Keyword ID"),
    ("gad_source", "Google Ads Source"),
    ("gad_campaignid", "Google Ads Campaign ID"),
    ("gbraid", "gbraid"),
    ("gclid", "gclid"),
]

_SELECT_LIST = ", ".join(f"[{col}]" for col, _label in COLUMNS)
VIEW = "[PlanetWeb].[dbo].[View_FormDataAnalytics]"

# QuoteID/AvailabilityID are numeric in the view -- CAST to text so the
# search box's LIKE can match them the same way it matches the text
# columns, without erroring on a non-numeric search term.
_SEARCH_COLUMNS = [
    "CAST([QuoteID] AS NVARCHAR(50))",
    "CAST([AvailabilityID] AS NVARCHAR(50))",
    "[NJPR_municipalityName]",
    "[Zipcode]",
    "[MarketingToken]",
    "[utm_campaign]",
    "[utm_source]",
]

# Dropdown filter options are read fresh from the view (scoped to the same
# base date window, not cross-filtered by the other active filters -- same
# convention app.py's unique_sorted_values() already uses for the Main
# Sales filter dropdowns).
DROPDOWN_COLUMNS = [
    ("municipality", "NJPR_municipalityName"),
    ("utm_source", "utm_source"),
    ("utm_medium", "utm_medium"),
    ("utm_campaign", "utm_campaign"),
]


def _build_where(filters, search):
    """Returns (sql, params) -- the base date window ANDed with every
    filter/search value actually supplied. Every value is a `?`
    parameter, never string-interpolated, regardless of source."""
    clauses = ["[InsertDate] >= ?", "[InsertDate] <= GETDATE()"]
    params = [BASE_START_DATE]

    date_from = filters.get("date_from")
    if date_from:
        clauses.append("[InsertDate] >= ?")
        params.append(date_from)
    date_to = filters.get("date_to")
    if date_to:
        clauses.append("[InsertDate] <= ?")
        params.append(date_to)

    municipality = filters.get("municipality")
    if municipality:
        clauses.append("[NJPR_municipalityName] = ?")
        params.append(municipality)

    zipcode = filters.get("zipcode")
    if zipcode:
        clauses.append("[Zipcode] = ?")
        params.append(zipcode)

    utm_source = filters.get("utm_source")
    if utm_source:
        clauses.append("[utm_source] = ?")
        params.append(utm_source)

    utm_medium = filters.get("utm_medium")
    if utm_medium:
        clauses.append("[utm_medium] = ?")
        params.append(utm_medium)

    utm_campaign = filters.get("utm_campaign")
    if utm_campaign:
        clauses.append("[utm_campaign] = ?")
        params.append(utm_campaign)

    if search:
        like_term = f"%{search}%"
        clauses.append("(" + " OR ".join(f"{col} LIKE ?" for col in _SEARCH_COLUMNS) + ")")
        params.extend([like_term] * len(_SEARCH_COLUMNS))

    return " AND ".join(clauses), params


def get_filter_options():
    """{filter_key: [distinct values...]} for the dropdown filters
    (Municipality/UTM Source/UTM Medium/UTM Campaign), scoped to the base
    date window. Returns an empty dict (never raises) on any failure --
    the page still renders and still lets an admin search/paginate even
    if this one supporting query fails; only the dropdowns go empty."""
    options = {key: [] for key, _col in DROPDOWN_COLUMNS}
    conn = None
    try:
        conn = db.get_planetweb_connection()
        cursor = conn.cursor()
        for key, column in DROPDOWN_COLUMNS:
            cursor.execute(
                f"SELECT DISTINCT [{column}] FROM {VIEW} "
                f"WHERE [InsertDate] >= ? AND [InsertDate] <= GETDATE() "
                f"AND [{column}] IS NOT NULL AND [{column}] <> '' "
                f"ORDER BY [{column}]",
                [BASE_START_DATE],
            )
            options[key] = [row[0] for row in cursor.fetchall()]
        return options
    except Exception:
        return options
    finally:
        if conn is not None:
            conn.close()


def get_marketing_form_data(filters, search, page):
    """Runs the spec query (base date window + whatever filters/search
    were supplied) against PlanetWeb, paginated server-side via SQL
    Server's OFFSET/FETCH so the browser never receives more than one
    page's worth of rows regardless of how large the view grows.

    Returns a dict, always with every key present (never raises):
      ok, error            -- connection/query outcome; error is already
                               sanitized (db.sanitize_error()) and safe to
                               show an admin, never a raw driver message
                               that could contain a connection string.
      rows                 -- list of {column: value} dicts, this page
                               only, in COLUMNS order.
      total, earliest, latest -- COUNT/MIN/MAX over the CURRENT filtered
                               scope (not the whole unfiltered view), so
                               they describe exactly what's being paged
                               through.
      page, total_pages    -- 1-indexed; page is clamped into
                               [1, total_pages].

    On failure every count comes back 0/None and rows [] -- the caller
    (app.py) always has a complete, renderable shape to hand the
    template, so a query failure can never surface as a raw exception on
    an otherwise-working Admin Portal page."""
    where_sql, params = _build_where(filters, search)
    empty = {
        "ok": False, "error": None, "rows": [],
        "total": 0, "earliest": None, "latest": None,
        "page": 1, "total_pages": 1, "page_size": PAGE_SIZE,
    }

    conn = None
    try:
        conn = db.get_planetweb_connection()
        cursor = conn.cursor()

        cursor.execute(
            f"SELECT COUNT(*), MIN([InsertDate]), MAX([InsertDate]) FROM {VIEW} WHERE {where_sql}",
            params,
        )
        total, earliest, latest = cursor.fetchone()
        total = int(total or 0)
        total_pages = max(1, -(-total // PAGE_SIZE))  # ceil division
        page = min(max(1, page), total_pages)
        offset = (page - 1) * PAGE_SIZE

        cursor.execute(
            f"SELECT {_SELECT_LIST} FROM {VIEW} WHERE {where_sql} "
            f"ORDER BY [InsertDate] OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
            params + [offset, PAGE_SIZE],
        )
        column_names = [col for col, _label in COLUMNS]
        rows = [dict(zip(column_names, row)) for row in cursor.fetchall()]

        return {
            "ok": True, "error": None, "rows": rows,
            "total": total, "earliest": earliest, "latest": latest,
            "page": page, "total_pages": total_pages, "page_size": PAGE_SIZE,
        }
    except Exception as exc:
        # Full (unsanitized-for-secrets-only) detail to stdout for
        # troubleshooting -- same convention as the rest of this app
        # (print(), not a logging framework -- see sales_metrics.py's
        # DASHBOARD DATA PIPELINE banner). The UI only ever gets the
        # sanitized version below.
        print("=" * 60)
        print("MARKETING FORM DATA QUERY FAILED")
        print("=" * 60)
        print(f"Server:   {db.PLANETWEB_HOST}")
        print(f"Database: {db.PLANETWEB_DATABASE}")
        print(f"Error:    {exc}")
        print("=" * 60)
        empty["error"] = db.sanitize_error(exc)
        return empty
    finally:
        if conn is not None:
            conn.close()
