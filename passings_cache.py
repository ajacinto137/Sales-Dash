"""Passings & Leads -- in-memory snapshot (added 2026-10-20, by request:
"does it need to load all the data every time?").

Every /passings page view used to run ~8 PlanetWeb queries back to back,
and each one re-evaluated passings_data._BASE_CTE from scratch (a windowed
scan of FTTPFormData plus View_Places). Now the base rows are pulled ONCE
into a pandas DataFrame and the page's KPIs, charts, filter dropdowns and
paginated list are computed from that copy in memory -- a page view issues
no PlanetWeb query at all.

Keeping the copy current, on a background thread (never on a request):
- Incremental (every PASSINGS_INCREMENTAL_SECONDS, default 2 min): only
  FTTPFormData rows with an ID above the last one seen. Those are what add
  a lead (a newer email) or a Prequal-as-Business flag to a passing, and
  they're fetched by primary key, so the check is cheap.
- Full rebuild (every PASSINGS_FULL_REFRESH_SECONDS, default 30 min): the
  whole base query again. View_Places rows change in place (availability,
  addresses, class) and can drop out of the footprint, which an "only
  what's new" check can't see -- so new passings and those changes land
  on the next full rebuild, at most ~30 minutes later.

Every function here mirrors its passings_data.py SQL counterpart's
semantics and return shape exactly (same filters, same KPI definitions,
same NULL ordering, case-insensitive matching like SQL Server's default
collation). Until the first snapshot has loaded (process startup),
app.py falls back to the live SQL versions, so the page never waits on it.

Single gunicorn worker (docker-compose.prod.yml), so one copy per process
is one copy total. The DataFrame is never mutated in place: refreshes
build a new frame and swap the reference, so a request mid-render keeps a
consistent view."""

import os
import threading
import time
from datetime import timedelta

import pandas as pd

import db
import passings_classification
import passings_data

INCREMENTAL_SECONDS = int(os.environ.get("PASSINGS_INCREMENTAL_SECONDS", "120"))
FULL_REFRESH_SECONDS = int(os.environ.get("PASSINGS_FULL_REFRESH_SECONDS", "1800"))
MAX_BACKOFF_SECONDS = 1800

_SNAPSHOT_COLUMNS = [
    "PlaceID", "EmailAddress", "EmailInsertDate", "FullAddress", "Street", "City", "State", "ZipCode",
    "AvailabilityID", "BecameAvailable", "AddressUnits", "PrequalAsBusiness", "ClassName", "PropertyTypeLabel",
]
_SEARCH_COLUMNS = ["FullAddress", "Street", "City", "ZipCode", "EmailAddress", "PlaceID"]

_state = {
    "df": None,                # derived DataFrame, see _prepare()
    "watermark": None,         # highest FTTPFormData.ID folded into df
    "loaded_at": None,         # time.time() of the last full rebuild
    "updated_at": None,        # time.time() of the last full OR incremental refresh
    "error": None,
}
_refresh_lock = threading.Lock()


def _log(message):
    print(f"[passings-cache] {message}", flush=True)


# ================================================================
# Loading
# ================================================================

def _email_valid(series):
    """passings_data._EMAIL_VALID_SQL: not NULL, not blank once trimmed,
    and not the literal 'none'."""
    trimmed = series.fillna("").astype(str).str.strip(" ")
    return (trimmed != "") & (trimmed.str.lower() != "none")


