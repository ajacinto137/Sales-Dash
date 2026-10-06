"""App-owned persistence for the Passings & Leads outreach/activity
workflow (README.md "Passings & Leads"). Sibling to attention_store.py --
same appdb (db.get_appdb_connection()), same
degrade-gracefully-never-raise contract, same
"this is pure workflow metadata" boundary: nothing here is ever allowed to
influence a Passing's classification (passings_classification.classify()),
which is computed entirely from PlanetWeb source data and never touches
this module (spec #24, "Do Not Conflate Activity With Lead Status").

Keyed by `place_id` (PlanetWeb's View_Places.ID / the SP's AuxVar5) --
the same identifier passings_data.py's every row carries, so a Passing can
always be matched to its activity history regardless of whether it ever
became a Lead.

Append-only history, like account_attention_notes -- there is no mutable
"current activity" row to update in place (unlike account_attention,
nothing here is ever "reclassified"; every logged action is a permanent
fact: "a call happened on this date"). Last Activity is always DERIVED
from the latest row, never stored redundantly -- see get_last_activity_map().

Every public function degrades gracefully: if appdb is unreachable, read
functions return an "unavailable" flag (never raise, never silently imply
zero activity) and write functions return a clear ok=False/error tuple,
same contract attention_store.py already established."""

import db
import db_migrations

ACTIVITY_TYPES = ["Call", "Email", "Field Visit"]
ACTIVITY_TYPE_SET = set(ACTIVITY_TYPES)

MAX_NOTE_LENGTH = 2000


def _ensure_ready(conn):
    db_migrations.ensure_schema(conn)


def _clean_place_ids(place_ids):
    cleaned = set()
    for value in place_ids or []:
        if value is None:
            continue
        try:
            cleaned.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(cleaned)


def _display_name(user):
    if not user:
        return None
    return user.get("sales_rep_name") or user.get("email")


def log_activity(place_id, activity_type, note, user):
    """Appends one activity row -- the ONLY write path into
    passing_activity, so "every activity has an acting user and a
    timestamp" is structural, not a rule enforced elsewhere. `note` is
    optional (unlike attention_store.add_note(), which requires one --
    a quick "Log Call" shouldn't be blocked on typing something).
    Returns (ok, error, activity) -- `activity` (serialized, newest-first
    shape) is populated on success so the caller can update the UI
    without a second round trip."""
    try:
        place_id = int(place_id)
    except (TypeError, ValueError):
        return False, "Invalid Passing.", None

    if activity_type not in ACTIVITY_TYPE_SET:
        return False, "Invalid activity type.", None

    note = (note or "").strip()
    if len(note) > MAX_NOTE_LENGTH:
        return False, f"Note is too long (max {MAX_NOTE_LENGTH} characters).", None

    acting_user_id = user.get("id") if user else None
    display_name = _display_name(user)

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO passing_activity (place_id, activity_type, note, user_id, rep_display_name)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, place_id, activity_type, activity_at, note, rep_display_name
                    """,
                    (place_id, activity_type, note or None, acting_user_id, display_name),
                )
                row = cur.fetchone()
        activity = {
            "id": row[0],
            "place_id": row[1],
            "activity_type": row[2],
            "activity_at": row[3],
            "note": row[4],
            "rep_display_name": row[5],
        }
        return True, None, activity
    except Exception as exc:
        return False, db.sanitize_error(exc), None
    finally:
        if conn is not None:
            conn.close()


def get_last_activity_map(place_ids):
    """(available, {place_id: last_activity_dict}) for a batch of
    place_ids -- one query, no N+1, for the list/table view. A place_id
    with no entry has "No Activity" (spec #20) -- callers use .get() and
    treat a missing key that way, same convention attention_store.py's
    status_map already establishes. available=False (appdb unreachable)
    returns an empty map -- callers must render every row as "No Activity"
    rather than crash or imply the population truly has none."""
    place_ids = _clean_place_ids(place_ids)
    if not place_ids:
        return True, {}

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (place_id)
                    place_id, activity_type, activity_at, note, rep_display_name
                FROM passing_activity
                WHERE place_id = ANY(%s)
                ORDER BY place_id, activity_at DESC, id DESC
                """,
                (place_ids,),
            )
            last_map = {}
            for place_id, activity_type, activity_at, note, rep_display_name in cur.fetchall():
                last_map[place_id] = {
                    "activity_type": activity_type,
                    "activity_at": activity_at,
                    "note": note,
                    "rep_display_name": rep_display_name,
                }
        return True, last_map
    except Exception:
        return False, {}
    finally:
        if conn is not None:
            conn.close()


def get_activity_history(place_id):
    """(available, [activity dicts, newest first]) -- the full history for
    one Passing, e.g. an expandable "Activity History" panel on its card.
    Never overwrites/deletes a row -- see this module's docstring."""
    try:
        place_id = int(place_id)
    except (TypeError, ValueError):
        return True, []

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT activity_type, activity_at, note, rep_display_name
                FROM passing_activity
                WHERE place_id = %s
                ORDER BY activity_at DESC, id DESC
                """,
                (place_id,),
            )
            history = [
                {"activity_type": t, "activity_at": at, "note": note, "rep_display_name": rep}
                for t, at, note, rep in cur.fetchall()
            ]
        return True, history
    except Exception:
        return False, []
    finally:
        if conn is not None:
            conn.close()


def get_place_ids_with_activity_type(activity_type):
    """place_ids with AT LEAST ONE activity row of this type, ever (e.g.
    "has this Passing EVER been called" -- the Called/Visited filters,
    spec #15). Deliberately not "the MOST RECENT activity was this type"
    -- a lead that was called and then emailed should still count as
    Called. Returns an empty list (never raises) on failure -- app.py
    treats that the same as "no matches", never a crash."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT place_id FROM passing_activity WHERE activity_type = %s",
                (activity_type,),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_place_ids_by_rep(rep_display_name):
    """place_ids with at least one activity row logged by this rep, ever
    -- the Rep filter (spec #15). Returns an empty list (never raises) on
    failure."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT place_id FROM passing_activity WHERE rep_display_name = %s",
                (rep_display_name,),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_place_ids_by_last_activity_range(last_activity_from=None, last_activity_to=None):
    """place_ids whose MOST RECENT activity falls within [from, to]
    (either bound optional) -- the Last Activity date-range filter
    (spec #15), which is genuinely about recency, unlike Called/Visited
    above. Returns an empty list (never raises) on failure."""
    if not last_activity_from and not last_activity_to:
        return []
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (place_id) place_id, activity_at
                FROM passing_activity
                ORDER BY place_id, activity_at DESC, id DESC
                """
            )
            place_ids = []
            for place_id, activity_at in cur.fetchall():
                activity_date = activity_at.date()
                if last_activity_from and activity_date < last_activity_from:
                    continue
                if last_activity_to and activity_date > last_activity_to:
                    continue
                place_ids.append(place_id)
        return place_ids
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_all_place_ids_with_activity():
    """Every place_id with at least one activity row, ever -- used for the
    Has Activity/No Activity filter and as the Outreach Coverage numerator
    input (see passings_data.get_kpis()'s `activity_place_ids` argument).
    Returns an empty list (never raises) on failure."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT place_id FROM passing_activity")
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def get_reps_with_activity():
    """Distinct rep display names that have ever logged activity -- for
    the Rep filter dropdown. Returns an empty list (never raises) on
    failure."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT rep_display_name FROM passing_activity "
                "WHERE rep_display_name IS NOT NULL ORDER BY rep_display_name"
            )
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()
