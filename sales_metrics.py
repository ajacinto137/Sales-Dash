"""Centralized business logic for the /dashboard Team Leaderboard page.

Everything the dashboard displays is derived from one normalized DataFrame
(build_sales_dataset) that holds exactly one row per Main Sales record. All
status/rate/grouping rules live here so they are easy to audit and change
in one place instead of being scattered across routes and templates.
"""

import calendar as calendar_module
from datetime import datetime, timedelta

import pandas as pd

SALES_CHANNELS = {
    1: "Inbound Call",
    2: "Outbound Call",
    3: "Inbound Email",
    4: "Outbound Email",
    5: "Marketing Event",
    6: "Door to Door",
    7: "Texting",
    8: "Social Media",
}

# Leaderboard-level grouping of the detailed channels above. Channels not
# listed here (e.g. "Unknown") are intentionally excluded from both sets so
# they are never force-counted as either direction.
INBOUND_CHANNELS = {"Inbound Call", "Inbound Email"}
OUTBOUND_CHANNELS = {
    "Outbound Call",
    "Outbound Email",
    "Marketing Event",
    "Door to Door",
    "Texting",
    "Social Media",
}

REP_TOKEN_MARKER = "PAS Employee:"


def extract_rep_name(source_token):
    """'PAS Employee: Laryssa Dasilva' -> 'Laryssa Dasilva'. Anything that
    doesn't match the expected pattern (null, blank, unexpected format)
    falls back to 'Unknown' rather than raising."""
    if not isinstance(source_token, str):
        return "Unknown"
    idx = source_token.find(REP_TOKEN_MARKER)
    if idx == -1:
        return "Unknown"
    name = source_token[idx + len(REP_TOKEN_MARKER):].strip()
    return name or "Unknown"


def map_sales_channel(channel_id):
    try:
        key = int(channel_id)
    except (TypeError, ValueError):
        return "Unknown"
    return SALES_CHANNELS.get(key, "Unknown")


def build_sales_dataset(main_sales, vision_packages, service_cancellations):
    """Builds one normalized row per Main Sales record.

    Installed is read off the existing "Install Status" column (already
    computed elsewhere as a deduped match against Vision Packages, which the
    source query already restricts to first_invoice_date IS NOT NULL).
    Cancelled is decided by membership (isin), not a merge, so a subscriber
    with multiple Service Cancellations rows can never fan a sale out into
    duplicate rows. The result is guaranteed one row per Main Sales row.
    """
    if main_sales is None:
        return None

    original_count = len(main_sales)
    df = main_sales.copy()

    if "Install Status" in df.columns:
        installed_mask = df["Install Status"] == "Installed"
    else:
        installed_mask = pd.Series(False, index=df.index)

    if service_cancellations is not None and "subscriber_uuid" in service_cancellations.columns:
        cancelled_uuids = set(service_cancellations["subscriber_uuid"].dropna().unique())
    else:
        cancelled_uuids = set()

    if "vi_subscriber_uuid" in df.columns:
        subscriber_uuids = df["vi_subscriber_uuid"]
    else:
        subscriber_uuids = pd.Series([None] * len(df), index=df.index)

    cancelled_mask = (~installed_mask) & subscriber_uuids.isin(cancelled_uuids)

    status = pd.Series("Pending", index=df.index)
    status[installed_mask] = "Installed"
    status[cancelled_mask] = "Cancelled"

    source_tokens = df["SourceToken"] if "SourceToken" in df.columns else pd.Series([None] * len(df), index=df.index)
    channel_ids = df["SalesChannel_ID"] if "SalesChannel_ID" in df.columns else pd.Series([None] * len(df), index=df.index)

    normalized = pd.DataFrame({
        "sale_id": df["ID"] if "ID" in df.columns else df.index,
        "sale_date": pd.to_datetime(df["Date"], errors="coerce") if "Date" in df.columns else pd.NaT,
        "source_token": source_tokens,
        "sales_rep": source_tokens.apply(extract_rep_name),
        "sales_channel_id": channel_ids,
        "sales_channel": channel_ids.apply(map_sales_channel),
        "subscriber_uuid": subscriber_uuids,
        "scheduled": df["Scheduled"] if "Scheduled" in df.columns else None,
        "scheduled_date": df["StartDate"] if "StartDate" in df.columns else None,
        "installed": installed_mask.values,
        "cancelled": cancelled_mask.values,
        "status": status.values,
        # Passthrough contact/address fields, needed by the Rep Profile's
        # Needs Attention tab. Purely additive -- doesn't affect row count
        # or any existing column above.
        "first_name": df["FirstName"] if "FirstName" in df.columns else None,
        "last_name": df["LastName"] if "LastName" in df.columns else None,
        "address": df["Address"] if "Address" in df.columns else None,
        "apartment_suite": df["ApartmentSuite"] if "ApartmentSuite" in df.columns else None,
        "city": df["City"] if "City" in df.columns else None,
        "state": df["State"] if "State" in df.columns else None,
        "zipcode": df["Zipcode"] if "Zipcode" in df.columns else None,
    })

    row_count_ok = len(normalized) == original_count

    print("=" * 60)
    print("DASHBOARD DATA PIPELINE")
    print("=" * 60)
    print(f"Main rows loaded:            {original_count:,}")
    print(f"Vision rows loaded:          {len(vision_packages):,}" if vision_packages is not None else "Vision rows loaded:          0")
    print(
        f"Cancellation rows loaded:    {len(service_cancellations):,}"
        if service_cancellations is not None
        else "Cancellation rows loaded:    0"
    )
    print(f"Normalized sales rows:       {len(normalized):,}")
    print(f"Unique reps:                 {normalized['sales_rep'].nunique():,}")
    print(f"Installed count:             {int(installed_mask.sum()):,}")
    print(f"Pending count:               {int((status == 'Pending').sum()):,}")
    print(f"Cancelled count:             {int(cancelled_mask.sum()):,}")
    print(f"Row count preserved:         {'YES' if row_count_ok else 'NO -- INVESTIGATE'}")
    print("=" * 60)

    return normalized