def _prepare(df):
    """Adds the derived columns every query below reuses, computed once
    per refresh instead of once per page view."""
    df = df.copy()
    df["PrequalAsBusiness"] = df["PrequalAsBusiness"].fillna(0).astype(int)
    df["BecameAvailable"] = pd.to_datetime(df["BecameAvailable"], errors="coerce")
    df["EmailInsertDate"] = pd.to_datetime(df["EmailInsertDate"], errors="coerce")
    df["_email_valid"] = _email_valid(df["EmailAddress"])
    df["_marked"] = False
    _classify(df)
    df["_city_l"] = df["City"].fillna("").astype(str).str.lower()
    df["_state_l"] = df["State"].fillna("").astype(str).str.lower()
    df["_zip_l"] = df["ZipCode"].fillna("").astype(str).str.lower()
    # One lowercase blob per row for Search -- newline-joined so a term
    # can only match within one column, like the SQL's per-column LIKEs.
    parts = [df[c].fillna("").astype(str) for c in _SEARCH_COLUMNS]
    blob = parts[0]
    for part in parts[1:]:
        blob = blob + "\n" + part
    df["_search"] = blob.str.lower()
    return df


def _classify(df):
    """(Re)computes the category columns in place. `_business`/`_type_label`
    are the Prequal-as-Business flag and property type as classification
    sees them -- cleared for places marked "Residential Address"
    (passings_activity_store.get_residential_marks()), which therefore
    land in Residential Leads (valid email) or no category."""
    marked = df["_marked"]
    df["_business"] = (df["PrequalAsBusiness"] == 1) & ~marked
    df["_type_label"] = df["PropertyTypeLabel"].where(~marked, None)
    business, typed, valid = df["_business"], df["_type_label"].notna(), df["_email_valid"]
    df["_residential"] = ~business & ~typed & valid
    df["_field_target"] = ~business & typed
    df["_none"] = ~business & ~typed & ~valid
    df["_actionable"] = business | typed | valid


def _with_marks(df, residential_ids):
    """The snapshot as classification sees it right now: marks are read
    per request (they change on a click, not on a PlanetWeb refresh), so
    they're applied to a copy here rather than baked into the snapshot."""
    if not residential_ids:
        return df
    marked = df["PlaceID"].isin({int(i) for i in residential_ids})
    if not marked.any():
        return df
    df = df.copy()
    df["_marked"] = marked
    _classify(df)
    return df


def wait_until_ready(timeout):
    """Blocks up to `timeout` seconds for the first snapshot (right after a
    restart it loads in ~5s). Returns ready()."""
    deadline = time.time() + timeout
    while _state["df"] is None and time.time() < deadline:
        time.sleep(0.25)
    return ready()


def _load_full():
    conn = db.get_planetweb_connection()
    try:
        cursor = conn.cursor()
        # Watermark first: a form row inserted while the big query runs
        # is re-applied by the next incremental pass (idempotent).
        cursor.execute("SELECT MAX(ID) FROM [PlanetWeb].[dbo].[FTTPFormData]")
        watermark = cursor.fetchone()[0] or 0
        select_list = ", ".join(f"[{c}]" for c in _SNAPSHOT_COLUMNS)
        cursor.execute(f"{passings_data._BASE_CTE} SELECT {select_list} FROM passings_base")
        rows = cursor.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame.from_records([tuple(r) for r in rows], columns=_SNAPSHOT_COLUMNS)
    return _prepare(df), int(watermark)


def apply_form_rows(df, form_rows):
    """Folds new FTTPFormData rows [(ID, PlaceID, EmailAddress,
    InsertDate, IsBusiness), ...] into a snapshot, same rules as the base
    CTE: the most recent non-blank email per place wins, and any
    IsBusiness = 1 row flags the place. Places not in the snapshot (not a
    qualifying passing) are ignored. Returns a NEW prepared frame, or the
    same one when nothing applies."""
    if not form_rows or df is None or df.empty:
        return df
    updates = pd.DataFrame.from_records(
        [tuple(r) for r in form_rows], columns=["ID", "PlaceID", "EmailAddress", "InsertDate", "IsBusiness"]
    )
    updates = updates[updates["PlaceID"].isin(df["PlaceID"])]
    if updates.empty:
        return df
    updates["InsertDate"] = pd.to_datetime(updates["InsertDate"], errors="coerce")
    base = df[_SNAPSHOT_COLUMNS].copy()

    emails = updates[updates["EmailAddress"].fillna("").astype(str).str.strip(" ") != ""]
    if not emails.empty:
        newest = emails.sort_values(["InsertDate", "ID"]).groupby("PlaceID").tail(1)
        for place_id, email, inserted in zip(newest["PlaceID"], newest["EmailAddress"], newest["InsertDate"]):
            rows = base.index[base["PlaceID"] == place_id]
            for i in rows:
                current = base.at[i, "EmailInsertDate"]
                if pd.isna(current) or pd.isna(inserted) or inserted >= current:
                    base.at[i, "EmailAddress"] = email
                    base.at[i, "EmailInsertDate"] = inserted

    business_places = updates.loc[updates["IsBusiness"].fillna(0).astype(int) == 1, "PlaceID"]
    if not business_places.empty:
        base.loc[base["PlaceID"].isin(business_places), "PrequalAsBusiness"] = 1
    return _prepare(base)


