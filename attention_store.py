"""App-owned persistence for the Needs Attention workflow.

Sales reps can attach an Attention Status and freeform notes to a Needs
Attention account (e.g. "Engineering Issues" + "Engineering confirmed
additional design work is required"). This is pure workflow metadata --
it is NEVER allowed to influence which accounts are classified as Needs
Attention (build_sales_dataset() in sales_metrics.py) or the Install Rate
formula (calculate_rep_metrics()). Nothing in this module is imported by
sales_metrics.py, and nothing here recomputes or overrides a `status`
value -- it only reads/writes a completely separate table, keyed by the
account's `sale_id` (Main Sales' own primary key, FTTPFormData.ID).

Why `sale_id` and not `subscriber_uuid`: the task that introduced this
feature suggested preferring the existing subscriber UUID
(vi_subscriber_uuid) already used for Vision links. Checked against the
live data before committing to it: ~6.5% of Needs Attention accounts
(42 of 647 at the time of writing) have no subscriber_uuid at all --
exactly the not-yet-installed population this feature exists to serve,
since that UUID is only reliably populated once an account is further
along. `sale_id` is the Main Sales row's own database primary key -- 0
nulls, 100% unique, guaranteed present for every row in the normalized
dataset -- so it's the only identifier that lets every Needs Attention
account be classified, not just the ~93% that happen to have a UUID.

Writes go to a dedicated "appdb" Postgres database/Docker service (see
db.get_appdb_connection(), docker-compose.yml) -- NOT the KPI PostgreSQL
database. KPI is one of this app's two read-only source databases (see
README.md); this task's instructions were explicit not to assume
permission to write into an existing business database, so this feature
gets its own database instead, purely app-owned from the start.

Every public function here degrades gracefully: if the appdb is
unreachable, read functions return an "unavailable" flag (never raise,
never silently pretend zero accounts are classified) and write functions
return a clear ok=False/error tuple. A down appdb must never crash the
Rep Profile or affect Needs Attention/Install Rate, which are computed
entirely from the source data and don't touch this module at all.

Auth-aware since 2026-08-18: set_attention_status()/add_note() now take
a `user` dict (see auth.py's current_user()) identifying who actually
performed the action, instead of a free-text "author" name typed into
the browser. `acting_user_id` is who acted; `owner_sales_rep` (passed in
separately by the caller, since this module never touches
sales_metrics.py) is a snapshot of who owned the account at write time.
The two are never the same column -- see README.md "Needs Attention
Ownership" for why that distinction matters (working an account never
moves it to the acting user's book of business)."""

import db
import db_migrations

ATTENTION_STATUSES = [
    "Duplicate",
    "Cancellation",
    "Engineering Issues",
    "Underground Issues",
    "Existing Customer",
    "Other",
    "Called",
    "Re-Sold",
]
ATTENTION_STATUS_SET = set(ATTENTION_STATUSES)

MAX_NOTE_LENGTH = 2000


