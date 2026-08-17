"""Centralized business logic for the /dashboard Team Leaderboard page.

Everything the dashboard displays is derived from one normalized DataFrame
(build_sales_dataset) that holds exactly one row per Main Sales record. All
status/rate/grouping rules live here so they are easy to audit and change
in one place instead of being scattered across routes and templates.
"""

import calendar as calendar_module
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

# InsertDate on the PlanetWeb SQL Server is already written in
# America/New_York local time, not UTC -- so sale_datetime only needs this
# attached for correct EST/EDT display (%Z), never an astimezone() convert.
EASTERN_TZ = ZoneInfo("America/New_York")


def _today():
    """The business's "today". The app server's clock runs in UTC, but
    every sale/scheduled date in the normalized dataset is Eastern local
    time -- so every "today"-relative calculation (period filters, Daily
    Sales, Records leaderboards, Monthly Forecast/Projection, Needs
    Attention) must anchor to Eastern "today", not datetime.now().date().
    Using the raw UTC date would go stale for several hours every evening
    (UTC rolls to the next calendar day ~4-5 hours before Eastern does),
    silently zeroing out anything compared against "today"."""
    return datetime.now(EASTERN_TZ).date()

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

    # Pending vs Needs Attention (2026-08-17, by request): Pending is now
    # ONLY an account with a real install date strictly in the future --
    # everything else not-yet-installed/not-cancelled (no install date,
    # explicitly "Not Scheduled", or an install date today-or-earlier)
    # is Needs Attention instead. The two are mutually exclusive and
    # exhaustive over the non-installed/non-cancelled population by
    # construction. `today` compares calendar dates, not exact
    # timestamps, so an install scheduled for later today still counts
    # as "today" (Needs Attention), not "future" (Pending).
    today_ts = pd.Timestamp(_today())
    scheduled_flag = df["Scheduled"] if "Scheduled" in df.columns else pd.Series(None, index=df.index)
    scheduled_dates_raw = (
        pd.to_datetime(df["StartDate"], errors="coerce") if "StartDate" in df.columns
        else pd.Series(pd.NaT, index=df.index)
    )
    no_date_mask = scheduled_dates_raw.isna()
    unscheduled_mask = scheduled_flag.astype(str).str.strip().str.casefold().eq("not scheduled")
    # .dt.normalize() (not .dt.date) -- on some pandas versions .dt.date
    # on an all-NaT column silently stays datetime64 dtype instead of
    # converting to object, which breaks a direct comparison against a
    # plain date. normalize() keeps datetime64 dtype throughout, so it
    # compares safely against another Timestamp in every case.
    past_or_today_mask = scheduled_dates_raw.notna() & (scheduled_dates_raw.dt.normalize() <= today_ts)
    needs_attention_mask = (~installed_mask) & (~cancelled_mask) & (no_date_mask | unscheduled_mask | past_or_today_mask)

    status = pd.Series("Pending", index=df.index)
    status[needs_attention_mask] = "Needs Attention"
    status[installed_mask] = "Installed"
    status[cancelled_mask] = "Cancelled"

    source_tokens = df["SourceToken"] if "SourceToken" in df.columns else pd.Series([None] * len(df), index=df.index)
    channel_ids = df["SalesChannel_ID"] if "SalesChannel_ID" in df.columns else pd.Series([None] * len(df), index=df.index)

    normalized = pd.DataFrame({
        "sale_id": df["ID"] if "ID" in df.columns else df.index,
        "sale_date": pd.to_datetime(df["Date"], errors="coerce") if "Date" in df.columns else pd.NaT,
        # Raw InsertDate incl. time-of-day, already in America/New_York
        # local time on the SQL Server -- no tz conversion needed, only
        # display formatting. Used by the "Latest Sales" record card.
        "sale_datetime": pd.to_datetime(df["DateTime"], errors="coerce") if "DateTime" in df.columns else pd.NaT,
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
    print(f"Needs Attention count:       {int((status == 'Needs Attention').sum()):,}")
    print(f"Cancelled count:             {int(cancelled_mask.sum()):,}")
    print(f"Row count preserved:         {'YES' if row_count_ok else 'NO -- INVESTIGATE'}")
    print("=" * 60)

    return normalized


def filter_by_period(df, period, start=None, end=None):
    if df is None or df.empty or period == "all_time":
        return df

    today = _today()

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


def calculate_total_daily_sales(df):
    """Count of sales made today, for the Team Overview KPI bar. Always
    operates on the full ALL-TIME normalized dataset (like
    calculate_records and calculate_calendar_sales) so it reflects today's
    sales regardless of the page's period filter."""
    if df is None or df.empty:
        return 0
    today = _today()
    valid = df.dropna(subset=["sale_date"])
    return int((valid["sale_date"].dt.date == today).sum())


def calculate_daily_averages(df):
    """Trailing 7-day and 30-day average daily sales counts, for the Team
    Overview KPI bar. Always operates on the full ALL-TIME normalized
    dataset and today's real date -- window is today back N-1 days,
    inclusive -- same period-filter independence as calculate_records and
    calculate_monthly_forecast."""
    today = _today()

    def avg_over(days):
        if df is None or df.empty:
            return 0.0
        valid = df.dropna(subset=["sale_date"])
        sale_dates = valid["sale_date"].dt.date
        start = today - timedelta(days=days - 1)
        count = int(((sale_dates >= start) & (sale_dates <= today)).sum())
        return count / days

    return {
        "avg_7_day": avg_over(7),
        "avg_30_day": avg_over(30),
    }


def calculate_rep_metrics(df):
    if df is None or df.empty:
        return []

    rows = []
    for rep, group in df.groupby("sales_rep"):
        sales_count = len(group)
        installs = int((group["status"] == "Installed").sum())
        pending = int((group["status"] == "Pending").sum())
        cancels = int((group["status"] == "Cancelled").sum())
        needs_attention = int((group["status"] == "Needs Attention").sum())
        inbound = int(group["sales_channel"].isin(INBOUND_CHANNELS).sum())
        outbound = int(group["sales_channel"].isin(OUTBOUND_CHANNELS).sum())
        denom = installs + cancels
        # Install Rate redefined 2026-08-17, by request: 1.0 - (Needs
        # Attention / Total Sales) -- not the installs/(installs+cancels)
        # ratio used elsewhere. Needs Attention is always <= sales_count
        # (it's a status subset of the same rep's rows), so this stays in
        # [0, 100]. Cancel Rate is unchanged (still installs/cancels
        # based) -- only Install Rate's formula and the visible Cancels
        # count column (now Needs Attention) changed.
        install_rate = (1 - (needs_attention / sales_count)) * 100 if sales_count else None
        rows.append({
            "sales_rep": rep,
            "sales": sales_count,
            "inbound": inbound,
            "outbound": outbound,
            "installs": installs,
            "pending": pending,
            "cancels": cancels,
            "needs_attention": needs_attention,
            "chargebacks": None,
            "install_rate": install_rate,
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
    ("needs_attention", "Needs Attention", "desc"),
    ("install_rate", "Install Rate", "desc"),
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
        "recent_sales": [],
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

    def last_n_sales(n=3):
        has_datetime = "sale_datetime" in valid.columns and valid["sale_datetime"].notna().any()
        sort_col = "sale_datetime" if has_datetime else "sale_date"
        sort_cols, ascending = [sort_col], [False]
        if "sale_id" in valid.columns:
            # Timestamps can still tie -- sale_id as a secondary key breaks
            # ties in insertion order.
            sort_cols.append("sale_id")
            ascending.append(False)
        latest = valid.sort_values(sort_cols, ascending=ascending, na_position="last").head(n)

        def label_for(row):
            dt = row.get("sale_datetime")
            if pd.notna(dt):
                # dt is a naive timestamp already in Eastern local time
                # (see EASTERN_TZ note above) -- attach, don't convert.
                return dt.replace(tzinfo=EASTERN_TZ).strftime("%b %-d, %Y, %-I:%M %p %Z")
            return row["sale_date"].strftime("%b %-d, %Y")

        def account_name_for(row):
            first = _clean_text(row.get("first_name")) or ""
            last = _clean_text(row.get("last_name")) or ""
            name = f"{first} {last}".strip()
            return name or None

        return [
            {"rep": row["sales_rep"], "account": account_name_for(row), "label": label_for(row)}
            for _, row in latest.iterrows()
        ]

    return {
        "best_days": top3("day", lambda d: d.strftime("%b %-d, %Y")),
        "best_weeks": top3("week_start", lambda d: "Week of " + d.strftime("%b %-d")),
        "best_months": top3("month_period", lambda p: p.strftime("%B %Y")),
        "daily_leaders": top_reps(filter_by_period(valid, "today")),
        "weekly_leaders": top_reps(filter_by_period(valid, "this_week")),
        "monthly_leaders": top_reps(filter_by_period(valid, "this_month")),
        "yearly_leaders": top_reps(filter_by_period(valid, "this_year")),
        "recent_sales": last_n_sales(),
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
    today = _today()

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


def calculate_monthly_sales_trend(df, months=12):
    """Team-wide total sales per calendar month for the trailing N months
    (oldest first, ending at and including the current month to date), for
    the Team Leaderboard's Monthly Sales Trend line chart. Always all-time
    / independent of the page's period filter -- same convention as
    calculate_records()/calculate_calendar_sales(), since a multi-month
    trend isn't meaningful squeezed into a single period-filter window.
    Distinct from calculate_rep_monthly_activity(), which is per-rep and
    day-granularity within one month -- this is team-wide and
    month-granularity across many months."""
    today = _today()
    month_keys = []
    year, month = today.year, today.month
    for _ in range(months):
        month_keys.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    month_keys.reverse()

    counts = {key: 0 for key in month_keys}
    if df is not None and not df.empty:
        valid = df.dropna(subset=["sale_date"])
        if not valid.empty:
            grouped = valid.groupby([valid["sale_date"].dt.year, valid["sale_date"].dt.month]).size()
            for (year, month), count in grouped.items():
                key = (int(year), int(month))
                if key in counts:
                    counts[key] = int(count)

    return [
        {
            "year": year,
            "month": month,
            "month_label": datetime(year, month, 1).strftime("%b %Y"),
            "count": counts[(year, month)],
        }
        for (year, month) in month_keys
    ]


def calculate_monthly_forecast(df):
    """Run-rate projection for the CURRENT real calendar month: month-to-date
    sales divided by days elapsed, scaled to the full month. Like
    calculate_records, this always looks at the actual current month on the
    full ALL-TIME dataset — independent of the page's period filter and of
    Sales Calendar navigation, since a forecast is inherently about the
    ongoing month regardless of what range a manager happens to be viewing."""
    today = _today()
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


def calculate_hourly_breakdown(df):
    """Per-hour sale counts (0-23, every real clock hour -- never trimmed
    to a business-hours window) for whatever slice of the normalized
    dataset is passed in (e.g. one rep's period-filtered rows), for the
    Rep Profile's Time-of-Day Performance chart. Uses sale_datetime, not
    sale_date (date-only) or scheduled_date (the future install
    appointment, unrelated to when the sale occurred) -- sale_datetime is
    the only field carrying the real time-of-day, already Eastern local
    time (see EASTERN_TZ note above)."""
    counts = {hour: 0 for hour in range(24)}
    if df is not None and not df.empty:
        valid = df.dropna(subset=["sale_datetime"])
        if not valid.empty:
            hour_counts = valid["sale_datetime"].dt.hour.value_counts().to_dict()
            for hour, count in hour_counts.items():
                counts[int(hour)] = int(count)
    return [{"hour": hour, "count": counts[hour]} for hour in range(24)]


def calculate_rep_monthly_activity(df, rep, year, month):
    """One rep's sales per day for one calendar month, computed once and
    shaped two ways so the Sales Activity section's calendar and
    MonthlySalesChart never compute the same day-count twice or risk
    disagreeing: `weeks` (calendar grid, identical shape to
    calculate_calendar_sales()) and `days` (a flat 1..N array -- what
    MonthlySalesChart plots -- each day also carrying its own
    SALES_CHANNELS-shaped breakdown, non-zero channels only, for the
    chart's per-day tooltip). Always all-time / independent of the page's
    period filter, with its own cal_year/cal_month month nav -- same
    convention calculate_calendar_sales() already uses on the Team
    Leaderboard, just applied per-rep here."""
    days_in_month = calendar_module.monthrange(year, month)[1]
    counts = {day: 0 for day in range(1, days_in_month + 1)}
    channel_counts = {day: {} for day in range(1, days_in_month + 1)}

    if df is not None and not df.empty:
        rep_df = df[df["sales_rep"] == rep] if rep else df
        valid = rep_df.dropna(subset=["sale_date"])
        month_mask = (valid["sale_date"].dt.year == year) & (valid["sale_date"].dt.month == month)
        month_df = valid.loc[month_mask]
        day_counts = month_df["sale_date"].dt.day.value_counts().to_dict()
        for day, count in day_counts.items():
            counts[int(day)] = int(count)
        if not month_df.empty:
            grouped = month_df.groupby([month_df["sale_date"].dt.day, "sales_channel"]).size()
            for (day, channel), count in grouped.items():
                channel_counts[int(day)][channel] = int(count)

    first_weekday = calendar_module.monthrange(year, month)[0]  # Monday = 0
    today = _today()

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

    days = [
        {
            "day": day,
            "count": counts[day],
            "channels": [
                {"channel": name, "count": channel_counts[day][name]}
                for name in SALES_CHANNELS.values()
                if channel_counts[day].get(name, 0) > 0
            ],
        }
        for day in range(1, days_in_month + 1)
    ]

    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    return {
        "year": year,
        "month": month,
        "month_label": datetime(year, month, 1).strftime("%B %Y"),
        "weeks": weeks,
        "days": days,
        "total": sum(counts.values()),
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
    }


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
    Account View row: sale_id (stable identifier), First Name, Last Name,
    Address, Scheduled Install Date, account status/category, Sales
    Channel, and Vision link. This is the ONE place
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
            # Main Sales' own primary key (FTTPFormData.ID) -- the stable
            # identifier the Needs Attention workflow (attention_store.py)
            # keys Attention Status/notes off of. Always present and
            # unique, unlike subscriber_uuid (see attention_store.py's
            # module docstring for why that one was rejected).
            "sale_id": int(row["sale_id"]),
            "first_name": _clean_text(row["first_name"]) or "",
            "last_name": _clean_text(row["last_name"]) or "",
            "address": address,
            "city_state_zip": city_state_zip or None,
            "scheduled_date": scheduled.strftime("%m/%d/%Y") if pd.notna(scheduled) else None,
            "scheduled_date_sort": scheduled.strftime("%Y-%m-%d") if pd.notna(scheduled) else "",
            "status": row["status"],
            "sales_channel": _clean_text(row["sales_channel"]),
            "vision_url": build_vision_url(state, row["subscriber_uuid"]),
        })
    return accounts


def _bulk_status_filter(status):
    return lambda df: df[df["status"] == status]


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
        "filter": _bulk_status_filter("Needs Attention"),
    },
    "all_sales": {
        "title": "All Sales",
        "all_time": False,
        "filter": lambda df: df,
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


def get_channel_account_view(df, rep, channel, period, start, end):
    """Bulk Account View scoped to one sales channel (a bar click on the
    Rep Profile's SalesByChannelChart). Rep + period scoped exactly like a
    non-all_time BULK_ACCOUNT_VIEWS entry, but the channel is a runtime
    value from the click rather than one baked into the registry at
    import time, so it can't go through get_bulk_account_view() directly.
    Mirrors that function's own scoping steps (rep filter -> period filter
    -> row filter -> sort by scheduled_date -> build_bulk_account_rows())
    so results are shaped identically to every other Bulk Account View.
    Returns (title, []) for an unknown channel or empty scope, never
    raises."""
    if channel not in SALES_CHANNELS.values():
        return channel, []
    title = f"{channel} Accounts"
    if df is None or df.empty:
        return title, []
    scoped = df[df["sales_rep"] == rep] if rep else df
    scoped = filter_by_period(scoped, period, start, end)
    if scoped is None or scoped.empty:
        return title, []
    filtered = scoped[scoped["sales_channel"] == channel]
    if filtered.empty:
        return title, []
    filtered = filtered.assign(
        _scheduled_sort=pd.to_datetime(filtered["scheduled_date"], errors="coerce")
    ).sort_values("_scheduled_sort", na_position="last")
    return title, build_bulk_account_rows(filtered)


# Metric drill-downs for the Individual Rep Leaderboard's ranked table --
# clicking a rep's Sales/Outbound/Inbound/Installs/Pending/Needs Attention
# cell opens a Bulk Account View of exactly the rows that make up that
# number. These mirror calculate_rep_metrics()'s own per-column logic
# (same status strings / same INBOUND_CHANNELS/OUTBOUND_CHANNELS sets) so
# the accounts shown always match the count that was clicked. This can't
# reuse the "needs_attention"/"pending" BULK_ACCOUNT_VIEWS entries as-is --
# "needs_attention" there is deliberately all-time (for the Rep Profile's
# Needs Attention tab), but every Leaderboard column is period-scoped, so
# all six of these stay period-scoped for consistency with each other.
LEADERBOARD_METRIC_FILTERS = {
    "sales": lambda df: df,
    "outbound": lambda df: df[df["sales_channel"].isin(OUTBOUND_CHANNELS)],
    "inbound": lambda df: df[df["sales_channel"].isin(INBOUND_CHANNELS)],
    "installs": _bulk_status_filter("Installed"),
    "pending": _bulk_status_filter("Pending"),
    "needs_attention": _bulk_status_filter("Needs Attention"),
}

LEADERBOARD_METRIC_TITLES = {
    "sales": "Sales",
    "outbound": "Outbound",
    "inbound": "Inbound",
    "installs": "Installs",
    "pending": "Pending",
    "needs_attention": "Needs Attention",
}


def get_leaderboard_metric_account_view(df, rep, metric, period, start, end):
    """Bulk Account View for a click on one of the Individual Rep
    Leaderboard's metric cells. Rep + period scoped exactly like a
    non-all_time BULK_ACCOUNT_VIEWS entry (mirrors get_bulk_account_view's
    own scoping steps), but the metric is a runtime value from the click
    rather than one baked into that registry, so it goes through
    LEADERBOARD_METRIC_FILTERS instead. Returns (title, []) for an unknown
    metric or empty scope, never raises."""
    row_filter = LEADERBOARD_METRIC_FILTERS.get(metric)
    if row_filter is None:
        return metric, []
    title = f"{LEADERBOARD_METRIC_TITLES[metric]} Accounts"
    if df is None or df.empty:
        return title, []
    scoped = df[df["sales_rep"] == rep] if rep else df
    scoped = filter_by_period(scoped, period, start, end)
    if scoped is None or scoped.empty:
        return title, []
    filtered = row_filter(scoped)
    if filtered.empty:
        return title, []
    filtered = filtered.assign(
        _scheduled_sort=pd.to_datetime(filtered["scheduled_date"], errors="coerce")
    ).sort_values("_scheduled_sort", na_position="last")
    return title, build_bulk_account_rows(filtered)


def get_calendar_day_account_view(df, rep, year, month, day):
    """Bulk Account View scoped to one calendar day (a Rep Sales Calendar
    day-cell click). Filters by sale_date, rep-scoped when `rep` is given,
    and intentionally independent of the page's period filter -- same
    reasoning as calculate_rep_monthly_activity()/calculate_calendar_sales()
    themselves: the calendar it's clicked from doesn't respect the period
    filter either, so neither should its drill-down. Returns (title, [])
    for an invalid date or empty scope, never raises."""
    try:
        target = date(year, month, day)
    except (TypeError, ValueError):
        return "Invalid Date", []
    title = target.strftime("%B %-d, %Y") + " Accounts"
    if df is None or df.empty:
        return title, []
    scoped = df[df["sales_rep"] == rep] if rep else df
    valid = scoped.dropna(subset=["sale_date"])
    if valid.empty:
        return title, []
    filtered = valid[valid["sale_date"].dt.date == target]
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
