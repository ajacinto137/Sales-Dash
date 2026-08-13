from datetime import datetime
import os
import threading

import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, url_for

import db
from queries import (
    MAIN_SALES_QUERY,
    SERVICE_CANCELLATIONS_QUERY,
    VISION_PACKAGES_QUERY,
)
from sales_metrics import (
    build_sales_dataset,
    calculate_channel_breakdown,
    calculate_records,
    calculate_rep_metrics,
    calculate_team_metrics,
    filter_by_period,
)

app = Flask(__name__)
app.jinja_env.globals["zip"] = zip

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
    data_store["last_refreshed"] = datetime.now().strftime("%-m/%-d/%Y %-I:%M %p")


def load_all_data():
    with _lock:
        _load_planetweb()
        _load_kpi()
        _apply_install_date()
        _touch_last_refreshed()


def ensure_data_loaded():
    if data_store["last_refreshed"] is None:
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

    team_metrics = calculate_team_metrics(period_df)
    rep_rows = calculate_rep_metrics(period_df)
    total_rep_count = len(rep_rows)

    if search:
        rep_rows = [r for r in rep_rows if search.lower() in r["sales_rep"].lower()]

    if not selected_rep and rep_rows:
        selected_rep = rep_rows[0]["sales_rep"]

    channel_breakdown = calculate_channel_breakdown(period_df, selected_rep)
    # Records are always ALL TIME, regardless of the selected dashboard period.
    records = calculate_records(normalized)

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        period=period,
        custom_start=custom_start,
        custom_end=custom_end,
        search=search,
        selected_rep=selected_rep,
        total_rep_count=total_rep_count,
        team_metrics=team_metrics,
        rep_rows=rep_rows,
        channel_breakdown=channel_breakdown,
        records=records,
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
