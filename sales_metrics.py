"""Centralized business logic for the /dashboard Team Leaderboard page.

Everything the dashboard displays is derived from one normalized DataFrame
(build_sales_dataset) that holds exactly one row per Main Sales record. All
status/rate/grouping rules live here so they are easy to audit and change
in one place instead of being scattered across routes and templates.
"""

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

    if period == "this_month":
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
            "chargebacks": None,
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
        "chargebacks": None,
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
        denom = installs + cancels
        rows.append({
            "sales_rep": rep,
            "sales": len(group),
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


def calculate_channel_breakdown(df, rep):
    channels = list(SALES_CHANNELS.values())
    if df is None or df.empty or not rep:
        return [{"channel": c, "count": 0, "pct": 0.0} for c in channels]

    rep_df = df[df["sales_rep"] == rep]
    total = len(rep_df)
    counts = rep_df["sales_channel"].value_counts().to_dict()

    return [
        {
            "channel": c,
            "count": counts.get(c, 0),
            "pct": (counts.get(c, 0) / total * 100) if total else 0.0,
        }
        for c in channels
    ]


def calculate_records(df):
    """Always operates on the full ALL-TIME normalized dataset — callers
    must not pass a period-filtered DataFrame here."""
    empty = {"best_days": [], "best_weeks": [], "best_months": []}
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

    return {
        "best_days": top3("day", lambda d: d.strftime("%b %-d, %Y")),
        "best_weeks": top3("week_start", lambda d: "Week of " + d.strftime("%b %-d")),
        "best_months": top3("month_period", lambda p: p.strftime("%B %Y")),
    }
