"""Door Knockers dashboard data (added 2026-10-22, by request) -- an
internal, Planet-staff-only view of the door-to-door sales already synced
into appdb's external_rep_sales by external_sales_store.py.

Knockers are identified by REFERRAL CODE only (by request -- no names,
even where PlanetWeb or Admin -> External Rep Sales has one). A code is
not necessarily one person (mastle / mastle2), so everything here is
per code.

The orders are reshaped into the same columns as the main normalized
dataset (sales_metrics.build_sales_dataset()), so the existing calendar,
trend, forecast, period-filter and Bulk Account View code runs on them
unchanged. Account status mirrors the main dashboard's definitions:

- Installed: has a Vision install date (install_date, first invoice).
- Cancelled: order cancelled (order_cancellation_date) and never installed.
- Pending: not installed/cancelled, install appointment today or later.
- Needs Attention: everything else -- no appointment, or a past one.
- Service Cancelled: has a service cancellation date (customer left
  after install). Takes precedence over every other status, so the five
  statuses never overlap and always add up to Sales (changed 2026-10-23,
  by request -- these used to also count as Installed).

Card on File (added 2026-10-22): the order's payment method at the time
it was placed (FTTPFormData.Cc_PayBy, synced as payment_method) is "card"
(card last 4/expiry/name captured) or "ach" (bank account captured) --
ACH counts, by request. Not on file: "check" (invoiced, nothing
captured) and "skip".
"""

import threading
import time
from datetime import date, timedelta

import pandas as pd

import db
from sales_metrics import _today, build_bulk_account_rows, filter_by_period

CARD_ON_FILE_METHODS = {"card", "ach"}

CACHE_SECONDS = 60
_cache = {"df": None, "loaded_at": 0.0, "for_date": None}
_cache_lock = threading.Lock()

STATUS_KEYS = {
    "installed": "Installed",
    "pending": "Pending",
    "needs_attention": "Needs Attention",
    "cancelled": "Cancelled",
    "service_cancelled": "Service Cancelled",
}
# Account-list filters: the five statuses plus everything and card on file.
ACCOUNT_FILTERS = {
    "all": ("All Sales", lambda df: df),
    **{key: (label, (lambda label: lambda df: df[df["status"] == label])(label)) for key, label in STATUS_KEYS.items()},
    "card_on_file": ("Card on File", lambda df: df[df["card_on_file"]]),
    "no_card_on_file": ("No Card on File", lambda df: df[~df["card_on_file"]]),
}

KNOCKER_TABLE_COLUMNS = [
    ("code", "Referral Code", "asc"),
    ("sales", "Sales", "desc"),
    ("installed", "Installs", "desc"),
    ("all_time_install_rate", "All Time Install Rate", "desc"),
    ("card_on_file_rate", "Card on File", "desc"),
    ("pending", "Pending", "desc"),
    ("needs_attention", "Needs Attention", "desc"),
    ("cancelled", "Cancelled", "desc"),
    ("service_cancelled", "Service Cancels", "desc"),
]
KNOCKER_SORT_KEYS = {key for key, _, _ in KNOCKER_TABLE_COLUMNS}

# The equation behind every Door Knocker metric, shown in a hover box
# (added 2026-10-23, by request). Templates use it as the `formulas`
# global: data-formula="{{ formulas.install_rate }}".
FORMULAS = {
    "sales": "Sales = every door-knocker order, by sale date\n= Installed + Pending + Needs Attention + Cancelled + Service Cancelled",
    "installed": "Installed = sales with a Vision install date and no service cancellation",
    "install_rate": "Install Rate = Installed ÷ (Sales − Pending)",
    "all_time_install_rate": "All Time Install Rate = Installed ÷ (Sales − Pending), counting every sale this knocker has ever made (ignores the period filter)",
    "rep_all_time_install_rate": "All Time Install Rate = 1 − (Needs Attention ÷ Sales), counting every sale this rep has ever made (ignores the period filter)",
    "pending": "Pending = not installed or cancelled, install appointment today or later",
    "needs_attention": "Needs Attention = not installed or cancelled, no install appointment or a past one",
    "cancelled": "Cancelled = order cancelled before it was installed",
    "service_cancelled": "Service Cancelled = installed, then the customer cancelled service",
    "service_cancel_rate": "Service Cancel Rate = Service Cancelled ÷ (Installed + Service Cancelled)",
    "card_on_file": "Card on File = sales paid by card or ACH (not check or skip)",
    "card_on_file_rate": "Card on File % = Card on File ÷ Sales",
    "order_cancel_rate": "Order Cancel Rate = Cancelled ÷ Sales",
    "needs_attention_rate": "Needs Attention Rate = Needs Attention ÷ Sales",
    "pct_of_sales": "% of Sales = this status ÷ Sales",
    "per_day": "Per Day = Sales ÷ selling days (days with at least one sale)",
    "knockers": "Knockers = distinct referral codes with at least one sale",
    "monthly_sales": "Monthly Sales = sales made this calendar month, through today",
    "daily_sales": "Daily Sales = sales made today (Eastern time)",
    "avg_7_day": "7 Day Average = sales in the last 7 days (incl. today) ÷ 7",
    "avg_30_day": "30 Day Average = sales in the last 30 days (incl. today) ÷ 30",
    "monthly_projection": "Monthly Projection = Monthly Sales ÷ days elapsed × days in the month",
}