def _load_incremental():
    df, watermark = _state["df"], _state["watermark"]
    conn = db.get_planetweb_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ID, NJPR_PlaceID, EmailAddress, InsertDate, IsBusiness "
            "FROM [PlanetWeb].[dbo].[FTTPFormData] WHERE ID > ? AND NJPR_PlaceID IS NOT NULL ORDER BY ID",
            [watermark],
        )
        rows = cursor.fetchall()
    finally:
        conn.close()
    if not rows:
        return df, watermark, 0
    return apply_form_rows(df, rows), int(max(r[0] for r in rows)), len(rows)


def refresh(full=False):
    """Never raises. Full rebuild when asked or when no snapshot exists
    yet; otherwise the incremental pass. Returns True on success."""
    with _refresh_lock:
        started = time.time()
        try:
            if full or _state["df"] is None:
                df, watermark = _load_full()
                _state.update(df=df, watermark=watermark, loaded_at=time.time(), updated_at=time.time(), error=None)
                _log(f"Full snapshot: {len(df):,} passings in {time.time() - started:.1f}s")
            else:
                df, watermark, applied = _load_incremental()
                _state.update(df=df, watermark=watermark, updated_at=time.time(), error=None)
                if applied:
                    _log(f"Incremental: {applied} new form row(s) in {time.time() - started:.1f}s")
            return True
        except Exception as exc:
            _state["error"] = db.sanitize_error(exc)
            _log(f"Refresh failed ({'full' if full else 'incremental'}): {_state['error']}")
            return False


_scheduler_started = False
_scheduler_lock = threading.Lock()


def _scheduler_loop():
    failures = 0
    while True:
        due_full = _state["loaded_at"] is None or time.time() - _state["loaded_at"] >= FULL_REFRESH_SECONDS
        failures = 0 if refresh(full=due_full) else failures + 1
        delay = INCREMENTAL_SECONDS if not failures else min(MAX_BACKOFF_SECONDS, INCREMENTAL_SECONDS * 2 ** failures)
        time.sleep(delay)


def start_scheduler():
    """Idempotent. Loads the first snapshot right away, in the background."""
    global _scheduler_started
    if _scheduler_started:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=_scheduler_loop, name="passings-cache", daemon=True).start()
        _scheduler_started = True
        _log(f"Snapshot refresh scheduled: incremental every {INCREMENTAL_SECONDS}s, full every {FULL_REFRESH_SECONDS}s")


def ready():
    return _state["df"] is not None


def updated_at():
    return _state["updated_at"]


# ================================================================
# Queries -- same semantics/return shapes as passings_data.py
# ================================================================