def filter_by_period(df, period, start=None, end=None):
    if df is None or df.empty or period == "all_time":
        return df

    today = datetime.now().date()

    if period == "today":
        start_date = today
        end_date = today
    elif period == "yesterday":
        start_date = today - timedelta(days=1)
        end_date = start_date
    elif period == "this_week":
        start_date = today - timedelta(days=today.weekday())
        end_date = today
    elif period == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        start_date = this_week_start - timedelta(days=7)
        end_date = this_week_start - timedelta(days=1)
    elif period == "this_month":
        start_date = today.replace(day=1)
        end_date = today
    elif period == "last_month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        start_date = last_month_end.replace(day=1)
        end_date = last_month_end
    elif period == "this_year":
        start_date = today.replace(month=1, day=1)
        end_date = today
    elif period == "custom":
        try:
            start_date = pd.to_datetime(start).date() if start else None
            end_date = pd.to_datetime(end).date() if end else None
        except (ValueError, TypeError):
            return df
        if not start_date or not end_date:
            return df
    else:
        return df

    sale_dates = df["sale_date"].dt.date
    return df[(sale_dates >= start_date) & (sale_dates <= end_date)]


def _rate(numerator, denominator):
    if not denominator:
        return None
    return numerator / denominator * 100


def calculate_team_metrics(df):
    if df is None or df.empty:
        return {
            "total_sales": 0,
            "installed": 0,
            "pending": 0,
            "cancelled": 0,
            "install_rate": None,
        }

    installed = int((df["status"] == "Installed").sum())
    pending = int((df["status"] == "Pending").sum())
    cancelled = int((df["status"] == "Cancelled").sum())

    return {
        "total_sales": len(df),
        "installed": installed,
        "pending": pending,
        "cancelled": cancelled,
        "install_rate": _rate(installed, installed + cancelled),
    }


def calculate_rep_metrics(df):
    if df is None or df.empty:
        return []

    rows = []
    for rep, group in df.groupby("sales_rep"):
        installs = int((group["status"] == "Installed").sum())
        pending = int((group["status"] == "Pending").sum())
        cancels = int((group["status"] == "Cancelled").sum())
        inbound = int(group["sales_channel"].isin(INBOUND_CHANNELS).sum())
        outbound = int(group["sales_channel"].isin(OUTBOUND_CHANNELS).sum())
        denom = installs + cancels
        rows.append({
            "sales_rep": rep,
            "sales": len(group),
            "inbound": inbound,
            "outbound": outbound,
            "installs": installs,
            "pending": pending,
            "cancels": cancels,
            "chargebacks": None,
            "install_rate": _rate(installs, denom),
            "cancel_rate": _rate(cancels, denom),
        })

    rows.sort(key=lambda r: (-r["sales"], -r["installs"]))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