_QUERY = """
SELECT order_id, referral_code, sale_date, first_name, last_name, address, apartment_suite,
       city, state, zipcode, municipality, subscriber_uuid, scheduled_status, installation_date,
       install_date, order_cancellation_date, service_cancellation_date, package_speed, voice,
       payment_method, place_id
FROM external_rep_sales
WHERE in_source
"""


def normalize(raw):
    """external_rep_sales rows -> the main normalized dataset's shape."""
    df = raw.copy()
    installed = df["install_date"].notna()
    cancelled = df["order_cancellation_date"].notna() & ~installed
    scheduled = pd.to_datetime(df["installation_date"], errors="coerce")
    today = pd.Timestamp(_today())
    pending = ~installed & ~cancelled & scheduled.notna() & (scheduled.dt.normalize() >= today)

    status = pd.Series("Needs Attention", index=df.index)
    status[pending] = "Pending"
    status[cancelled] = "Cancelled"
    status[installed] = "Installed"
    service_cancelled = df["service_cancellation_date"].notna()
    status[service_cancelled] = "Service Cancelled"
    payment_method = df["payment_method"].fillna("").astype(str).str.strip().str.lower()

    return pd.DataFrame({
        "sale_id": df["order_id"].astype("int64"),
        "sale_date": pd.to_datetime(df["sale_date"], errors="coerce"),
        "sale_datetime": pd.NaT,
        "sales_rep": df["referral_code"].fillna("").str.strip().str.lower(),
        "sales_channel": "Door to Door",
        "subscriber_uuid": df["subscriber_uuid"],
        "scheduled": df["scheduled_status"],
        "scheduled_date": df["installation_date"],
        "install_date": pd.to_datetime(df["install_date"], errors="coerce"),
        "installed": installed.values,
        "cancelled": cancelled.values,
        "service_cancelled": service_cancelled.values,
        "payment_method": payment_method.values,
        "card_on_file": payment_method.isin(CARD_ON_FILE_METHODS).values,
        "status": status.values,
        "first_name": df["first_name"],
        "last_name": df["last_name"],
        "address": df["address"],
        "place_id": df["place_id"] if "place_id" in df.columns else None,
        "apartment_suite": df["apartment_suite"],
        "city": df["city"],
        "state": df["state"].fillna("").astype(str).str.strip().str.upper(),
        "zipcode": df["zipcode"],
        "municipality": df["municipality"].fillna("").astype(str).str.strip(),
        "package_speed": df["package_speed"],
        "voice": df["voice"],
    })


def load_knocker_sales():
    """The normalized knocker dataset, cached CACHE_SECONDS (appdb is only
    refreshed by the 5-minute External Rep Sales sync anyway). Raises on
    an appdb failure the first time; afterwards a failed reload keeps
    serving the last good copy."""
    now = time.monotonic()
    with _cache_lock:
        fresh = (_cache["df"] is not None and now - _cache["loaded_at"] < CACHE_SECONDS
                 and _cache["for_date"] == _today())
        if fresh:
            return _cache["df"].copy()
        conn = None
        try:
            conn = db.get_appdb_connection()
            with conn.cursor() as cur:
                cur.execute(_QUERY)
                columns = [c[0] for c in cur.description]
                raw = pd.DataFrame(cur.fetchall(), columns=columns)
        except Exception:
            if _cache["df"] is not None:
                return _cache["df"].copy()
            raise
        finally:
            if conn is not None:
                conn.close()
        _cache.update(df=normalize(raw), loaded_at=now, for_date=_today())
        return _cache["df"].copy()


def _rate(n, d):
    return n / d * 100 if d else None


