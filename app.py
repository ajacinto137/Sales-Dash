from datetime import datetime
import os
import threading
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for

import attention_store
import auth
import db
import email_service
import import_service
import marketing_cleaning
import marketing_data
import marketing_metrics
import needs_attention_service
import permissions
import user_store
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
    calculate_sales_volume_trend,
    calculate_total_daily_sales,
    filter_by_period,
    get_bulk_account_view,
    get_calendar_day_account_view,
    get_channel_account_view,
    get_leaderboard_metric_account_view,
    get_rep_achievements,
    search_dataset,
    sort_rep_rows,
)

app = Flask(__name__)
app.jinja_env.globals["zip"] = zip

# Signed-cookie session secret -- required for auth.py's session to work
# at all (Flask refuses to set a session cookie without one). No
# fallback default: an app handling real logins must never silently run
# with a guessable/empty key. Generate one with
# `python3 -c "import secrets; print(secrets.token_hex(32))"` and put it
# in .env as SECRET_KEY (see README.md "Authentication").
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Add one to .env -- see README.md \"Authentication\" "
        "for how to generate it. The app refuses to start without it once logins are "
        "involved, since Flask's session cookie can't be signed securely otherwise."
    )

INITIAL_ADMIN_EMAIL = os.environ.get("INITIAL_ADMIN_EMAIL", "avelino@planet.net")


@app.context_processor
def inject_auth_context():
    """Makes current_user/permissions available in every template without
    passing them explicitly in every render_template() call -- used by
    _topnav.html to show/hide Admin-only nav items and by any page that
    needs to know who's logged in."""
    return {"current_user": auth.current_user(), "permissions": permissions}

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


def _sync_reps_and_needs_attention():
    """Keeps sales_reps (user_store.py) and needs_attention_tracking
    (needs_attention_service.py) in lockstep with the source data on
    every refresh -- sales_reps gets any newly-seen rep name;
    needs_attention_tracking gets a first_seen_at row for any account
    newly in Needs Attention, and loses the row for any account that's
    left, so a later re-entry starts a fresh 15-day clock. Silently
    no-ops on appdb failure inside each function -- must never block the
    dashboard itself from loading."""
    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )
    if normalized is None or normalized.empty:
        return
    user_store.sync_sales_reps(normalized["sales_rep"].dropna().unique().tolist())
    needs_attention_ids = normalized.loc[normalized["status"] == "Needs Attention", "sale_id"].tolist()
    needs_attention_service.sync_tracking(needs_attention_ids)


_admin_bootstrap_done = False


def _bootstrap_initial_admin():
    """Creates the initial Admin account (INITIAL_ADMIN_EMAIL, default
    avelino@planet.net) the very first time this process ever finds it
    missing -- no manual database work, no .env editing beyond the
    one-time deployment config (SECRET_KEY/APPDB_*/SMTP_*) this app
    already requires. Sends a first-time setup email; if SMTP isn't
    configured yet (a fresh deployment), email_service logs the setup
    link to stdout instead (see that module's docstring) so the very
    first Admin is never locked out waiting on email to be live.
    Runs at most once per process; a still-pending admin gets the
    current valid link re-logged (never re-emailed) on every later call
    in this process, so a restart before setup is finished doesn't spam
    a real inbox but also never leaves you stuck. See README.md
    "First-Time Admin Setup"."""
    global _admin_bootstrap_done
    if _admin_bootstrap_done:
        return
    user = user_store.get_user_by_email(INITIAL_ADMIN_EMAIL)
    if user is None:
        ok, error, user = user_store.create_user(INITIAL_ADMIN_EMAIL, "Admin")
        if not ok:
            print(f"Could not bootstrap initial Admin account: {error}")
            return
    if user["status"] == "pending":
        token_ok, token_error, raw_token = user_store.create_token(user["id"], "setup")
        if token_ok:
            email_service.send_setup_email(user, raw_token)
    _admin_bootstrap_done = True


