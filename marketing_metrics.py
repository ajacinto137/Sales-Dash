"""Backend for the Admin Portal's Marketing Form Data > Attribution
Quality panel (/admin/marketing-form-data) -- the ONLY caller left of
this module (see get_attribution_quality()). The live Marketing Channel
Report at /marketing was rewritten 2026-08-20 to use marketing_cleaning.py
instead (a different, more detailed 5-way channel model); this module's
former get_dashboard_data()/compute_*() functions and the Flask-rendered
templates/marketing_dashboard.html + static/js/marketing_charts.js that
consumed them were removed as dead code in that same change -- see
marketing_cleaning.py's module docstring and README.md "Marketing
Dashboard" / "Marketing Channel Report" for the full picture.

fetch_leads() pulls from the exact same source as marketing_data.py --
PlanetWeb's [dbo].[View_FormDataAnalytics] -- but attributes every row via
marketing_attribution.attribute_record() (the centralized Paid/Not-Paid +
platform attribution function; see that module's docstring), which
marketing_data.py's own raw-row browser deliberately does not do.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import db
import marketing_attribution
from marketing_data import BASE_START_DATE, VIEW

# Same reasoning as sales_metrics.py's own EASTERN_TZ/_today(): the app
# server's clock runs in UTC, but "today" here must anchor to Eastern
# local time or the window can silently drift by a day right around
# midnight UTC.
EASTERN_TZ = ZoneInfo("America/New_York")


def _today():
    return datetime.now(EASTERN_TZ).date()


# Only the columns attribute_record()/grouping actually need -- narrower
# than marketing_data.COLUMNS (the raw browser's full 25-column list)
# since this module never displays a raw row, only aggregates.
_FETCH_COLUMNS = [
    "InsertDate", "NJPR_municipalityName", "utm_source", "utm_medium",
    "utm_campaign", "MarketingToken", "ReferralData", "uniqueURL",
    "fbclid", "msclkid", "ndclid", "tw_source", "gad_source",
    "gbraid", "gclid",
]
_ATTRIBUTION_COLUMNS = ["is_paid", "paid_platform", "attribution_tier", "attribution_method"]
_SELECT_LIST = ", ".join(f"[{c}]" for c in _FETCH_COLUMNS)


def _empty_df():
    return pd.DataFrame(columns=_FETCH_COLUMNS + _ATTRIBUTION_COLUMNS)


def fetch_leads(start_date, end_date, municipality):
    """Returns (df, ok, error). On any failure df is an empty-but-
    correctly-shaped DataFrame and ok is False -- callers never need a
    special "did the query fail" branch before touching df, same
    convention as marketing_data.get_marketing_form_data()."""
    conn = None
    try:
        conn = db.get_planetweb_connection()
        cursor = conn.cursor()

        clauses = ["[InsertDate] >= ?", "[InsertDate] < ?"]
        # end_date is inclusive of the whole day -- compare against the
        # start of the NEXT day rather than "<= end_date" so a submission
        # made late on the last selected day is still included.
        params = [start_date, end_date + timedelta(days=1)]
        if municipality:
            clauses.append("[NJPR_municipalityName] = ?")
            params.append(municipality)
        where_sql = " AND ".join(clauses)

        cursor.execute(f"SELECT {_SELECT_LIST} FROM {VIEW} WHERE {where_sql}", params)
        rows = cursor.fetchall()

        records = []
        for row in rows:
            record = dict(zip(_FETCH_COLUMNS, row))
            attribution = marketing_attribution.attribute_record(record)
            record["is_paid"] = attribution["isPaid"]
            record["paid_platform"] = attribution["paidPlatform"]
            record["attribution_tier"] = attribution["attributionTier"]
            record["attribution_method"] = attribution["attributionMethod"]
            records.append(record)

        df = pd.DataFrame.from_records(records, columns=list(_empty_df().columns)) if records else _empty_df()
        return df, True, None
    except Exception as exc:
        print("=" * 60)
        print("MARKETING METRICS QUERY FAILED")
        print("=" * 60)
        print(f"Server:   {db.PLANETWEB_HOST}")
        print(f"Database: {db.PLANETWEB_DATABASE}")
        print(f"Error:    {exc}")
        print("=" * 60)
        return _empty_df(), False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def get_attribution_quality():
    """Tier 1/2/3 breakdown for the Admin Portal's Marketing Form Data >
    Attribution Quality panel -- deliberately not on any business-facing
    dashboard, since attribution-tier detail is a debugging/data-
    validation concern. Always the raw browser's own base date window
    (marketing_data.BASE_START_DATE through today), unfiltered -- this is
    a data-validation view, not meant to be sliced by the admin's own
    search/filter controls."""
    df, ok, error = fetch_leads(BASE_START_DATE, _today(), "")
    if not ok:
        return {"ok": False, "error": error, "total": 0, "paid": 0, "pct_paid": 0.0, "tiers": [], "methods": []}

    total = int(len(df))
    paid_df = df[df["is_paid"] == True]  # noqa: E712 -- explicit True match, not truthiness, over a mixed-dtype column
    paid = int(len(paid_df))
    pct_paid = (paid / total * 100) if total else 0.0

    tiers = []
    for tier in (1, 2, 3):
        count = int((paid_df["attribution_tier"] == tier).sum())
        tiers.append({"tier": tier, "count": count, "pct": (count / paid * 100) if paid else 0.0})

    method_counts = paid_df["attribution_method"].value_counts()
    methods = [{"method": m, "count": int(c)} for m, c in method_counts.items()]
    methods.sort(key=lambda r: r["count"], reverse=True)

    return {"ok": True, "error": error, "total": total, "paid": paid, "pct_paid": pct_paid, "tiers": tiers, "methods": methods}