def _summarize(group):
    sales = len(group)
    installed = int((group["status"] == "Installed").sum())
    service_cancelled = int((group["status"] == "Service Cancelled").sum())
    card_on_file = int(group["card_on_file"].sum())
    pending = int((group["status"] == "Pending").sum())
    return {
        "sales": sales,
        "installed": installed,
        # Installed / (Sales - Pending): orders still waiting on their
        # install date don't count against the rate (user request 2026-10-23).
        "install_rate": _rate(installed, sales - pending),
        "pending": pending,
        "needs_attention": int((group["status"] == "Needs Attention").sum()),
        "cancelled": int((group["status"] == "Cancelled").sum()),
        "service_cancelled": service_cancelled,
        # Everyone who got installed, including those who later left --
        # the base for the service cancel rate.
        "ever_installed": installed + service_cancelled,
        "card_on_file": card_on_file,
        "card_on_file_rate": _rate(card_on_file, sales),
    }


def status_summary(df):
    if df is None or df.empty:
        return _summarize(pd.DataFrame(columns=["installed", "status", "service_cancelled", "card_on_file"]))
    return _summarize(df)


def knocker_rows(df):
    """One leaderboard row per referral code, ranked by sales then installs."""
    if df is None or df.empty:
        return []
    rows = [{"code": code, **_summarize(group)} for code, group in df.groupby("sales_rep")]
    rows.sort(key=lambda r: (-r["sales"], -r["installed"], r["code"]))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def add_all_time_install_rates(rows, all_time_df):
    """Sets each row's all_time_install_rate from that code's whole
    history, whatever period the rows themselves cover."""
    all_time = {r["code"]: r["install_rate"] for r in knocker_rows(all_time_df)}
    for row in rows:
        row["all_time_install_rate"] = all_time.get(row["code"])
    return rows


def sort_knocker_rows(rows, key, direction):
    if key not in KNOCKER_SORT_KEYS:
        return rows
    reverse = direction == "desc"
    with_value = [r for r in rows if r.get(key) is not None]
    without = [r for r in rows if r.get(key) is None]
    return sorted(with_value, key=lambda r: r[key], reverse=reverse) + without


def state_rows(df):
    """Per-state summary rows (NJ/NY/PA/VA order, then any other), plus Total."""
    if df is None or df.empty:
        return [], None
    order = ["NJ", "NY", "PA", "VA"]
    states = sorted(df["state"].unique(), key=lambda s: (order.index(s) if s in order else 99, s))
    rows = [{"state": s or "Unknown", "state_key": s, **_summarize(df[df["state"] == s])} for s in states]
    return rows, {"state": "Total", **_summarize(df)}


def town_rows(df, limit=30):
    """Top municipalities by sales (municipality + state)."""
    if df is None or df.empty:
        return []
    named = df[df["municipality"] != ""]
    rows = [
        {"municipality": town, "state": state, **_summarize(group)}
        for (town, state), group in named.groupby(["municipality", "state"])
    ]
    rows.sort(key=lambda r: (-r["sales"], r["municipality"]))
    return rows[:limit]


def account_view(df, code=None, status="all", state=None, municipality=None,
                 period="this_month", start=None, end=None, day=None):
    """(title, accounts) for a Knockers account list. `day` (a date) scopes
    to one sale date instead of the period (calendar drill-down)."""
    label, row_filter = ACCOUNT_FILTERS.get(status, ACCOUNT_FILTERS["all"])
    if df is None or df.empty:
        return label, []
    scoped = df
    if code:
        scoped = scoped[scoped["sales_rep"] == code]
    if state:
        scoped = scoped[scoped["state"] == state]
    if municipality:
        scoped = scoped[scoped["municipality"] == municipality]
    if day is not None:
        scoped = scoped[scoped["sale_date"].dt.date == day]
        label = f"{day.strftime('%B %-d, %Y')} — {label}"
    else:
        scoped = filter_by_period(scoped, period, start, end)
    if scoped is None or scoped.empty:
        return label, []
    filtered = row_filter(scoped)
    if filtered.empty:
        return label, []
    filtered = filtered.assign(
        _scheduled_sort=pd.to_datetime(filtered["scheduled_date"], errors="coerce")
    ).sort_values(["_scheduled_sort", "sale_date"], na_position="last")
    return label, build_bulk_account_rows(filtered)