def load_all_data():
    with _lock:
        _load_planetweb()
        _load_kpi()
        _apply_install_date()
        _sync_reps_and_needs_attention()
        _touch_last_refreshed()
        _bootstrap_initial_admin()


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
    `attention_status` (None if unclassified), `attention_notes` (newest
    first, each with a display-formatted timestamp), and `can_work`/
    `cannot_work_reason` (the CURRENT user's permission for this specific
    account, from the one authoritative permissions.can_work_account() --
    used only to proactively disable Classify/Add Note in the UI; the
    real enforcement is server-side on every write, see
    _authorize_account_action() above) to every account dict. Returns
    (attention_available, attention_progress) for the page header/progress
    bar -- see attention_store.py's module docstring for the availability
    contract. Purely additive: `accounts` already has everything the Bulk
    Account View needs from the source data alone, so a down appdb still
    leaves a fully renderable (just unclassified-looking) account list.
    Callers must only invoke this for the needs_attention view -- see
    bulk_account_view()/rep_profile() below."""
    sale_ids = [a["sale_id"] for a in accounts]
    available, status_map, notes_map, progress = attention_store.get_attention_overview(sale_ids)
    since_map = needs_attention_service.get_first_seen_map(sale_ids)
    user = auth.current_user()
    for account in accounts:
        sale_id = account["sale_id"]
        notes = notes_map.get(sale_id, [])
        account["attention_status"] = status_map.get(sale_id)
        account["attention_notes"] = [
            {**note, "created_at_display": _format_note_timestamp(note["created_at"])}
            for note in notes
        ]
        can_work, reason = permissions.can_work_account(user, account.get("sales_rep"), since_map.get(sale_id))
        account["can_work"] = can_work
        account["cannot_work_reason"] = reason
    return available, progress


# Team/NJ/NY/VA "Sales Rep Group" split, by the sale's `state` column --
# shared by dashboard_page() (Sales Calendar & Monthly Sales Trend's own
# toggle) and all_sales_view() (that same calendar's day-cell drill-down,
# added 2026-08-19, so a day's Bulk Account View is scoped to whichever
# state view was showing when it was clicked, same reasoning as
# get_calendar_day_account_view()'s rep-scoping). Module-level (not a
# per-route local) specifically so both routes read the one definition
# rather than risking two independently-maintained state filters
# drifting apart -- "state" is the normalized dataset's passthrough of
# PlanetWeb's State column (build_sales_dataset() in sales_metrics.py),
# upper-cased/stripped for the same reason build_vision_url()'s
# VISION_BASE_URLS comparison already does, since the raw column isn't
# guaranteed clean.
DASHBOARD_STATE_VIEWS = [("team", "Team", None), ("nj", "NJ", "NJ"), ("ny", "NY", "NY"), ("va", "VA", "VA")]
DASHBOARD_STATE_VIEW_KEYS = {key for key, _, _ in DASHBOARD_STATE_VIEWS}


def _state_filtered(df, state_code):
    if state_code is None:
        return df
    if df is None or df.empty or "state" not in df.columns:
        return df
    return df[df["state"].astype(str).str.strip().str.upper() == state_code]


def _state_code_for_view(view_key):
    for key, _, state_code in DASHBOARD_STATE_VIEWS:
        if key == view_key:
            return state_code
    return None


# ================================================================
# ROUTES
# ================================================================

@app.route("/")
def index():
    # Not /overview -- that's Admin-only as of 2026-08-18, and most users
    # of this app now are not Admins. /dashboard's own @login_required
    # sends a logged-out visitor to /login (with ?next= back to here).
    return redirect(url_for("dashboard_page"))


@app.route("/overview")
@auth.admin_required
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
@auth.admin_required
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
@auth.login_required
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

    # Team + one row per state, so the Sales Calendar and Monthly Sales
    # Trend can each show 4 independently-selectable views without adding
    # another row to the page (a segmented toggle switches between them
    # client-side -- see wireCalendarToggle()/wireChartToggle() in
    # charts.js). DASHBOARD_STATE_VIEWS/_state_filtered() are module-level
    # (see above ROUTES) so all_sales_view()'s day-cell drill-down reads
    # the exact same state split, not a second copy of it.
    dashboard_view_options = [{"key": key, "label": label} for key, label, _ in DASHBOARD_STATE_VIEWS]
    valid_view_keys = DASHBOARD_STATE_VIEW_KEYS

    cal_view = request.args.get("cal_view", "team")
    if cal_view not in valid_view_keys:
        cal_view = "team"
    trend_view = request.args.get("trend_view", "team")
    if trend_view not in valid_view_keys:
        trend_view = "team"

    state_filtered_normalized = {key: _state_filtered(normalized, state_code) for key, _, state_code in DASHBOARD_STATE_VIEWS}

    calendar_views = {key: calculate_calendar_sales(view_df, cal_year, cal_month) for key, view_df in state_filtered_normalized.items()}
    monthly_forecast = calculate_monthly_forecast(normalized)

    # Always all-time / independent of the period filter -- a 12-month
    # trend isn't meaningful squeezed into a single period-filter window,
    # same reasoning as Records/Calendar above.
    monthly_trend_views = {key: calculate_monthly_sales_trend(view_df) for key, view_df in state_filtered_normalized.items()}

    # Team-wide, period-filtered -- same convention as the Individual Rep
    # Leaderboard table (period_df), just not scoped to one rep. Reuses
    # the exact same functions the Rep Profile's Channel Performance /
    # Time-of-Day Performance sections already use.
    channel_breakdown = calculate_channel_breakdown(period_df)
    hourly_breakdown = calculate_hourly_breakdown(period_df)

    # Sales Volume tab (default tab of Channel & Time-of-Day Performance,
    # added 2026-08-18, team selector reworked 2026-08-19) -- one
    # cumulative-outbound-sales-per-rep line per Sales Rep TEAM (Junior/
    # NJ - Sales Reps/NY - Sales Reps/VA - Sales Reps -- sales_reps.team,
    # see user_store.SALES_REP_TEAMS), always the current month,
    # independent of the page's period filter (same reasoning as
    # Records/Calendar above). This is a DIFFERENT grouping from the
    # Sales Calendar & Monthly Sales Trend section's Team/NJ/NY/VA above
    # (that one is the sale's `state` column; this one is the rep's own
    # assigned team) -- they intentionally don't share
    # state_filtered_normalized/dashboard_view_options.
    #
    # Filtered by team assignment ALONE (changed 2026-08-19, by request,
    # reversing the 2026-08-18 "only users with the Sales Rep role"
    # filter) -- deliberate exception to "ownership is always read from
    # source data, never users/sales_reps" (see README.md "User Records
    # vs Sales Rep Records"), but only via sales_reps.team, not users.
    # The Admin Portal's Sales Reps tab already requires an explicit
    # per-rep Admin action to assign a team, and that assignment needs no
    # `users` row at all (see "User Records vs Sales Rep Records") -- an
    # additional "also has a Sales Rep-role login" requirement turned out
    # to make the chart show nobody at all in a real deployment (found
    # 2026-08-19: assigning a rep's team from the Sales Reps tab is not
    # the same action as creating them a login, so almost no rep ever
    # satisfied both at once). A rep with no team assigned is still
    # correctly excluded from every team panel (never guessed at -- see
    # db_migrations.py migration 3); that's the only filter this chart
    # applies now.
    def _team_slug(team_name):
        return team_name.lower().replace(" ", "-")

    team_view_options = [{"key": _team_slug(team), "label": team} for team in user_store.SALES_REP_TEAMS]
    sales_reps_by_team = user_store.list_sales_reps_by_team()

    sales_volume_views = {}
    team_has_reps = {}
    for opt, team in zip(team_view_options, user_store.SALES_REP_TEAMS):
        rep_names = sales_reps_by_team.get(team, [])
        team_has_reps[opt["key"]] = bool(rep_names)
        if normalized is not None and not normalized.empty and rep_names:
            team_df = normalized[normalized["sales_rep"].isin(rep_names)]
        else:
            team_df = normalized.iloc[0:0] if normalized is not None else None
        sales_volume_views[opt["key"]] = calculate_sales_volume_trend(team_df)

    valid_volume_keys = {opt["key"] for opt in team_view_options}
    volume_view = request.args.get("volume_view", team_view_options[0]["key"])
    if volume_view not in valid_volume_keys:
        volume_view = team_view_options[0]["key"]

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
        calendar_views=calendar_views,
        cal_view=cal_view,
        monthly_forecast=monthly_forecast,
        monthly_trend_views=monthly_trend_views,
        trend_view=trend_view,
        dashboard_view_options=dashboard_view_options,
        channel_breakdown=channel_breakdown,
        hourly_breakdown=hourly_breakdown,
        sales_volume_views=sales_volume_views,
        volume_view=volume_view,
        team_view_options=team_view_options,
        team_has_reps=team_has_reps,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/dashboard/reps/<rep_name>")
@auth.login_required
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
        "outbound_installs": 0,
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
@auth.login_required
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


def _lookup_account_owner(sale_id):
    """(found, owner_rep_name) for a sale_id against the currently loaded
    normalized dataset. Every attention write route checks `found` before
    touching attention_store -- a sale_id is a value the browser sends
    back to us, so it must never be trusted to correspond to an account
    this app can actually see without checking first. `owner_rep_name` is
    whatever sales_metrics.py already attributes the sale to (never
    changed by this module -- see permissions.py/README.md "Needs
    Attention Ownership") and feeds permissions.can_work_account()."""
    ensure_data_loaded()
    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )
    if normalized is None or normalized.empty:
        return False, None
    matches = normalized.loc[normalized["sale_id"] == sale_id, "sales_rep"]
    if matches.empty:
        return False, None
    owner = matches.iloc[0]
    return True, (owner if pd.notna(owner) else None)


