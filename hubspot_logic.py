"""Pure functions for the HubSpot Outbound Call Analytics feature -- owner
matching, call-property parsing, date-range resolution, time-bucket
filling and duration formatting. No I/O, no Flask, no database, so
tests/test_hubspot_logic.py covers them directly (same convention as
permissions.py)."""

import os
import re
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- matching

METHOD_EMAIL_EXACT = "email_exact"
METHOD_EMAIL_NORMALIZED = "email_normalized"
METHOD_NAME = "name"
METHOD_MANUAL = "manual"
METHOD_NONE = "none"

STATUS_MATCHED = "matched"
STATUS_UNMATCHED = "unmatched"
STATUS_AMBIGUOUS = "ambiguous"


def normalize_email(email):
    return (email or "").strip().lower()


def normalize_name(name):
    """'  José  O'Brien-Smith ' -> 'jose obriensmith'. Accents folded,
    punctuation dropped, whitespace collapsed, case-insensitive."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(text.split())


def owner_full_name(owner):
    return " ".join(p for p in ((owner.get("firstName") or "").strip(), (owner.get("lastName") or "").strip()) if p)


def match_owners(owners, users, reps):
    """Automatic HubSpot owner -> Dashboard mapping.

    owners: [{id, email, firstName, lastName}, ...] (HubSpot owners)
    users:  [{id, email, sales_rep_id}, ...]       (Dashboard users)
    reps:   [{id, name}, ...]                       (Dashboard Sales Reps -- what a profile is)

    Priority per owner: 1. exact email -> user, 2. case-insensitive
    normalized email -> user, 3. normalized first+last name -> exactly one
    Sales Rep. An email-matched user that is linked to a Sales Rep gives
    that rep directly; one with no rep link still falls through to the
    name rule for the rep (keeping the user id). Name matching never
    guesses: a name equal to 2+ reps, or a rep name claimed by 2+ owners,
    stays unmatched as 'ambiguous'.

    Returns {owner_id: {dashboard_user_id, sales_rep_id, method, status}}."""
    users_by_exact = {}
    users_by_norm = {}
    for user in users:
        if user.get("email"):
            users_by_exact[user["email"]] = user
            users_by_norm.setdefault(normalize_email(user["email"]), []).append(user)

    reps_by_name = {}
    for rep in reps:
        key = normalize_name(rep.get("name"))
        if key:
            reps_by_name.setdefault(key, []).append(rep)

    owner_name_counts = {}
    for owner in owners:
        key = normalize_name(owner_full_name(owner))
        if key:
            owner_name_counts[key] = owner_name_counts.get(key, 0) + 1

    results = {}
    email_methods = {}
    for owner in owners:
        owner_id = str(owner["id"])
        email = owner.get("email") or ""
        user, method = None, METHOD_NONE
        if email and email in users_by_exact:
            user, method = users_by_exact[email], METHOD_EMAIL_EXACT
        else:
            candidates = users_by_norm.get(normalize_email(email), []) if email else []
            if len(candidates) == 1:
                user, method = candidates[0], METHOD_EMAIL_NORMALIZED

        result = {
            "dashboard_user_id": user["id"] if user else None,
            "sales_rep_id": user.get("sales_rep_id") if user else None,
            "method": method,
            "status": STATUS_MATCHED if user and user.get("sales_rep_id") else STATUS_UNMATCHED,
        }

        email_methods[owner_id] = method
        if result["sales_rep_id"] is None:
            name_key = normalize_name(owner_full_name(owner))
            matches = reps_by_name.get(name_key, []) if name_key else []
            if len(matches) == 1 and owner_name_counts.get(name_key, 0) == 1:
                result["sales_rep_id"] = matches[0]["id"]
                result["method"] = METHOD_NAME
                result["status"] = STATUS_MATCHED
            elif len(matches) > 1 or owner_name_counts.get(name_key, 0) > 1:
                result["status"] = STATUS_AMBIGUOUS

        results[owner_id] = result

    # A name match onto a rep that a DIFFERENT owner already reached by
    # email could be a second HubSpot account for that person, or a
    # different person with the same name -- don't guess.
    email_claimed = {r["sales_rep_id"] for r in results.values()
                     if r["sales_rep_id"] is not None and r["method"] != METHOD_NAME}
    for owner_id, result in results.items():
        if result["method"] == METHOD_NAME and result["sales_rep_id"] in email_claimed:
            result.update(sales_rep_id=None, status=STATUS_AMBIGUOUS, method=email_methods[owner_id])
    return results


# ---------------------------------------------------------------- call parsing

def parse_hubspot_datetime(value):
    """HubSpot v3 sends ISO-8601 ('2026-10-15T14:32:05.120Z'); some
    properties/older payloads send epoch milliseconds. Returns an aware
    UTC datetime, or None for missing/unparseable values."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def parse_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_call(record):
    """One HubSpot call object -> hubspot_calls row dict. Missing
    properties stay None (never 0 / '' substitutes)."""
    props = record.get("properties") or {}
    call_id = record.get("id") or props.get("hs_object_id")
    return {
        "hubspot_call_id": str(call_id) if call_id not in (None, "") else None,
        "hubspot_owner_id": _clean(props.get("hubspot_owner_id")),
        "call_timestamp": parse_hubspot_datetime(props.get("hs_timestamp")),
        "direction": _clean(props.get("hs_call_direction")),
        "duration_ms": parse_int(props.get("hs_call_duration")),
        "disposition": _clean(props.get("hs_call_disposition")),
        "status": _clean(props.get("hs_call_status")),
        "to_number": _clean(props.get("hs_call_to_number")),
        "from_number": _clean(props.get("hs_call_from_number")),
        "title": _clean(props.get("hs_call_title")),
        "recording_url": safe_https_url(props.get("hs_call_recording_url")),
        "hubspot_created_at": parse_hubspot_datetime(props.get("hs_createdate") or record.get("createdAt")),
        "hubspot_modified_at": parse_hubspot_datetime(props.get("hs_lastmodifieddate") or record.get("updatedAt")),
    }


