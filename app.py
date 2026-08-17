from datetime import datetime
import os
import threading
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import attention_store
import db
from queries import (
    MAIN_SALES_QUERY,
    SERVICE_CANCELLATIONS_QUERY,
    VISION_PACKAGES_QUERY,
)
from sales_metrics import (
    BULK_ACCOUNT_VIEWS,
    REP_SORT_KEYS,
    REP_TABLE_COLUMNS,
    build_sales_dataset,
    calculate_calendar_sales,
    calculate_channel_breakdown,
    calculate_daily_averages,
    calculate_hourly_breakdown,
    calculate_monthly_forecast,
    calculate_monthly_sales_trend,
    calculate_needs_attention,
    calculate_records,
    calculate_rep_metrics,
    calculate_rep_monthly_activity,
    calculate_total_daily_sales,
    filter_by_period,
    get_bulk_account_view,
    get_calendar_day_account_view,
    get_channel_account_view,
    get_leaderboard_metric_account_view,
    get_rep_achievements,
    sort_rep_rows,
)

app = Flask(__name__)
app.jinja_env.globals["zip"] = zip

# The container clock runs in UTC, but every sale/scheduled date in the
# data is Eastern local time (SQL Server InsertDate). The "Last refreshed"
# timestamp below uses EASTERN_TZ for display; any "today"-relative
# comparison against the sales data must too -- see sales_metrics._today()
# and don't reintroduce a bare datetime.now().date() for "today" (that bug
# silently zeroed out Daily Sales / Records / View All Sales every evening
# until fixed 2026-08-15, since UTC rolls to the next day ~4-5 hours
# before Eastern does).
EASTERN_TZ = ZoneInfo("America/New_York")

_lock = threading.Lock()

data_store = {
    "planetweb_connected": None,
    "planetweb_error": None,
    "kpi_connected": None,
    "kpi_error": None,
    "main_sales": None,
    "main_sales_status": None,
    "main_sales_error": None,
    "service_cancellations": None,
    "service_cancellations_status": None,
    "service_cancellations_error": None,
    "vision_packages": None,
    "vision_packages_status": None,
    "vision_packages_error": None,
    "last_refreshed": None,
    "install_validation": None,
    "install_error": None,
}

# Excel "Main" worksheet column order, left to right, with Install Date/Status
# inserted where the Excel report places them. Anything not listed here keeps
# its original position, appended after these.
MAIN_SALES_COLUMN_ORDER = [
    "ID",
    "Date",
    "FirstName",
    "LastName",
    "Address",
    "ApartmentSuite",
    "City",
    "State",
    "Zipcode",
    "Municipality",
    "Speed",
    "Voice",
    "Scheduled",
    "StartDate",
    "Install Date",
    "Install Status",
    "SalesChannel_ID",
]


# ================================================================
# DATA LOADING
# ================================================================