def _filter(df, filters, search):
    """passings_data._build_where(), as a boolean mask."""
    filters = filters or {}
    mask = pd.Series(True, index=df.index)

    category = filters.get("category")
    if category == "commercial_hot_lead":
        mask &= df["_business"]
    elif category == "commercial_field_target":
        mask &= df["_field_target"]
    elif category == "residential_lead":
        mask &= df["_residential"]
    elif category == "none":
        mask &= df["_none"]

    if filters.get("prequal_only"):
        mask &= df["PrequalAsBusiness"] == 1

    property_type = filters.get("property_type")
    if property_type in passings_data._COMMERCIAL_LABELS:
        mask &= df["_type_label"] == property_type

    has_lead = filters.get("has_lead")
    if has_lead is True:
        mask &= df["_email_valid"]
    elif has_lead is False:
        mask &= ~df["_email_valid"]

    for key, column in (("city", "_city_l"), ("state", "_state_l"), ("zip", "_zip_l")):
        value = filters.get(key)
        if value:
            mask &= df[column] == str(value).strip().lower()

    availability_id = filters.get("availability_id")
    if availability_id in (1, 3):
        mask &= df["AvailabilityID"] == availability_id

    became_from = filters.get("became_from")
    if became_from:
        mask &= df["BecameAvailable"] >= pd.Timestamp(became_from)
    became_to = filters.get("became_to")
    if became_to:
        mask &= df["BecameAvailable"] < pd.Timestamp(became_to + timedelta(days=1))

    place_id_in = filters.get("place_id_in")
    if place_id_in is not None:
        mask &= df["PlaceID"].isin({int(i) for i in place_id_in}) if place_id_in else False

    place_id_not_in = filters.get("place_id_not_in")
    if place_id_not_in:
        mask &= ~df["PlaceID"].isin({int(i) for i in place_id_not_in})

    if search:
        mask &= df["_search"].str.contains(search.lower(), regex=False)

    return df[mask]


