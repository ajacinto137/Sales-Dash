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


# ================================================================
# "Residential Address" marks (added 2026-10-20, by request)
# ================================================================
# The one deliberate exception to "nothing here influences
# classification": a rep can mark a Commercial Hot Lead / Field Target as
# really being a house, and passings_cache.py then classifies that place
# as residential (Residential Lead if it has a valid email, else none) so
# the commercial team isn't sent there. Undo deletes the mark.

def get_residential_marks():
    """(available, {place_id: {"marked_by_name", "marked_at"}}). Never
    raises; available=False when appdb is unreachable."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT place_id, marked_by_name, marked_at FROM passing_residential_marks")
            return True, {
                place_id: {"marked_by_name": name, "marked_at": marked_at}
                for place_id, name, marked_at in cur.fetchall()
            }
    except Exception:
        return False, {}
    finally:
        if conn is not None:
            conn.close()


def set_residential_mark(place_id, residential, user):
    """Marks (residential=True) or un-marks a place. Returns (ok, error)."""
    try:
        place_id = int(place_id)
    except (TypeError, ValueError):
        return False, "Invalid Passing."
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn, conn.cursor() as cur:
            if residential:
                cur.execute(
                    """
                    INSERT INTO passing_residential_marks (place_id, marked_by_user_id, marked_by_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (place_id) DO NOTHING
                    """,
                    (place_id, user.get("id") if user else None, _display_name(user)),
                )
            else:
                cur.execute("DELETE FROM passing_residential_marks WHERE place_id = %s", (place_id,))
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


# ================================================================
# Check Out (added 2026-10-27, by request)
# ================================================================
# Selected Passings & Leads cards can be checked out to a sales rep. The
# cards stay on /passings (with a "Checked out to ..." badge) and also
# show on that rep's Rep Profile "Checked Out" tab until returned.

MAX_CHECKOUT_BATCH = 500


def get_checkouts(rep_name=None):
    """(available, {place_id: {"rep_name", "checked_out_by_name",
    "checked_out_at", "bucket_id"}}) -- every checkout, or only one rep's. Never
    raises; available=False when appdb is unreachable."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            sql = "SELECT place_id, rep_name, checked_out_by_name, checked_out_at, bucket_id FROM passing_checkouts"
            if rep_name is not None:
                cur.execute(sql + " WHERE rep_name = %s", (rep_name,))
            else:
                cur.execute(sql)
            return True, {
                place_id: {"rep_name": rep, "checked_out_by_name": by, "checked_out_at": at, "bucket_id": bucket_id}
                for place_id, rep, by, at, bucket_id in cur.fetchall()
            }
    except Exception:
        return False, {}
    finally:
        if conn is not None:
            conn.close()


def check_out(place_ids, sales_rep_id, user):
    """Checks places out to one sales rep (a place already checked out
    moves to this rep, leaving the old rep's bucket). Returns (ok, error,
    count)."""
    place_ids = _clean_place_ids(place_ids)
    if not place_ids:
        return False, "Select at least one account.", 0
    if len(place_ids) > MAX_CHECKOUT_BATCH:
        return False, f"Check out at most {MAX_CHECKOUT_BATCH} accounts at a time.", 0
    try:
        sales_rep_id = int(sales_rep_id)
    except (TypeError, ValueError):
        return False, "Choose a sales rep.", 0
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn, conn.cursor() as cur:
            cur.execute("SELECT name FROM sales_reps WHERE id = %s", (sales_rep_id,))
            rep = cur.fetchone()
            if not rep:
                return False, "Choose a sales rep.", 0
            by_id, by_name = (user.get("id") if user else None), _display_name(user)
            for place_id in place_ids:
                cur.execute(
                    """
                    INSERT INTO passing_checkouts
                        (place_id, sales_rep_id, rep_name, checked_out_by_user_id, checked_out_by_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (place_id) DO UPDATE SET
                        sales_rep_id = EXCLUDED.sales_rep_id, rep_name = EXCLUDED.rep_name,
                        checked_out_by_user_id = EXCLUDED.checked_out_by_user_id,
                        checked_out_by_name = EXCLUDED.checked_out_by_name, checked_out_at = now(),
                        bucket_id = CASE WHEN passing_checkouts.rep_name = EXCLUDED.rep_name
                                         THEN passing_checkouts.bucket_id END
                    """,
                    (place_id, sales_rep_id, rep[0], by_id, by_name),
                )
        return True, None, len(place_ids)
    except Exception as exc:
        return False, db.sanitize_error(exc), 0
    finally:
        if conn is not None:
            conn.close()


def return_checkout(place_id):
    """Returns (un-checks-out) one place. Returns (ok, error)."""
    try:
        place_id = int(place_id)
    except (TypeError, ValueError):
        return False, "Invalid Passing."
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM passing_checkouts WHERE place_id = %s", (place_id,))
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


# ---- Buckets: a rep profile's own groups for its checked-out cards ----
# Scoped to one rep_name and only shown on that Rep Profile's Checked Out
# tab -- never on /passings or another rep's profile (by request, so
# nobody sees buckets they don't know about).

MAX_BUCKET_NAME_LENGTH = 60


def get_buckets(rep_name):
    """(available, [{"id", "name"}, ...]) oldest first. Never raises."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM passing_checkout_buckets WHERE rep_name = %s ORDER BY created_at, id",
                (rep_name,),
            )
            return True, [{"id": i, "name": n} for i, n in cur.fetchall()]
    except Exception:
        return False, []
    finally:
        if conn is not None:
            conn.close()


def create_bucket(rep_name, name):
    """Returns (ok, error, bucket)."""
    name = " ".join((name or "").split())
    if not name:
        return False, "Name the bucket.", None
    if len(name) > MAX_BUCKET_NAME_LENGTH:
        return False, f"Bucket names are at most {MAX_BUCKET_NAME_LENGTH} characters.", None
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM passing_checkout_buckets WHERE rep_name = %s AND lower(name) = lower(%s)",
                (rep_name, name),
            )
            if cur.fetchone():
                return False, "That bucket already exists.", None
            cur.execute(
                "INSERT INTO passing_checkout_buckets (rep_name, name) VALUES (%s, %s) RETURNING id",
                (rep_name, name),
            )
            return True, None, {"id": cur.fetchone()[0], "name": name}
    except Exception as exc:
        return False, db.sanitize_error(exc), None
    finally:
        if conn is not None:
            conn.close()


def delete_bucket(rep_name, bucket_id):
    """Deletes one of this rep's buckets; its cards go back to Unsorted.
    Returns (ok, error)."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM passing_checkout_buckets WHERE id = %s AND rep_name = %s",
                (int(bucket_id), rep_name),
            )
        return True, None
    except (TypeError, ValueError):
        return False, "Invalid bucket."
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def set_checkout_bucket(rep_name, place_id, bucket_id):
    """Puts one of this rep's checked-out cards in one of this rep's
    buckets (bucket_id None = Unsorted). Returns (ok, error)."""
    try:
        place_id = int(place_id)
        bucket_id = int(bucket_id) if bucket_id not in (None, "") else None
    except (TypeError, ValueError):
        return False, "Invalid bucket."
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn, conn.cursor() as cur:
            if bucket_id is not None:
                cur.execute(
                    "SELECT 1 FROM passing_checkout_buckets WHERE id = %s AND rep_name = %s",
                    (bucket_id, rep_name),
                )
                if not cur.fetchone():
                    return False, "Invalid bucket."
            cur.execute(
                "UPDATE passing_checkouts SET bucket_id = %s WHERE place_id = %s AND rep_name = %s",
                (bucket_id, place_id, rep_name),
            )
            if cur.rowcount == 0:
                return False, "That account isn't checked out to this rep."
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
