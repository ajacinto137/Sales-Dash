from datetime import date, datetime, timedelta
import os
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, stream_with_context,
    url_for,
)

import app_secrets
import attention_store
import auth
import customer_profile
import db
import email_service
import external_sales_store
import hubspot_client
import hubspot_logic
import hubspot_store
import import_service
import install_escalation
import knockers
import marketing_cleaning
import marketing_data
import marketing_metrics
import needs_attention_service
import order_search
import passings_activity_store
import passings_cache
import passings_classification
import passings_data
import permissions
import staffing_store
import user_store
from queries import (
    MAIN_SALES_QUERY,
    WEB_SALES_QUERY,
    SERVICE_CANCELLATIONS_QUERY,
    VISION_PACKAGES_QUERY,
    pas_place_url,
)
from sales_metrics import (
    BULK_ACCOUNT_VIEWS,
    _today,
    REP_SORT_KEYS,
    REP_TABLE_COLUMNS,
    WEB_REP,
    build_bulk_account_rows,
    build_sales_dataset,
    calculate_calendar_sales,
    calculate_channel_breakdown,
    calculate_channel_state_breakdown,
    calculate_town_breakdown,
    get_town_account_view,
    calculate_monthly_sales_by_status,
    MONTHLY_SALES_STATUSES,
    get_month_status_account_view,
    calculate_daily_averages,
    calculate_hourly_breakdown,
    calculate_monthly_forecast,
    calculate_monthly_sales_trend,
    calculate_month_to_date_comparison,
    calculate_needs_attention,
    calculate_records,
    calculate_rep_metrics,
    add_all_time_install_rates,
    calculate_rep_monthly_activity,
    calculate_rep_sales_by_month,
    calculate_rep_monthly_installs,
    calculate_rep_monthly_status,
    calculate_sales_volume_trend,
    calculate_total_daily_sales,
    calculate_week_to_date_comparison,
    exclude_web,
    filter_by_period,
    get_bulk_account_view,
    get_calendar_day_account_view,
    get_install_month_account_view,
    get_status_month_account_view,
    get_channel_account_view,
    get_leaderboard_metric_account_view,
    get_rep_achievements,
    SEARCH_RESULT_LIMIT,
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


@app.template_filter("eastern_ts")
def eastern_ts_filter(dt):
    """Aware datetime (appdb TIMESTAMPTZ) -> '10/16/2026 2:05 PM EDT'."""
    return dt.astimezone(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z")


@app.context_processor
def inject_auth_context():
    """Makes current_user/permissions available in every template without
    passing them explicitly in every render_template() call -- used by
    _topnav.html to show/hide Admin-only nav items and by any page that
    needs to know who's logged in."""
    return {"current_user": auth.current_user(), "permissions": permissions}


# A Jinja global rather than context so imported macros (_kpi_bar.html)
# see it too: the equation behind each metric, for the hover box.
app.jinja_env.globals["formulas"] = knockers.FORMULAS

# The container clock runs in UTC, but every sale/scheduled date in the
# data is Eastern local time (SQL Server InsertDate). The "Last refreshed"
# timestamp below uses EASTERN_TZ for display; any "today"-relative
# comparison against the sales data must too -- see sales_metrics._today()
# and don't reintroduce a bare datetime.now().date() for "today" (that bug
# silently zeroed out Daily Sales / Records / View All Sales every evening
# until fixed 2026-08-15, since UTC rolls to the next day ~4-5 hours
# before Eastern does).
EASTERN_TZ = ZoneInfo("America/New_York")

# Source data is refreshed by ONE background thread on a fixed timer
# (_refresh_loop() below), never by page requests -- see
# ensure_data_loaded() for why. _lock only serializes refreshes against
# each other (timer vs. the "Refresh Data" button); readers never take it.
DATA_REFRESH_SECONDS = int(os.environ.get("DATA_REFRESH_SECONDS", "60"))
_lock = threading.Lock()
_normalize_lock = threading.Lock()
_refresher_lock = threading.Lock()
_refresher_started = False
_initial_load_done = False

data_store = {
    "planetweb_connected": None,
    "planetweb_error": None,
    "kpi_connected": None,
    "kpi_error": None,
    "main_sales": None,
    "main_sales_status": None,
    "main_sales_error": None,
    # Website orders for the "Web" pseudo-rep (WEB_SALES_QUERY) -- kept
    # apart from main_sales so the raw Main Sales views are unchanged.
    "web_sales": None,
    "web_sales_status": None,
    "web_sales_error": None,
    "service_cancellations": None,
    "service_cancellations_status": None,
    "service_cancellations_error": None,
    "vision_packages": None,
    "vision_packages_status": None,
    "vision_packages_error": None,
    "last_refreshed": None,
    "install_validation": None,
    "install_error": None,
    # build_sales_dataset() output, computed once per refresh instead of
    # once per request -- see get_normalized_sales().
    "normalized_sales": None,
    "normalized_for_date": None,
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

def _load_planetweb(store):
    connected, error = db.test_planetweb_connection()
    store["planetweb_connected"] = connected
    store["planetweb_error"] = error

    if not connected:
        for key in ("main_sales", "web_sales"):
            store[key] = None
            store[f"{key}_status"] = "FAILED"
            store[f"{key}_error"] = "Skipped: PlanetWeb SQL Server connection failed."
        return

    conn = None
    try:
        conn = db.get_planetweb_connection()
        for key, query in (("main_sales", MAIN_SALES_QUERY), ("web_sales", WEB_SALES_QUERY)):
            try:
                store[key] = pd.read_sql(query, conn)
                store[f"{key}_status"] = "SUCCESS"
                store[f"{key}_error"] = None
            except Exception as exc:
                store[key] = None
                store[f"{key}_status"] = "FAILED"
                store[f"{key}_error"] = db.sanitize_error(exc)
    except Exception as exc:
        for key in ("main_sales", "web_sales"):
            store[key] = None
            store[f"{key}_status"] = "FAILED"
            store[f"{key}_error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def _load_kpi(store):
    connected, error = db.test_kpi_connection()
    store["kpi_connected"] = connected
    store["kpi_error"] = error

    if not connected:
        for key in ("service_cancellations", "vision_packages"):
            store[key] = None
            store[f"{key}_status"] = "FAILED"
            store[f"{key}_error"] = "Skipped: KPI PostgreSQL connection failed."
        return

    conn = None
    try:
        conn = db.get_kpi_connection()

        try:
            store["service_cancellations"] = pd.read_sql(SERVICE_CANCELLATIONS_QUERY, conn)
            store["service_cancellations_status"] = "SUCCESS"
            store["service_cancellations_error"] = None
        except Exception as exc:
            conn.rollback()
            store["service_cancellations"] = None
            store["service_cancellations_status"] = "FAILED"
            store["service_cancellations_error"] = db.sanitize_error(exc)

        try:
            store["vision_packages"] = pd.read_sql(VISION_PACKAGES_QUERY, conn)
            store["vision_packages_status"] = "SUCCESS"
            store["vision_packages_error"] = None
        except Exception as exc:
            conn.rollback()
            store["vision_packages"] = None
            store["vision_packages_status"] = "FAILED"
            store["vision_packages_error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def reorder_main_sales_columns(df):
    priority = [c for c in MAIN_SALES_COLUMN_ORDER if c in df.columns]
    remaining = [c for c in df.columns if c not in priority]
    return df[priority + remaining]


def _print_install_validation(store):
    v = store.get("install_validation")
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


def _merge_install_date(sales_df, vision_packages):
    """Adds "Install Date"/"Install Status" to a PlanetWeb sales frame --
    the Excel-formula lookup described in _apply_install_date() below."""
    merged = sales_df.copy()

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

    formatted_dates = install_dates.dt.strftime("%m/%d/%Y")
    merged["Install Date"] = formatted_dates.fillna("Not Yet Installed")
    merged["Install Status"] = merged["Install Date"].apply(
        lambda v: "Not Yet Installed" if v == "Not Yet Installed" else "Installed"
    )
    return merged


def _apply_install_date(store):
    """Reproduces the existing Excel 'Main' worksheet Install Date formula:

        =IFNA(INDEX('Vision Packages'!$Y:$Y,
                     MATCH(Main!$R2, 'Vision Packages'!$B:$B, 0)),
              "Not Yet Installed")

    i.e. Main.vi_subscriber_uuid -> Vision Packages.subscriber_uuid (first
    match only, exactly like Excel's MATCH) -> subscriber_first_invoice_date.
    Adds "Install Date" and "Install Status" to main_sales without ever
    changing the row count.
    """
    main_sales = store["main_sales"]
    vision_packages = store["vision_packages"]

    if main_sales is None:
        store["install_validation"] = None
        store["install_error"] = None
        return

    original_count = len(main_sales)
    merged = _merge_install_date(main_sales, vision_packages)
    # Web sales get the same Install Date/Status lookup (no validation
    # report -- that one describes Main Sales only).
    if store["web_sales"] is not None:
        store["web_sales"] = _merge_install_date(store["web_sales"], vision_packages)

    final_count = len(merged)
    duplicate_rows_created = final_count - original_count

    store["main_sales"] = reorder_main_sales_columns(merged)

    installed_count = int((merged["Install Status"] == "Installed").sum())
    not_yet_count = int((merged["Install Status"] == "Not Yet Installed").sum())
    mapping_status = "SUCCESS" if duplicate_rows_created == 0 else "ERROR"

    store["install_validation"] = {
        "main_sales_rows": original_count,
        "vision_packages_rows": len(vision_packages) if vision_packages is not None else 0,
        "installed": installed_count,
        "not_yet_installed": not_yet_count,
        "rows_before_merge": original_count,
        "rows_after_merge": final_count,
        "duplicate_rows_created": duplicate_rows_created,
        "mapping_status": mapping_status,
    }
    store["install_error"] = (
        None
        if duplicate_rows_created == 0
        else (
            f"Install Date mapping created {duplicate_rows_created} extra row(s) — "
            "Main Sales record count no longer matches the source query. This should "
            "never happen; treat it as a data integrity error and investigate before "
            "trusting Install Date/Install Status on this page."
        )
    )
    _print_install_validation(store)


def _touch_last_refreshed(store):
    store["last_refreshed"] = datetime.now(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z")


def _sync_reps_and_needs_attention(store):
    """Keeps sales_reps (user_store.py) and needs_attention_tracking
    (needs_attention_service.py) in lockstep with the source data on
    every refresh -- sales_reps gets any newly-seen rep name;
    needs_attention_tracking gets a first_seen_at row for any account
    newly in Needs Attention, and loses the row for any account that's
    left, so a later re-entry starts a fresh 15-day clock. Silently
    no-ops on appdb failure inside each function -- must never block the
    dashboard itself from loading."""
    normalized = store["normalized_sales"]
    if normalized is None or normalized.empty:
        return
    # Web isn't a person -- never add it to sales_reps.
    user_store.sync_sales_reps([r for r in normalized["sales_rep"].dropna().unique() if r != WEB_REP])
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


_SOURCE_DATASETS = {
    "planetweb": ("main_sales", "web_sales"),
    "kpi": ("service_cancellations", "vision_packages"),
}


def _keep_last_good(store, previous):
    """A failed refresh shouldn't blank out the dashboard: if a dataset
    failed to load this round but the previous round had it, keep serving
    the previous copy (the connection pills still show the failure, and
    "Last refreshed" stops advancing so the staleness is visible).
    Returns True if anything had to fall back."""
    fell_back = False
    for keys in _SOURCE_DATASETS.values():
        for key in keys:
            if store[key] is None and previous[key] is not None:
                print(f"Refresh of {key} failed ({store[f'{key}_error']}); keeping previous data.")
                store[key] = previous[key]
                store[f"{key}_status"] = previous[f"{key}_status"]
                store[f"{key}_error"] = previous[f"{key}_error"]
                fell_back = True
    return fell_back


def load_all_data(sources=("planetweb", "kpi")):
    """Builds a complete new snapshot off to the side, then swaps it into
    data_store in one dict.update() -- requests reading data_store never
    see a half-loaded mix of old and new data and never wait on this."""
    global _initial_load_done
    with _lock:
        started = time.monotonic()
        previous = dict(data_store)
        store = dict(data_store)
        if "planetweb" in sources:
            _load_planetweb(store)
        if "kpi" in sources:
            _load_kpi(store)
        fell_back = _keep_last_good(store, previous)
        # Install Date depends on both main_sales and vision_packages, so
        # recompute it after any refresh that could have touched either one.
        _apply_install_date(store)
        store["normalized_sales"] = build_sales_dataset(
            store["main_sales"],
            store["vision_packages"],
            store["service_cancellations"],
            store["web_sales"],
        )
        store["normalized_for_date"] = _today()
        _sync_reps_and_needs_attention(store)
        if not fell_back:
            _touch_last_refreshed(store)
        data_store.update(store)
        _initial_load_done = True
        print(f"Data refresh finished in {time.monotonic() - started:.1f}s")
        _bootstrap_initial_admin()


def _refresh_loop():
    while True:
        time.sleep(DATA_REFRESH_SECONDS)
        try:
            load_all_data()
        except Exception as exc:
            # Never let one bad refresh kill the thread -- the site would
            # silently freeze on old data forever.
            print(f"Background data refresh failed: {db.sanitize_error(exc)}")


def _start_background_refresh():
    global _refresher_started
    if _refresher_started:
        return
    with _refresher_lock:
        if _refresher_started:
            return
        threading.Thread(target=_refresh_loop, name="data-refresh", daemon=True).start()
        _refresher_started = True


def _debug_enabled():
    # Same check as the __main__ block's app.run(debug=...) below.
    return __name__ == "__main__" and os.environ.get("FLASK_DEBUG", "false").lower() == "true"


def ensure_data_loaded():
    # Changed 2026-10-07 after two 504 outages: pages used to re-query
    # both source databases on EVERY request (by request, 2026-08-17),
    # serialized behind _lock on prod's single gunicorn worker, so load
    # scaled with traffic -- a few reps' auto-update tabs were enough to
    # queue requests past nginx's 60s timeout for everyone. Now a single
    # background thread refreshes every DATA_REFRESH_SECONDS (default
    # 60s) regardless of how many people are viewing, and requests just
    # read the latest snapshot. Data is at most ~1 refresh interval old,
    # and auto-update reloads (static/js/auto_refresh.js) cost almost
    # nothing. Only the very first load (process startup) is synchronous.
    _start_background_refresh()
    # HubSpot call sync runs on its own thread (every 60s by default),
    # independent of the source-data refresh above (no-op without a
    # service key). Under `python app.py` with FLASK_DEBUG=true, Werkzeug's
    # reloader re-imports this module in a parent "file watcher" process
    # that never serves requests -- skip the scheduler there so local dev
    # doesn't run every sync twice. (gunicorn/prod never sets this up.)
    if not (_debug_enabled() and os.environ.get("WERKZEUG_RUN_MAIN") != "true"):
        hubspot_store.start_scheduler()
        # External Rep Sales: PlanetWeb -> appdb every
        # EXTERNAL_SALES_SYNC_INTERVAL_SECONDS (default 5 min).
        external_sales_store.start_scheduler()
        # Passings & Leads in-memory snapshot (passings_cache.py).
        passings_cache.start_scheduler()
    if not _initial_load_done:
        load_all_data()


def get_normalized_sales():
    """The build_sales_dataset() frame cached by the last refresh. A copy,
    so a route mutating its frame can't corrupt everyone else's. Rebuilt
    here if Eastern "today" has rolled over since that refresh (Pending vs
    Needs Attention is relative to today) -- e.g. a source DB was down at
    midnight so no refresh has landed yet."""
    if data_store["normalized_for_date"] != _today():
        with _normalize_lock:
            if data_store["normalized_for_date"] != _today():
                data_store["normalized_sales"] = build_sales_dataset(
                    data_store["main_sales"],
                    data_store["vision_packages"],
                    data_store["service_cancellations"],
                    data_store["web_sales"],
                )
                data_store["normalized_for_date"] = _today()
    normalized = data_store["normalized_sales"]
    return None if normalized is None else normalized.copy()


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
DASHBOARD_STATE_VIEWS = [
    ("team", "Team", None),
    ("nj", "NJ", "NJ"),
    ("ny", "NY", "NY"),
    ("va", "VA", "VA"),
    ("pa", "PA", "PA"),
    # Combined NY/PA (added 2026-10-06, by request) -- state_code is a
    # tuple here instead of a single string, so _state_filtered() below
    # matches either state rather than adding a second "combined views"
    # concept alongside DASHBOARD_STATE_VIEWS/_state_filtered().
    ("ny-pa", "NY/PA", ("NY", "PA")),
]
DASHBOARD_STATE_VIEW_KEYS = {key for key, _, _ in DASHBOARD_STATE_VIEWS}


def _state_filtered(df, state_code):
    if state_code is None:
        return df
    if df is None or df.empty or "state" not in df.columns:
        return df
    normalized_state = df["state"].astype(str).str.strip().str.upper()
    if isinstance(state_code, (tuple, list, set)):
        return df[normalized_state.isin(state_code)]
    return df[normalized_state == state_code]


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
    normalized = get_normalized_sales()

    period = request.args.get("period", "this_month")
    custom_start = request.args.get("start", "")
    custom_end = request.args.get("end", "")
    search = request.args.get("search", "").strip()
    selected_rep = request.args.get("rep", "")

    period_df = filter_by_period(normalized, period, custom_start, custom_end)

    # Web (website orders) is in normalized for the sections that count it
    # -- the Individual Rep Leaderboard, Channel & Time-of-Day
    # Performance, Channel Breakdown by State, Sales Calendar & Monthly
    # Sales Trend, and the Monthly Sales/Projection KPIs (by request,
    # 2026-10-22). Everything else on
    # this page uses reps_only, since Web isn't a sales rep.
    reps_only = exclude_web(normalized)

    rep_rows = add_all_time_install_rates(calculate_rep_metrics(period_df), normalized)
    total_rep_count = sum(1 for r in rep_rows if r["sales_rep"] != WEB_REP)

    if search:
        rep_rows = [r for r in rep_rows if search.lower() in r["sales_rep"].lower()]

    if not selected_rep and rep_rows:
        selected_rep = rep_rows[0]["sales_rep"]

    # Sorting is a pure display-ordering concern layered on top of the
    # rows above -- applied last so it never affects total_rep_count, the
    # search filter, or which rep gets auto-selected by default.
    # Default: Outbound, highest first (by request, 2026-10-13). Ties keep
    # their sales-rank order, since sort_rep_rows() is a stable sort.
    sort = request.args.get("sort", "outbound")
    sort_dir = request.args.get("dir", "desc")
    if sort not in REP_SORT_KEYS:
        sort, sort_dir = "outbound", "desc"
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
    records = calculate_records(reps_only)
    total_daily_sales = calculate_total_daily_sales(reps_only)
    daily_averages = calculate_daily_averages(reps_only)

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
    # Month to Date mode of the same chart -- current month vs previous
    # month vs YTD-average cumulative pace, per state view.
    mtd_trend_views = {key: calculate_month_to_date_comparison(view_df) for key, view_df in state_filtered_normalized.items()}
    trend_range = request.args.get("trend_range", "12m")
    if trend_range not in ("12m", "mtd"):
        trend_range = "12m"

    # Team-wide, period-filtered -- same convention as the Individual Rep
    # Leaderboard table (period_df), just not scoped to one rep. Reuses
    # the exact same functions the Rep Profile's Channel Performance /
    # Time-of-Day Performance sections already use.
    channel_breakdown = calculate_channel_breakdown(period_df)
    hourly_breakdown = calculate_hourly_breakdown(period_df)
    # Channel Breakdown by State (added 2026-10-12) -- same period_df.
    channel_state_breakdown = calculate_channel_state_breakdown(period_df)
    # Sales by Town (added 2026-10-27) -- same period_df. "All" plus one
    # breakdown per state, so each state's view has its own totals/shares.
    town_breakdown = {"All": calculate_town_breakdown(period_df)}
    for state_code in town_breakdown["All"]["states"]:
        town_breakdown[state_code] = calculate_town_breakdown(_state_filtered(period_df, state_code))

    # Monthly Sales by Status (added 2026-10-27) -- all-time stacked bars,
    # one panel per state view that has sales. Each row carries the
    # drill-down URL; the chart appends &status=<segment>.
    monthly_status_views = []
    for key, label, _ in DASHBOARD_STATE_VIEWS:
        rows = calculate_monthly_sales_by_status(state_filtered_normalized[key])
        if not any(r["sales"] for r in rows):
            continue
        for r in rows:
            r["name"] = r["label"]
            r["url"] = url_for("all_sales_view", view="month_status", year=r["year"], month=r["month"], state_view=key)
        monthly_status_views.append({"key": key, "label": label, "rows": rows})

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
    # NJ - Sales Reps is the default team shown (by request, 2026-10-06;
    # previously defaulted to team_view_options[0], i.e. whichever team
    # happens to lead SALES_REP_TEAMS -- Junior). Falls back to that same
    # first-team behavior if NJ is ever missing from the list, same as
    # the invalid-key fallback below.
    default_volume_view = next(
        (opt["key"] for opt in team_view_options if opt["label"] == "NJ - Sales Reps"),
        team_view_options[0]["key"],
    )
    volume_view = request.args.get("volume_view", default_volume_view)
    if volume_view not in valid_volume_keys:
        volume_view = default_volume_view

    # Team Outbound Calls (added 2026-10-20) -- last 30 days of the whole
    # team's HubSpot outbound calls + each NJ rep's latest call. appdb only
    # (synced HubSpot data), independent of the period filter.
    team_calls = hubspot_store.get_team_call_overview(_today())
    # Calls per Sale = today's calls / today's OUTBOUND sales (by request,
    # 2026-10-20), same calculate_rep_metrics() count the Rep Profile's KPI uses.
    today_df = filter_by_period(normalized, "today")
    sales_today = {r["sales_rep"]: r["outbound"] for r in calculate_rep_metrics(today_df)}
    for row in team_calls["last_calls"]:
        row["sales_today"] = sales_today.get(row["rep"], 0)
        row["calls_per_sale"] = row["calls_today"] / row["sales_today"] if row["sales_today"] else None

    # Door Knockers KPI row (added 2026-10-23) -- same five numbers from
    # the knocker dataset; omitted if appdb is unreachable.
    knocker_df, _knocker_error = _load_knockers_or_error()
    # Monthly Sales by Status's "Internal vs Door Knockers" tab (added
    # 2026-10-27, by request): per month, an internal stack and a knocker
    # stack side by side -- never summed. Team-wide, all-time.
    if knocker_df is not None and monthly_status_views:
        internal = {r["label"]: r for r in monthly_status_views[0]["rows"]}
        knocker_months = {r["label"]: r for r in calculate_monthly_sales_by_status(knocker_df)}
        empty = {"sales": 0, **{k: 0 for k in MONTHLY_SALES_STATUSES}}
        compare_rows = []
        for period_month in pd.period_range(
                min(pd.Period(year=r["year"], month=r["month"], freq="M") for r in [*internal.values(), *knocker_months.values()]),
                pd.Period(_today(), freq="M"), freq="M"):
            label = period_month.strftime("%b %Y")
            first, last = period_month.start_time.date(), period_month.end_time.date()
            k = knocker_months.get(label, empty)
            compare_rows.append({
                "label": label, "short": period_month.strftime("%b"),
                "internal": {**internal.get(label, empty), "url": url_for(
                    "all_sales_view", view="month_status", year=period_month.year, month=period_month.month, state_view="team")},
                "knockers": {**{key: k[key] for key in empty}, "url": url_for(
                    "knocker_accounts", period="custom", start=first.isoformat(), end=last.isoformat())},
            })
        monthly_status_views.append({"key": "compare", "label": "Internal vs Door Knockers", "compare": True, "rows": compare_rows})
    knocker_kpis = total_kpis = None
    if knocker_df is not None:
        knocker_kpis = {
            "forecast": calculate_monthly_forecast(knocker_df),
            "daily": calculate_total_daily_sales(knocker_df),
            "averages": calculate_daily_averages(knocker_df),
        }
        # Total row = Team + Door Knockers, each order counted once (a
        # knocker order a rep entered is in both datasets). Follows the
        # Team row's own rules: Monthly Sales/Projection include Web,
        # Daily Sales/averages don't (reps_only).
        def _union(team_df):
            frames = [f[["sale_id", "sale_date", "state"]] for f in (team_df, knocker_df) if f is not None and not f.empty]
            return pd.concat(frames).drop_duplicates("sale_id") if frames else knocker_df
        total_kpis = {
            "forecast": calculate_monthly_forecast(_union(normalized)),
            "daily": calculate_total_daily_sales(_union(reps_only)),
            "averages": calculate_daily_averages(_union(reps_only)),
        }

    # Sales Calendar & Monthly Sales Trend follow the KPI bar's Team /
    # Door Knockers / Total toggle (2026-10-23, by request): the same
    # calendar + trend views, computed once per dataset. "team" is the
    # existing views above, unchanged; Total = Team + Door Knockers, each
    # order once (Web included, like Team's calendar/trend).
    dataset_views = [{
        "key": "team", "label": "Team",
        "calendar": calendar_views, "trend": monthly_trend_views, "mtd": mtd_trend_views,
    }]
    if knocker_df is not None:
        for ds_key, ds_label, ds_df in (("knockers", "Door Knockers", knocker_df), ("total", "Total", _union(normalized))):
            by_state = {key: _state_filtered(ds_df, code) for key, _, code in DASHBOARD_STATE_VIEWS}
            dataset_views.append({
                "key": ds_key, "label": ds_label,
                "calendar": {key: calculate_calendar_sales(v, cal_year, cal_month) for key, v in by_state.items()},
                "trend": {key: calculate_monthly_sales_trend(v) for key, v in by_state.items()},
                "mtd": {key: calculate_month_to_date_comparison(v) for key, v in by_state.items()},
            })
        # Total calendar cells carry their Team / Door Knockers split for
        # the hover title (there's no combined account list to link to).
        team_cal, knocker_cal, total_cal = (dataset_views[i]["calendar"] for i in range(3))
        for key in total_cal:
            split = {}
            for part, cal in (("team_count", team_cal[key]), ("knocker_count", knocker_cal[key])):
                for week in cal["weeks"]:
                    for cell in week:
                        if cell:
                            split.setdefault(cell["day"], {})[part] = cell["count"]
            for week in total_cal[key]["weeks"]:
                for cell in week:
                    if cell:
                        cell.update(split.get(cell["day"], {}))

    return render_template(
        "dashboard.html",
        knocker_kpis=knocker_kpis,
        total_kpis=total_kpis,
        dataset_views=dataset_views,
        opt_state_codes={key: code for key, _, code in DASHBOARD_STATE_VIEWS},
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
        mtd_trend_views=mtd_trend_views,
        trend_range=trend_range,
        dashboard_view_options=dashboard_view_options,
        channel_breakdown=channel_breakdown,
        hourly_breakdown=hourly_breakdown,
        channel_state_breakdown=channel_state_breakdown,
        town_breakdown=town_breakdown,
        monthly_status_views=monthly_status_views,
        sales_volume_views=sales_volume_views,
        volume_view=volume_view,
        team_view_options=team_view_options,
        team_has_reps=team_has_reps,
        team_calls=team_calls,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/dashboard/reps/<rep_name>")
@auth.login_required
def rep_profile(rep_name):
    ensure_data_loaded()

    normalized = get_normalized_sales()

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
    rep_sales_by_month = calculate_rep_sales_by_month(normalized, rep_name)

    # Always all-time, independent of the period filter -- see
    # calculate_needs_attention()'s docstring.
    needs_attention = calculate_needs_attention(normalized, rep_name)

    # Installs tab (added 2026-10-20) -- installs per install month,
    # all-time, each count drilling into ?view=installs_month.
    monthly_installs = calculate_rep_monthly_installs(normalized, rep_name)

    # Monthly Status tab (added 2026-10-26) -- installed / pending / needs
    # attention per scheduled install month, all-time, each count drilling into
    # ?view=status_month.
    monthly_status = calculate_rep_monthly_status(normalized, rep_name)

    # Cosmetic achievement badges (icons next to the rep's name) -- see
    # get_rep_achievements()'s docstring. Always all-time, purely display.
    achievements = get_rep_achievements(normalized, rep_name)

    # Checked Out tab (added 2026-10-27) -- Passings & Leads cards checked
    # out to this rep, newest checkout first.
    checked_out_available, checked_out, hubspot_calls_available = _checked_out_passings(rep_name)

    # Needs Attention workflow controls -- see attach_attention_metadata()
    # and README.md "Needs Attention Workflow". needs_attention_count
    # stays len(needs_attention) below, computed before this call and
    # completely unaffected by it -- Attention Status is metadata attached
    # to the same accounts, never a filter over them.
    attention_available, attention_progress = attach_attention_metadata(needs_attention)

    # Highlight "My Profile" in the top nav when the logged-in user is
    # viewing their own linked rep's profile; "Dashboard" otherwise.
    viewer = auth.current_user()
    is_own_profile = bool(viewer and viewer.get("sales_rep_name") == rep_name)

    # Outbound Call Performance -- only mapping/freshness here; the
    # numbers load client-side from the /calls/* JSON routes below.
    hubspot_context = hubspot_store.get_rep_call_context(rep_name)
    hubspot_context["last_success_display"] = (
        hubspot_context["last_success_at"].astimezone(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z")
        if hubspot_context["last_success_at"] else None
    )

    return render_template(
        "rep_profile.html",
        active_page="my_profile" if is_own_profile else "dashboard",
        rep_name=rep_name,
        period=period,
        custom_start=custom_start,
        custom_end=custom_end,
        rep_metrics=rep_metrics,
        channel_breakdown=channel_breakdown,
        hourly_breakdown=hourly_breakdown,
        rep_monthly_activity=rep_monthly_activity,
        rep_sales_by_month=rep_sales_by_month,
        needs_attention=needs_attention,
        needs_attention_count=len(needs_attention),
        monthly_installs=monthly_installs,
        monthly_status=monthly_status,
        attention_available=attention_available,
        attention_progress=attention_progress,
        attention_statuses=attention_store.ATTENTION_STATUSES,
        achievements=achievements,
        checked_out=checked_out,
        checked_out_available=checked_out_available,
        checkout_buckets=passings_activity_store.get_buckets(rep_name)[1],
        activity_types=passings_activity_store.ACTIVITY_TYPES,
        hubspot_calls_available=hubspot_calls_available,
        hubspot_context=hubspot_context,
        can_view_call_history=permissions.can_view_call_history(viewer, rep_name),
        call_date_ranges=hubspot_logic.DATE_RANGES,
        call_default_range=hubspot_logic.DEFAULT_DATE_RANGE,
        last_refreshed=data_store["last_refreshed"],
    )


def _checked_out_passings(rep_name):
    """(available, rows, hubspot_calls_available) for the Rep Profile's
    Checked Out tab: the Passings & Leads rows checked out to this rep,
    decorated like /passings cards, newest checkout first. available is
    False when appdb or the passings data couldn't be read."""
    available, checkouts = passings_activity_store.get_checkouts(rep_name)
    if not available or not checkouts:
        return available, [], False
    passings_cache.start_scheduler()
    source = passings_cache if passings_cache.wait_until_ready(20) else passings_data
    _, residential_marks = passings_activity_store.get_residential_marks()
    filters = {"place_id_in": sorted(checkouts), "residential_ids": set(residential_marks)}
    rows, page = [], 1
    while True:
        result = source.get_page(filters, None, "place_id", "asc", page)
        if not result.get("ok"):
            return False, [], False
        rows.extend(result["rows"])
        if page >= result["total_pages"]:
            break
        page += 1
    billed_uuids = _billed_subscriber_uuids()
    order_statuses = (
        passings_cache.order_statuses(billed_uuids)
        if billed_uuids is not None and passings_cache._state["orders"] is not None else None
    )
    hubspot_calls_available, hubspot_call_stats = hubspot_store.get_call_stats_by_email()
    _decorate_passing_rows(rows, order_statuses, hubspot_call_stats, residential_marks, checkouts)
    for row in rows:
        row["checkout"] = {
            **row["checkout"],
            "checked_out_at_display": _format_note_timestamp(row["checkout"]["checked_out_at"]),
        }
    rows.sort(key=lambda r: r["checkout"]["checked_out_at"], reverse=True)
    return True, rows, hubspot_calls_available


# ================================================================
# OUTBOUND CALL PERFORMANCE (HubSpot) -- Rep Profile JSON endpoints
# ================================================================
# Read only from appdb (hubspot_store), never from HubSpot directly.
# Same visibility as the profile page itself (login_required) for the
# aggregate KPIs/chart; individual call records (dialed numbers) are
# further restricted by permissions.can_view_call_history().

def _require_known_rep(rep_name):
    ensure_data_loaded()
    normalized = get_normalized_sales()
    if normalized is not None and not normalized.empty and rep_name not in set(normalized["sales_rep"].unique()):
        abort(404)
    return normalized


def _call_range_args():
    """(start_date, end_date, range_key) from ?range=&start=&end=, or
    raises ValueError with a user-facing message."""
    range_key = request.args.get("range", hubspot_logic.DEFAULT_DATE_RANGE)
    start_date, end_date = hubspot_logic.resolve_date_range(
        range_key, request.args.get("start"), request.args.get("end"), _today(),
    )
    # Nothing before HUBSPOT_SYNC_START_DATE is synced -- clamp so an
    # older range never reports "0 calls" for days we simply don't have.
    floor = hubspot_store.SYNC_START_DATE
    if end_date < floor:
        raise ValueError(f"Call data is only synced from {floor.strftime('%b %-d, %Y')} onward.")
    return max(start_date, floor), end_date, range_key


def _call_scale_arg(range_key, start_date, end_date):
    """(scale, mode, allowed). ?scale= is "auto" (default -- derived from
    the date range) or a manual grouping that must be allowed for that
    range. Grouping only shapes the chart's X axis; it never changes which
    calls are counted."""
    requested = request.args.get("scale", hubspot_logic.AUTO_SCALE)
    scale, allowed = hubspot_logic.resolve_time_scale(requested, range_key, start_date, end_date)
    if hubspot_logic.bucket_count(start_date, end_date, scale) > hubspot_logic.MAX_BUCKETS:
        raise ValueError("That date range has too many points for this grouping -- pick a coarser one.")
    mode = hubspot_logic.AUTO_SCALE if requested in ("", hubspot_logic.AUTO_SCALE) else "manual"
    return scale, mode, allowed


def _scale_json(scale, mode, allowed, range_key, start_date, end_date):
    return {
        "scale": scale,
        "scale_mode": mode,
        "auto_scale": hubspot_logic.auto_time_scale(range_key, start_date, end_date),
        "allowed_scales": allowed,
    }


def _call_api_error(exc):
    if isinstance(exc, ValueError):
        return jsonify({"ok": False, "error": str(exc)}), 400
    print(f"Outbound call analytics query failed: {db.sanitize_error(exc)}")
    return jsonify({"ok": False, "error": "Call data is temporarily unavailable (database error)."}), 503


def _iso(dt):
    return dt.astimezone(EASTERN_TZ).isoformat() if dt else None


def _last_call_display(dt):
    """'Today, 3:25 PM' / 'Yesterday, 9:02 AM' / 'Oct 14, 4:10 PM'
    (year added outside the current one), Eastern."""
    if not dt:
        return None
    local = dt.astimezone(EASTERN_TZ)
    days_ago = (_today() - local.date()).days
    time_part = local.strftime("%-I:%M %p")
    if days_ago == 0:
        return f"Today, {time_part}"
    if days_ago == 1:
        return f"Yesterday, {time_part}"
    fmt = "%b %-d" if local.year == _today().year else "%b %-d, %Y"
    return f"{local.strftime(fmt)}, {time_part}"


@app.route("/dashboard/reps/<rep_name>/calls/summary")
@auth.login_required
def rep_call_summary(rep_name):
    normalized = _require_known_rep(rep_name)
    try:
        start_date, end_date, range_key = _call_range_args()
        scale, scale_mode, allowed = _call_scale_arg(range_key, start_date, end_date)
        summary = hubspot_store.get_call_summary(rep_name, start_date, end_date, scale)
    except Exception as exc:
        return _call_api_error(exc)

    # Calls per Sale counts OUTBOUND sales only (by request, 2026-10-20) --
    # calculate_rep_metrics()'s outbound count over the same
    # business-timezone days as the call range.
    period_df = filter_by_period(normalized, "custom", start_date.isoformat(), end_date.isoformat())
    rep_row = next((r for r in calculate_rep_metrics(period_df) if r["sales_rep"] == rep_name), None)
    sales = (rep_row["outbound"] or 0) if rep_row else 0

    context = hubspot_store.get_rep_call_context(rep_name)
    total = summary["total_calls"]
    return jsonify({
        "ok": True,
        "range": {"key": range_key, "start": start_date.isoformat(), "end": end_date.isoformat()},
        **_scale_json(scale, scale_mode, allowed, range_key, start_date, end_date),
        "mapped": summary["owner_count"] > 0,
        "ever_synced": context["ever_synced"],
        "last_success_at": _iso(context["last_success_at"]),
        "last_error": context["last_error"],
        "kpis": {
            "total_calls": total,
            "connected_calls": summary["connected_calls"],
            "connection_rate": summary["connection_rate"],
            "total_talk_ms": summary["total_talk_ms"],
            "total_talk_display": hubspot_logic.format_duration_hms(summary["total_talk_ms"]),
            "avg_duration_ms": summary["avg_duration_ms"],
            "avg_duration_display": hubspot_logic.format_duration_ms(summary["avg_duration_ms"]),
            "calls_with_duration": summary["calls_with_duration"],
            "sales": sales,
            "calls_per_sale": (total / sales) if sales else None,
            "calls_per_hour": summary["calls_per_hour"],
            "working_days": summary["working_days"],
            "working_time_display": hubspot_logic.format_hours_minutes(summary["working_seconds"])
                if summary["working_seconds"] else None,
            "last_call_at": _iso(summary["last_call_at"]),
            "last_call_display": _last_call_display(summary["last_call_at"]),
        },
        "series": summary["series"],
    })


@app.route("/dashboard/reps/<rep_name>/calls/volume")
@auth.login_required
def rep_call_volume(rep_name):
    _require_known_rep(rep_name)
    try:
        start_date, end_date, range_key = _call_range_args()
        scale, scale_mode, allowed = _call_scale_arg(range_key, start_date, end_date)
        series = hubspot_store.get_call_volume(rep_name, start_date, end_date, scale)
    except Exception as exc:
        return _call_api_error(exc)
    return jsonify({"ok": True, **_scale_json(scale, scale_mode, allowed, range_key, start_date, end_date), "series": series})


@app.route("/dashboard/reps/<rep_name>/calls/history")
@auth.login_required
def rep_call_history(rep_name):
    _require_known_rep(rep_name)
    if not permissions.can_view_call_history(auth.current_user(), rep_name):
        return jsonify({"ok": False, "error": "Only Admins and this rep can view individual call records."}), 403
    try:
        start_date, end_date, _range_key = _call_range_args()
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            raise ValueError("Invalid page number.") from None
        if page < 1 or page > 100000:
            raise ValueError("Invalid page number.")
        order = request.args.get("order", "desc")
        if order not in ("asc", "desc"):
            raise ValueError("Invalid sort order.")
        outcome = (request.args.get("outcome") or "").strip()[:100] or None
        history = hubspot_store.get_call_history(
            rep_name, start_date, end_date, page=page, outcome=outcome, newest_first=order == "desc",
        )
    except Exception as exc:
        return _call_api_error(exc)
    return jsonify({"ok": True, **history})


@app.route("/dashboard/reps/<rep_name>/accounts")
@auth.login_required
def bulk_account_view(rep_name):
    ensure_data_loaded()

    normalized = get_normalized_sales()

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
    elif view == "installs_month":
        # Rep Profile Installs tab's per-month drill-down -- by install
        # month, all-time like the tab itself.
        try:
            view_title, accounts = get_install_month_account_view(
                normalized, rep_name, int(request.args.get("year", 0)), int(request.args.get("month", 0)),
            )
        except (TypeError, ValueError):
            abort(404)
        all_time = True
    elif view == "status_month":
        # Rep Profile Monthly Status tab's per-cell drill-down -- by install
        # month, all-time like the tab itself.
        try:
            view_title, accounts = get_status_month_account_view(
                normalized, rep_name, int(request.args.get("year", 0)), int(request.args.get("month", 0)),
                request.args.get("status", "all"),
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
    normalized = get_normalized_sales()
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

    normalized = get_normalized_sales()

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
    elif view == "town":
        # Sales by Town count click-through (added 2026-10-27) -- same
        # period-filtered dataset as the section's own counts.
        view_title, accounts = get_town_account_view(
            normalized, request.args.get("town", ""), request.args.get("state", ""),
            request.args.get("metric", "sales"), period, custom_start, custom_end,
        )
        all_time = False
    elif view == "month_status":
        # Monthly Sales by Status segment click (added 2026-10-27) --
        # all-time, scoped to the chart's Team/state panel.
        state_view = request.args.get("state_view", "team")
        if state_view not in DASHBOARD_STATE_VIEW_KEYS:
            state_view = "team"
        view_title, accounts = get_month_status_account_view(
            _state_filtered(normalized, _state_code_for_view(state_view)),
            request.args.get("year"), request.args.get("month"), request.args.get("status", "all"),
        )
        if state_view != "team":
            view_title = f"{view_title} — {next(label for key, label, _ in DASHBOARD_STATE_VIEWS if key == state_view)}"
        all_time = True
    elif view in BULK_ACCOUNT_VIEWS:
        # Team-wide registry views (e.g. Latest Accounts Sold's "View All
        # Sales") leave Web out, like Live Sales Activity itself. The
        # channel/date drill-downs above keep it, matching the channel
        # chart and Sales Calendar counts they're clicked from.
        view_title, accounts = get_bulk_account_view(exclude_web(normalized), None, view, period, custom_start, custom_end)
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
    there are two render paths sharing one template/pipeline). Runs the
    FULL pipeline -- fetch PlanetWeb, clean, tag, guardrails -- via
    marketing_cleaning.run_cleaning_cached(), which only actually
    re-fetches/re-cleans once an hour (CACHE_TTL_SECONDS in
    marketing_cleaning.py) rather than on every request -- changed
    2026-08-20, by request, after "clean on every refresh" (the original
    behavior, ~10s against ~15k PlanetWeb rows) proved too slow for real
    usage. Still no data_store/ensure_data_loaded() involved -- this
    pipeline has its own separate cache, unrelated to the main
    dashboard's.

    Returns the rendered HTML directly (not render_template()) -- the
    report is entirely self-contained (its own <style>/<script>, no
    team_dashboard.css, no _topnav.html) so it can be shared as a plain
    file outside this app unchanged; back_url adds a small "Dashboard"
    link back into the rest of the app, live-served copy only. The
    standalone file has none: no app to link back into.

    The "Available Now (ads)" KPI card used to be a clickable drill-down
    into real name/address data (a dedicated /marketing/available-now
    route + marketing_cleaning.get_available_now_accounts()) -- removed
    2026-08-20, by request. The KPI card is now a plain, non-clickable
    number like every other KPI card on this report."""
    html, _guardrail_report = marketing_cleaning.generate_report(back_url=url_for("dashboard_page"))
    return html


# ================================================================
# PASSINGS & LEADS (README.md "Passings & Leads")
# ================================================================

PASSINGS_CATEGORY_OPTIONS = [
    ("", "All"),
    (passings_classification.CATEGORY_RESIDENTIAL_LEAD, "Residential Leads"),
    (passings_classification.CATEGORY_COMMERCIAL_HOT_LEAD, "Commercial Hot Leads"),
    (passings_classification.CATEGORY_COMMERCIAL_FIELD_TARGET, "Commercial Field Targets"),
]

PASSINGS_SORT_OPTIONS = [
    ("became_available", "Became Available"),
    ("address", "Address"),
    ("city", "City"),
    ("state", "State"),
    ("zip", "ZIP"),
    ("email", "Email"),
    ("availability", "Availability"),
    ("units", "Units"),
    ("place_id", "Passing ID"),
]

_PROPERTY_TYPE_LABELS = {label for label, _keywords in passings_classification.COMMERCIAL_PROPERTY_KEYWORDS}


def _parse_bool_param(value):
    """'yes'/'no' query param -> True/False/None ("not set" -- the filter
    is inactive, not "explicitly false")."""
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _parse_date_param(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _resolve_passings_activity_filters(has_activity, called, visited, rep, last_activity_from, last_activity_to):
    """Cross-database orchestration for the activity-based filters
    (spec #15: Has Activity/No Activity, Called/Not Called, Visited/Not
    Visited, Rep, Last Activity date range) -- see passings_data.py's
    module docstring for why this can't be a single SQL join across
    PlanetWeb (SQL Server) and appdb (Postgres). Returns
    (place_id_in, place_id_not_in) as sets or None, ready to drop into a
    passings_data filters dict.

    Every "positive"/narrowing constraint (Has Activity=Yes, Called=Yes,
    Visited=Yes, Rep, Last Activity range) is ANDed together via set
    intersection so combined filters behave correctly -- e.g. spec #15's
    own example of Rep + Called both active should mean "this rep called
    this Passing", not "either". Every "No"/negative constraint is
    unioned into one exclusion set instead, since excluding a Passing for
    ANY one of several reasons is the correct OR-of-exclusions semantics."""
    required_sets = []
    excluded_ids = set()
    cache = {}

    def all_activity_ids():
        if "ids" not in cache:
            cache["ids"] = set(passings_activity_store.get_all_place_ids_with_activity())
        return cache["ids"]

    if has_activity is True:
        required_sets.append(all_activity_ids())
    elif has_activity is False:
        excluded_ids |= all_activity_ids()

    if called is True:
        required_sets.append(set(passings_activity_store.get_place_ids_with_activity_type("Call")))
    elif called is False:
        excluded_ids |= set(passings_activity_store.get_place_ids_with_activity_type("Call"))

    if visited is True:
        required_sets.append(set(passings_activity_store.get_place_ids_with_activity_type("Field Visit")))
    elif visited is False:
        excluded_ids |= set(passings_activity_store.get_place_ids_with_activity_type("Field Visit"))

    if rep:
        required_sets.append(set(passings_activity_store.get_place_ids_by_rep(rep)))

    if last_activity_from or last_activity_to:
        required_sets.append(set(passings_activity_store.get_place_ids_by_last_activity_range(last_activity_from, last_activity_to)))

    place_id_in = set.intersection(*required_sets) if required_sets else None
    place_id_not_in = excluded_ids if excluded_ids else None
    return place_id_in, place_id_not_in


_billed_uuids_cache = {"source": None, "uuids": None}


def _billed_subscriber_uuids():
    """Lowercased Vision subscriber uuids with a first invoice -- the
    dashboard's "Installed" test (_merge_install_dates()) -- for the
    Passings & Leads order status. Rebuilt only when the background data
    refresh swaps in new Vision data; None until it has loaded once."""
    vision = data_store.get("vision_packages")
    if vision is None or "subscriber_first_invoice_date" not in vision.columns:
        return None
    if _billed_uuids_cache["source"] is not vision:
        billed = vision.loc[vision["subscriber_first_invoice_date"].notna(), "subscriber_uuid"]
        _billed_uuids_cache.update(
            source=vision, uuids=set(billed.dropna().astype(str).str.strip().str.lower()),
        )
    return _billed_uuids_cache["uuids"]


def _decorate_passing_rows(rows, order_statuses, hubspot_call_stats, residential_marks, checkouts):
    """Adds the appdb/snapshot extras a Passing card shows (last activity,
    PAS link, order status, HubSpot calls, residential mark, checkout) to
    rows in place. Shared by /passings and the Rep Profile's Checked Out
    tab. Returns whether last activity could be loaded."""
    last_activity_available, last_activity_map = passings_activity_store.get_last_activity_map(
        [row["PlaceID"] for row in rows]
    )
    for row in rows:
        activity = last_activity_map.get(row["PlaceID"])
        if activity is not None:
            activity = {**activity, "activity_at_display": _format_note_timestamp(activity["activity_at"])}
        row["last_activity"] = activity
        row["pas_url"] = pas_place_url(row["PlaceID"])
        row["order_status"] = (
            order_statuses.get(int(row["PlaceID"]), passings_cache.NO_ORDER) if order_statuses is not None else None
        )
        # HubSpot call count / last call for leads (2026-10-26) -- None
        # when the lead's email has no outbound HubSpot calls.
        stats = hubspot_call_stats.get((row["EmailAddress"] or "").strip().lower()) if row["is_lead"] else None
        row["hubspot_calls"] = (
            {"calls": stats["calls"], "last_call_display": _format_note_timestamp(stats["last_call_at"])}
            if stats else None
        )
        # Offer "Residential Address" on commercial cards; once marked, the
        # card shows as residential with an Undo.
        row["can_mark_residential"] = row["category"] in ("commercial_hot_lead", "commercial_field_target")
        mark = residential_marks.get(row["PlaceID"])
        row["residential_mark"] = mark
        if mark:
            reclassified = passings_classification.classify(
                {"email": row["EmailAddress"], "prequal_as_business": 0, "class_name": None}
            )
            row.update({k: reclassified[k] for k in ("category", "category_label", "primary_action", "property_type_label")})
            row["can_mark_residential"] = False
        # Check Out (2026-10-27): who this place is checked out to, if anyone.
        row["checkout"] = checkouts.get(row["PlaceID"])
    return last_activity_available


@app.route("/passings")
@auth.login_required
def passings_page():
    """Passings & Leads -- footprint/lead-penetration analytics plus the
    three prospecting workflows (Residential Leads, Commercial Hot Leads,
    Commercial Field Targets). Every filter/search/sort/page value is read
    fresh from the query string and re-queried on this one request, same
    convention admin_marketing_form_data() already uses -- there is no
    cached copy to go stale. Server-side filtered/searched/sorted/
    paginated against PlanetWeb the whole way (passings_data.py) -- never
    a whole-dataset load, see that module's docstring.

    Activity-based filters are resolved against appdb FIRST
    (_resolve_passings_activity_filters(), passings_activity_store.py),
    then folded into the PlanetWeb query as a place_id allow-/deny-list."""
    # Became Available filter. All Time by default since it now scopes the
    # KPI totals too (2026-10-20); the list still sorts newest-first.
    time_range = request.args.get("time_range", "all_time")
    custom_start_raw = request.args.get("start", "")
    custom_end_raw = request.args.get("end", "")
    became_from, became_to = passings_data.resolve_time_range(time_range, custom_start_raw, custom_end_raw)

    category = request.args.get("category", "")
    if category not in {key for key, _label in PASSINGS_CATEGORY_OPTIONS if key}:
        category = ""

    property_type = request.args.get("property_type", "")
    if property_type not in _PROPERTY_TYPE_LABELS:
        property_type = ""

    availability_raw = request.args.get("availability", "")
    availability_id = int(availability_raw) if availability_raw in ("1", "3") else None

    has_lead_raw = request.args.get("has_lead", "")
    has_activity_raw = request.args.get("has_activity", "")
    called_raw = request.args.get("called", "")
    visited_raw = request.args.get("visited", "")
    has_lead = _parse_bool_param(has_lead_raw)
    has_activity = _parse_bool_param(has_activity_raw)
    called = _parse_bool_param(called_raw)
    visited = _parse_bool_param(visited_raw)
    prequal_only = request.args.get("prequal_only") == "1"

    city = request.args.get("city", "").strip()
    state = request.args.get("state", "").strip()
    zip_code = request.args.get("zip", "").strip()
    rep = request.args.get("rep", "").strip()
    last_activity_from_raw = request.args.get("last_activity_from", "")
    last_activity_to_raw = request.args.get("last_activity_to", "")
    last_activity_from = _parse_date_param(last_activity_from_raw)
    last_activity_to = _parse_date_param(last_activity_to_raw)
    search = request.args.get("search", "").strip()

    # HubSpot call filters (added 2026-10-26) -- leads only, matched by
    # email to synced outbound HubSpot calls; see
    # passings_cache._hubspot_call_mask().
    hubspot_called_raw = request.args.get("hubspot_called", "")
    hubspot_called = _parse_bool_param(hubspot_called_raw)
    hubspot_min_calls_raw = request.args.get("hubspot_min_calls", "").strip()
    try:
        hubspot_min_calls = max(0, int(hubspot_min_calls_raw)) if hubspot_min_calls_raw else None
    except ValueError:
        hubspot_min_calls, hubspot_min_calls_raw = None, ""
    hubspot_last_call_from_raw = request.args.get("hubspot_last_call_from", "")
    hubspot_last_call_to_raw = request.args.get("hubspot_last_call_to", "")
    hubspot_last_call_from = _parse_date_param(hubspot_last_call_from_raw)
    hubspot_last_call_to = _parse_date_param(hubspot_last_call_to_raw)
    hubspot_calls_available, hubspot_call_stats = hubspot_store.get_call_stats_by_email()

    # Order status per passing (added 2026-10-26) -- see
    # passings_cache.compute_order_statuses(). Unavailable (no badge, no
    # filter) until both the order list and Vision billing have loaded.
    order_status = request.args.get("order_status", "")
    if order_status not in passings_cache.ORDER_STATUSES:
        order_status = ""
    billed_uuids = _billed_subscriber_uuids()
    order_statuses = (
        passings_cache.order_statuses(billed_uuids)
        if billed_uuids is not None and passings_cache._state["orders"] is not None else None
    )

    sort_key = request.args.get("sort", "became_available")
    sort_dir = request.args.get("dir", "desc")
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    place_id_in, place_id_not_in = _resolve_passings_activity_filters(
        has_activity, called, visited, rep, last_activity_from, last_activity_to
    )

    filters = {
        "category": category or None,
        "prequal_only": prequal_only,
        "property_type": property_type or None,
        "has_lead": has_lead,
        "city": city or None,
        "state": state or None,
        "zip": zip_code or None,
        "availability_id": availability_id,
        "became_from": became_from,
        "became_to": became_to,
        "place_id_in": place_id_in,
        "place_id_not_in": place_id_not_in,
    }
    # "Residential Address" marks: classified as residential, so they
    # leave Commercial Hot Leads / Field Targets (passings_cache only --
    # the live-SQL fallback below can't see appdb marks).
    _, residential_marks = passings_activity_store.get_residential_marks()
    filters["residential_ids"] = set(residential_marks)
    filters.update(
        hubspot_called=hubspot_called,
        hubspot_min_calls=hubspot_min_calls,
        hubspot_last_call_from=hubspot_last_call_from,
        hubspot_last_call_to=hubspot_last_call_to,
        hubspot_call_stats=hubspot_call_stats,
        order_status=order_status if order_statuses is not None else None,
        order_statuses=order_statuses,
    )
    snapshot_filters_active = bool(
        hubspot_called is not None or hubspot_min_calls or hubspot_last_call_from or hubspot_last_call_to
        or order_status
    )

    # In-memory snapshot (passings_cache.py, refreshed in the background)
    # once it has loaded; the live PlanetWeb queries only until then, e.g.
    # right after a restart. Same functions, same results either way.
    # Right after a restart the first snapshot takes ~5s; wait for it so
    # marks apply, and only fall back to live SQL if PlanetWeb is slow/down.
    passings_cache.start_scheduler()
    source = passings_cache if passings_cache.wait_until_ready(20) else passings_data

    page_result = source.get_page(filters, search, sort_key, sort_dir, page)

    activity_place_ids = passings_activity_store.get_all_place_ids_with_activity()
    kpis = source.get_kpis(filters, search=search, activity_place_ids=activity_place_ids)

    granularity = passings_data.auto_granularity(became_from, became_to)
    over_time = source.get_passings_over_time(filters, search, granularity)
    market_by_city = source.get_market_breakdown(filters, search, dimension="city", top_n=15)
    # Same rows as market_by_city, just re-sorted lowest-penetration-first
    # (spec #13: "High Passings + Low Lead Penetration" markets) -- one
    # query, two views, rather than a second query with a different
    # ORDER BY (see passings_data.get_market_breakdown()'s docstring).
    market_by_city_low_penetration = sorted(
        market_by_city.get("markets") or [], key=lambda m: m["lead_penetration_pct"]
    )[:15]

    # Chart-ready {label, value}/{label, a, b} series, computed here rather
    # than in the template -- simpler and more testable than zipping
    # parallel lists inside Jinja for a chart's data-* JSON attribute.
    over_time_buckets = over_time.get("buckets") or []
    new_passings_series = [{"label": b["bucket"], "value": b["new_passings"]} for b in over_time_buckets]
    cumulative_passings_series = [{"label": b["bucket"], "value": b["cumulative_passings"]} for b in over_time_buckets]
    new_vs_leads_series = [{"label": b["bucket"], "a": b["new_passings"], "b": b["new_leads"]} for b in over_time_buckets]

    market_labels = [m["market"] for m in (market_by_city.get("markets") or [])]
    market_values = [m["passings"] for m in (market_by_city.get("markets") or [])]
    low_penetration_labels = [m["market"] for m in market_by_city_low_penetration]
    low_penetration_values = [round(m["lead_penetration_pct"], 1) for m in market_by_city_low_penetration]

    _, checkouts = passings_activity_store.get_checkouts()
    last_activity_available = _decorate_passing_rows(
        page_result["rows"], order_statuses, hubspot_call_stats, residential_marks, checkouts
    )

    reps_with_activity = passings_activity_store.get_reps_with_activity()
    filter_options = source.get_filter_options()

    return render_template(
        "passings.html",
        active_page="passings",
        page_result=page_result,
        kpis=kpis,
        over_time=over_time,
        over_time_granularity=granularity,
        new_passings_series=new_passings_series,
        cumulative_passings_series=cumulative_passings_series,
        new_vs_leads_series=new_vs_leads_series,
        market_by_city=market_by_city,
        market_labels=market_labels,
        market_values=market_values,
        low_penetration_labels=low_penetration_labels,
        low_penetration_values=low_penetration_values,
        filter_options=filter_options,
        reps_with_activity=reps_with_activity,
        last_activity_available=last_activity_available,
        hubspot_calls_available=hubspot_calls_available,
        # The live-SQL fallback (right after a restart) can't apply the
        # HubSpot call / order status filters -- the template says so
        # instead of quietly showing unfiltered results.
        snapshot_filters_ignored=snapshot_filters_active and (source is passings_data or order_statuses is None),
        order_status_options=passings_cache.ORDER_STATUSES,
        activity_types=passings_activity_store.ACTIVITY_TYPES,
        # Every Sales Rep (by request, 2026-10-27), not just reps with a login.
        checkout_reps=user_store.list_sales_reps(),
        checkout_default_rep_id=(auth.current_user() or {}).get("sales_rep_id"),
        category_options=PASSINGS_CATEGORY_OPTIONS,
        sort_options=PASSINGS_SORT_OPTIONS,
        time_range_options=passings_data.TIME_RANGE_OPTIONS,
        selected={
            "time_range": time_range, "start": custom_start_raw, "end": custom_end_raw,
            "category": category, "property_type": property_type, "availability": availability_raw,
            "has_lead": has_lead_raw, "has_activity": has_activity_raw,
            "called": called_raw, "visited": visited_raw,
            "prequal_only": "1" if prequal_only else "",
            "city": city, "state": state, "zip": zip_code, "rep": rep,
            "last_activity_from": last_activity_from_raw, "last_activity_to": last_activity_to_raw,
            "search": search, "sort": sort_key, "dir": sort_dir,
            "hubspot_called": hubspot_called_raw, "hubspot_min_calls": hubspot_min_calls_raw,
            "hubspot_last_call_from": hubspot_last_call_from_raw, "hubspot_last_call_to": hubspot_last_call_to_raw,
            "order_status": order_status,
        },
        category_labels=passings_classification.CATEGORY_LABELS,
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/passings/residential", methods=["POST"])
@auth.login_required
def passings_mark_residential():
    """Marks a Commercial Hot Lead / Field Target card as a "Residential
    Address" (or undoes it) -- any logged-in user, same as logging
    activity. The page reloads afterwards so the card leaves (or returns
    to) the commercial lists and every count updates."""
    payload = request.get_json(silent=True) or {}
    ok, error = passings_activity_store.set_residential_mark(
        payload.get("place_id"), bool(payload.get("residential")), auth.current_user()
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@app.route("/passings/checkout", methods=["POST"])
@auth.login_required
def passings_checkout():
    """Check Out (2026-10-27): the selected cards go to one sales rep's
    Rep Profile "Checked Out" tab. They stay on /passings. Any logged-in
    user may check out to any Sales Rep (check_out() rejects unknown ones)."""
    payload = request.get_json(silent=True) or {}
    ok, error, count = passings_activity_store.check_out(
        payload.get("place_ids"), payload.get("sales_rep_id"), auth.current_user()
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "count": count})


@app.route("/passings/checkout/return", methods=["POST"])
@auth.login_required
def passings_checkout_return():
    """"Return" on a Checked Out card: takes it off the rep's profile."""
    payload = request.get_json(silent=True) or {}
    ok, error = passings_activity_store.return_checkout(payload.get("place_id"))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


# Checked Out buckets (2026-10-27): each Rep Profile's own custom groups
# for its checked-out cards -- scoped to that rep, never shown elsewhere.

@app.route("/dashboard/reps/<rep_name>/checkout-buckets", methods=["POST"])
@auth.login_required
def checkout_bucket_create(rep_name):
    payload = request.get_json(silent=True) or {}
    ok, error, bucket = passings_activity_store.create_bucket(rep_name, payload.get("name"))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "bucket": bucket})


@app.route("/dashboard/reps/<rep_name>/checkout-buckets/<int:bucket_id>/delete", methods=["POST"])
@auth.login_required
def checkout_bucket_delete(rep_name, bucket_id):
    ok, error = passings_activity_store.delete_bucket(rep_name, bucket_id)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@app.route("/dashboard/reps/<rep_name>/checkout-buckets/assign", methods=["POST"])
@auth.login_required
def checkout_bucket_assign(rep_name):
    payload = request.get_json(silent=True) or {}
    ok, error = passings_activity_store.set_checkout_bucket(
        rep_name, payload.get("place_id"), payload.get("bucket_id")
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@app.route("/passings/<int:place_id>/hubspot")
@auth.login_required
def passings_open_hubspot(place_id):
    """"Open in HubSpot" on a Passings & Leads card: redirects to the
    HubSpot contact with the passing's email (or HubSpot's contact search
    for it when there's no match). Keyed by Passing ID so the email never
    appears in a dashboard URL; looked up server-side at click time so
    rendering a page of cards costs no HubSpot calls."""
    source = passings_cache if passings_cache.ready() else passings_data
    result = source.get_page({"place_id_in": [place_id]}, None, "email", "asc", 1)
    row = (result.get("rows") or [None])[0]
    if not row or not row.get("is_lead"):
        abort(404)
    try:
        url, _matched = hubspot_store.contact_url_for_email(row["EmailAddress"])
    except Exception as exc:
        print(f"[hubspot] Open in HubSpot failed for passing {place_id}: {hubspot_client.sanitize(exc)}")
        url = None
    if not url:
        return Response("HubSpot isn't connected yet, so this contact can't be opened. "
                        "Run a HubSpot sync from Admin first.", status=503, mimetype="text/plain")
    return redirect(url)


@app.route("/passings/activity", methods=["POST"])
@auth.login_required
def passings_log_activity():
    """Quick Actions (Log Call/Email/Visit/Note, spec #21) -- small
    fetch() POST + in-place DOM update, same shape as
    attention_set_status()/attention_add_note(). There is no
    account-ownership permission check like _authorize_account_action()
    here -- a Passing has no "owning rep" concept (spec #24: activity is
    workflow metadata, not sales attribution), so any logged-in user may
    log activity on any Passing, same as every other login_required-only
    page in this app."""
    payload = request.get_json(silent=True) or {}
    place_id = payload.get("place_id")
    activity_type = (payload.get("activity_type") or "").strip()
    note = payload.get("note") or ""

    user = auth.current_user()
    ok, error, activity = passings_activity_store.log_activity(place_id, activity_type, note, user)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    return jsonify({
        "ok": True,
        "place_id": activity["place_id"],
        "last_activity": {
            "activity_type": activity["activity_type"],
            "activity_at_display": _format_note_timestamp(activity["activity_at"]),
            "rep_display_name": activity["rep_display_name"],
            "note": activity["note"],
        },
    })


# ================================================================
# Staffing calendar -- events that need reps + door-knocking shifts
# ================================================================

def _staffing_redirect(day=None, event_id=None):
    """Back to the calendar on the day (and event) just acted on."""
    day = day or staffing_store._parse_date(request.form.get("return_day")) or _today()
    url = url_for("staffing_page", month=day.strftime("%Y-%m"), day=day.isoformat(), event=event_id)
    return redirect(url + (f"#event-{event_id}" if event_id else ""))


def _staffing_result(ok, error, success_message, event_id=None):
    flash(success_message if ok else error, "ok" if ok else "error")
    return _staffing_redirect(event_id=event_id)


@app.route("/staffing")
@auth.login_required
def staffing_page():
    """Month calendar of staffing events (Admins post, reps sign up) and
    reps' own door-knocking shifts. ?month=YYYY-MM picks the month,
    ?day=YYYY-MM-DD the day shown in the detail panel (defaults to the
    next Saturday if it's in view, else today / the 1st), ?event=<id>
    the event to highlight there and preselect in "Staff an event"."""
    today = _today()
    try:
        year, month = (int(x) for x in request.args.get("month", "").split("-"))
        date(year, month, 1)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    selected = staffing_store._parse_date(request.args.get("day"))
    if selected is None or (selected.year, selected.month) != (year, month):
        next_saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        if (next_saturday.year, next_saturday.month) == (year, month):
            selected = next_saturday
        elif (today.year, today.month) == (year, month):
            selected = today
        else:
            selected = date(year, month, 1)

    user = auth.current_user()
    is_admin = permissions.can_manage_staffing(user)
    cal = staffing_store.get_calendar(year, month, selected, today)
    my_staff_key = staffing_store.staff_key(user)
    return render_template(
        "staffing.html",
        active_page="staffing",
        cal=cal,
        joinable=[e for e in cal["upcoming_events"] if my_staff_key not in e["staff_keys"]],
        my_events=[e for e in cal["upcoming_events"] if my_staff_key in e["staff_keys"]],
        today=today,
        is_admin=is_admin,
        can_staff_self=permissions.can_staff_self(user),
        my_rep_id=user.get("sales_rep_id"),
        my_staff_key=my_staff_key,
        selected_event=request.args.get("event", type=int),
        # Everyone can add any sales rep to an event or book them a shift --
        # only reps whose user account has the Sales Rep role are offered.
        sales_reps=user_store.list_role_sales_reps(),
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/staffing/events", methods=["POST"])
@auth.login_required
def staffing_create_event():
    ok, error = staffing_store.create_event(request.form, auth.current_user(), _today())
    flash("Event posted." if ok else error, "ok" if ok else "error")
    day = staffing_store._parse_date(request.form.get("event_date")) if ok else None
    return _staffing_redirect(day)


@app.route("/staffing/events/<int:event_id>/delete", methods=["POST"])
@auth.login_required
def staffing_delete_event(event_id):
    ok, error = staffing_store.delete_event(event_id, auth.current_user())
    return _staffing_result(ok, error, "Event removed.")


@app.route("/staffing/events/<int:event_id>/signup", methods=["POST"])
@auth.login_required
def staffing_event_signup(event_id):
    user = auth.current_user()
    ok, error = staffing_store.sign_up(event_id, request.form, user, _today())
    target = request.form.get("sales_rep_id", "")
    adding_someone_else = target.isdigit() and int(target) != user.get("sales_rep_id")
    return _staffing_result(ok, error, "Rep added to the event." if adding_someone_else else "Signed up -- see you there.", event_id)


@app.route("/staffing/events/signup", methods=["POST"])
@auth.login_required
def staffing_signup_picked():
    """The "Staff an event" picker: event chosen from a dropdown rather
    than a day's event card. Lands on that event's day afterwards."""
    event_id = request.form.get("event_id", type=int)
    if not event_id:
        flash("Choose an event to staff.", "error")
        return _staffing_redirect()
    ok, error = staffing_store.sign_up(event_id, request.form, auth.current_user(), _today())
    flash("Signed up -- see you there." if ok else error, "ok" if ok else "error")
    day = staffing_store.get_event_date(event_id)
    if day is None:
        return _staffing_redirect()
    return redirect(url_for("staffing_page", month=day.strftime("%Y-%m"), day=day.isoformat(), event=event_id)
                    + f"#event-{event_id}")


@app.route("/staffing/events/<int:event_id>/leave", methods=["POST"])
@auth.login_required
def staffing_event_leave(event_id):
    ok, error = staffing_store.leave_event(event_id, request.form, auth.current_user(), _today())
    return _staffing_result(ok, error, "Removed from the event.", event_id)


@app.route("/staffing/shifts", methods=["POST"])
@auth.login_required
def staffing_add_shift():
    user = auth.current_user()
    ok, error = staffing_store.add_shift(request.form, user, _today())
    target = request.form.get("sales_rep_id", "")
    for_someone_else = target.isdigit() and int(target) != user.get("sales_rep_id")
    label = "Weekend shift" if request.form.get("kind") == "weekend" else "Door-knocking shift"
    flash((f"{label} booked for that rep." if for_someone_else else f"{label} added.") if ok else error,
          "ok" if ok else "error")
    day = staffing_store._parse_date(request.form.get("shift_date")) if ok else None
    return _staffing_redirect(day)


@app.route("/staffing/shifts/<int:shift_id>/delete", methods=["POST"])
@auth.login_required
def staffing_delete_shift(shift_id):
    ok, error = staffing_store.delete_shift(shift_id, auth.current_user(), _today())
    return _staffing_result(ok, error, "Shift removed.")


# ================================================================
# Tools > Install Escalation -- call Ready accounts to move them up
# ================================================================

def _serialize_escalation_mark(mark, user):
    if not mark:
        return None
    return {
        "outcome": mark["outcome"],
        "outcome_label": mark["outcome_label"],
        "note": mark["note"],
        "called_by_name": mark["called_by_name"],
        "called_time_display": mark["called_at"].astimezone(EASTERN_TZ).strftime("%-I:%M %p"),
        "call_date_iso": mark["call_date"].isoformat(),
        "call_date_display": mark["call_date"].strftime("%b %-d"),
        "can_undo": permissions.is_admin(user) or (user and mark["called_by_user_id"] == user.get("id")),
    }


@app.route("/tools/install-escalation")
@auth.login_required
def install_escalation_page():
    """Install Escalation (added 2026-10-26) -- every account whose
    PlanetWeb install job is "Ready" but booked for a later day, for reps
    to call and offer an earlier slot. Calls are marked per Eastern day
    so the same customer isn't called twice in one day. Data and rules:
    install_escalation.py."""
    user = auth.current_user()
    accounts, load_error = install_escalation.load_accounts(force=request.args.get("reload") == "1")
    accounts = [dict(a) for a in accounts]  # don't decorate the cached rows
    marks_ok, today_marks, prior_marks = install_escalation.get_calls([a["order_id"] for a in accounts])

    ensure_data_loaded()
    normalized = get_normalized_sales()
    rep_by_sale = {}
    if normalized is not None:
        rep_by_sale = dict(zip(normalized["sale_id"], normalized["sales_rep"]))

    for account in accounts:
        account["sales_rep"] = rep_by_sale.get(account["order_id"])
        account["in_dashboard"] = account["order_id"] in rep_by_sale
        account["today_mark"] = _serialize_escalation_mark(today_marks.get(account["order_id"]), user)
        account["last_mark"] = _serialize_escalation_mark(prior_marks.get(account["order_id"]), user)

    called = [a for a in accounts if a["today_mark"]]
    return render_template(
        "install_escalation.html",
        active_page="tools",
        accounts=accounts,
        load_error=load_error,
        marks_ok=marks_ok,
        summary={
            "ready": len(accounts),
            "to_call": len(accounts) - len(called),
            "called": len(called),
            "moved_up": sum(1 for a in called if a["today_mark"]["outcome"] == "moved_up"),
        },
        states=sorted({a["state"] for a in accounts if a["state"]}),
        outcomes=install_escalation.OUTCOMES,
        today=_today(),
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/tools/install-escalation/<int:order_id>/call", methods=["POST"])
@auth.login_required
def install_escalation_mark(order_id):
    """Mark (or re-mark) an account as called today -- small fetch() POST,
    the row updates in place. {"undo": true} removes today's mark."""
    payload = request.get_json(silent=True) or {}
    user = auth.current_user()
    if payload.get("undo"):
        ok, error = install_escalation.unmark_called(order_id, user)
        if not ok:
            return jsonify({"ok": False, "error": error}), 400
        return jsonify({"ok": True, "mark": None})
    ok, error, mark = install_escalation.mark_called(order_id, payload.get("outcome"), payload.get("note"), user)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "mark": _serialize_escalation_mark(mark, user)})


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

    if dataset == "main_sales":
        sources = ("planetweb",)
    elif dataset in ("service_cancellations", "vision_packages"):
        sources = ("kpi",)
    else:
        sources = ("planetweb", "kpi")
    # If a refresh is already running (the background timer, or someone
    # else's click), wait for it and use its result instead of queueing a
    # second identical one behind it -- mashing the button can't pile up
    # database work.
    if _lock.acquire(blocking=False):
        _lock.release()
        load_all_data(sources)
    else:
        with _lock:
            pass

    return redirect(request.referrer or url_for("overview"))


# ================================================================
# DOOR KNOCKERS (added 2026-10-22) -- internal dashboard over the
# External Rep Sales data (appdb external_rep_sales, synced every 5 min
# by external_sales_store.py). See knockers.py for status definitions.
# ================================================================

def _period_args():
    return (
        request.args.get("period", "this_month"),
        request.args.get("start", ""),
        request.args.get("end", ""),
    )


def _calendar_month_args():
    today = datetime.now(EASTERN_TZ)
    try:
        cal_year = int(request.args.get("cal_year", today.year))
        cal_month = int(request.args.get("cal_month", today.month))
        if not 1 <= cal_month <= 12:
            raise ValueError
    except (TypeError, ValueError):
        cal_year, cal_month = today.year, today.month
    return cal_year, cal_month


def _load_knockers_or_error():
    try:
        return knockers.load_knocker_sales(), None
    except Exception as exc:
        return None, db.sanitize_error(exc)


def _knockers_sync_display():
    status = external_sales_store.get_status()
    last = status["state"].get("last_success_at") if status["available"] else None
    return last.astimezone(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z") if last else None


# Team / per-state views for the Door Knockers rate trend chart (only the
# states knockers actually sell in).
KNOCKER_RATE_VIEWS = [("team", "Team", None), ("nj", "NJ", "NJ"), ("ny", "NY", "NY"), ("va", "VA", "VA")]


@app.route("/knockers")
@auth.login_required
def knockers_page():
    ensure_data_loaded()
    df, load_error = _load_knockers_or_error()
    period, custom_start, custom_end = _period_args()
    search = request.args.get("search", "").strip().lower()

    period_df = filter_by_period(df, period, custom_start, custom_end)

    rows = knockers.add_all_time_install_rates(knockers.knocker_rows(period_df), df)
    # Card on File column: each code's latest blitz (default) or all time
    # (dropdown added 2026-10-26, by request). It also drives that
    # column's sort.
    cof_view = request.args.get("cof", "blitz")
    if cof_view not in ("blitz", "all_time"):
        cof_view = "blitz"
    latest_blitz = knockers.latest_blitz_by_code(df)
    active = knockers.active_codes(df)
    for row in rows:
        blitz = latest_blitz.get(row["code"])
        row["active"] = row["code"] in active
        if cof_view == "all_time":
            row["card_on_file_rate"] = row["all_time_card_on_file_rate"]
            row["cof_url"] = url_for("knocker_accounts", code=row["code"], status="card_on_file", period="all_time")
            row["cof_note"] = None
        else:
            row["card_on_file_rate"] = blitz["card_on_file_rate"] if blitz else None
            row["cof_url"] = url_for(
                "knocker_accounts", code=row["code"], status="card_on_file", state=blitz["state_key"],
                period="custom", start=blitz["start"].isoformat(), end=blitz["end"].isoformat()) if blitz else None
            row["cof_note"] = (f"{blitz['town'] + ', ' if blitz['town'] else ''}{blitz['state']} · {blitz['date_label']}"
                               if blitz else "No blitzes yet")
    active_codes = len(rows)
    active_count = sum(r["active"] for r in rows)
    # "Active only" filter (2026-10-26, by request): sold in the last 24 hours.
    active_only = request.args.get("active") == "1"
    if active_only:
        rows = [r for r in rows if r["active"]]
    if search:
        rows = [r for r in rows if search in r["code"]]
    sort = request.args.get("sort", "sales")
    sort_dir = request.args.get("dir", "desc")
    if sort not in knockers.KNOCKER_SORT_KEYS:
        sort, sort_dir = "sales", "desc"
    rows = knockers.sort_knocker_rows(rows, sort, sort_dir)
    table_columns = [{
        "key": key, "label": label, "active": key == sort,
        "dir": sort_dir if key == sort else None,
        "next_dir": ("asc" if sort_dir == "desc" else "desc") if key == sort else default_dir,
    } for key, label, default_dir in knockers.KNOCKER_TABLE_COLUMNS]
    # The rates sit in their own table to the right (2026-10-26, by request).
    rate_columns = [c for c in table_columns if c["key"] in knockers.KNOCKER_RATE_COLUMN_KEYS]
    table_columns = [c for c in table_columns if c["key"] not in knockers.KNOCKER_RATE_COLUMN_KEYS]

    state_rows, state_total = knockers.state_rows(period_df)

    # Calendar + Monthly Sales trend: all-time, independent of the period
    # filter -- same convention as the Team Leaderboard.
    cal_year, cal_month = _calendar_month_args()
    trend_view = request.args.get("trend_view", "team")
    if trend_view not in DASHBOARD_STATE_VIEW_KEYS:
        trend_view = "team"
    trend_range = request.args.get("trend_range", "12m")
    if trend_range not in ("12m", "mtd", "wtd"):
        trend_range = "12m"
    state_views = {key: _state_filtered(df, code) for key, _, code in DASHBOARD_STATE_VIEWS}
    rate_views = [
        {"key": key, "label": label, "rows": knockers.monthly_rates(_state_filtered(df, code))}
        for key, label, code in KNOCKER_RATE_VIEWS
    ]
    first_sale = df["sale_date"].min() if df is not None and not df.empty else None
    first_sale_month = date(first_sale.year, first_sale.month, 1) if pd.notna(first_sale) else knockers.RATE_TREND_START

    return render_template(
        "knockers.html",
        active_page="knockers",
        load_error=load_error,
        period=period, custom_start=custom_start, custom_end=custom_end, search=search,
        today_sales=calculate_total_daily_sales(df),
        daily_averages=calculate_daily_averages(df),
        monthly_forecast=calculate_monthly_forecast(df),
        status=knockers.status_summary(period_df),
        rows=rows, active_codes=active_codes, table_columns=table_columns, rate_columns=rate_columns, cof_view=cof_view, active_only=active_only, active_count=active_count, sort=sort, sort_dir=sort_dir,
        state_rows=state_rows, state_total=state_total,
        town_rows=knockers.town_rows(period_df),
        rep_monthly_activity=calculate_rep_monthly_activity(df, None, cal_year, cal_month),
        cal_year=cal_year, cal_month=cal_month,
        dashboard_view_options=[{"key": key, "label": label} for key, label, _ in DASHBOARD_STATE_VIEWS],
        monthly_trend_views={key: calculate_monthly_sales_trend(v) for key, v in state_views.items()},
        mtd_trend_views={key: calculate_month_to_date_comparison(v) for key, v in state_views.items()},
        wtd_trend_views={key: calculate_week_to_date_comparison(v) for key, v in state_views.items()},
        rate_views=rate_views,
        # Monthly Sales by Status (2026-10-27, by request): the same monthly
        # rows as stacked bars -- Installed + Pending + Needs Attention +
        # Cancels (order + service cancels together) = that month's sales. Clicking a segment opens that month's accounts.
        monthly_status_views=[{"key": key, "label": label, "rows": [{
            **r, "name": r["label"], "cancels": r["cancelled"] + r["service_cancelled"],
            "url": url_for("knocker_accounts", period="custom", start=r["start"], end=r["end"], state=code),
        } for r in knockers.monthly_rates(_state_filtered(df, code), start=first_sale_month)]}
            for key, label, code in KNOCKER_RATE_VIEWS],
        trend_view=trend_view, trend_range=trend_range,
        synced_at=_knockers_sync_display(),
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/knockers/codes/<code>")
@auth.login_required
def knocker_profile(code):
    ensure_data_loaded()
    df, load_error = _load_knockers_or_error()
    code = code.strip().lower()
    if df is not None and code not in set(df["sales_rep"]):
        abort(404)
    period, custom_start, custom_end = _period_args()
    code_df = df[df["sales_rep"] == code] if df is not None else None
    period_df = filter_by_period(code_df, period, custom_start, custom_end)
    cal_year, cal_month = _calendar_month_args()
    state_rows, state_total = knockers.state_rows(period_df)
    _title, accounts = knockers.account_view(code_df, status="all", period=period, start=custom_start, end=custom_end)
    _with_knocker_links(accounts)
    rep_blitzes, rep_blitz_outside, rep_blitz_total = knockers.rep_blitzes(code_df)

    return render_template(
        "knocker_profile.html",
        active_page="knockers",
        load_error=load_error,
        code=code,
        active=code in knockers.active_codes(code_df),
        period=period, custom_start=custom_start, custom_end=custom_end,
        status=knockers.status_summary(period_df),
        all_time=knockers.status_summary(code_df),
        first_sale=code_df["sale_date"].min() if code_df is not None and not code_df.empty else None,
        state_rows=state_rows, state_total=state_total,
        town_rows=knockers.town_rows(period_df, limit=15),
        rep_blitzes=rep_blitzes, rep_blitz_outside=rep_blitz_outside, rep_blitz_total=rep_blitz_total,
        rep_blitz_rule=(knockers.REP_BLITZ_MIN_SELLING_DAYS, knockers.REP_BLITZ_MAX_GAP_DAYS),
        rep_monthly_activity=calculate_rep_monthly_activity(code_df, code, cal_year, cal_month),
        accounts=accounts,
        view="knockers",
        synced_at=_knockers_sync_display(),
        last_refreshed=data_store["last_refreshed"],
    )


def _blitz_accounts_url(slugs, **extra):
    """Account list for one or more blitzes (the union of their sales)."""
    return url_for("knocker_accounts", blitz=list(slugs), period="all_time", **extra)


def _blitz_row(summary, slugs):
    """A blitz (or group of blitzes) as an overview row: its summary plus
    where its name and counts link to."""
    detail = (url_for("knocker_blitz", slug=slugs[0]) if len(slugs) == 1
              else url_for("knocker_blitz_group", b=list(slugs)))
    return {**summary, "slugs": list(slugs), "detail_url": detail, "accounts_url": _blitz_accounts_url(slugs)}


def _blitz_status_row(row):
    """One row of the status stacked-bar chart; the chart appends &status=."""
    return {"name": row["name"], **row["stats"], "url": row["accounts_url"]}


@app.route("/knockers/blitzes")
@auth.login_required
def knocker_blitzes():
    ensure_data_loaded()
    df, load_error = _load_knockers_or_error()
    states = list(dict.fromkeys(b["state"] for b in knockers.BLITZES))
    state = request.args.get("state", "").upper()
    if state not in states:
        state = ""
    # Group by "blitz" (one row each) or "state" (each state's blitzes
    # combined into one row) -- added 2026-10-22, by request.
    group_by = request.args.get("group_by", "blitz")
    if group_by == "state":
        rows = [_blitz_row(g, g["slugs"]) for g in knockers.state_group_summaries(df)
                if not state or g["state"] == state]
    else:
        group_by = "blitz"
        rows = [_blitz_row(b, [b["slug"]]) for b in knockers.all_blitz_summaries(df, state=state or None)]
    return render_template(
        "knocker_blitzes.html",
        active_page="knockers",
        load_error=load_error,
        states=states,
        state=state,
        group_by=group_by,
        blitzes=rows,
        status_chart=[_blitz_status_row(r) for r in rows],
        synced_at=_knockers_sync_display(),
        last_refreshed=data_store["last_refreshed"],
    )


def _render_blitz_page(df, load_error, blitzes, summary):
    slugs = [b["slug"] for b in blitzes]
    sales = knockers.group_sales(df, blitzes)
    _title, accounts = knockers.account_view(sales, status="all", period="all_time")
    _with_knocker_links(accounts)
    members = [_blitz_row(m, [m["slug"]]) for m in summary.get("members", [])]
    combined = _blitz_row(summary, slugs)
    return render_template(
        "knocker_blitz.html",
        active_page="knockers",
        load_error=load_error,
        blitz=combined,
        members=members,
        status_chart=[_blitz_status_row(r) for r in members] + [_blitz_status_row({**combined, "name": "Combined"} if members else combined)],
        daily_drilldown=url_for("knocker_accounts", blitz=slugs, status="all"),
        knocker_rows=knockers.knocker_rows(sales),
        town_rows=knockers.town_rows(sales, limit=15),
        accounts=accounts,
        view="knockers",
        acct_args={"blitz": slugs, "period": "all_time"},
        synced_at=_knockers_sync_display(),
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/knockers/blitzes/group")
@auth.login_required
def knocker_blitz_group():
    """Any set of blitzes combined (?b=slug&b=slug...)."""
    slugs = list(dict.fromkeys(s for s in request.args.getlist("b") if s in knockers.BLITZ_BY_SLUG))
    if not slugs:
        return redirect(url_for("knocker_blitzes"))
    if len(slugs) == 1:
        return redirect(url_for("knocker_blitz", slug=slugs[0]))
    ensure_data_loaded()
    df, load_error = _load_knockers_or_error()
    blitzes = [knockers.BLITZ_BY_SLUG[s] for s in slugs]
    states = {b["state"] for b in blitzes}
    same_state_all = len(states) == 1 and len(blitzes) == sum(1 for b in knockers.BLITZES if b["state"] in states)
    name = f"All {next(iter(states))} Blitzes" if same_state_all else None
    return _render_blitz_page(df, load_error, blitzes, knockers.group_summary(df, blitzes, name=name))


@app.route("/knockers/blitzes/<slug>")
@auth.login_required
def knocker_blitz(slug):
    if slug not in knockers.BLITZ_BY_SLUG:
        abort(404)
    ensure_data_loaded()
    df, load_error = _load_knockers_or_error()
    blitz = knockers.BLITZ_BY_SLUG[slug]
    return _render_blitz_page(df, load_error, [blitz], knockers.blitz_summary(df, blitz))


def _with_knocker_links(accounts):
    for account in accounts:
        account["rep_label"] = "Knocker"
        account["rep_url"] = url_for("knocker_profile", code=account["sales_rep"])
    return accounts


@app.route("/knockers/accounts")
@auth.login_required
def knocker_accounts():
    ensure_data_loaded()
    df, load_error = _load_knockers_or_error()
    period, custom_start, custom_end = _period_args()
    code = request.args.get("code", "").strip().lower() or None
    status_key = request.args.get("status", "all")
    if status_key not in knockers.ACCOUNT_FILTERS:
        status_key = "all"
    state = request.args.get("state", "").strip().upper() or None
    municipality = request.args.get("municipality", "").strip() or None
    day = None
    if request.args.get("day"):
        try:
            day = date(int(request.args["year"]), int(request.args["month"]), int(request.args["day"]))
        except (KeyError, TypeError, ValueError):
            abort(404)
    # ?blitz=slug (repeatable): only those blitzes' sales.
    blitzes = [knockers.BLITZ_BY_SLUG[b] for b in dict.fromkeys(request.args.getlist("blitz")) if b in knockers.BLITZ_BY_SLUG]
    if blitzes:
        df = knockers.group_sales(df, blitzes)

    title, accounts = knockers.account_view(
        df, code=code, status=status_key, state=state, municipality=municipality,
        period=period, start=custom_start, end=custom_end, day=day,
    )
    _with_knocker_links(accounts)
    blitz_label = (blitzes[0]["name"] if len(blitzes) == 1 else f"{len(blitzes)} blitzes") if blitzes else None
    scope = " · ".join(p for p in [blitz_label, code, municipality, state] if p)

    return render_template(
        "knocker_accounts.html",
        active_page="knockers",
        load_error=load_error,
        view_title=title, scope=scope, accounts=accounts,
        blitz_slugs=[b["slug"] for b in blitzes],
        code=code, status_key=status_key, state=state or "", municipality=municipality or "",
        day=day, all_time=day is not None,
        status_options=[(key, label) for key, (label, _f) in knockers.ACCOUNT_FILTERS.items()],
        period=period, custom_start=custom_start, custom_end=custom_end,
        view="knockers",
        last_refreshed=data_store["last_refreshed"],
    )


@app.route("/dashboard/customers/<int:sale_id>")
@auth.login_required
def customer_profile_page(sale_id):
    """Customer Profile (added 2026-10-22) -- everything known about one
    order/customer, opened by clicking an account card in any Bulk
    Account View or a global search result. The account comes from the
    sales dataset, else the knocker dataset, else (any other order) the
    global search's order index; the deeper
    data is queried on demand by customer_profile.py. HubSpot call
    records follow the same rule as a Rep Profile's call history
    (permissions.can_view_call_history() for the owning rep)."""
    ensure_data_loaded()
    normalized = get_normalized_sales()
    matches = normalized[normalized["sale_id"] == sale_id] if normalized is not None else None
    is_knocker_order = False
    if matches is None or matches.empty:
        # Door-knocker orders placed on the website aren't in the main
        # sales data (see WEB_SALES_QUERY) -- open them from the Door
        # Knockers dataset instead.
        knocker_df, _error = _load_knockers_or_error()
        matches = knocker_df[knocker_df["sale_id"] == sale_id] if knocker_df is not None else None
        is_knocker_order = True
    if matches is not None and not matches.empty:
        account = build_bulk_account_rows(matches.head(1))[0]
        if is_knocker_order:
            _with_knocker_links([account])
        row = matches.iloc[0]
    else:
        # Any other order (e.g. before 2026) -- opened from the global
        # search, which covers every order (added 2026-10-26).
        order = order_search.find(order_search.get_index(), sale_id)
        if order is None:
            abort(404)
        known = (set(normalized["sales_rep"]) if normalized is not None else set(),
                 set(knocker_df["sales_rep"]) if knocker_df is not None else set())
        account = _order_account(order, known)
        row = {"state": order["state"], "subscriber_uuid": None}
    viewer = auth.current_user()
    can_view_calls = permissions.can_view_call_history(viewer, account["sales_rep"])

    profile = customer_profile.load_customer_profile(
        sale_id,
        state=row.get("state"),
        subscriber_uuid=row.get("subscriber_uuid"),
        phone=None,
        include_calls=can_view_calls,
    )
    if profile["order"]:
        known_ids = set(normalized["sale_id"]) if normalized is not None else set()
        for related in profile["order"]["related_orders"]:
            related["in_dashboard"] = related["id"] in known_ids

    return render_template(
        "customer_profile.html",
        active_page="dashboard",
        account=account,
        profile=profile,
        can_view_calls=can_view_calls,
        last_refreshed=data_store["last_refreshed"],
    )


def _order_account(order, known):
    """A Customer Profile `account` for an order that isn't in the sales
    or knocker datasets (e.g. pre-2026), built from its order_search row.
    `known` = (rep names in the sales data, knocker codes)."""
    reps, codes = known
    sold_by, kind = order.get("sold_by"), order.get("sold_by_kind")
    rep_url = None
    if kind == "knocker" and sold_by in codes:
        rep_url = url_for("knocker_profile", code=sold_by)
    elif sold_by in reps:
        rep_url = url_for("rep_profile", rep_name=sold_by)
    order_date = order.get("order_date")
    return {
        "sale_id": int(order["sale_id"]),
        "sales_rep": sold_by or "Unknown",
        "rep_label": "Knocker" if kind == "knocker" else None,
        "rep_url": rep_url or url_for("dashboard_page"),
        "first_name": order["first_name"],
        "last_name": order["last_name"],
        "address": order["address"] or None,
        "city_state_zip": order["city_state_zip"] or None,
        "created": order_date.strftime("%m/%d/%Y") if pd.notna(order_date) else None,
        "scheduled_date": None,
        "status": "Order",
        "sales_channel": None,
        "vision_url": None,
        "pas_url": None,
    }


@app.route("/search")
@auth.login_required
def search():
    """Global search bar (templates/_topnav.html + static/js/search.js),
    JSON response. Reps: sales reps (by name) and door knockers (by
    code). Accounts: EVERY order ever placed (2026-10-26, by request --
    the sales data only goes back to 2026 and leaves out knocker orders),
    from order_search's in-memory index; each opens its Customer Profile.
    Status comes from the sales or knocker data when the order is there."""
    query = request.args.get("q", "")
    q = query.strip().casefold()
    normalized = get_normalized_sales()
    knocker_df, _error = _load_knockers_or_error()
    rep_matches, _accounts = search_dataset(normalized, query)

    reps = [{"name": rep, "url": url_for("rep_profile", rep_name=rep), "subtitle": "Individual Sales Profile"}
            for rep in rep_matches]
    if q and knocker_df is not None and not knocker_df.empty:
        codes = sorted(c for c in knocker_df["sales_rep"].unique() if c and q in c)
        codes.sort(key=lambda c: not c.startswith(q))
        reps += [{"name": c, "url": url_for("knocker_profile", code=c), "subtitle": "Door Knocker Profile"}
                 for c in codes[:SEARCH_RESULT_LIMIT]]

    accounts = []
    hits, closest = order_search.search(order_search.get_index(), query, SEARCH_RESULT_LIMIT)
    if hits is not None and not hits.empty:
        status = {}
        for df in (knocker_df, normalized):  # sales data wins on overlap
            if df is not None and not df.empty:
                rows = df[df["sale_id"].isin(hits["sale_id"])]
                status.update(zip(rows["sale_id"], zip(rows["status"], rows["sales_rep"])))
        for order in hits.to_dict("records"):
            known_status, rep = status.get(order["sale_id"], (None, None))
            order_date = order["order_date"]
            accounts.append({
                "first_name": order["first_name"],
                "last_name": order["last_name"],
                "address": ", ".join(p for p in (order["address"], order["city_state_zip"]) if p),
                "sales_rep": rep or order["sold_by"] or "Unknown",
                "status": known_status,
                "order_date": order_date.strftime("%b %-d, %Y") if pd.notna(order_date) else None,
                "url": url_for("customer_profile_page", sale_id=order["sale_id"]),
                "external": False,
            })

    # closest: no exact account match, these are the nearest (2026-10-27).
    return jsonify({"reps": reps, "accounts": accounts, "closest_accounts": bool(accounts) and closest})


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
    normalized = get_normalized_sales()

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


@app.route("/admin/passings-data")
@auth.admin_required
def admin_passings_data():
    """Admin-only raw-data verification tool for the Passings & Leads
    feature (spec #25/#26) -- mirrors admin_marketing_form_data()'s shape:
    confirms the app can reach PlanetWeb's View_Places/FTTPFormData and
    shows exactly what it returns (raw columns, source field name next to
    this app's interpretation -- see passings_data.ADMIN_COLUMNS), plus
    the validation metrics (Rows Imported/Unique+Duplicate PlaceIDs/Valid+
    Missing Emails/Prequal as Business Count/Commercial+Industrial+Church
    Property Counts/Missing AuxVar1+AuxVar4) at the top, always computed
    over the FULL unfiltered population regardless of this page's own
    search/filter controls below -- see
    passings_data.get_validation_metrics()'s docstring."""
    search = request.args.get("search", "").strip()
    filters = {
        "city": request.args.get("city", "").strip() or None,
        "state": request.args.get("state", "").strip() or None,
        "zip": request.args.get("zip", "").strip() or None,
    }
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    result = passings_data.get_admin_page(filters, search, page)
    metrics = passings_data.get_validation_metrics()
    filter_options = passings_data.get_filter_options()

    return render_template(
        "admin/passings_data.html",
        active_page="admin_passings_data",
        columns=passings_data.ADMIN_COLUMNS,
        result=result,
        metrics=metrics,
        search=search,
        filters=filters,
        filter_options=filter_options,
        availability_labels_full=passings_classification.AVAILABILITY_LABELS_FULL,
        passings_last_refreshed=datetime.now(EASTERN_TZ).strftime("%-m/%-d/%Y %-I:%M %p %Z"),
        last_refreshed=data_store["last_refreshed"],
    )


# ================================================================
# ADMIN -- External Rep Sales
# ================================================================

def _external_sales_status_json(status):
    state = status.get("state") or {}
    run = status.get("latest_run")
    return {
        "available": status["available"],
        "connection_ok": state.get("connection_ok"),
        "last_success_at": _iso(state.get("last_success_at")),
        "last_attempt_at": _iso(state.get("last_attempt_at")),
        "last_error": state.get("last_error"),
        "in_source_total": status["in_source_total"],
        "latest_run": {**run, "started_at": _iso(run["started_at"]), "finished_at": _iso(run["finished_at"])} if run else None,
    }


@app.route("/admin/external-rep-sales")
@auth.admin_required
def admin_external_rep_sales():
    """Admin-only. Sales made under external door-to-door referral codes,
    synced from PlanetWeb into appdb by external_sales_store.py (the page
    itself never queries PlanetWeb). view=mapping shows the referral code
    -> rep/organization/manager mapping instead of the sales table."""
    view = "mapping" if request.args.get("view") == "mapping" else "sales"
    status = external_sales_store.get_status()
    context = {
        "active_page": "admin_external_rep_sales",
        "view": view,
        "status": status,
        "last_refreshed": data_store["last_refreshed"],
    }
    if view == "mapping":
        context.update(
            mappings=external_sales_store.list_rep_mappings(),
            organizations=external_sales_store.get_filter_options()["organizations"],
            users=[u for u in user_store.list_users() if u.get("status") != "disabled"],
        )
    else:
        filters = external_sales_store.parse_filters(request.args)
        today = _today()
        context.update(
            filters=filters,
            result=external_sales_store.query_sales(filters),
            filter_options=external_sales_store.get_filter_options(),
            columns=external_sales_store.COLUMNS,
            page_size_options=external_sales_store.PAGE_SIZE_OPTIONS,
            unmapped_org=external_sales_store.UNMAPPED_ORG,
            date_presets=[
                ("Month to Date", today.replace(day=1), today),
                ("Last 30 Days", today - timedelta(days=29), today),
                ("Year to Date", today.replace(month=1, day=1), today),
            ],
        )
    return render_template("admin/external_rep_sales.html", **context)


@app.route("/admin/external-rep-sales/export.csv")
@auth.admin_required
def admin_external_rep_sales_export():
    """Every row matching the page's current filters (not just the visible
    page). Contains customer PII -- admin-only, never cached, and logged."""
    filters = external_sales_store.parse_filters(request.args)
    try:
        chunks = external_sales_store.iter_export_csv(filters)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Export failed: {db.sanitize_error(exc)}"}), 503
    user = auth.current_user()
    applied = {k: str(v) for k, v in filters.items() if v and k not in ("page", "per_page", "sort", "dir")}
    print(f"[external-sales] CSV export by {user['email']} (filters: {applied or 'none'})")
    filename = f"external_rep_sales_{_today().isoformat()}.csv"
    return Response(
        stream_with_context(chunks),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/admin/external-rep-sales/status")
@auth.admin_required
def admin_external_rep_sales_status():
    return jsonify({"ok": True, **_external_sales_status_json(external_sales_store.get_status())})


@app.route("/admin/external-rep-sales/sync", methods=["POST"])
@auth.admin_required
def admin_external_rep_sales_sync():
    started, error = external_sales_store.start_sync_in_background("manual")
    if not started:
        return jsonify({"ok": False, "error": error}), 409
    print(f"[external-sales] Manual refresh requested by {auth.current_user()['email']}")
    return jsonify({"ok": True})


@app.route("/admin/external-rep-sales/reps/<int:rep_id>", methods=["POST"])
@auth.admin_required
def admin_external_rep_update(rep_id):
    ok, error = external_sales_store.update_rep_mapping(rep_id, request.form, auth.current_user()["id"])
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


# ================================================================
# ADMIN -- HubSpot Integration
# ================================================================

@app.route("/admin/hubspot")
@auth.admin_required
def admin_hubspot():
    return render_template(
        "admin/hubspot.html",
        active_page="admin_hubspot",
        status=hubspot_store.get_integration_status(),
        mappings=hubspot_store.list_mappings(),
        sales_reps=user_store.list_sales_reps(),
        key_hint=_hubspot_key_hint(),
        key_source=hubspot_client.key_source(),
        sync_start_display=hubspot_store.SYNC_START_DATE.strftime("%b %-d, %Y"),
        saved_key=app_secrets.get_secret(hubspot_client.SECRET_NAME),
        last_refreshed=data_store["last_refreshed"],
    )


def _hubspot_key_hint():
    """Last 4 characters only -- enough to tell which key is loaded,
    never enough to use it."""
    key = hubspot_client.get_service_key()
    return f"…{key[-4:]}" if len(key) >= 12 else ("set" if key else None)


def _status_json(status):
    def run_json(run):
        if not run:
            return None
        return {**run, "started_at": _iso(run["started_at"]), "finished_at": _iso(run["finished_at"])}
    state = status.get("state") or {}
    return {
        "available": status["available"],
        "configured": status["configured"],
        "connection_ok": state.get("connection_ok"),
        "last_success_at": _iso(state.get("last_success_at")),
        "last_attempt_at": _iso(state.get("last_attempt_at")),
        "last_error": state.get("last_error"),
        "initial_sync_completed_at": _iso(state.get("initial_sync_completed_at")),
        "owners_total": status["owners_total"],
        "owners_mapped": status["owners_mapped"],
        "owners_unmatched": status["owners_unmatched"],
        "outbound_calls_total": status["outbound_calls_total"],
        "calls_total": status["calls_total"],
        "latest_run": run_json(status["latest_run"]),
    }


@app.route("/admin/hubspot/key", methods=["POST"])
@auth.admin_required
def admin_hubspot_save_key():
    """Admin-entered service key: checked against HubSpot, then stored
    encrypted (app_secrets.py). Never echoed back in any response."""
    candidate = (request.form.get("service_key") or "").strip()
    if not candidate or len(candidate) < 10 or len(candidate) > 512 or any(ch.isspace() for ch in candidate):
        return jsonify({"ok": False, "error": "That doesn't look like a HubSpot service key."}), 400
    usable, message = hubspot_client.validate_key(candidate)
    if not usable:
        return jsonify({"ok": False, "error": f"HubSpot rejected this key -- not saved. {message}"}), 400
    ok, error = app_secrets.set_secret(hubspot_client.SECRET_NAME, candidate, auth.current_user()["id"])
    if not ok:
        return jsonify({"ok": False, "error": f"Could not save the key: {error}"}), 500
    print(f"[hubspot] Service key updated in Admin Portal by {auth.current_user()['email']}")
    hubspot_store.start_scheduler()
    started, _ = hubspot_store.start_sync_in_background("manual")
    return jsonify({"ok": True, "warning": message, "sync_started": started})


@app.route("/admin/hubspot/key/delete", methods=["POST"])
@auth.admin_required
def admin_hubspot_delete_key():
    ok, error = app_secrets.delete_secret(hubspot_client.SECRET_NAME)
    if not ok:
        return jsonify({"ok": False, "error": error}), 500
    print(f"[hubspot] Saved service key removed in Admin Portal by {auth.current_user()['email']}")
    return jsonify({"ok": True})


@app.route("/admin/hubspot/status")
@auth.admin_required
def admin_hubspot_status():
    return jsonify({"ok": True, **_status_json(hubspot_store.get_integration_status())})


@app.route("/admin/hubspot/sync", methods=["POST"])
@auth.admin_required
def admin_hubspot_sync():
    started, error = hubspot_store.start_sync_in_background("manual")
    if not started:
        return jsonify({"ok": False, "error": error}), 409
    return jsonify({"ok": True})


@app.route("/admin/hubspot/test", methods=["POST"])
@auth.admin_required
def admin_hubspot_test():
    return jsonify(hubspot_store.test_connection())


@app.route("/admin/hubspot/mappings/refresh", methods=["POST"])
@auth.admin_required
def admin_hubspot_refresh_mappings():
    ok, error = hubspot_store.rematch_existing_owners()
    if not ok:
        return jsonify({"ok": False, "error": error}), 500
    return jsonify({"ok": True})


@app.route("/admin/hubspot/mappings/<owner_id>", methods=["POST"])
@auth.admin_required
def admin_hubspot_update_mapping(owner_id):
    if not owner_id.isdigit():
        abort(404)
    action = request.form.get("action", "")
    sales_rep_id = None
    if action == "assign":
        try:
            sales_rep_id = int(request.form.get("sales_rep_id", ""))
        except ValueError:
            return jsonify({"ok": False, "error": "Choose a Sales Rep."}), 400
    ok, error = hubspot_store.set_manual_mapping(owner_id, sales_rep_id, action)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


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
# in production, or by `python app.py` locally -- doing the first
# (synchronous) load and starting the background refresh thread. This closes a
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