def safe_https_url(value):
    """Only absolute https:// URLs survive -- they end up as href
    attributes, so anything else (javascript:, data:, relative) is
    dropped rather than rendered."""
    text = _clean(value)
    if not text or not text.lower().startswith("https://") or any(ch.isspace() for ch in text):
        return None
    return text


def hubspot_contact_url(ui_domain, portal_id, contact_id):
    if not (portal_id and contact_id):
        return None
    domain = ui_domain if ui_domain and re.fullmatch(r"[a-z0-9.-]+\.hubspot\.com", ui_domain) else "app.hubspot.com"
    return f"https://{domain}/contacts/{portal_id}/record/0-1/{contact_id}"


def hubspot_contact_search_url(ui_domain, portal_id, query):
    """HubSpot's own contacts list, searched for `query` -- the fallback
    when no contact record could be matched."""
    if not portal_id:
        return None
    domain = ui_domain if ui_domain and re.fullmatch(r"[a-z0-9.-]+\.hubspot\.com", ui_domain) else "app.hubspot.com"
    return f"https://{domain}/contacts/{portal_id}/objects/0-1/views/all/list?" + urllib.parse.urlencode({"query": query})


# ---------------------------------------------------------------- connected outcome

# HubSpot's built-in "Connected" call outcome id -- identical in every
# portal. Used alongside the label lookup so connected calls are still
# recognized before the dispositions endpoint has been synced.
DEFAULT_CONNECTED_DISPOSITION_ID = "f240bbac-87c9-4f6e-bf70-924b57d47db7"

# HubSpot's built-in outcomes, used only as label fallbacks when the
# portal's own dispositions haven't been fetched.
DEFAULT_DISPOSITION_LABELS = {
    "f240bbac-87c9-4f6e-bf70-924b57d47db7": "Connected",
    "a4c4c377-d246-4b32-a13b-75a56a4cd0ff": "Left live message",
    "b2cf5968-551e-4856-9783-52b3da59a7d0": "Left voicemail",
    "73a0d17f-1163-4015-bdd5-ec830791da20": "No answer",
    "9d9162e7-6cf3-4944-bf63-4dff82258764": "Busy",
    "17b47fee-58de-441e-a44c-c6300d46f273": "Wrong number",
}


def connected_outcome_labels():
    """Which HubSpot outcome labels count as Connected. Defaults to
    exactly 'Connected'; HUBSPOT_CONNECTED_OUTCOMES (comma-separated
    labels) overrides it if this portal uses custom outcomes."""
    raw = os.environ.get("HUBSPOT_CONNECTED_OUTCOMES", "Connected")
    return {label.strip().lower() for label in raw.split(",") if label.strip()}


def connected_disposition_ids(disposition_labels):
    labels = connected_outcome_labels()
    ids = {d_id for d_id, label in disposition_labels.items() if (label or "").strip().lower() in labels}
    if "connected" in labels:
        ids.add(DEFAULT_CONNECTED_DISPOSITION_ID)
    return ids