# ------------------------------------------------- Team rate trends
# Cancellation & Needs Attention rates (added 2026-10-22, by request --
# team-level, not per knocker), by the month the sale was MADE:
#   Order Cancel Rate    = Cancelled (cancelled before install) / Sales
#   Needs Attention Rate = Needs Attention (as of today)        / Sales
#   Service Cancel Rate  = Service cancellations / (Installed + Service cancellations)
# Order cancellations are only tracked reliably from 2026-03-24 (see
# external_sales_store / EXTERNAL_ORDER_CANCELLATIONS_QUERY), so months
# before April 2026 get no order-cancel rate. Recent months are flagged
# `maturing`: their orders haven't had time to install or cancel yet.
RATE_TREND_START = date(2026, 3, 1)
ORDER_CANCEL_RELIABLE_FROM = date(2026, 4, 1)
MATURING_DAYS = 45
# A month with fewer sales (or installs, for the service rate) than this
# gets no rate -- 1-3 sale months in NY/VA would otherwise plot as 0%/100%.
MIN_RATE_BASE = 10


def _rate_if_enough(n, base):
    return _rate(n, base) if base >= MIN_RATE_BASE else None


def monthly_rates(df, today=None):
    today = today or _today()
    months = []
    y, m = RATE_TREND_START.year, RATE_TREND_START.month
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    groups = {}
    if df is not None and not df.empty:
        valid = df.dropna(subset=["sale_date"])
        for (yy, mm), group in valid.groupby([valid["sale_date"].dt.year, valid["sale_date"].dt.month]):
            groups[(int(yy), int(mm))] = group
    rows = []
    for y, m in months:
        g = groups.get((y, m))
        stats = _summarize(g) if g is not None else _summarize(pd.DataFrame(columns=["installed", "status", "service_cancelled", "card_on_file"]))
        first = date(y, m, 1)
        next_first = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        rows.append({
            "label": first.strftime("%b %Y"),
            "short": first.strftime("%b"),
            **stats,
            "order_cancel_rate": _rate_if_enough(stats["cancelled"], stats["sales"]) if first >= ORDER_CANCEL_RELIABLE_FROM else None,
            "needs_attention_rate": _rate_if_enough(stats["needs_attention"], stats["sales"]),
            "service_cancel_rate": _rate_if_enough(stats["service_cancelled"], stats["ever_installed"]),
            "maturing": (today - next_first).days < MATURING_DAYS,
        })
    return rows


# ---------------------------------------------------------------- Blitzes
# A blitz (added 2026-10-22, by request) is a short, concentrated push of
# door knocking in one state -- the NY and VA sales spikes. Every
# door-knocker sale in that STATE within the window is attributed to the
# blitz. Windows were read off the daily sales: each is a run of
# consecutive selling days well above that state's baseline. NY and VA
# have ~no knocker sales between blitzes; NJ is knocked year-round
# (~24 sales/day typical), so its blitzes are the surges well above that
# -- and its windows include that day's normal NJ knocking too.
# end=None = still running (through today). To add the next blitz,
# append an entry. Display order: state, then date.
BLITZES = [
    {"slug": "nj-butler-launch-2026", "name": "Butler Launch Blitz", "state": "NJ",
     "start": date(2026, 3, 10), "end": date(2026, 3, 14)},
    {"slug": "nj-wantage-vernon-may-2026", "name": "Wantage–Vernon May Blitz", "state": "NJ",
     "start": date(2026, 5, 18), "end": date(2026, 5, 30)},
    {"slug": "nj-jefferson-june-2026", "name": "Jefferson June Blitz", "state": "NJ",
     "start": date(2026, 6, 22), "end": date(2026, 6, 27)},
    {"slug": "nj-jefferson-august-2026", "name": "Jefferson August Blitz", "state": "NJ",
     "start": date(2026, 8, 17), "end": date(2026, 8, 28)},
    {"slug": "nj-fall-all-hands-2026", "name": "Fall All-Hands Blitz", "state": "NJ",
     "start": date(2026, 9, 21), "end": date(2026, 10, 3)},
    {"slug": "ny-new-paltz-spring-2026", "name": "New Paltz Spring Blitz", "state": "NY",
     "start": date(2026, 4, 6), "end": date(2026, 4, 26)},
    {"slug": "ny-new-paltz-summer-2026", "name": "New Paltz Summer Blitz", "state": "NY",
     "start": date(2026, 7, 13), "end": date(2026, 7, 19)},
    {"slug": "ny-saugerties-fall-2026", "name": "Saugerties Fall Blitz", "state": "NY",
     "start": date(2026, 10, 2), "end": date(2026, 10, 12)},
    {"slug": "va-farmville-july-2026", "name": "Farmville July Blitz", "state": "VA",
     "start": date(2026, 7, 21), "end": date(2026, 7, 26)},
    {"slug": "va-farmville-prospect-september-2026", "name": "Farmville–Prospect September Blitz", "state": "VA",
     "start": date(2026, 9, 7), "end": date(2026, 9, 20)},
    {"slug": "va-farmville-october-2026", "name": "Farmville October Blitz", "state": "VA",
     "start": date(2026, 10, 15), "end": None},
]
BLITZ_BY_SLUG = {b["slug"]: b for b in BLITZES}


