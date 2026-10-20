"""Staffing calendar (/staffing, templates/staffing.html) -- appdb tables
from db_migrations.py migration 9. See README.md "Staffing Calendar".

Two kinds of entries on one calendar:
  - Events that need staffing: anyone logged in posts them with a head
    count and signs up (staffing_events + staffing_event_signups); the
    poster or an Admin can delete one.
  - Door-knocking shifts: a rep adds their own shift -- date, time, area
    (door_knock_shifts).

Everything is keyed by sales_reps.id, so a user can only act for the rep
their account is linked to (users.sales_rep_id); Admins can act for any
rep. All rules live here (permissions.can_staff_self /
can_manage_staffing decide who) -- app.py routes just pass the user in.
Dates are Eastern (sales_metrics._today()), never the server's UTC date.

Same raw-psycopg2 conventions as user_store.py: reads return safe empty
values when appdb is down; writes return (ok, error)."""

import calendar
from datetime import date, time, timedelta

import db
import db_migrations
import permissions
import user_store

MAX_TEXT = 200
MAX_NOTES = 1000
MAX_STAFF_NEEDED = 100


def _connect():
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    return conn


def _log(message):
    print(f"[staffing] {message}")


def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _parse_time(value):
    try:
        return time.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _text(form, name, limit=MAX_TEXT):
    return (form.get(name) or "").strip()[:limit] or None


def format_time(t):
    if t is None:
        return ""
    return t.strftime("%-I:%M %p") if t.minute else t.strftime("%-I %p")


def format_range(start, end):
    if start and end:
        return f"{format_time(start)} – {format_time(end)}"
    return format_time(start) or ""


# ================================================================
# Reads
# ================================================================

_EVENT_SELECT = """
    SELECT e.id, e.title, e.event_date, e.start_time, e.end_time, e.location, e.notes,
           e.staff_needed, cu.email, e.created_by,
           coalesce(array_agg(coalesce(sr.name, su.email) ORDER BY s.created_at) FILTER (WHERE s.id IS NOT NULL), '{}'),
           coalesce(array_agg(CASE WHEN s.sales_rep_id IS NOT NULL THEN 'rep:' || s.sales_rep_id
                                   ELSE 'user:' || s.staff_user_id END ORDER BY s.created_at)
                    FILTER (WHERE s.id IS NOT NULL), '{}'),
           coalesce(array_agg(s.created_by ORDER BY s.created_at) FILTER (WHERE s.id IS NOT NULL), '{}')
    FROM staffing_events e
    LEFT JOIN users cu ON cu.id = e.created_by
    LEFT JOIN staffing_event_signups s ON s.event_id = e.id
    LEFT JOIN sales_reps sr ON sr.id = s.sales_rep_id
    LEFT JOIN users su ON su.id = s.staff_user_id
"""
_EVENT_GROUP = " GROUP BY e.id, cu.email ORDER BY e.event_date, e.start_time NULLS FIRST, e.id"


def _event_dict(row):
    (event_id, title, event_date, start_time, end_time, location, notes,
     staff_needed, created_by_email, created_by, staff_names, staff_keys, added_by) = row
    filled = len(staff_keys)
    return {
        "id": event_id, "title": title, "date": event_date,
        "start_time": start_time, "end_time": end_time, "time_display": format_range(start_time, end_time),
        "location": location, "notes": notes, "staff_needed": staff_needed,
        "created_by_email": created_by_email, "created_by": created_by,
        # Who's staffing, as {"key": "rep:<id>" | "user:<id>", "name",
        # "added_by": users.id who signed them up} -- compare a viewer's
        # staff_key(user) against "staff_keys".
        "staff": [{"key": k, "name": n, "added_by": a} for k, n, a in zip(staff_keys, staff_names, added_by)],
        "staff_keys": set(staff_keys), "filled": filled, "open_spots": max(0, staff_needed - filled),
    }


_SHIFT_SELECT = """
    SELECT k.id, k.shift_date, k.area, k.notes, k.sales_rep_id, sr.name
    FROM door_knock_shifts k JOIN sales_reps sr ON sr.id = k.sales_rep_id
"""


def _shift_dict(row):
    shift_id, shift_date, area, notes, rep_id, rep_name = row
    return {"id": shift_id, "date": shift_date, "area": area, "notes": notes, "rep_id": rep_id, "rep_name": rep_name}


def _load_range(cur, start, end):
    cur.execute(_EVENT_SELECT + " WHERE e.event_date BETWEEN %s AND %s" + _EVENT_GROUP, (start, end))
    events = [_event_dict(r) for r in cur.fetchall()]
    cur.execute(_SHIFT_SELECT + " WHERE k.shift_date BETWEEN %s AND %s ORDER BY k.shift_date, sr.name",
                (start, end))
    shifts = [_shift_dict(r) for r in cur.fetchall()]
    return events, shifts