def _authorize_account_action(sale_id):
    """Shared by both attention write routes below: looks up the account,
    404s if it isn't real, then runs the ONE authoritative permission
    check (permissions.can_work_account()) for the current user against
    it. Returns (owner_rep, current_user) on success; sends a 403 JSON
    response and returns None otherwise. needs_attention_since comes from
    needs_attention_service, never inferred from install/sale/invoice
    dates (see that module's docstring)."""
    found, owner_rep = _lookup_account_owner(sale_id)
    if not found:
        abort(404)

    user = auth.current_user()
    since = needs_attention_service.get_needs_attention_since(sale_id)
    allowed, reason = permissions.can_work_account(user, owner_rep, since)
    if not allowed:
        return None, None, (jsonify({"ok": False, "error": reason}), 403)
    return owner_rep, user, None


@app.route("/dashboard/attention/<int:sale_id>/status", methods=["POST"])
@auth.login_required
def attention_set_status(sale_id):
    """Sets/changes an account's Attention Status -- the only write path
    that can ever create or move an account_attention row, which is what
    makes 'a status always has a note' structural rather than a rule that
    has to be remembered in two places. See attention_store.set_attention_status().
    Server-side permission check (permissions.can_work_account()) runs
    before anything is written -- the 15-day ownership rule is never
    enforced by hiding a button alone."""
    owner_rep, user, denied = _authorize_account_action(sale_id)
    if denied:
        return denied

    payload = request.get_json(silent=True) or {}
    ok, error, notes = attention_store.set_attention_status(
        sale_id,
        (payload.get("status") or "").strip(),
        payload.get("note") or "",
        user,
        owner_rep=owner_rep,
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    user_store.record_needs_attention_activity(user["id"])

    current_status = notes[0]["attention_status"] if notes else None
    return jsonify({
        "ok": True,
        "sale_id": sale_id,
        "attention_status": current_status,
        "notes": [_serialize_note(n) for n in notes],
    })


@app.route("/dashboard/attention/<int:sale_id>/notes", methods=["POST"])
@auth.login_required
def attention_add_note(sale_id):
    """Appends a note without changing Attention Status. Requires the
    account to already be classified -- see attention_store.add_note().
    Same server-side permission check as attention_set_status() above."""
    owner_rep, user, denied = _authorize_account_action(sale_id)
    if denied:
        return denied

    payload = request.get_json(silent=True) or {}
    ok, error, notes = attention_store.add_note(
        sale_id,
        payload.get("note") or "",
        user,
        owner_rep=owner_rep,
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    user_store.record_needs_attention_activity(user["id"])

    current_status = notes[0]["attention_status"] if notes else None
    return jsonify({
        "ok": True,
        "sale_id": sale_id,
        "attention_status": current_status,
        "notes": [_serialize_note(n) for n in notes],
    })


@app.route("/dashboard/accounts")
@auth.login_required
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

    # "channel" and "date" are runtime-parameterized drill-downs -- same
    # reasoning as their rep-scoped counterparts in bulk_account_view()
    # above: they deliberately bypass BULK_ACCOUNT_VIEWS since the
    # channel/date is a click value, not a filter fixed at import time.
    # rep=None in both scopes team-wide.
    if view == "channel":
        channel = request.args.get("channel", "")
        view_title, accounts = get_channel_account_view(normalized, None, channel, period, custom_start, custom_end)
        all_time = False
    elif view == "date":
        # Sales Calendar & Monthly Sales Trend's day-cell drill-down
        # (added 2026-08-19). cal_view carries forward which of the
        # calendar's own Team/NJ/NY/VA panels was showing when the count
        # was clicked (see dashboard.html), so the accounts listed here
        # match the exact same state scope as the number that was
        # clicked -- same DASHBOARD_STATE_VIEWS/_state_filtered() the
        # calendar itself uses, never a second definition of "NJ".
        cal_view = request.args.get("cal_view", "team")
        if cal_view not in DASHBOARD_STATE_VIEW_KEYS:
            cal_view = "team"
        scoped = _state_filtered(normalized, _state_code_for_view(cal_view))
        try:
            view_title, accounts = get_calendar_day_account_view(
                scoped, None,
                int(request.args.get("year", 0)), int(request.args.get("month", 0)), int(request.args.get("day", 0)),
            )
        except (TypeError, ValueError):
            abort(404)
        if cal_view != "team":
            state_label = next(label for key, label, _ in DASHBOARD_STATE_VIEWS if key == cal_view)
            view_title = f"{view_title} — {state_label}"
        all_time = True
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


@app.route("/marketing")
@auth.login_required
def marketing_dashboard_page():
    """Marketing Channel Report -- live version of the same self-contained
    report scripts/generate_marketing_report.py can also write to a
    standalone file (see marketing_cleaning.py's module docstring for why
    there are two render paths sharing one template/pipeline). Re-runs
    the FULL pipeline -- fetch PlanetWeb, clean, tag, guardrails -- on
    EVERY request, by explicit request ("I want the data to be cleaned
    every time on refresh"), so there is no caching here and no
    data_store/ensure_data_loaded() involved; a request takes roughly
    10s against ~15k PlanetWeb rows.

    Returns the rendered HTML directly (not render_template()) -- the
    report is entirely self-contained (its own <style>/<script>, no
    team_dashboard.css, no _topnav.html) so it can be shared as a plain
    file outside this app unchanged; back_url adds a small "Dashboard"
    link back into the rest of the app, and accounts_url makes the
    "Available Now (ads)" KPI card a clickable drill-down into real
    name/address data (see marketing_available_now_accounts() below) --
    both only for this, the live-served copy. The standalone file has
    neither: no app to link back into, and no backend to query PII from."""
    html, _guardrail_report = marketing_cleaning.generate_report(
        back_url=url_for("dashboard_page"),
        accounts_url=url_for("marketing_available_now_accounts"),
    )
    return html


@app.route("/marketing/available-now")
@auth.login_required
def marketing_available_now_accounts():
    """Bulk Account View drill-down for the live Marketing Channel
    Report's "Available Now (ads)" KPI card -- real name/address/created
    date for the Paid + Available Now leads in the clicked date range
    (see marketing_cleaning.get_available_now_accounts()). Authenticated-
    only, like every other route in this app; this is the one place in
    the whole Marketing Channel Report feature that shows real customer
    PII -- deliberately never embedded in the shareable/exportable report
    itself (see that function's own docstring)."""
    try:
        from_date = datetime.strptime(request.args.get("from", ""), "%Y-%m-%d").date()
        to_date = datetime.strptime(request.args.get("to", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        from_date, to_date = marketing_data.BASE_START_DATE, marketing_cleaning._today()
    from_date = max(from_date, marketing_data.BASE_START_DATE)
    to_date = max(to_date, from_date)

    accounts, stats = marketing_cleaning.get_available_now_accounts(from_date, to_date)

    return render_template(
        "marketing_available_now.html",
        active_page="marketing",
        accounts=accounts,
        stats=stats,
        start_date=from_date.strftime("%-m/%-d/%Y"),
        end_date=to_date.strftime("%-m/%-d/%Y"),
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/cancellations")
@auth.admin_required
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
@auth.admin_required
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
@auth.login_required
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


@app.route("/search")
@auth.login_required
def search():
    """Global search bar (top nav, templates/_topnav.html +
    static/js/search.js) -- reps and customer accounts by substring
    match, JSON response. Deliberately does NOT call
    ensure_data_loaded(): every full-page route reloads fresh from both
    source databases on every request now (see "Data loading" in
    README.md), but this endpoint fires on every keystroke (debounced
    ~150ms client-side) and doing a live PlanetWeb/KPI round trip per
    keystroke would make the search feel sluggish instead of fast -- it
    reads whatever is already sitting in data_store from the last real
    page load (which, given every page load now refreshes, is never more
    than one navigation stale)."""
    query = request.args.get("q", "")
    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )
    rep_matches, account_matches = search_dataset(normalized, query)

    reps = [
        {"name": rep, "url": url_for("rep_profile", rep_name=rep)}
        for rep in rep_matches
    ]
    accounts = []
    for account in account_matches:
        vision_url = account.get("vision_url")
        accounts.append({
            "first_name": account["first_name"],
            "last_name": account["last_name"],
            "address": account["address"],
            "city_state_zip": account["city_state_zip"],
            "sales_rep": account["sales_rep"],
            "status": account["status"],
            # Vision is the closest thing this app has to a per-account
            # detail page (no account has its own route here) -- fall
            # back to that account's rep profile when there's no
            # subscriber_uuid to build a Vision link from, so a result
            # is never a dead end.
            "url": vision_url or url_for("rep_profile", rep_name=account["sales_rep"]),
            "external": bool(vision_url),
        })

    return jsonify({"reps": reps, "accounts": accounts})


# ================================================================
# AUTHENTICATION
# ================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.current_user() is not None:
        return redirect(url_for("dashboard_page"))

    error = None
    if request.method == "POST":
        user = user_store.get_user_by_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        if user is None or user["status"] != "active" or not user_store.verify_password(user, password):
            error = "Incorrect email or password."
        else:
            auth.login_user(user)
            user_store.record_login(user["id"])
            next_url = request.args.get("next") or url_for("dashboard_page")
            # Only ever redirect to a same-site relative path -- never
            # follow a `next` value off this domain (open-redirect
            # protection on a value that came from the query string).
            if not next_url.startswith("/"):
                next_url = url_for("dashboard_page")
            return redirect(next_url)

    return render_template("login.html", error=error, next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return redirect(url_for("login"))


@app.route("/setup/<token>", methods=["GET", "POST"])
def account_setup(token):
    """First-time account setup -- reached only via the emailed single-
    use link (see email_service.send_setup_email()). Also the completion
    step for an Admin's "Resend setup email" action."""
    ok, user_id, token_error = user_store.verify_token(token, "setup")
    if not ok:
        return render_template("setup_account.html", token_error=token_error, user=None), 400

    user = user_store.get_user_by_id(user_id)
    form_error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if password != confirm:
            form_error = "Passwords do not match."
        else:
            set_ok, set_error = user_store.set_password(user_id, password)
            if not set_ok:
                form_error = set_error
            else:
                user_store.consume_token(token)
                auth.login_user(user_store.get_user_by_id(user_id))
                user_store.record_login(user_id)
                return redirect(url_for("dashboard_page"))

    return render_template("setup_account.html", user=user, token=token, form_error=form_error)


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password_request():
    sent = False
    if request.method == "POST":
        user = user_store.get_user_by_email(request.form.get("email", ""))
        # Always show the same confirmation regardless of whether the
        # email matched a real account -- this form must never be usable
        # to discover which emails have accounts.
        if user is not None and user["status"] != user_store.STATUS_DISABLED:
            token_ok, _error, raw_token = user_store.create_token(user["id"], "reset")
            if token_ok:
                email_service.send_reset_email(user, raw_token)
        sent = True
    return render_template("reset_password_request.html", sent=sent)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password_complete(token):
    ok, user_id, token_error = user_store.verify_token(token, "reset")
    if not ok:
        return render_template("reset_password.html", token_error=token_error), 400

    form_error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if password != confirm:
            form_error = "Passwords do not match."
        else:
            set_ok, set_error = user_store.set_password(user_id, password)
            if not set_ok:
                form_error = set_error
            else:
                user_store.consume_token(token)
                return redirect(url_for("login"))

    return render_template("reset_password.html", token=token, form_error=form_error)


@app.errorhandler(403)
def forbidden(_error):
    return render_template("unauthorized.html"), 403


# ================================================================
# ADMIN PORTAL
# ================================================================

@app.route("/admin")
@auth.admin_required
def admin_home():
    return redirect(url_for("admin_users"))


def _needs_review_status(user, stats):
    """Spec default 'Needs Review' signal: a Sales Rep/Customer Success
    user with open Needs Attention accounts and no meaningful activity
    for 3+ days. Deliberately isolated in this one small function --
    change ONLY this to retune the rule, never duplicate the threshold
    elsewhere. Admin/Other aren't judged by this at all (None -> no
    badge shown)."""
    if user["role"] not in (permissions.ROLE_SALES_REP, permissions.ROLE_CUSTOMER_SUCCESS):
        return None
    if stats["count"] == 0:
        return None
    last_activity = user.get("last_needs_attention_activity_at")
    if last_activity is None:
        return "Needs Review"
    idle_days = (datetime.now(EASTERN_TZ) - last_activity.astimezone(EASTERN_TZ)).total_seconds() / 86400.0
    return "Needs Review" if idle_days >= 3 else "Active"


@app.route("/admin/users")
@auth.admin_required
def admin_users():
    ensure_data_loaded()
    normalized = build_sales_dataset(
        data_store["main_sales"],
        data_store["vision_packages"],
        data_store["service_cancellations"],
    )

    users = user_store.list_users()
    activity_counts = needs_attention_service.get_activity_counts([u["id"] for u in users])

    rep_sale_ids = {}
    if normalized is not None and not normalized.empty:
        needs_attention_df = normalized[normalized["status"] == "Needs Attention"]
        for rep, group in needs_attention_df.groupby("sales_rep"):
            rep_sale_ids[rep] = group["sale_id"].tolist()
    rep_stats = needs_attention_service.get_rep_needs_attention_stats(rep_sale_ids)

    rows = []
    for user in users:
        stats = rep_stats.get(user.get("sales_rep_name"), {"count": 0, "aged_15_plus": 0, "oldest_days": 0})
        counts = activity_counts.get(user["id"], {"last_7_days": 0, "last_30_days": 0})
        rows.append({
            "user": user,
            "needs_attention_count": stats["count"],
            "needs_attention_aged": stats["aged_15_plus"],
            "needs_attention_oldest_days": stats["oldest_days"],
            "actions_7d": counts["last_7_days"],
            "actions_30d": counts["last_30_days"],
            "review_status": _needs_review_status(user, stats),
        })

    return render_template(
        "admin/users.html",
        active_page="admin_users",
        rows=rows,
        roles=user_store.ROLES,
        sales_reps=user_store.list_sales_reps(),
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/admin/users/create", methods=["POST"])
@auth.admin_required
def admin_create_user():
    """Manual single-user creation from the Admin Portal, alongside the
    existing Excel import flow (import_service.py/admin_import.js) --
    goes through user_store.create_user() directly, so it lands in the
    same no-password/status=pending state as an imported row and does
    not auto-send a setup email (an Admin triggers "Send Setup" or "Set
    Password" afterward, same as for imported users)."""
    email = request.form.get("email", "")
    role = request.form.get("role", "")
    sales_rep_id_raw = request.form.get("sales_rep_id", "")
    sales_rep_id = int(sales_rep_id_raw) if sales_rep_id_raw else None
    ok, error, user = user_store.create_user(email, role, sales_rep_id=sales_rep_id)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "user_id": user["id"]})


@app.route("/admin/users/<int:user_id>", methods=["POST"])
@auth.admin_required
def admin_update_user(user_id):
    email = request.form.get("email") or None
    role = request.form.get("role") or None
    sales_rep_id_raw = request.form.get("sales_rep_id", "")
    sales_rep_id = int(sales_rep_id_raw) if sales_rep_id_raw else None
    ok, error = user_store.update_user(user_id, email=email, role=role, sales_rep_id=sales_rep_id)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@app.route("/admin/users/<int:user_id>/disable", methods=["POST"])
@auth.admin_required
def admin_disable_user(user_id):
    ok, error = user_store.set_user_status(user_id, user_store.STATUS_DISABLED)
    return (jsonify({"ok": True}) if ok else (jsonify({"ok": False, "error": error}), 400))


@app.route("/admin/users/<int:user_id>/enable", methods=["POST"])
@auth.admin_required
def admin_enable_user(user_id):
    ok, error = user_store.set_user_status(user_id, user_store.STATUS_ACTIVE)
    return (jsonify({"ok": True}) if ok else (jsonify({"ok": False, "error": error}), 400))


@app.route("/admin/users/<int:user_id>/send-setup", methods=["POST"])
@auth.admin_required
def admin_send_setup(user_id):
    user = user_store.get_user_by_id(user_id)
    if user is None:
        return jsonify({"ok": False, "error": "User not found."}), 404
    ok, error, raw_token = user_store.create_token(user_id, "setup")
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    sent_ok, sent_error = email_service.send_setup_email(user, raw_token)
    if not sent_ok:
        return jsonify({"ok": False, "error": sent_error}), 502
    return jsonify({"ok": True})


@app.route("/admin/users/<int:user_id>/set-password", methods=["POST"])
@auth.admin_required
def admin_set_password(user_id):
    """Lets an Admin set a user's password directly, bypassing the
    emailed-link flow entirely -- added 2026-08-18 for exactly the case
    SMTP isn't configured (or an Admin just wants to hand someone a
    password over the phone/in person). Still goes through
    user_store.set_password() (hashed immediately, never stored or
    logged in plaintext) and still activates the account -- the only
    difference from the token flow is who is choosing the password and
    how they learn it."""
    user = user_store.get_user_by_id(user_id)
    if user is None:
        return jsonify({"ok": False, "error": "User not found."}), 404
    payload = request.get_json(silent=True) or {}
    password = payload.get("password") or ""
    ok, error = user_store.set_password(user_id, password)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@app.route("/admin/users/<int:user_id>/send-reset", methods=["POST"])
@auth.admin_required
def admin_send_reset(user_id):
    user = user_store.get_user_by_id(user_id)
    if user is None:
        return jsonify({"ok": False, "error": "User not found."}), 404
    ok, error, raw_token = user_store.create_token(user_id, "reset")
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    sent_ok, sent_error = email_service.send_reset_email(user, raw_token)
    if not sent_ok:
        return jsonify({"ok": False, "error": sent_error}), 502
    return jsonify({"ok": True})


@app.route("/admin/users/import")
@auth.admin_required
def admin_import_page():
    return render_template("admin/import.html", active_page="admin_import", last_refreshed=data_store["last_refreshed"])


@app.route("/admin/users/import/validate", methods=["POST"])
@auth.admin_required
def admin_import_validate():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"ok": False, "error": "No file was selected."}), 400
    ok, error, rows, summary = import_service.parse_and_validate(file)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "rows": rows, "summary": summary})