def blitz_window(blitz, today=None):
    """(start, end) dates, end capped at today for a running blitz."""
    today = today or _today()
    return blitz["start"], min(blitz["end"] or today, today)


def blitz_sales(df, blitz):
    if df is None or df.empty:
        return df
    start, end = blitz_window(blitz)
    dates = df["sale_date"].dt.date
    return df[(df["state"] == blitz["state"]) & (dates >= start) & (dates <= end)]


def blitz_summary(df, blitz):
    """Everything the blitz pages show for one blitz."""
    start, end = blitz_window(blitz)
    sales = blitz_sales(df, blitz)
    empty = sales is None or sales.empty
    stats = status_summary(sales)
    counts = {} if empty else sales["sale_date"].dt.date.value_counts().to_dict()
    span = (end - start).days + 1
    daily = [
        {"date": (start + timedelta(days=i)).isoformat(),
         "label": (start + timedelta(days=i)).strftime("%a %-m/%-d"),
         "count": int(counts.get(start + timedelta(days=i), 0))}
        for i in range(span)
    ]
    selling_days = sum(1 for d in daily if d["count"])
    cities = [] if empty else [
        {"city": city, "sales": int(n)}
        for city, n in sales["city"].fillna("Unknown").str.strip().str.title().value_counts().head(6).items()
    ]
    return {
        **blitz,
        "start": start,
        "end": end,
        "ongoing": blitz["end"] is None,
        "date_label": f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}" + (" (ongoing)" if blitz["end"] is None else ""),
        "stats": stats,
        "knockers": 0 if empty else int(sales["sales_rep"].nunique()),
        "selling_days": selling_days,
        "per_day": stats["sales"] / selling_days if selling_days else None,
        "best_day": max(daily, key=lambda d: d["count"]) if daily else None,
        "daily": daily,
        "cities": cities,
        "top_city": cities[0]["city"] if cities else None,
    }


def all_blitz_summaries(df, state=None):
    return [blitz_summary(df, b) for b in BLITZES if not state or b["state"] == state]


# Grouping (added 2026-10-22, by request): any set of blitzes combined
# into one view -- e.g. "all NY blitzes" or a hand-picked comparison.
# A group's sales are the union of its blitzes' sales.

def group_sales(df, blitzes):
    if df is None or df.empty or not blitzes:
        return df.iloc[0:0] if df is not None else None
    return pd.concat([blitz_sales(df, b) for b in blitzes]).drop_duplicates("sale_id")


def group_summary(df, blitzes, name=None):
    """Same shape as blitz_summary(), for several blitzes at once. The
    daily series is each blitz's days back to back (gaps between blitzes
    are left out)."""
    members = [blitz_summary(df, b) for b in blitzes]
    sales = group_sales(df, blitzes)
    empty = sales is None or sales.empty
    stats = status_summary(sales)
    daily = [d for m in members for d in m["daily"]]
    selling_days = sum(m["selling_days"] for m in members)
    states = sorted({b["state"] for b in blitzes})
    start, end = min(m["start"] for m in members), max(m["end"] for m in members)
    cities = [] if empty else [
        {"city": city, "sales": int(n)}
        for city, n in sales["city"].fillna("Unknown").str.strip().str.title().value_counts().head(6).items()
    ]
    return {
        "slug": None,
        "name": name or f"{len(members)} Blitzes Combined",
        "state": ", ".join(states),
        "start": start,
        "end": end,
        "ongoing": any(m["ongoing"] for m in members),
        "date_label": f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')} · {len(members)} blitzes",
        "stats": stats,
        "knockers": 0 if empty else int(sales["sales_rep"].nunique()),
        "selling_days": selling_days,
        "per_day": stats["sales"] / selling_days if selling_days else None,
        "best_day": max(daily, key=lambda d: d["count"]) if daily else None,
        "daily": daily,
        "cities": cities,
        "top_city": cities[0]["city"] if cities else None,
        "members": members,
    }


def state_group_summaries(df):
    """One combined summary per state, in BLITZES' state order."""
    states = list(dict.fromkeys(b["state"] for b in BLITZES))
    return [
        {**group_summary(df, [b for b in BLITZES if b["state"] == st], name=f"All {st} Blitzes"),
         "slugs": [b["slug"] for b in BLITZES if b["state"] == st]}
        for st in states
    ]