def _plain(value, column):
    """pandas/numpy scalar -> the plain Python value pyodbc would return."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        value = value.item()
    if column in ("AddressUnits", "AvailabilityID", "PlaceID", "PrequalAsBusiness") and isinstance(value, float):
        return int(value)
    return value


def get_filter_options(filters=None):
    df = _state["df"]

    def distinct(column):
        values = df[column].dropna().astype(str)
        values = values[values != ""].unique().tolist()
        return sorted(values, key=str.lower)

    return {
        "city": distinct("City"),
        "state": distinct("State"),
        "property_type": [label for label, _keywords in passings_classification.COMMERCIAL_PROPERTY_KEYWORDS],
    }


def _sort_key(series):
    return series.str.lower() if series.dtype == object else series


def get_page(filters, search, sort_key, sort_dir, page):
    filtered = _filter(_with_marks(_state["df"], (filters or {}).get("residential_ids")), filters, search)
    page_size = passings_data.PAGE_SIZE
    total = int(len(filtered))
    total_pages = max(1, -(-total // page_size))
    clamped_page = min(max(1, page), total_pages)
    offset = (clamped_page - 1) * page_size

    column = passings_data._SORT_COLUMNS.get(sort_key, "BecameAvailable")
    ascending = sort_dir == "asc"
    # SQL Server sorts NULLs first ascending, last descending.
    ordered = filtered.sort_values(
        [column, "PlaceID"], ascending=[ascending, True],
        na_position="first" if ascending else "last", key=_sort_key, kind="mergesort",
    )
    window = ordered.iloc[offset:offset + page_size]
    rows = [
        passings_data._row_to_dict(tuple(_plain(v, c) for v, c in zip(record, passings_data._ROW_COLUMNS)))
        for record in window[passings_data._ROW_COLUMNS].itertuples(index=False, name=None)
    ]
    return {"ok": True, "error": None, "rows": rows, "total": total, "page": clamped_page,
            "total_pages": total_pages, "page_size": page_size}


def get_kpis(filters, search=None, activity_place_ids=None):
    df = _with_marks(_state["df"], (filters or {}).get("residential_ids"))
    # The Became Available filter scopes every KPI (2026-10-20, by request).
    footprint_filters = dict(filters or {})
    footprint = _filter(df, footprint_filters, search)
    timed = _filter(df, dict(footprint_filters,
                             became_from=(filters or {}).get("became_from"),
                             became_to=(filters or {}).get("became_to")), search)

    total_passings = int(footprint["PlaceID"].nunique())
    total_leads = int(footprint["_email_valid"].sum())
    actionable = footprint["_actionable"]
    activity_ids = {int(i) for i in (activity_place_ids or [])}
    actionable_total = int(actionable.sum())
    actionable_with_activity = int((actionable & footprint["PlaceID"].isin(activity_ids)).sum()) if activity_ids else 0
    return {
        "ok": True, "error": None,
        "total_passings": total_passings,
        "total_leads": total_leads,
        "lead_penetration_pct": (total_leads / total_passings * 100) if total_passings else 0.0,
        "new_passings": int(timed["PlaceID"].nunique()),
        "residential_leads": int(footprint["_residential"].sum()),
        "commercial_hot_leads": int(footprint["_business"].sum()),
        "commercial_field_targets": int(footprint["_field_target"].sum()),
        "available_now": int((footprint["AvailabilityID"] == 1).sum()),
        "pre_order": int((footprint["AvailabilityID"] == 3).sum()),
        "actionable_total": actionable_total,
        "actionable_with_activity": actionable_with_activity,
        "outreach_coverage_pct": (actionable_with_activity / actionable_total * 100) if actionable_total else 0.0,
    }


def _bucket(series, granularity):
    """passings_data._bucket_expr(), including SQL Server's DATEDIFF(WEEK)
    quirk: weeks break on Sunday, so a Sunday lands in the NEXT Monday's
    bucket."""
    day = series.dt.normalize()
    if granularity == "day":
        return day.dt.date.map(lambda d: d.isoformat())
    if granularity == "week":
        shifted = day + pd.Timedelta(days=1)
        monday = shifted - pd.to_timedelta(shifted.dt.weekday, unit="D")
        return monday.map(lambda d: d.to_pydatetime().isoformat())
    return day.dt.to_period("M").dt.start_time.dt.date.map(lambda d: d.isoformat())


def get_passings_over_time(filters, search, granularity):
    filtered = _filter(_with_marks(_state["df"], (filters or {}).get("residential_ids")), filters, search)
    filtered = filtered[filtered["BecameAvailable"].notna()]
    if filtered.empty:
        return {"ok": True, "error": None, "buckets": []}
    grouped = (
        filtered.assign(_bucket=_bucket(filtered["BecameAvailable"], granularity))
        .groupby("_bucket")
        .agg(new_passings=("PlaceID", "nunique"), new_leads=("_email_valid", "sum"))
        .sort_index()
    )
    cumulative = 0
    buckets = []
    for bucket, row in grouped.iterrows():
        cumulative += int(row["new_passings"])
        buckets.append({
            "bucket": bucket,
            "new_passings": int(row["new_passings"]),
            "new_leads": int(row["new_leads"]),
            "cumulative_passings": cumulative,
        })
    return {"ok": True, "error": None, "buckets": buckets}


def get_market_breakdown(filters, search, dimension="city", top_n=15):
    column = "State" if dimension == "state" else "City"
    filtered = _filter(_with_marks(_state["df"], (filters or {}).get("residential_ids")), filters, search)
    filtered = filtered[filtered[column].notna() & (filtered[column].astype(str) != "")]
    if filtered.empty:
        return {"ok": True, "error": None, "markets": []}
    grouped = (
        filtered.groupby(column)
        .agg(passings=("PlaceID", "nunique"), leads=("_email_valid", "sum"))
        .sort_values("passings", ascending=False, kind="mergesort")
        .head(int(top_n) if top_n else 1000)
    )
    markets = []
    for market, row in grouped.iterrows():
        passings, leads = int(row["passings"]), int(row["leads"])
        markets.append({
            "market": market, "passings": passings, "leads": leads,
            "lead_penetration_pct": (leads / passings * 100) if passings else 0.0,
        })
    return {"ok": True, "error": None, "markets": markets}
