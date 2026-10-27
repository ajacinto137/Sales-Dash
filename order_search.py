"""Every order ever placed, for the global search bar (added 2026-10-26,
by request -- "any account that has an order", whatever page you search
from). The dashboards' datasets only hold 2026 rep sales and door-knocker
orders, so search reads this separate, lightweight index instead: one row
per FTTPFormData order (QuoteID set, same rule as every sales query), any
year, any source.

The index is ~40k small rows, loaded from PlanetWeb and kept in memory
for REFRESH_SECONDS. A stale index is refreshed in a background thread
while searches keep using the old copy; only the very first load blocks.
"""

import threading
import time

import pandas as pd

import db
from queries import DOOR_KNOCKER_CODES, WEB_SALES_SOURCE_TOKEN
from sales_metrics import extract_rep_name

REFRESH_SECONDS = 600

_QUERY = """
SELECT fd.ID, fd.FirstName, fd.LastName, fd.Address, fd.ApartmentSuite, fd.City, fd.State,
       fd.Zipcode, fd.InsertDate, fd.ReferralData, fd.SourceToken, fd.InsertByEmployeeID
FROM FTTPFormData fd
WHERE fd.QuoteID IS NOT NULL
"""

_state = {"df": None, "loaded_at": 0.0, "refreshing": False}
_lock = threading.Lock()


def _sold_by(row):
    """(name, kind): a door-knocker code, a rep, "Web", or (None, None)."""
    code = row["ReferralData"].strip().lower() if isinstance(row["ReferralData"], str) else ""
    if code and code in DOOR_KNOCKER_CODES:
        return code, "knocker"
    if pd.notna(row["InsertByEmployeeID"]):
        name = extract_rep_name(row["SourceToken"])
        return (name, "rep") if name != "Unknown" else (None, None)
    if row["SourceToken"] == WEB_SALES_SOURCE_TOKEN:
        return "Web", "web"
    return None, None


def _clean(series):
    return series.fillna("").astype(str).str.strip()


def build_index(raw):
    """PlanetWeb rows -> the search index (newest order first)."""
    df = raw.copy()
    sold = df.apply(_sold_by, axis=1, result_type="expand") if not df.empty else pd.DataFrame(columns=[0, 1])
    first, last = _clean(df["FirstName"]), _clean(df["LastName"])
    address = _clean(df["Address"])
    apt = _clean(df["ApartmentSuite"])
    city, state, zipcode = _clean(df["City"]), _clean(df["State"]).str.upper(), _clean(df["Zipcode"])
    index = pd.DataFrame({
        "sale_id": df["ID"].astype("int64"),
        "first_name": first,
        "last_name": last,
        "address": (address + apt.where(apt == "", " " + apt)).str.strip(),
        "city_state_zip": (city + state.where(state == "", ", " + state) + zipcode.where(zipcode == "", " " + zipcode)).str.strip(" ,"),
        "state": state,
        "order_date": pd.to_datetime(df["InsertDate"], errors="coerce"),
        "sold_by": sold[0] if not df.empty else None,
        "sold_by_kind": sold[1] if not df.empty else None,
    })
    index["_name"] = (index["first_name"] + " " + index["last_name"]).str.strip().str.casefold()
    index["_haystack"] = index["_name"] + " " + index["address"].str.casefold() + " " + index["city_state_zip"].str.casefold()
    return index.sort_values("order_date", ascending=False, na_position="last").reset_index(drop=True)


def _fetch():
    conn = db.get_planetweb_connection()
    try:
        cur = conn.cursor()
        cur.execute(_QUERY)
        columns = [c[0] for c in cur.description]
        return build_index(pd.DataFrame.from_records(cur.fetchall(), columns=columns))
    finally:
        conn.close()


def _refresh_in_background():
    try:
        df = _fetch()
        with _lock:
            _state.update(df=df, loaded_at=time.monotonic())
    except Exception as exc:
        print(f"[order-search] refresh failed: {db.sanitize_error(exc)}", flush=True)
    finally:
        with _lock:
            _state["refreshing"] = False


def get_index():
    """The current index; None if it has never loaded (PlanetWeb down)."""
    with _lock:
        df, stale = _state["df"], time.monotonic() - _state["loaded_at"] > REFRESH_SECONDS
        if df is not None and stale and not _state["refreshing"]:
            _state["refreshing"] = True
            threading.Thread(target=_refresh_in_background, daemon=True).start()
    if df is not None:
        return df
    try:
        df = _fetch()
    except Exception as exc:
        print(f"[order-search] load failed: {db.sanitize_error(exc)}", flush=True)
        return None
    with _lock:
        _state.update(df=df, loaded_at=time.monotonic())
    return df


def search(index, query, limit):
    """Orders whose customer name, address or city/state/ZIP contains the
    query (case-insensitive). Name-prefix matches first, then newest."""
    q = (query or "").strip().casefold()
    if not q or index is None or index.empty:
        return index.iloc[0:0] if index is not None else None
    hits = index[index["_haystack"].str.contains(q, regex=False)]
    if hits.empty:
        return hits
    return hits.assign(_rank=~hits["_name"].str.startswith(q)).sort_values("_rank", kind="stable").head(limit)


def find(index, sale_id):
    """One order's index row as a dict, or None."""
    if index is None:
        return None
    rows = index[index["sale_id"] == int(sale_id)]
    return rows.iloc[0].to_dict() if not rows.empty else None