def _load_planetweb():
    connected, error = db.test_planetweb_connection()
    data_store["planetweb_connected"] = connected
    data_store["planetweb_error"] = error

    if not connected:
        data_store["main_sales"] = None
        data_store["main_sales_status"] = "FAILED"
        data_store["main_sales_error"] = "Skipped: PlanetWeb SQL Server connection failed."
        return

    conn = None
    try:
        conn = db.get_planetweb_connection()
        data_store["main_sales"] = pd.read_sql(MAIN_SALES_QUERY, conn)
        data_store["main_sales_status"] = "SUCCESS"
        data_store["main_sales_error"] = None
    except Exception as exc:
        data_store["main_sales"] = None
        data_store["main_sales_status"] = "FAILED"
        data_store["main_sales_error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def _load_kpi():
    connected, error = db.test_kpi_connection()
    data_store["kpi_connected"] = connected
    data_store["kpi_error"] = error

    if not connected:
        for key in ("service_cancellations", "vision_packages"):
            data_store[key] = None
            data_store[f"{key}_status"] = "FAILED"
            data_store[f"{key}_error"] = "Skipped: KPI PostgreSQL connection failed."
        return

    conn = None
    try:
        conn = db.get_kpi_connection()

        try:
            data_store["service_cancellations"] = pd.read_sql(SERVICE_CANCELLATIONS_QUERY, conn)
            data_store["service_cancellations_status"] = "SUCCESS"
            data_store["service_cancellations_error"] = None
        except Exception as exc:
            conn.rollback()
            data_store["service_cancellations"] = None
            data_store["service_cancellations_status"] = "FAILED"
            data_store["service_cancellations_error"] = db.sanitize_error(exc)

        try:
            data_store["vision_packages"] = pd.read_sql(VISION_PACKAGES_QUERY, conn)
            data_store["vision_packages_status"] = "SUCCESS"
            data_store["vision_packages_error"] = None
        except Exception as exc:
            conn.rollback()
            data_store["vision_packages"] = None
            data_store["vision_packages_status"] = "FAILED"
            data_store["vision_packages_error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def reorder_main_sales_columns(df):
    priority = [c for c in MAIN_SALES_COLUMN_ORDER if c in df.columns]
    remaining = [c for c in df.columns if c not in priority]
    return df[priority + remaining]


def _print_install_validation():
    v = data_store.get("install_validation")
    if not v:
        return
    print("=" * 60)
    print("INSTALL DATE VALIDATION")
    print("=" * 60)
    print()
    print(f"Main Sales rows:             {v['main_sales_rows']:,}")
    print(f"Vision Packages rows:        {v['vision_packages_rows']:,}")
    print()
    print(f"Installed:                   {v['installed']:,}")
    print(f"Not Yet Installed:           {v['not_yet_installed']:,}")
    print()
    print(f"Main rows before merge:      {v['rows_before_merge']:,}")
    print(f"Main rows after merge:       {v['rows_after_merge']:,}")
    print()
    print(f"Duplicate rows created:      {v['duplicate_rows_created']:,}")
    print()
    print(f"Install Date mapping:        {v['mapping_status']}")
    print("=" * 60)


def _apply_install_date():
    """Reproduces the existing Excel 'Main' worksheet Install Date formula:

        =IFNA(INDEX('Vision Packages'!$Y:$Y,
                     MATCH(Main!$R2, 'Vision Packages'!$B:$B, 0)),
              "Not Yet Installed")

    i.e. Main.vi_subscriber_uuid -> Vision Packages.subscriber_uuid (first
    match only, exactly like Excel's MATCH) -> subscriber_first_invoice_date.
    Adds "Install Date" and "Install Status" to main_sales without ever
    changing the row count.
    """
    main_sales = data_store["main_sales"]
    vision_packages = data_store["vision_packages"]

    if main_sales is None:
        data_store["install_validation"] = None
        data_store["install_error"] = None
        return

    original_count = len(main_sales)
    merged = main_sales.copy()

    has_lookup_data = (
        vision_packages is not None
        and "subscriber_uuid" in vision_packages.columns
        and "subscriber_first_invoice_date" in vision_packages.columns
    )

    if has_lookup_data:
        # Excel's MATCH() returns only the FIRST match it finds. Reproduce
        # that by deduping the lookup table before merging, so a subscriber
        # with multiple Vision Packages rows never fans out a Main Sales row.
        install_lookup = vision_packages[
            ["subscriber_uuid", "subscriber_first_invoice_date"]
        ].drop_duplicates(subset=["subscriber_uuid"], keep="first")

        merged = merged.merge(
            install_lookup,
            how="left",
            left_on="vi_subscriber_uuid",
            right_on="subscriber_uuid",
        )
        install_dates = pd.to_datetime(merged["subscriber_first_invoice_date"], errors="coerce")
        merged = merged.drop(columns=["subscriber_uuid", "subscriber_first_invoice_date"])
    else:
        install_dates = pd.Series(pd.NaT, index=merged.index)

    final_count = len(merged)
    duplicate_rows_created = final_count - original_count

    formatted_dates = install_dates.dt.strftime("%m/%d/%Y")
    merged["Install Date"] = formatted_dates.fillna("Not Yet Installed")
    merged["Install Status"] = merged["Install Date"].apply(
        lambda v: "Not Yet Installed" if v == "Not Yet Installed" else "Installed"
    )

    data_store["main_sales"] = reorder_main_sales_columns(merged)

    installed_count = int((merged["Install Status"] == "Installed").sum())
    not_yet_count = int((merged["Install Status"] == "Not Yet Installed").sum())
    mapping_status = "SUCCESS" if duplicate_rows_created == 0 else "ERROR"

    data_store["install_validation"] = {
        "main_sales_rows": original_count,
        "vision_packages_rows": len(vision_packages) if vision_packages is not None else 0,
        "installed": installed_count,
        "not_yet_installed": not_yet_count,
        "rows_before_merge": original_count,
        "rows_after_merge": final_count,
        "duplicate_rows_created": duplicate_rows_created,
        "mapping_status": mapping_status,
    }
    data_store["install_error"] = (
        None
        if duplicate_rows_created == 0
        else (
            f"Install Date mapping created {duplicate_rows_created} extra row(s) — "
            "Main Sales record count no longer matches the source query. This should "
            "never happen; treat it as a data integrity error and investigate before "
            "trusting Install Date/Install Status on this page."
        )
    )
    _print_install_validation()


def _touch_last_refreshed():
    data_store["last_refreshed"] = datetime.now(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z")


def load_all_data():
    with _lock:
        _load_planetweb()
        _load_kpi()
        _apply_install_date()
        _touch_last_refreshed()


def ensure_data_loaded():
    # By request (2026-08-17): every page load re-queries both source
    # databases, rather than lazily loading once and serving stale
    # in-memory data until someone clicks "Refresh Data". This means
    # every full-page GET now costs a live PlanetWeb + KPI round trip
    # (Main Sales/Vision Packages/Service Cancellations) -- accepted
    # trade-off for always-current data over request latency. The
    # "Refresh Data" button (refresh() below) still works the same way,
    # just redundant with what a plain page load now already does.
    load_all_data()


# ================================================================
# HELPERS
# ================================================================

def dataframe_to_table(df, limit=500):
    if df is None:
        return {"columns": [], "rows": []}
    display_df = df.head(limit)
    columns = list(display_df.columns)
    rows = []
    for record in display_df.itertuples(index=False, name=None):
        rows.append(["" if pd.isna(value) else str(value) for value in record])
    return {"columns": columns, "rows": rows}


def unique_sorted_values(df, column):
    if df is None or column not in df.columns:
        return []
    values = df[column].dropna().unique().tolist()
    return sorted(str(v) for v in values)


def apply_main_sales_filters(df, args):
    filtered = df

    date_from = args.get("date_from")
    date_to = args.get("date_to")
    state = args.get("state")
    municipality = args.get("municipality")
    scheduled = args.get("scheduled")
    speed = args.get("speed")
    install_status = args.get("install_status")

    try:
        if date_from:
            filtered = filtered[pd.to_datetime(filtered["Date"]) >= pd.to_datetime(date_from)]
        if date_to:
            filtered = filtered[pd.to_datetime(filtered["Date"]) <= pd.to_datetime(date_to)]
    except Exception:
        pass

    if state:
        filtered = filtered[filtered["State"].astype(str) == state]
    if municipality:
        filtered = filtered[filtered["Municipality"].astype(str) == municipality]
    if scheduled:
        filtered = filtered[filtered["Scheduled"].astype(str) == scheduled]
    if speed:
        filtered = filtered[filtered["Speed"].astype(str) == speed]
    if install_status and "Install Status" in filtered.columns:
        filtered = filtered[filtered["Install Status"] == install_status]

    return filtered


def install_summary(df):
    if df is None or df.empty or "Install Status" not in df.columns:
        return {"total": 0, "installed": 0, "not_yet_installed": 0, "install_rate": 0.0}
    total = len(df)
    installed = int((df["Install Status"] == "Installed").sum())
    return {
        "total": total,
        "installed": installed,
        "not_yet_installed": total - installed,
        "install_rate": (installed / total * 100) if total else 0.0,
    }


def sales_over_time(df):
    if df is None or df.empty or "Date" not in df.columns:
        return []
    counts = (
        df.assign(Date=pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d"))
        .groupby("Date")
        .size()
        .reset_index(name="count")
        .sort_values("Date")
    )
    return [{"date": row.Date, "count": int(row.count)} for row in counts.itertuples()]


def report_period(df):
    if df is None or df.empty or "Date" not in df.columns:
        return None
    dates = pd.to_datetime(df["Date"])
    return {
        "start": dates.min().strftime("%b %-d, %Y"),
        "end": dates.max().strftime("%b %-d, %Y"),
    }


def _format_note_timestamp(dt):
    if dt is None:
        return ""
    return dt.astimezone(EASTERN_TZ).strftime("%b %-d, %Y · %-I:%M %p")


def _serialize_note(note):
    """Shapes one attention_store note dict for a JSON response -- same
    display fields attach_attention_metadata() adds for the server-rendered
    page, so the initial render and every AJAX update after it are always
    byte-identical in how a note is presented."""
    return {
        "note": note["note"],
        "attention_status": note["attention_status"],
        "previous_status": note["previous_status"],
        "created_at_display": _format_note_timestamp(note["created_at"]),
        "created_by": note["created_by"],
    }


def attach_attention_metadata(accounts):
    """Needs Attention workflow only: mutates `accounts` in place, adding
    `attention_status` (None if unclassified) and `attention_notes`
    (newest first, each with a display-formatted timestamp) to every
    account dict from attention_store.get_attention_overview(). Returns
    (attention_available, attention_progress) for the page header/progress
    bar -- see attention_store.py's module docstring for the availability
    contract. Purely additive: `accounts` already has everything the Bulk
    Account View needs from the source data alone, so a down appdb still
    leaves a fully renderable (just unclassified-looking) account list.
    Callers must only invoke this for the needs_attention view -- see
    bulk_account_view()/rep_profile() below."""
    sale_ids = [a["sale_id"] for a in accounts]
    available, status_map, notes_map, progress = attention_store.get_attention_overview(sale_ids)
    for account in accounts:
        notes = notes_map.get(account["sale_id"], [])
        account["attention_status"] = status_map.get(account["sale_id"])
        account["attention_notes"] = [
            {**note, "created_at_display": _format_note_timestamp(note["created_at"])}
            for note in notes
        ]
    return available, progress


# ================================================================
# ROUTES
# ================================================================

@app.route("/")
def index():
    return redirect(url_for("overview"))


@app.route("/overview")
def overview():
    ensure_data_loaded()
    main_sales = data_store["main_sales"]
    vision_packages = data_store["vision_packages"]
    service_cancellations = data_store["service_cancellations"]

    return render_template(
        "overview.html",
        active_page="overview",
        planetweb_connected=data_store["planetweb_connected"],
        planetweb_error=data_store["planetweb_error"],
        kpi_connected=data_store["kpi_connected"],
        kpi_error=data_store["kpi_error"],
        last_refreshed=data_store["last_refreshed"],
        gross_sales=len(main_sales) if main_sales is not None else 0,
        vision_package_count=len(vision_packages) if vision_packages is not None else 0,
        cancellation_count=len(service_cancellations) if service_cancellations is not None else 0,
        main_sales_status=data_store["main_sales_status"],
        service_cancellations_status=data_store["service_cancellations_status"],
        vision_packages_status=data_store["vision_packages_status"],
        chart_data=sales_over_time(main_sales),
        report_period=report_period(main_sales),
    )


@app.route("/main-sales")
def main_sales_page():
    ensure_data_loaded()
    df = data_store["main_sales"]
    filtered = apply_main_sales_filters(df, request.args) if df is not None else None
    table = dataframe_to_table(filtered if filtered is not None else df)

    return render_template(
        "main_sales.html",
        active_page="main_sales",
        refresh_dataset="main_sales",
        database_label="PlanetWeb / SQL Server",
        connection_status=data_store["planetweb_connected"],
        status=data_store["main_sales_status"],
        error=data_store["main_sales_error"],
        install_error=data_store["install_error"],
        total_rows=len(df) if df is not None else 0,
        filtered_rows=len(filtered) if filtered is not None else 0,
        total_columns=len(df.columns) if df is not None else 0,
        displayed_rows=len(table["rows"]),
        table=table,
        install_summary=install_summary(filtered),
        filters={
            "date_from": request.args.get("date_from", ""),
            "date_to": request.args.get("date_to", ""),
            "state": request.args.get("state", ""),
            "municipality": request.args.get("municipality", ""),
            "scheduled": request.args.get("scheduled", ""),
            "speed": request.args.get("speed", ""),
            "install_status": request.args.get("install_status", ""),
        },
        state_options=unique_sorted_values(df, "State"),
        municipality_options=unique_sorted_values(df, "Municipality"),
        scheduled_options=unique_sorted_values(df, "Scheduled"),
        speed_options=unique_sorted_values(df, "Speed"),
        install_status_options=["Installed", "Not Yet Installed"],
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/dashboard")
def dashboard_page():
    ensure_data_loaded()

    # Built from the DataFrames already sitting in memory -- no extra
    # database calls happen on this request.
    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )

    period = request.args.get("period", "this_month")
    custom_start = request.args.get("start", "")
    custom_end = request.args.get("end", "")
    search = request.args.get("search", "").strip()
    selected_rep = request.args.get("rep", "")

    period_df = filter_by_period(normalized, period, custom_start, custom_end)

    rep_rows = calculate_rep_metrics(period_df)
    total_rep_count = len(rep_rows)

    if search:
        rep_rows = [r for r in rep_rows if search.lower() in r["sales_rep"].lower()]

    if not selected_rep and rep_rows:
        selected_rep = rep_rows[0]["sales_rep"]

    # Sorting is a pure display-ordering concern layered on top of the
    # rows above -- applied last so it never affects total_rep_count, the
    # search filter, or which rep gets auto-selected by default.
    sort = request.args.get("sort", "rank")
    sort_dir = request.args.get("dir", "asc")
    if sort not in REP_SORT_KEYS:
        sort, sort_dir = "rank", "asc"
    rep_rows = sort_rep_rows(rep_rows, sort, sort_dir)

    rep_table_columns = []
    for key, label, default_dir in REP_TABLE_COLUMNS:
        is_active = key == sort
        rep_table_columns.append({
            "key": key,
            "label": label,
            "active": is_active,
            "dir": sort_dir if is_active else None,
            "next_dir": ("asc" if sort_dir == "desc" else "desc") if is_active else default_dir,
        })

    # Records, the Sales Calendar, and the Team Overview KPI bar are always
    # ALL TIME / current-month / today, regardless of the selected
    # dashboard period -- same reasoning as Records: they answer questions
    # the period filter isn't meant to control.
    records = calculate_records(normalized)
    total_daily_sales = calculate_total_daily_sales(normalized)
    daily_averages = calculate_daily_averages(normalized)

    # Eastern, not the container's raw UTC clock -- the Sales Calendar's
    # default month must agree with calculate_calendar_sales()'s own
    # Eastern "today" (see _today() in sales_metrics.py), or the "is_today"
    # cell could point at the wrong day/month for part of the evening.
    today = datetime.now(EASTERN_TZ)
    try:
        cal_year = int(request.args.get("cal_year", today.year))
        cal_month = int(request.args.get("cal_month", today.month))
        if not 1 <= cal_month <= 12:
            raise ValueError
    except (TypeError, ValueError):
        cal_year, cal_month = today.year, today.month

    calendar_data = calculate_calendar_sales(normalized, cal_year, cal_month)
    monthly_forecast = calculate_monthly_forecast(normalized)

    # Always all-time / independent of the period filter -- a 12-month
    # trend isn't meaningful squeezed into a single period-filter window,
    # same reasoning as Records/Calendar above.
    monthly_sales_trend = calculate_monthly_sales_trend(normalized)

    # Team-wide, period-filtered -- same convention as the Individual Rep
    # Leaderboard table (period_df), just not scoped to one rep. Reuses
    # the exact same functions the Rep Profile's Channel Performance /
    # Time-of-Day Performance sections already use.
    channel_breakdown = calculate_channel_breakdown(period_df)
    hourly_breakdown = calculate_hourly_breakdown(period_df)

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        period=period,
        custom_start=custom_start,
        custom_end=custom_end,
        search=search,
        selected_rep=selected_rep,
        total_rep_count=total_rep_count,
        total_daily_sales=total_daily_sales,
        daily_averages=daily_averages,
        rep_rows=rep_rows,
        rep_table_columns=rep_table_columns,
        sort=sort,
        sort_dir=sort_dir,
        records=records,
        calendar_data=calendar_data,
        monthly_forecast=monthly_forecast,
        monthly_sales_trend=monthly_sales_trend,
        channel_breakdown=channel_breakdown,
        hourly_breakdown=hourly_breakdown,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/dashboard/reps/<rep_name>")
def rep_profile(rep_name):
    ensure_data_loaded()

    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )

    known_reps = (
        set(normalized["sales_rep"].unique())
        if normalized is not None and not normalized.empty
        else set()
    )
    if normalized is not None and rep_name not in known_reps:
        abort(404)

    period = request.args.get("period", "this_month")
    custom_start = request.args.get("start", "")
    custom_end = request.args.get("end", "")

    period_df = filter_by_period(normalized, period, custom_start, custom_end)

    # Same function/output the Rep Leaderboard table uses, so the profile
    # and leaderboard numbers can never disagree for a given period.
    rep_rows = calculate_rep_metrics(period_df)
    rep_metrics = next((r for r in rep_rows if r["sales_rep"] == rep_name), None) or {
        "sales_rep": rep_name,
        "sales": 0,
        "inbound": 0,
        "outbound": 0,
        "installs": 0,
        "pending": 0,
        "cancels": 0,
        "needs_attention": 0,
        "install_rate": None,
        "cancel_rate": None,
    }

    rep_period_df = (
        period_df[period_df["sales_rep"] == rep_name]
        if period_df is not None and not period_df.empty
        else None
    )
    channel_breakdown = calculate_channel_breakdown(rep_period_df)
    hourly_breakdown = calculate_hourly_breakdown(rep_period_df)

    # Eastern, not the container's raw UTC clock -- same reasoning as the
    # Team Leaderboard's own cal_year/cal_month block (see _today() in
    # sales_metrics.py). Independent of the page's period filter, same
    # convention as the Team Leaderboard's Sales Calendar.
    today = datetime.now(EASTERN_TZ)
    try:
        cal_year = int(request.args.get("cal_year", today.year))
        cal_month = int(request.args.get("cal_month", today.month))
        if not 1 <= cal_month <= 12:
            raise ValueError
    except (TypeError, ValueError):
        cal_year, cal_month = today.year, today.month

    rep_monthly_activity = calculate_rep_monthly_activity(normalized, rep_name, cal_year, cal_month)

    # Always all-time, independent of the period filter -- see
    # calculate_needs_attention()'s docstring.
    needs_attention = calculate_needs_attention(normalized, rep_name)

    # Cosmetic achievement badges (icons next to the rep's name) -- see
    # get_rep_achievements()'s docstring. Always all-time, purely display.
    achievements = get_rep_achievements(normalized, rep_name)

    # Needs Attention workflow controls -- see attach_attention_metadata()
    # and README.md "Needs Attention Workflow". needs_attention_count
    # stays len(needs_attention) below, computed before this call and
    # completely unaffected by it -- Attention Status is metadata attached
    # to the same accounts, never a filter over them.
    attention_available, attention_progress = attach_attention_metadata(needs_attention)

    return render_template(
        "rep_profile.html",
        active_page="dashboard",
        rep_name=rep_name,
        period=period,
        custom_start=custom_start,
        custom_end=custom_end,
        rep_metrics=rep_metrics,
        channel_breakdown=channel_breakdown,
        hourly_breakdown=hourly_breakdown,
        rep_monthly_activity=rep_monthly_activity,
        needs_attention=needs_attention,
        needs_attention_count=len(needs_attention),
        attention_available=attention_available,
        attention_progress=attention_progress,
        attention_statuses=attention_store.ATTENTION_STATUSES,
        achievements=achievements,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/dashboard/reps/<rep_name>/accounts")
def bulk_account_view(rep_name):
    ensure_data_loaded()

    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )

    known_reps = (
        set(normalized["sales_rep"].unique())
        if normalized is not None and not normalized.empty
        else set()
    )
    if normalized is not None and rep_name not in known_reps:
        abort(404)

    view = request.args.get("view", "")
    period = request.args.get("period", "this_month")
    custom_start = request.args.get("start", "")
    custom_end = request.args.get("end", "")
    metric = request.args.get("metric", "")

    # "channel", "date", and "metric" are runtime-parameterized drill-downs
    # (from the Rep Profile's SalesByChannelChart / Sales Activity
    # calendar, and the Individual Rep Leaderboard's metric cells,
    # respectively) -- they deliberately bypass BULK_ACCOUNT_VIEWS (whose
    # filters take no runtime argument) rather than being added to it. The
    # 3 registry entries below (pending/needs_attention/all_sales) are
    # unchanged.
    if view == "channel":
        channel = request.args.get("channel", "")
        view_title, accounts = get_channel_account_view(normalized, rep_name, channel, period, custom_start, custom_end)
        all_time = False
    elif view == "metric":
        view_title, accounts = get_leaderboard_metric_account_view(normalized, rep_name, metric, period, custom_start, custom_end)
        all_time = False
    elif view == "date":
        try:
            view_title, accounts = get_calendar_day_account_view(
                normalized, rep_name,
                int(request.args.get("year", 0)), int(request.args.get("month", 0)), int(request.args.get("day", 0)),
            )
        except (TypeError, ValueError):
            abort(404)
        all_time = True
    elif view in BULK_ACCOUNT_VIEWS:
        view_title, accounts = get_bulk_account_view(normalized, rep_name, view, period, custom_start, custom_end)
        all_time = BULK_ACCOUNT_VIEWS[view]["all_time"]
    else:
        abort(404)

    # Needs Attention workflow controls (Attention Status + notes) only
    # ever attach to the needs_attention view -- every other Bulk Account
    # View (Pending, All Sales, channel/metric/date drill-downs) renders
    # exactly as before, untouched. See attach_attention_metadata()'s
    # docstring and README.md "Needs Attention Workflow".
    attention_available = None
    attention_progress = None
    if view == "needs_attention":
        attention_available, attention_progress = attach_attention_metadata(accounts)

    return render_template(
        "bulk_account_view.html",
        active_page="dashboard",
        rep_name=rep_name,
        view=view,
        view_title=view_title,
        metric=metric,
        accounts=accounts,
        period=period,
        custom_start=custom_start,
        custom_end=custom_end,
        all_time=all_time,
        attention_available=attention_available,
        attention_progress=attention_progress,
        attention_statuses=attention_store.ATTENTION_STATUSES,
        last_refreshed=data_store["last_refreshed"],
    )


def _known_sale_id(sale_id):
    """True if sale_id belongs to a real account in the currently loaded
    normalized dataset. Every attention write route checks this before
    touching attention_store -- a sale_id is a value the browser sends
    back to us, so it must never be trusted to correspond to an account
    this app can actually see without checking first."""
    ensure_data_loaded()
    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )
    if normalized is None or normalized.empty:
        return False
    return sale_id in set(normalized["sale_id"])


@app.route("/dashboard/attention/<int:sale_id>/status", methods=["POST"])
def attention_set_status(sale_id):
    """Sets/changes an account's Attention Status -- the only write path
    that can ever create or move an account_attention row, which is what
    makes 'a status always has a note' structural rather than a rule that
    has to be remembered in two places. See attention_store.set_attention_status()."""
    if not _known_sale_id(sale_id):
        abort(404)

    payload = request.get_json(silent=True) or {}
    ok, error, notes = attention_store.set_attention_status(
        sale_id,
        (payload.get("status") or "").strip(),
        payload.get("note") or "",
        payload.get("author") or "",
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    current_status = notes[0]["attention_status"] if notes else None
    return jsonify({
        "ok": True,
        "sale_id": sale_id,
        "attention_status": current_status,
        "notes": [_serialize_note(n) for n in notes],
    })


@app.route("/dashboard/attention/<int:sale_id>/notes", methods=["POST"])
def attention_add_note(sale_id):
    """Appends a note without changing Attention Status. Requires the
    account to already be classified -- see attention_store.add_note()."""
    if not _known_sale_id(sale_id):
        abort(404)

    payload = request.get_json(silent=True) or {}
    ok, error, notes = attention_store.add_note(
        sale_id,
        payload.get("note") or "",
        payload.get("author") or "",
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    current_status = notes[0]["attention_status"] if notes else None
    return jsonify({
        "ok": True,
        "sale_id": sale_id,
        "attention_status": current_status,
        "notes": [_serialize_note(n) for n in notes],
    })


@app.route("/dashboard/accounts")
def all_sales_view():
    """Team-wide (not rep-scoped) Bulk Account View -- reached via the
    "View All Sales" button on the Planet Networks Records section.
    Defaults to today, unlike bulk_account_view() above which defaults to
    this_month, since the button is meant to answer "what happened today"
    at a glance."""
    ensure_data_loaded()

    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )

    view = request.args.get("view", "all_sales")
    period = request.args.get("period", "today")
    custom_start = request.args.get("start", "")
    custom_end = request.args.get("end", "")

    # "channel" is a runtime-parameterized drill-down from the Team
    # Leaderboard's Channel Performance chart -- same reasoning as its
    # rep-scoped counterpart in bulk_account_view() above: it deliberately
    # bypasses BULK_ACCOUNT_VIEWS since the channel is a click value, not
    # a filter fixed at import time. rep=None here scopes team-wide.
    if view == "channel":
        channel = request.args.get("channel", "")
        view_title, accounts = get_channel_account_view(normalized, None, channel, period, custom_start, custom_end)
        all_time = False
    elif view in BULK_ACCOUNT_VIEWS:
        view_title, accounts = get_bulk_account_view(normalized, None, view, period, custom_start, custom_end)
        all_time = BULK_ACCOUNT_VIEWS[view]["all_time"]
    else:
        abort(404)

    # See the matching block in bulk_account_view() above -- team-wide
    # Needs Attention (reachable via ?view=needs_attention here, same
    # registry entry) gets the same workflow controls, gated the same way.
    attention_available = None
    attention_progress = None
    if view == "needs_attention":
        attention_available, attention_progress = attach_attention_metadata(accounts)

    return render_template(
        "bulk_account_view.html",
        active_page="dashboard",
        rep_name=None,
        view=view,
        view_title=view_title,
        accounts=accounts,
        period=period,
        custom_start=custom_start,
        custom_end=custom_end,
        all_time=all_time,
        attention_available=attention_available,
        attention_progress=attention_progress,
        attention_statuses=attention_store.ATTENTION_STATUSES,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/cancellations")
def cancellations_page():
    ensure_data_loaded()
    df = data_store["service_cancellations"]
    table = dataframe_to_table(df)

    return render_template(
        "cancellations.html",
        active_page="cancellations",
        refresh_dataset="service_cancellations",
        database_label="KPI Reporting / PostgreSQL",
        connection_status=data_store["kpi_connected"],
        status=data_store["service_cancellations_status"],
        error=data_store["service_cancellations_error"],
        total_rows=len(df) if df is not None else 0,
        total_columns=len(df.columns) if df is not None else 0,
        displayed_rows=len(table["rows"]),
        table=table,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/vision-packages")
def vision_packages_page():
    ensure_data_loaded()
    df = data_store["vision_packages"]
    table = dataframe_to_table(df)

    return render_template(
        "vision_packages.html",
        active_page="vision_packages",
        refresh_dataset="vision_packages",
        database_label="KPI Reporting / PostgreSQL",
        connection_status=data_store["kpi_connected"],
        status=data_store["vision_packages_status"],
        error=data_store["vision_packages_error"],
        total_rows=len(df) if df is not None else 0,
        total_columns=len(df.columns) if df is not None else 0,
        displayed_rows=len(table["rows"]),
        table=table,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/refresh", methods=["POST"])
def refresh():
    dataset = request.args.get("dataset", "all")

    with _lock:
        if dataset == "main_sales":
            _load_planetweb()
        elif dataset in ("service_cancellations", "vision_packages"):
            _load_kpi()
        else:
            _load_planetweb()
            _load_kpi()
        # Install Date depends on both main_sales and vision_packages, so
        # recompute it after any refresh that could have touched either one.
        _apply_install_date()
        _touch_last_refreshed()

    return redirect(request.referrer or url_for("overview"))


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "planetweb_connected": data_store["planetweb_connected"],
            "kpi_connected": data_store["kpi_connected"],
            "last_refreshed": data_store["last_refreshed"],
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "3005"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
