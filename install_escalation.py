"""Install Escalation (Tools > Install Escalation, added 2026-10-26).

Reps call customers whose install job is already "Ready" in PlanetWeb
(FTTPScheduleJobStatuses ID 8 -- the house is ready to install) but
booked for a later day, to offer them an earlier slot and fill holes in
the install schedule. Only jobs booked AFTER today (Eastern) are listed:
PlanetWeb also holds ~2,000 past-dated jobs still marked Ready whose
status was never updated after the visit, and those can't be moved up.

The account list is read live from PlanetWeb (read-only, cached for
CACHE_SECONDS). Call marks live in appdb (install_escalation_calls,
db_migrations.py migration 16): one per order per Eastern day, so a
marked account shows as called for the rest of that day and is fair game
again tomorrow. Reads return safe empty values when a database is down;
writes return (ok, error)."""

import threading
import time
from datetime import timedelta

import pandas as pd

import db
import db_migrations
import permissions
from sales_metrics import _today

READY_STATUS_ID = 8
CACHE_SECONDS = 120
MAX_NOTE = 500

# (key, label) -- what came of the call. Stored as the key.
OUTCOMES = (
    ("moved_up", "Moved Up"),
    ("voicemail", "Left Voicemail"),
    ("no_answer", "No Answer"),
    ("keeping_date", "Keeping Date"),
)
OUTCOME_LABELS = dict(OUTCOMES)

# One row per Ready job booked after `?` (tomorrow, naive Eastern like
# every PlanetWeb datetime). An order with more than one such job keeps
# its earliest in load_accounts().
READY_JOBS_QUERY = f"""
SELECT
    j.ID AS JobID,
    j.FormDataID,
    t.Name AS JobType,
    s.StartDate,
    s.EndDate,
    a.AreaName,
    j.SchedulingNotes,
    j.StatusDate,
    fd.FirstName,
    fd.LastName,
    fd.PhoneNumber,
    fd.EmailAddress,
    fd.Address,
    fd.ApartmentSuite,
    fd.City,
    fd.State,
    fd.Zipcode,
    fd.InsertDate
FROM FTTPScheduleJobs j
JOIN FTTPScheduleSlots s
    ON s.ID = j.SlotID
JOIN [PlanetWeb].[dbo].[FTTPFormData] fd
    ON fd.ID = j.FormDataID
LEFT JOIN FTTPScheduleJobTypes t
    ON t.ID = j.TypeID
LEFT JOIN FTTPScheduleAreas a
    ON a.ID = s.AreaID
WHERE j.StatusID = {READY_STATUS_ID}
  AND s.StartDate >= ?
ORDER BY s.StartDate ASC, j.ID ASC
"""

_cache = {"at": 0.0, "day": None, "rows": None}
_cache_lock = threading.Lock()


def _log(message):
    print(f"[install-escalation] {message}")


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _fetch_ready_jobs(after_day):
    conn = db.get_planetweb_connection()
    try:
        cur = conn.cursor()
        cur.execute(READY_JOBS_QUERY, after_day)
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def load_accounts(force=False):
    """(rows, error). One dict per order with a Ready job booked after
    today, soonest first. Cached for CACHE_SECONDS; a failed refresh
    serves the last good list (with the error) rather than nothing."""
    today = _today()
    with _cache_lock:
        fresh = (_cache["rows"] is not None and _cache["day"] == today
                 and time.time() - _cache["at"] < CACHE_SECONDS)
        if fresh and not force:
            return _cache["rows"], None
        try:
            jobs = _fetch_ready_jobs(today + timedelta(days=1))
        except Exception as exc:
            error = db.sanitize_error(exc)
            _log(f"PlanetWeb query failed: {error}")
            return (_cache["rows"] if _cache["day"] == today else None) or [], error

        rows, seen = [], set()
        for job in jobs:
            order_id = job["FormDataID"]
            if order_id in seen:
                continue
            seen.add(order_id)
            start, end = job["StartDate"], job["EndDate"]
            name = " ".join(p for p in (_clean(job["FirstName"]), _clean(job["LastName"])) if p)
            rows.append({
                "order_id": order_id,
                "job_id": job["JobID"],
                "job_type": _clean(job["JobType"]),
                "install_date": start.date(),
                "window": (f"{start.strftime('%-I:%M %p')} – {end.strftime('%-I:%M %p')}"
                           if start and end else None),
                "area": _clean(job["AreaName"]),
                "scheduling_notes": _clean(job["SchedulingNotes"]),
                "ready_since": job["StatusDate"].date() if job["StatusDate"] else None,
                "customer_name": name or f"Order #{order_id}",
                "phone": _clean(job["PhoneNumber"]),
                "email": _clean(job["EmailAddress"]),
                "address": ", ".join(p for p in (_clean(job["Address"]), _clean(job["ApartmentSuite"])) if p),
                "city": _clean(job["City"]),
                "state": (_clean(job["State"]) or "").upper() or None,
                "zip": _clean(job["Zipcode"]),
                "ordered": job["InsertDate"].date() if job["InsertDate"] else None,
            })
        _cache.update(at=time.time(), day=today, rows=rows)
        return rows, None