def _reclassify_removed_statuses(conn):
    """Safety net for whenever ATTENTION_STATUSES drops a value that some
    account_attention row still holds -- e.g. a category is removed or
    renamed. Deletes the account_attention row for any such account,
    moving it back to Unclassified -- account_attention_notes is never
    touched, so its full history/audit trail survives untouched. A no-op
    in the common case (every write already validates against the
    approved list, so nothing becomes orphaned by normal use)."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM account_attention WHERE attention_status NOT IN %s",
                (tuple(ATTENTION_STATUSES),),
            )


# Guards _reclassify_removed_statuses() so it runs at most once per
# process (like db_migrations' own _schema_ready) rather than on every
# single request.
_reclassify_done = False


def _ensure_ready(conn):
    global _reclassify_done
    db_migrations.ensure_schema(conn)
    if not _reclassify_done:
        _reclassify_removed_statuses(conn)
        _reclassify_done = True


def _clean_sale_ids(sale_ids):
    cleaned = set()
    for value in sale_ids:
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


def get_attention_overview(sale_ids):
    """Everything the Needs Attention Bulk Account View needs to render,
    in one connection (two queries): (available, status_map, notes_map,
    progress).

    - status_map: {sale_id: attention_status}. A sale_id with no entry is
      Unclassified -- callers should use .get(sale_id) and treat a
      missing key / None the same way.
    - notes_map: {sale_id: [note dicts, newest first]}. A note dict has
      note/attention_status/previous_status/created_at/created_by/
      action/owner_sales_rep.
    - progress: {"total", "addressed", "remaining", "by_status"} -- pure
      workflow metrics derived from status_map. NEVER feed these back
      into Needs Attention/Install Rate; they exist only to answer "how
      far through the list is this rep."

    available=False (appdb unreachable) returns empty maps and a None
    progress -- callers must render every account as Unclassified with an
    "Attention Status/Notes are temporarily unavailable" notice, not
    crash and not imply the population is actually unclassified."""
    sale_ids = _clean_sale_ids(sale_ids)
    if not sale_ids:
        return True, {}, {}, {"total": 0, "addressed": 0, "remaining": 0, "by_status": {}}

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sale_id, attention_status FROM account_attention WHERE sale_id = ANY(%s)",
                (sale_ids,),
            )
            status_map = {row[0]: row[1] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT sale_id, note, attention_status, previous_status, created_at,
                       created_by, action, owner_sales_rep
                FROM account_attention_notes
                WHERE sale_id = ANY(%s)
                ORDER BY created_at DESC, id DESC
                """,
                (sale_ids,),
            )
            notes_map = {}
            for sale_id, note, status, previous_status, created_at, created_by, action, owner_sales_rep in cur.fetchall():
                notes_map.setdefault(sale_id, []).append({
                    "note": note,
                    "attention_status": status,
                    "previous_status": previous_status,
                    "created_at": created_at,
                    "created_by": created_by,
                    "action": action,
                    "owner_sales_rep": owner_sales_rep,
                })

        addressed = sum(1 for sid in sale_ids if status_map.get(sid))
        by_status = {}
        for sid in sale_ids:
            status = status_map.get(sid)
            if status:
                by_status[status] = by_status.get(status, 0) + 1

        progress = {
            "total": len(sale_ids),
            "addressed": addressed,
            "remaining": len(sale_ids) - addressed,
            "by_status": by_status,
        }
        return True, status_map, notes_map, progress
    except Exception:
        return False, {}, {}, None
    finally:
        if conn is not None:
            conn.close()


def get_notes(sale_id):
    """Notes for one account, newest first. Returns (available, notes) --
    same availability convention as get_attention_overview()."""
    available, _status_map, notes_map, _progress = get_attention_overview([sale_id])
    if not available:
        return False, []
    return True, notes_map.get(int(sale_id), [])