@app.route("/admin/users/import/commit", methods=["POST"])
@auth.admin_required
def admin_import_commit():
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") or []
    if not rows:
        return jsonify({"ok": False, "error": "Nothing to import."}), 400
    results = import_service.commit_import(rows)
    return jsonify({"ok": True, **results})


@app.route("/admin/audit")
@auth.admin_required
def admin_audit():
    entries = attention_store.get_recent_activity(limit=300)
    for entry in entries:
        entry["created_at_display"] = _format_note_timestamp(entry["created_at"])
    return render_template(
        "admin/audit.html", active_page="admin_audit", entries=entries, last_refreshed=data_store["last_refreshed"],
    )


@app.route("/admin/sales-reps")
@auth.admin_required
def admin_sales_reps():
    return render_template(
        "admin/sales_reps.html",
        active_page="admin_sales_reps",
        sales_reps=user_store.list_sales_reps(),
        teams=user_store.SALES_REP_TEAMS,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/admin/sales-reps/<int:rep_id>/team", methods=["POST"])
@auth.admin_required
def admin_update_sales_rep_team(rep_id):
    team = request.form.get("team") or None
    ok, error = user_store.update_sales_rep_team(rep_id, team)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@app.route("/admin/marketing-form-data")
@auth.admin_required
def admin_marketing_form_data():
    """Admin-only raw-data verification tool for PlanetWeb's
    [dbo].[View_FormDataAnalytics] (marketing_data.py) -- confirms the app
    can reach the view and shows exactly what it returns. Every
    filter/search/page value is read fresh from the query string and
    re-queried on this one request -- there is no cached copy to go stale,
    so "Refresh Data" (the template's own link back to this same URL) and
    a plain page reload do the exact same thing.

    Also renders the Attribution Quality panel (Tier 1/2/3 counts +
    attribution-method breakdown, via marketing_metrics.get_attribution_
    quality()) -- deliberately kept HERE and not on the main Marketing
    Dashboard (spec: attribution-tier detail is a debugging/data-
    validation concern, not a business-facing metric). It reuses the exact
    same centralized marketing_attribution.attribute_record() the
    Marketing Dashboard itself calls, just surfaced differently -- see
    marketing_metrics.py's module docstring. Always the raw browser's own
    base date window (marketing_data.BASE_START_DATE through today),
    independent of this page's own search/filter controls above, same
    reasoning dashboard.html's Planet Networks Records section documents
    for staying independent of the page's period filter."""
    search = request.args.get("search", "").strip()
    filters = {
        "date_from": request.args.get("date_from", "").strip(),
        "date_to": request.args.get("date_to", "").strip(),
        "municipality": request.args.get("municipality", "").strip(),
        "zipcode": request.args.get("zipcode", "").strip(),
        "utm_source": request.args.get("utm_source", "").strip(),
        "utm_medium": request.args.get("utm_medium", "").strip(),
        "utm_campaign": request.args.get("utm_campaign", "").strip(),
    }
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    result = marketing_data.get_marketing_form_data(filters, search, page)
    filter_options = marketing_data.get_filter_options()
    attribution_quality = marketing_metrics.get_attribution_quality()

    return render_template(
        "admin/marketing_form_data.html",
        active_page="admin_marketing_form_data",
        columns=marketing_data.COLUMNS,
        result=result,
        search=search,
        filters=filters,
        filter_options=filter_options,
        attribution_quality=attribution_quality,
        base_start_date=marketing_data.BASE_START_DATE,
        # This route's OWN query-time timestamp -- deliberately not
        # data_store["last_refreshed"] (the unrelated main PlanetWeb/KPI
        # pipeline's own refresh time, still passed below for _topnav.html's
        # separate "Last refreshed" display). Every page load re-runs the
        # marketing query fresh (see this route's docstring), so this is
        # simply "now" at query time.
        marketing_last_refreshed=datetime.now(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z"),
        last_refreshed=data_store["last_refreshed"],
    )


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


# Runs once when this module is imported -- by gunicorn's worker process
# in production, or by `python app.py` locally -- NOT lazily on first
# request like every other ensure_data_loaded() call site. This closes a
# real deadlock: since every route that used to trigger a data load now
# requires login (@auth.login_required), and /health deliberately stays
# lightweight and never loads data, there was no longer any PUBLIC route
# that could ever run load_all_data() -- which is also what runs
# _bootstrap_initial_admin(). Without this, a brand-new deployment could
# never create its first Admin account: nobody could log in (no users
# exist yet) to trigger the load that would create the first user.
# Safe with gunicorn's required --workers 1 (see docker-compose.prod.yml)
# -- this module is imported exactly once per process either way.
ensure_data_loaded()


if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "3005"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