def get_calendar(year, month, selected_day, today):
    """Month grid (Mon-Sun weeks, None for padding cells) plus the
    selected day's full detail and the upcoming events still short on
    reps. `ok` False when appdb is unreachable."""
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    result = {
        "ok": False, "error": None, "year": year, "month": month,
        "month_label": first.strftime("%B %Y"), "weeks": [],
        "prev": (first - timedelta(days=1)).replace(day=1), "next": last + timedelta(days=1),
        "day": None, "open_events": [], "upcoming_events": [],
    }
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            events, shifts = _load_range(cur, first, last)
            day_events, day_shifts = (
                _load_range(cur, selected_day, selected_day) if not (first <= selected_day <= last)
                else ([e for e in events if e["date"] == selected_day], [s for s in shifts if s["date"] == selected_day])
            )
            cur.execute(_EVENT_SELECT + " WHERE e.event_date >= %s" + _EVENT_GROUP, (today,))
            result["upcoming_events"] = [_event_dict(r) for r in cur.fetchall()]
            result["open_events"] = [e for e in result["upcoming_events"] if e["open_spots"]][:8]
        result["ok"] = True
    except Exception as exc:
        result["error"] = db.sanitize_error(exc)
        _log(f"Loading calendar failed: {result['error']}")
        events, shifts, day_events, day_shifts = [], [], [], []

    by_day = {}
    for e in events:
        by_day.setdefault(e["date"], {"events": [], "shifts": []})["events"].append(e)
    for s in shifts:
        by_day.setdefault(s["date"], {"events": [], "shifts": []})["shifts"].append(s)

    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells = []
        for d in week:
            if d.month != month:
                cells.append(None)
                continue
            entries = by_day.get(d, {"events": [], "shifts": []})
            cells.append({
                "date": d, "day": d.day, "is_today": d == today, "is_past": d < today,
                "is_weekend": d.weekday() >= 5, "is_selected": d == selected_day,
                "events": entries["events"], "shifts": entries["shifts"],
            })
        result["weeks"].append(cells)

    result["day"] = {
        "date": selected_day, "is_past": selected_day < today, "is_weekend": selected_day.weekday() >= 5,
        "events": day_events, "shifts": day_shifts,
    }
    return result


# ================================================================
# Writes
# ================================================================

def _resolve_rep(user, form):
    """Which rep this action is for: a non-admin is always their own
    linked rep; an Admin may pick any rep (sales_rep_id in the form) or
    fall back to their own. Returns (rep_id, error)."""
    if permissions.can_manage_staffing(user):
        raw = (form.get("sales_rep_id") or "").strip()
        if raw:
            if not raw.isdigit() or not any(r["id"] == int(raw) for r in user_store.list_sales_reps()):
                return None, "Choose a sales rep."
            return int(raw), None
    if not permissions.can_staff_self(user):
        return None, "Your account isn't linked to a sales rep, so you can't sign up. Ask an Admin to link it."
    return user["sales_rep_id"], None


def staff_key(user):
    """How this account appears on an event: as its linked sales rep, or
    as itself when it has none."""
    if not user:
        return None
    return f"rep:{user['sales_rep_id']}" if user.get("sales_rep_id") else f"user:{user['id']}"


def _resolve_event_staff(user, form):
    """Who an event sign-up/leave is for, as (sales_rep_id, staff_user_id,
    error). Anyone logged in acts for themselves (their rep if linked,
    else their own account) or names any sales rep (sales_rep_id) -- open
    to everyone by request, 2026-10-20. Naming a non-rep account
    (staff_user_id) stays Admin-only."""
    if not permissions.can_join_events(user):
        return None, None, "You must be logged in with an active account to do this."
    raw_rep = (form.get("sales_rep_id") or "").strip()
    if raw_rep:
        if not raw_rep.isdigit() or not any(r["id"] == int(raw_rep) for r in user_store.list_sales_reps()):
            return None, None, "Choose a sales rep."
        return int(raw_rep), None, None
    if permissions.can_manage_staffing(user):
        raw_user = (form.get("staff_user_id") or "").strip()
        if raw_user:
            if not raw_user.isdigit():
                return None, None, "Choose a user."
            return None, int(raw_user), None
    if user.get("sales_rep_id"):
        return user["sales_rep_id"], None, None
    return None, user["id"], None


def _check_not_past(day, today, what):
    if day < today:
        return f"Can't {what} a date that has already passed."
    return None