def get_recent_activity(limit=300):
    """The Needs Attention audit log (spec: NeedsAttentionAudit) -- reads
    straight from account_attention_notes rather than a second, largely
    duplicate table (see this module's docstring). One row per note/
    status-change, newest first, joined against `users` for the acting
    user's email (a pre-auth historical row has acting_user_id NULL and
    falls back to its own `created_by` text). Empty list (never raises)
    if appdb is unreachable -- /admin/audit shows an "unavailable"
    notice rather than crashing."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.sale_id, n.note, n.attention_status, n.previous_status,
                       n.created_at, n.created_by, n.action, n.owner_sales_rep,
                       u.email AS acting_user_email
                FROM account_attention_notes n
                LEFT JOIN users u ON u.id = n.acting_user_id
                ORDER BY n.created_at DESC, n.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [
                {
                    "sale_id": row[0],
                    "note": row[1],
                    "attention_status": row[2],
                    "previous_status": row[3],
                    "created_at": row[4],
                    "created_by": row[5],
                    "action": row[6],
                    "owner_sales_rep": row[7],
                    "acting_user_email": row[8],
                }
                for row in cur.fetchall()
            ]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def set_attention_status(sale_id, status, note, user, owner_rep=None):
    """Set (or change) an account's Attention Status. A note is always
    required -- this is the ONLY way attention_status is ever written, so
    "a status can't exist without a note" is enforced structurally by
    this being the sole write path, not by a separate check elsewhere.
    Validates status server-side against ATTENTION_STATUS_SET regardless
    of what the frontend already checked.

    `user` is the acting user's dict (auth.current_user()) -- permission
    checks (can this user touch this account) are the CALLER's
    responsibility (see permissions.can_work_account()); this function
    only records who acted, it does not enforce who is allowed to.
    `owner_rep` is a point-in-time snapshot of the account's owning rep
    (from sales_metrics.py) for audit display -- never used for
    permission decisions here, and never fed back into that rep's own
    attribution.

    Returns (ok, error, notes) -- `notes` (newest first) is populated on
    success so the caller can update the UI without a second round trip."""
    try:
        sale_id = int(sale_id)
    except (TypeError, ValueError):
        return False, "Invalid account.", []

    if status not in ATTENTION_STATUS_SET:
        return False, "Invalid Attention Status.", []

    note = (note or "").strip()
    if not note:
        return False, "A note is required when setting or changing Attention Status.", []
    if len(note) > MAX_NOTE_LENGTH:
        return False, f"Note is too long (max {MAX_NOTE_LENGTH} characters).", []

    acting_user_id = user.get("id") if user else None
    display_name = _display_name(user)

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT attention_status FROM account_attention WHERE sale_id = %s FOR UPDATE",
                    (sale_id,),
                )
                row = cur.fetchone()
                previous_status = row[0] if row else None
                changed = previous_status is not None and previous_status != status

                cur.execute(
                    """
                    INSERT INTO account_attention (sale_id, attention_status, updated_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (sale_id) DO UPDATE
                        SET attention_status = EXCLUDED.attention_status,
                            updated_at = now(),
                            updated_by = EXCLUDED.updated_by
                    """,
                    (sale_id, status, display_name),
                )
                cur.execute(
                    """
                    INSERT INTO account_attention_notes
                        (sale_id, note, attention_status, previous_status, created_by,
                         acting_user_id, owner_sales_rep, action)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'status_change')
                    """,
                    (sale_id, note, status, previous_status if changed else None, display_name,
                     acting_user_id, owner_rep),
                )

        _, notes = get_notes(sale_id)
        return True, None, notes
    except Exception as exc:
        return False, db.sanitize_error(exc), []
    finally:
        if conn is not None:
            conn.close()


def add_note(sale_id, note, user, owner_rep=None):
    """Append a note without changing Attention Status. Requires the
    account to already be classified -- an account with no status yet has
    nothing to "add another note" to; set_attention_status() is the only
    entry point for an account's first note. See set_attention_status()
    for `user`/`owner_rep`. Returns (ok, error, notes)."""
    try:
        sale_id = int(sale_id)
    except (TypeError, ValueError):
        return False, "Invalid account.", []

    note = (note or "").strip()
    if not note:
        return False, "Note cannot be blank.", []
    if len(note) > MAX_NOTE_LENGTH:
        return False, f"Note is too long (max {MAX_NOTE_LENGTH} characters).", []

    acting_user_id = user.get("id") if user else None
    display_name = _display_name(user)

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_ready(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT attention_status FROM account_attention WHERE sale_id = %s",
                    (sale_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return False, "This account has not been classified yet -- set an Attention Status first.", []
                current_status = row[0]

                cur.execute(
                    """
                    INSERT INTO account_attention_notes
                        (sale_id, note, attention_status, created_by, acting_user_id, owner_sales_rep, action)
                    VALUES (%s, %s, %s, %s, %s, %s, 'note')
                    """,
                    (sale_id, note, current_status, display_name, acting_user_id, owner_rep),
                )

        _, notes = get_notes(sale_id)
        return True, None, notes
    except Exception as exc:
        return False, db.sanitize_error(exc), []
    finally:
        if conn is not None:
            conn.close()