# ================================================================
# Call marks (appdb)
# ================================================================

def _connect():
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    return conn


def _display_name(user):
    if not user:
        return None
    return user.get("display_name") or user.get("sales_rep_name") or user.get("email")


_MARK_COLUMNS = "form_data_id, call_date, outcome, note, called_by_user_id, called_by_name, called_at"


def _serialize(row):
    order_id, call_date, outcome, note, user_id, name, called_at = row
    return {
        "order_id": order_id,
        "call_date": call_date,
        "outcome": outcome,
        "outcome_label": OUTCOME_LABELS.get(outcome, outcome),
        "note": note,
        "called_by_user_id": user_id,
        "called_by_name": name,
        "called_at": called_at,
    }


def get_calls(order_ids):
    """(available, today_marks, last_prior_marks) for `order_ids`:
    {order_id: mark} for marks made today (Eastern), and for each order's
    most recent mark before today. available=False when appdb is down."""
    order_ids = [int(i) for i in order_ids]
    if not order_ids:
        return True, {}, {}
    today = _today()
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_MARK_COLUMNS} FROM install_escalation_calls"
                " WHERE form_data_id = ANY(%s) AND call_date = %s",
                (order_ids, today),
            )
            today_marks = {m["order_id"]: m for m in map(_serialize, cur.fetchall())}
            cur.execute(
                f"SELECT DISTINCT ON (form_data_id) {_MARK_COLUMNS} FROM install_escalation_calls"
                " WHERE form_data_id = ANY(%s) AND call_date < %s"
                " ORDER BY form_data_id, call_date DESC",
                (order_ids, today),
            )
            prior = {m["order_id"]: m for m in map(_serialize, cur.fetchall())}
            return True, today_marks, prior
    except Exception as exc:
        _log(f"reading call marks failed: {db.sanitize_error(exc)}")
        return False, {}, {}
    finally:
        if conn is not None:
            conn.close()


def _parse_order_id(order_id):
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return None
    return order_id if order_id > 0 else None


def mark_called(order_id, outcome, note, user):
    """Marks an order as called today (or updates today's mark).
    Returns (ok, error, mark)."""
    if not user or user.get("status") != "active":
        return False, "You must be logged in with an active account to do this.", None
    order_id = _parse_order_id(order_id)
    if order_id is None:
        return False, "Invalid account.", None
    if outcome not in OUTCOME_LABELS:
        return False, "Pick how the call went.", None
    note = (note or "").strip()[:MAX_NOTE] or None
    conn = None
    try:
        conn = _connect()
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO install_escalation_calls
                    (form_data_id, call_date, outcome, note, called_by_user_id, called_by_name)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (form_data_id, call_date) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    note = EXCLUDED.note,
                    called_by_user_id = EXCLUDED.called_by_user_id,
                    called_by_name = EXCLUDED.called_by_name,
                    called_at = now()
                RETURNING """ + _MARK_COLUMNS,
                (order_id, _today(), outcome, note, user.get("id"), _display_name(user)),
            )
            return True, None, _serialize(cur.fetchone())
    except Exception as exc:
        return False, db.sanitize_error(exc), None
    finally:
        if conn is not None:
            conn.close()


def unmark_called(order_id, user):
    """Undo today's mark -- whoever made it, or an Admin.
    Returns (ok, error)."""
    if not user or user.get("status") != "active":
        return False, "You must be logged in with an active account to do this."
    order_id = _parse_order_id(order_id)
    if order_id is None:
        return False, "Invalid account."
    conn = None
    try:
        conn = _connect()
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT called_by_user_id FROM install_escalation_calls WHERE form_data_id = %s AND call_date = %s",
                (order_id, _today()),
            )
            row = cur.fetchone()
            if row is None:
                return True, None
            if not permissions.is_admin(user) and row[0] != user.get("id"):
                return False, "Only the person who marked this call (or an Admin) can undo it."
            cur.execute(
                "DELETE FROM install_escalation_calls WHERE form_data_id = %s AND call_date = %s",
                (order_id, _today()),
            )
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
