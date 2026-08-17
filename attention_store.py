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
"""

import db

ATTENTION_STATUSES = [
    "Duplicate",
    "Cancellation",
    "Engineering Issues",
    "Underground Issues",
    "Existing Customer",
    "Other",
]
ATTENTION_STATUS_SET = set(ATTENTION_STATUSES)

MAX_NOTE_LENGTH = 2000
MAX_AUTHOR_LENGTH = 120

# account_attention: one row per account that has ever been classified.
# sale_id is the primary key -- an INSERT ... ON CONFLICT (sale_id) DO
# UPDATE upsert (see set_attention_status()) means a second classification
# of the same account always updates this one row rather than creating a
# duplicate, satisfying "no duplicate attention record ... for the same
# account" structurally, not just by convention.
#
# account_attention_notes: append-only activity history, one row per note,
# never updated or deleted. `attention_status` is the status in effect
# when the note was written (every note requires a status to already
# exist, so this is always set); `previous_status` is set only when this
# note accompanied an actual status change, which is what lets the UI
# render a "changed from X to Y" audit line without a separate table.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS account_attention (
    sale_id BIGINT PRIMARY KEY,
    attention_status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS account_attention_notes (
    id SERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES account_attention(sale_id),
    note TEXT NOT NULL,
    attention_status TEXT NOT NULL,
    previous_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_account_attention_notes_sale_id
    ON account_attention_notes (sale_id, created_at DESC);
"""

# Idempotent (CREATE TABLE/INDEX IF NOT EXISTS), so re-running it is
# always safe -- this flag is purely an optimization to skip it on every
# request once we know it has succeeded at least once in this process.
# Reset to False on failure so the next request retries rather than
# permanently assuming the schema exists after a transient connection
# error during startup.
_schema_ready = False


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
    _schema_ready = True


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


def get_attention_overview(sale_ids):
    """Everything the Needs Attention Bulk Account View needs to render,
    in one connection (two queries): (available, status_map, notes_map,
    progress).

    - status_map: {sale_id: attention_status}. A sale_id with no entry is
      Unclassified -- callers should use .get(sale_id) and treat a
      missing key / None the same way.
    - notes_map: {sale_id: [note dicts, newest first]}. A note dict has
      note/attention_status/previous_status/created_at/created_by.
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
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sale_id, attention_status FROM account_attention WHERE sale_id = ANY(%s)",
                (sale_ids,),
            )
            status_map = {row[0]: row[1] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT sale_id, note, attention_status, previous_status, created_at, created_by
                FROM account_attention_notes
                WHERE sale_id = ANY(%s)
                ORDER BY created_at DESC, id DESC
                """,
                (sale_ids,),
            )
            notes_map = {}
            for sale_id, note, status, previous_status, created_at, created_by in cur.fetchall():
                notes_map.setdefault(sale_id, []).append({
                    "note": note,
                    "attention_status": status,
                    "previous_status": previous_status,
                    "created_at": created_at,
                    "created_by": created_by,
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
        global _schema_ready
        _schema_ready = False
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


def set_attention_status(sale_id, status, note, author=None):
    """Set (or change) an account's Attention Status. A note is always
    required -- this is the ONLY way attention_status is ever written, so
    "a status can't exist without a note" is enforced structurally by
    this being the sole write path, not by a separate check elsewhere.
    Validates status server-side against ATTENTION_STATUS_SET regardless
    of what the frontend already checked. Returns (ok, error, notes) --
    `notes` (newest first) is populated on success so the caller can
    update the UI without a second round trip."""
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

    author = (author or "").strip()[:MAX_AUTHOR_LENGTH] or None

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_schema(conn)
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
                    (sale_id, status, author),
                )
                cur.execute(
                    """
                    INSERT INTO account_attention_notes
                        (sale_id, note, attention_status, previous_status, created_by)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (sale_id, note, status, previous_status if changed else None, author),
                )

        _, notes = get_notes(sale_id)
        return True, None, notes
    except Exception as exc:
        global _schema_ready
        _schema_ready = False
        return False, db.sanitize_error(exc), []
    finally:
        if conn is not None:
            conn.close()


def add_note(sale_id, note, author=None):
    """Append a note without changing Attention Status. Requires the
    account to already be classified -- an account with no status yet has
    nothing to "add another note" to; set_attention_status() is the only
    entry point for an account's first note. Returns (ok, error, notes)."""
    try:
        sale_id = int(sale_id)
    except (TypeError, ValueError):
        return False, "Invalid account.", []

    note = (note or "").strip()
    if not note:
        return False, "Note cannot be blank.", []
    if len(note) > MAX_NOTE_LENGTH:
        return False, f"Note is too long (max {MAX_NOTE_LENGTH} characters).", []

    author = (author or "").strip()[:MAX_AUTHOR_LENGTH] or None

    conn = None
    try:
        conn = db.get_appdb_connection()
        _ensure_schema(conn)
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
                    INSERT INTO account_attention_notes (sale_id, note, attention_status, created_by)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (sale_id, note, current_status, author),
                )

        _, notes = get_notes(sale_id)
        return True, None, notes
    except Exception as exc:
        global _schema_ready
        _schema_ready = False
        return False, db.sanitize_error(exc), []
    finally:
        if conn is not None:
            conn.close()