# Individual Rep Leaderboard columns a user can click to sort the table,
# in display order, with each column's default first-click direction.
# "rank" sorting ascending reproduces calculate_rep_metrics()'s own
# default order, so it doubles as the "reset to default" column.
REP_TABLE_COLUMNS = [
    ("rank", "#", "asc"),
    ("sales_rep", "Sales Rep", "asc"),
    ("sales", "Sales", "desc"),
    ("outbound", "Outbound", "desc"),
    ("inbound", "Inbound", "desc"),
    ("installs", "Installs", "desc"),
    ("pending", "Pending", "desc"),
    ("cancels", "Cancels", "desc"),
    ("install_rate", "Install Rate", "desc"),
    ("cancel_rate", "Cancel Rate", "desc"),
]
REP_SORT_KEYS = {key for key, _label, _default_dir in REP_TABLE_COLUMNS}


def sort_rep_rows(rows, sort_key, direction):
    """Reorders (never recomputes) Individual Rep Leaderboard rows for a
    clicked column header. Each row's own `rank` value is left untouched
    -- it always reflects the rep's actual sales-based leaderboard
    standing from calculate_rep_metrics(), even when the table itself is
    displayed sorted by a different column. Rows with no rate (None, no
    denominator) always sort to the bottom regardless of direction."""
    if sort_key not in REP_SORT_KEYS or not rows:
        return rows

    reverse = direction == "desc"
    with_value = [r for r in rows if r.get(sort_key) is not None]
    without_value = [r for r in rows if r.get(sort_key) is None]

    def key(row):
        value = row[sort_key]
        return value.lower() if isinstance(value, str) else value

    with_value.sort(key=key, reverse=reverse)
    return with_value + without_value


def calculate_records(df):
    """Always operates on the full ALL-TIME normalized dataset — callers
    must not pass a period-filtered DataFrame here. In addition to the
    all-time single-instance bests (best_days/weeks/months), this also
    returns live top-3 rep leaderboards for the ongoing day/week/month/
    year (daily/weekly/monthly/yearly_leaders) -- these use today's real
    date via filter_by_period, independent of the page's period filter,
    same reasoning as the rest of this function."""
    empty = {
        "best_days": [], "best_weeks": [], "best_months": [],
        "daily_leaders": [], "weekly_leaders": [], "monthly_leaders": [], "yearly_leaders": [],
    }
    if df is None or df.empty:
        return empty

    valid = df.dropna(subset=["sale_date"]).copy()
    if valid.empty:
        return empty

    valid["day"] = valid["sale_date"].dt.date
    valid["week_start"] = (
        valid["sale_date"] - pd.to_timedelta(valid["sale_date"].dt.weekday, unit="D")
    ).dt.date
    valid["month_period"] = valid["sale_date"].dt.to_period("M")

    def top3(group_col, label_fn):
        counts = (
            valid.groupby(["sales_rep", group_col])
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
            .head(3)
        )
        return [
            {"rep": row["sales_rep"], "label": label_fn(row[group_col]), "count": int(row["count"])}
            for _, row in counts.iterrows()
        ]

    def top_reps(period_df, n=3):
        if period_df is None or period_df.empty:
            return []
        counts = (
            period_df.groupby("sales_rep")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
            .head(n)
        )
        return [{"rep": row["sales_rep"], "count": int(row["count"])} for _, row in counts.iterrows()]

    return {
        "best_days": top3("day", lambda d: d.strftime("%b %-d, %Y")),
        "best_weeks": top3("week_start", lambda d: "Week of " + d.strftime("%b %-d")),
        "best_months": top3("month_period", lambda p: p.strftime("%B %Y")),
        "daily_leaders": top_reps(filter_by_period(valid, "today")),
        "weekly_leaders": top_reps(filter_by_period(valid, "this_week")),
        "monthly_leaders": top_reps(filter_by_period(valid, "this_month")),
        "yearly_leaders": top_reps(filter_by_period(valid, "this_year")),
    }