# ---------------------------------------------------------------- date ranges

DATE_RANGES = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_7_days", "Last 7 Days"),
    ("last_30_days", "Last 30 Days"),
    ("this_month", "This Month"),
    ("last_month", "Last Month"),
    ("last_90_days", "Last 90 Days"),
    ("last_6_months", "Last 6 Months"),
    ("last_12_months", "Last 12 Months"),
    ("this_year", "This Year"),
    ("custom", "Custom Date Range"),
]
DATE_RANGE_KEYS = {key for key, _ in DATE_RANGES}
DEFAULT_DATE_RANGE = "today"  # by request, 2026-10-20 (was Last 30 Days)
MAX_CUSTOM_RANGE_DAYS = 366 * 10


def resolve_date_range(range_key, start, end, today):
    """(start_date, end_date) inclusive, in the business timezone, or
    raises ValueError with a user-facing message."""
    if range_key not in DATE_RANGE_KEYS:
        raise ValueError(f"Unknown date range '{range_key}'.")
    if range_key == "today":
        return today, today
    if range_key == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if range_key == "last_7_days":
        return today - timedelta(days=6), today
    if range_key == "last_30_days":
        return today - timedelta(days=29), today
    if range_key == "last_90_days":
        return today - timedelta(days=89), today
    if range_key == "last_6_months":
        return _months_back(today, 6), today
    if range_key == "last_12_months":
        return _months_back(today, 12), today
    if range_key == "this_year":
        return today.replace(month=1, day=1), today
    if range_key == "this_month":
        return today.replace(day=1), today
    if range_key == "last_month":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    try:
        start_date = date.fromisoformat(start or "")
        end_date = date.fromisoformat(end or "")
    except ValueError:
        raise ValueError("Custom range needs a valid start and end date (YYYY-MM-DD).") from None
    if start_date > end_date:
        raise ValueError("Custom range start date must be on or before the end date.")
    if (end_date - start_date).days > MAX_CUSTOM_RANGE_DAYS:
        raise ValueError("Custom range is too long (10 years max).")
    return start_date, end_date


def _months_back(today, months):
    """The day after the same calendar day `months` ago, so 'Last 6
    Months' on Oct 16 is Apr 17 - Oct 16 (day clamped for short months)."""
    total = today.year * 12 + today.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    for day in (today.day, 30, 29, 28):
        try:
            return date(year, month, day) + timedelta(days=1)
        except ValueError:
            continue


# ---------------------------------------------------------------- time buckets

TIME_SCALES = ["hourly", "daily", "weekly", "monthly", "quarterly", "yearly"]
# The date range decides WHICH calls are counted; the time scale only
# decides how they're grouped on the chart's X axis. "auto" (default)
# derives the scale from the range -- see auto_time_scale().
AUTO_SCALE = "auto"
DEFAULT_TIME_SCALE = AUTO_SCALE

# Preset range -> grouping, by request (2026-10-16). Custom ranges use
# CUSTOM_AUTO_THRESHOLDS instead.
PRESET_AUTO_SCALES = {
    "today": "hourly",
    "yesterday": "hourly",
    "last_7_days": "daily",
    "last_30_days": "daily",
    "this_month": "daily",
    "last_month": "daily",
    "last_90_days": "weekly",
    "last_6_months": "weekly",
    "last_12_months": "monthly",
    "this_year": "monthly",
}
# (max days in range, scale) -- first match wins.
CUSTOM_AUTO_THRESHOLDS = [(2, "hourly"), (62, "daily"), (200, "weekly"), (730, "monthly"), (1830, "quarterly")]

# Which manual groupings make sense for a range of N days:
# (scale, min days, max days). Keeps every option readable -- no
# 1-bar "Daily" for Today, no 2,000-bar "Hourly" for a quarter.
_SCALE_DAY_LIMITS = {
    "hourly": (1, 7),
    "daily": (2, 186),
    "weekly": (14, 1100),
    "monthly": (45, None),
    "quarterly": (180, None),
    "yearly": (548, None),
}


def range_days(start_date, end_date):
    return (end_date - start_date).days + 1


def allowed_time_scales(start_date, end_date):
    """Manual Group-by options for this range, in TIME_SCALES order."""
    days = range_days(start_date, end_date)
    return [scale for scale in TIME_SCALES
            if days >= _SCALE_DAY_LIMITS[scale][0]
            and (_SCALE_DAY_LIMITS[scale][1] is None or days <= _SCALE_DAY_LIMITS[scale][1])]