def create_event(form, user, today):
    if not permissions.can_post_events(user):
        return False, "You must be logged in with an active account to post events."
    title = _text(form, "title")
    event_date = _parse_date(form.get("event_date"))
    start_time, end_time = _parse_time(form.get("start_time")), _parse_time(form.get("end_time"))
    raw_needed = (form.get("staff_needed") or "").strip()
    if not title:
        return False, "Give the event a title."
    if not event_date:
        return False, "Pick a date for the event."
    error = _check_not_past(event_date, today, "post an event on")
    if error:
        return False, error
    typed_start, typed_end = (form.get("start_time") or "").strip(), (form.get("end_time") or "").strip()
    if (typed_start and not start_time) or (typed_end and not end_time):
        return False, "Enter times like 10:00 AM."
    if start_time and end_time and end_time <= start_time:
        return False, "The end time must be after the start time."
    if not raw_needed.isdigit() or not 1 <= int(raw_needed) <= MAX_STAFF_NEEDED:
        return False, f"Reps needed must be a number from 1 to {MAX_STAFF_NEEDED}."
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO staffing_events (title, event_date, start_time, end_time, location, notes,
                                                 staff_needed, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (title, event_date, start_time, end_time, _text(form, "location"),
                     _text(form, "notes", MAX_NOTES), int(raw_needed), user["id"]),
                )
                event_id = cur.fetchone()[0]
        _log(f"Event #{event_id} '{title}' on {event_date} posted by {user['email']}")
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def delete_event(event_id, user):
    """The poster can delete their own event; Admins can delete any."""
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT created_by FROM staffing_events WHERE id = %s FOR UPDATE", (event_id,))
                found = cur.fetchone()
                if not found:
                    return False, "That event no longer exists."
                if not permissions.can_delete_event(user, found[0]):
                    return False, "Only the person who posted this event, or an Admin, can delete it."
                cur.execute("DELETE FROM staffing_events WHERE id = %s RETURNING title, event_date", (event_id,))
                row = cur.fetchone()
        _log(f"Event #{event_id} '{row[0]}' on {row[1]} removed by {user['email']}")
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def get_event_date(event_id):
    """The event's date, or None if it doesn't exist / appdb is down."""
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            return _event_date(cur, event_id)
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def _event_date(cur, event_id):
    cur.execute("SELECT event_date FROM staffing_events WHERE id = %s", (event_id,))
    row = cur.fetchone()
    return row[0] if row else None


def sign_up(event_id, form, user, today):
    rep_id, staff_user_id, error = _resolve_event_staff(user, form)
    if error:
        return False, error
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                event_date = _event_date(cur, event_id)
                if event_date is None:
                    return False, "That event no longer exists."
                error = _check_not_past(event_date, today, "sign up for")
                if error:
                    return False, error
                cur.execute(
                    """
                    INSERT INTO staffing_event_signups (event_id, sales_rep_id, staff_user_id, created_by)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """,
                    (event_id, rep_id, staff_user_id, user["id"]),
                )
                if cur.rowcount == 0:
                    return False, "Already signed up for this event."
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def leave_event(event_id, form, user, today):
    rep_id, staff_user_id, error = _resolve_event_staff(user, form)
    if error:
        return False, error
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                event_date = _event_date(cur, event_id)
                if event_date is None:
                    return False, "That event no longer exists."
                if event_date < today and not permissions.can_manage_staffing(user):
                    return False, "This event has already happened."
                cur.execute(
                    "SELECT id, created_by FROM staffing_event_signups WHERE event_id = %s "
                    "AND sales_rep_id IS NOT DISTINCT FROM %s AND staff_user_id IS NOT DISTINCT FROM %s FOR UPDATE",
                    (event_id, rep_id, staff_user_id),
                )
                signup = cur.fetchone()
                if not signup:
                    return False, "Not signed up for this event."
                is_self = staff_key(user) == (f"rep:{rep_id}" if rep_id else f"user:{staff_user_id}")
                if not permissions.can_remove_event_staff(user, is_self, signup[1]):
                    return False, "Only that rep, the person who added them, or an Admin can remove them."
                cur.execute("DELETE FROM staffing_event_signups WHERE id = %s", (signup[0],))
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def add_shift(form, user, today):
    rep_id, error = _resolve_rep(user, form)
    if error:
        return False, error
    shift_date = _parse_date(form.get("shift_date"))
    area = _text(form, "area")
    if not shift_date:
        return False, "Pick a date for the shift."
    error = _check_not_past(shift_date, today, "add a shift on")
    if error:
        return False, error
    if not area:
        return False, "Say where you'll be knocking (town or neighborhood)."
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                # One shift per rep per day (uq_door_knock_shifts_rep_date).
                cur.execute(
                    """
                    INSERT INTO door_knock_shifts (sales_rep_id, shift_date, area, notes, created_by)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sales_rep_id, shift_date) DO NOTHING
                    """,
                    (rep_id, shift_date, area, _text(form, "notes", MAX_NOTES), user["id"]),
                )
                if cur.rowcount == 0:
                    return False, ("There's already a door-knocking shift booked that day -- "
                                   "remove it first, or list every area in one shift.")
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def delete_shift(shift_id, user, today):
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT sales_rep_id, shift_date FROM door_knock_shifts WHERE id = %s FOR UPDATE",
                            (shift_id,))
                row = cur.fetchone()
                if not row:
                    return False, "That shift no longer exists."
                is_admin = permissions.can_manage_staffing(user)
                if not is_admin and row[0] != user.get("sales_rep_id"):
                    return False, "You can only remove your own shifts."
                if not is_admin and row[1] < today:
                    return False, "This shift has already happened."
                cur.execute("DELETE FROM door_knock_shifts WHERE id = %s", (shift_id,))
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