def calculate_calendar_sales(df, year, month):
    """Team total sales per day for one calendar month, for the navigable
    Sales Calendar section. Always operates on the full ALL-TIME normalized
    dataset (like calculate_records) so browsing to a different month is
    independent of the page's period filter. Weeks start Monday, matching
    the "this_week" period filter's own week-start convention."""
    days_in_month = calendar_module.monthrange(year, month)[1]
    counts = {day: 0 for day in range(1, days_in_month + 1)}

    if df is not None and not df.empty:
        valid = df.dropna(subset=["sale_date"])
        month_mask = (valid["sale_date"].dt.year == year) & (valid["sale_date"].dt.month == month)
        day_counts = valid.loc[month_mask, "sale_date"].dt.day.value_counts().to_dict()
        for day, count in day_counts.items():
            counts[int(day)] = int(count)

    first_weekday = calendar_module.monthrange(year, month)[0]  # Monday = 0
    today = datetime.now().date()

    weeks = []
    week = [None] * first_weekday
    for day in range(1, days_in_month + 1):
        week.append({
            "day": day,
            "count": counts[day],
            "is_today": (year, month, day) == (today.year, today.month, today.day),
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        week.extend([None] * (7 - len(week)))
        weeks.append(week)

    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    return {
        "year": year,
        "month": month,
        "month_label": datetime(year, month, 1).strftime("%B %Y"),
        "weeks": weeks,
        "total": sum(counts.values()),
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
    }


def calculate_monthly_forecast(df):
    """Run-rate projection for the CURRENT real calendar month: month-to-date
    sales divided by days elapsed, scaled to the full month. Like
    calculate_records, this always looks at the actual current month on the
    full ALL-TIME dataset — independent of the page's period filter and of
    Sales Calendar navigation, since a forecast is inherently about the
    ongoing month regardless of what range a manager happens to be viewing."""
    today = datetime.now().date()
    days_in_month = calendar_module.monthrange(today.year, today.month)[1]
    days_elapsed = today.day

    month_to_date = 0
    if df is not None and not df.empty:
        valid = df.dropna(subset=["sale_date"])
        month_mask = (valid["sale_date"].dt.year == today.year) & (valid["sale_date"].dt.month == today.month)
        month_to_date = int(month_mask.sum())

    pace = month_to_date / days_elapsed if days_elapsed else 0
    projected_total = round(pace * days_in_month)

    return {
        "month_label": today.strftime("%B %Y"),
        "month_to_date": month_to_date,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "projected_total": projected_total,
    }


def calculate_channel_breakdown(df):
    """Per-channel sale counts for whatever slice of the normalized dataset
    is passed in (e.g. one rep's period-filtered rows), for the Rep
    Profile's Sales by Channel section. Reuses the exact SALES_CHANNELS /
    INBOUND_CHANNELS / OUTBOUND_CHANNELS vocabulary that already powers the
    Rep Leaderboard's Inbound/Outbound columns -- no new classification
    logic."""
    counts = (
        df["sales_channel"].value_counts().to_dict()
        if df is not None and not df.empty
        else {}
    )
    return [
        {
            "channel": name,
            "count": int(counts.get(name, 0)),
            "direction": "inbound" if name in INBOUND_CHANNELS else "outbound",
        }
        for name in SALES_CHANNELS.values()
    ]


def _clean_text(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


VISION_BASE_URLS = {
    "VA": "https://kinextel.fibersmith.systems/subscribers/",
    "NJ": "https://planetnetworks.fibersmith.systems/subscribers/",
    "NY": "https://planetnetworks.fibersmith.systems/subscribers/",
}


def build_vision_url(state, subscriber_uuid):
    """VA -> Kinextel, NJ/NY -> Planet Networks, anything else (or a
    missing subscriber_uuid) -> None so the UI can omit the link instead
    of generating a broken one."""
    uuid = _clean_text(subscriber_uuid)
    if not uuid:
        return None
    base = VISION_BASE_URLS.get(_clean_text(state).upper() if _clean_text(state) else None)
    if not base:
        return None
    return f"{base}{uuid}"


def build_bulk_account_rows(df):
    """Shapes any slice of the normalized dataset into the standard Bulk
    Account View row: First Name, Last Name, Address, Scheduled Install
    Date, account status/category, and Vision link. This is the ONE place
    that defines what a Bulk Account View shows -- every feature that
    surfaces a filtered list of accounts (Pending, Needs Attention,
    Installed, Cancelled, ...) goes through this function rather than
    building its own row shape, so a field added here shows up everywhere
    a Bulk Account View is used."""
    if df is None or df.empty:
        return []

    scheduled_dates = pd.to_datetime(df["scheduled_date"], errors="coerce")

    accounts = []
    for idx, row in df.iterrows():
        address = _clean_text(row["address"])
        apartment_suite = _clean_text(row["apartment_suite"])
        if apartment_suite:
            address = f"{address}, Apt {apartment_suite}" if address else f"Apt {apartment_suite}"

        city = _clean_text(row["city"])
        state = _clean_text(row["state"])
        zipcode = _clean_text(row["zipcode"])
        city_state_zip = " ".join(
            part for part in [", ".join(p for p in [city, state] if p), zipcode] if part
        )

        scheduled = scheduled_dates.loc[idx]

        accounts.append({
            "first_name": _clean_text(row["first_name"]) or "",
            "last_name": _clean_text(row["last_name"]) or "",
            "address": address,
            "city_state_zip": city_state_zip or None,
            "scheduled_date": scheduled.strftime("%m/%d/%Y") if pd.notna(scheduled) else None,
            "status": row["status"],
            "vision_url": build_vision_url(state, row["subscriber_uuid"]),
        })
    return accounts


def _bulk_status_filter(status):
    return lambda df: df[df["status"] == status]


def _needs_attention_filter(df):
    """Scheduled Install Date strictly before today AND still Not Yet
    Installed under the same Vision Packages match already computed as
    the 'installed' column in build_sales_dataset -- no second
    install-detection system."""
    today = pd.Timestamp(datetime.now().date())
    scheduled_dates = pd.to_datetime(df["scheduled_date"], errors="coerce")
    overdue_mask = (~df["installed"]) & scheduled_dates.notna() & (scheduled_dates < today)
    return df[overdue_mask]


# Registry of every Bulk Account View this app currently offers. Each entry
# is a title plus a filter over the (already rep + date-scoped) normalized
# dataset. Adding a new view -- e.g. "Show Door to Door sales in a Bulk
# Account View" -- means adding one entry here, not building a new list UI.
# "all_time": True opts a view out of the period filter, same convention as
# calculate_records()/calculate_calendar_sales()/calculate_monthly_forecast().
BULK_ACCOUNT_VIEWS = {
    "pending": {
        "title": "Pending Accounts",
        "all_time": False,
        "filter": _bulk_status_filter("Pending"),
    },
    "needs_attention": {
        "title": "Needs Attention",
        "all_time": True,
        "filter": _needs_attention_filter,
    },
}


def get_bulk_account_view(df, rep, view, period, start, end):
    """The single entry point every feature should go through to open a
    Bulk Account View: given the full normalized dataset, a rep, a view
    key from BULK_ACCOUNT_VIEWS, and the currently applicable date range,
    returns (title, accounts) using the shared row shape from
    build_bulk_account_rows(). Returns an empty result (never raises) for
    an unknown view key so callers can decide how to handle it (e.g. 404)."""
    config = BULK_ACCOUNT_VIEWS.get(view)
    if config is None:
        return view, []

    title = config["title"]
    if df is None or df.empty:
        return title, []

    scoped = df[df["sales_rep"] == rep] if rep else df
    if not config["all_time"]:
        scoped = filter_by_period(scoped, period, start, end)
    if scoped is None or scoped.empty:
        return title, []

    filtered = config["filter"](scoped)
    if filtered.empty:
        return title, []

    filtered = filtered.assign(
        _scheduled_sort=pd.to_datetime(filtered["scheduled_date"], errors="coerce")
    ).sort_values("_scheduled_sort", na_position="last")

    return title, build_bulk_account_rows(filtered)


def calculate_needs_attention(df, rep):
    """Thin wrapper kept for the Rep Profile's inline Needs Attention tab
    -- routes through the same get_bulk_account_view() used by every other
    Bulk Account View so the two can never diverge. Always all-time; see
    the "needs_attention" entry in BULK_ACCOUNT_VIEWS."""
    _, accounts = get_bulk_account_view(df, rep, "needs_attention", None, None, None)
    return accounts