def auto_time_scale(range_key, start_date, end_date):
    if range_key in PRESET_AUTO_SCALES:
        return PRESET_AUTO_SCALES[range_key]
    days = range_days(start_date, end_date)
    for max_days, scale in CUSTOM_AUTO_THRESHOLDS:
        if days <= max_days:
            return scale
    return "yearly"


def resolve_time_scale(requested, range_key, start_date, end_date):
    """(scale, allowed) for a request. `requested` is "auto" or a manual
    scale; a manual scale not allowed for this range raises ValueError.
    `allowed` always contains the auto choice too, so the UI can list it."""
    allowed = allowed_time_scales(start_date, end_date)
    auto = auto_time_scale(range_key, start_date, end_date)
    if auto not in allowed:
        allowed = [s for s in TIME_SCALES if s in set(allowed) | {auto}]
    if requested in (None, "", AUTO_SCALE):
        return auto, allowed
    if requested not in SCALE_TRUNC_UNITS:
        raise ValueError(f"Unknown time scale '{requested}'.")
    if requested not in allowed:
        raise ValueError(f"{requested.title()} grouping isn't available for this date range.")
    return requested, allowed
# Postgres date_trunc() unit per scale -- passed to SQL only from this
# fixed map, never from request input.
SCALE_TRUNC_UNITS = {
    "hourly": "hour",
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
    "quarterly": "quarter",
    "yearly": "year",
}
MAX_BUCKETS = 5000


def truncate(dt, scale):
    """Same boundaries as Postgres date_trunc() (weeks start Monday)."""
    if scale == "hourly":
        return dt.replace(minute=0, second=0, microsecond=0)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if scale == "daily":
        return day
    if scale == "weekly":
        return day - timedelta(days=day.weekday())
    if scale == "monthly":
        return day.replace(day=1)
    if scale == "quarterly":
        return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)
    if scale == "yearly":
        return day.replace(month=1, day=1)
    raise ValueError(scale)


def _step(dt, scale):
    if scale == "hourly":
        return dt + timedelta(hours=1)
    if scale == "daily":
        return dt + timedelta(days=1)
    if scale == "weekly":
        return dt + timedelta(days=7)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[scale]
    total = dt.month - 1 + months
    return dt.replace(year=dt.year + total // 12, month=total % 12 + 1)


def bucket_label(dt, scale):
    if scale == "hourly":
        return dt.strftime("%b %-d, %-I %p")
    if scale == "daily":
        return dt.strftime("%b %-d")
    if scale == "weekly":
        return "Week of " + dt.strftime("%b %-d")
    if scale == "monthly":
        return dt.strftime("%b %Y")
    if scale == "quarterly":
        return f"Q{(dt.month - 1) // 3 + 1} {dt.year}"
    return str(dt.year)


def bucket_count(start_date, end_date, scale):
    current = truncate(datetime.combine(start_date, datetime.min.time()), scale)
    last = truncate(datetime.combine(end_date, datetime.max.time()), scale)
    n = 0
    while current <= last:
        n += 1
        if n > MAX_BUCKETS:
            return n
        current = _step(current, scale)
    return n


def fill_buckets(counts, start_date, end_date, scale):
    """counts: {naive local bucket-start datetime: n} (from SQL
    date_trunc). Returns every bucket in [start_date, end_date] in order,
    zero-filled, as [{start, label, count}]."""
    if scale not in SCALE_TRUNC_UNITS:
        raise ValueError(f"Unknown time scale '{scale}'.")
    if bucket_count(start_date, end_date, scale) > MAX_BUCKETS:
        raise ValueError("That date range has too many points for this time scale -- pick a shorter range or a coarser scale.")
    current = truncate(datetime.combine(start_date, datetime.min.time()), scale)
    last = truncate(datetime.combine(end_date, datetime.max.time()), scale)
    rows = []
    while current <= last:
        rows.append({"start": current.isoformat(), "label": bucket_label(current, scale), "count": int(counts.get(current, 0))})
        current = _step(current, scale)
    return rows


# ---------------------------------------------------------------- formatting

def format_duration_hms(total_ms):
    if total_ms is None:
        return None
    seconds = int(round(total_ms / 1000))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def format_hours_minutes(total_seconds):
    if total_seconds is None:
        return None
    minutes = int(round(total_seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def format_duration_ms(total_ms):
    if total_ms is None:
        return None
    seconds = int(round(total_ms / 1000))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s"


def humanize_status(status):
    if not status:
        return None
    return status.replace("_", " ").title()
