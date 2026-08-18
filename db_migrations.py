"""Lightweight, versioned SQL migration runner for the app-owned `appdb`
Postgres database (db.get_appdb_connection()).

There is no ORM or migration framework anywhere in this codebase (no
SQLAlchemy/Alembic/Django) -- every other module that talks to a database
does it with raw parameterized SQL (see attention_store.py, the first
module to write to appdb). Introducing a whole new framework just for
this would cut against that established pattern, so this is the smallest
thing that gives real migration behavior: a numbered, forward-only list
of SQL blocks, each applied exactly once inside its own transaction,
tracked in a `schema_migrations` table.

Add a new migration by appending a new `(version, description, sql)`
tuple to MIGRATIONS -- never edit an already-applied migration's `sql`
after it has shipped (a database that already recorded that version
would never see the edited SQL); ship a new migration instead, even for
a one-line fix.

Every module that owns appdb tables calls `ensure_schema(conn)` before
its first query in a process, passing the SAME connection it's about to
run its own queries on (so this never opens a second connection per
request). Safe to call many times -- after the first successful run in
this process, later calls are a fast no-op via `_schema_ready`, reset on
any failure so the next call retries instead of permanently assuming the
schema exists after a transient connection error."""

_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# (version, description, sql). Applied in order; a version already
# recorded in schema_migrations is skipped.
MIGRATIONS = [
    (
        1,
        "account_attention + account_attention_notes (Needs Attention workflow, 2026-08-17)",
        """
        CREATE TABLE IF NOT EXISTS account_attention (
            sale_id BIGINT PRIMARY KEY,
            attention_status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by TEXT
        );

        CREATE TABLE IF NOT EXISTS account_attention_notes (
            id SERIAL PRIMARY KEY,
            sale_id BIGINT NOT NULL,
            note TEXT NOT NULL,
            attention_status TEXT NOT NULL,
            previous_status TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by TEXT
        );

        -- sale_id is deliberately NOT a foreign key into account_attention
        -- (see attention_store.py) -- reclassifying an account to
        -- Unclassified deletes its account_attention row, and notes must
        -- survive that.
        ALTER TABLE account_attention_notes
            DROP CONSTRAINT IF EXISTS account_attention_notes_sale_id_fkey;

        CREATE INDEX IF NOT EXISTS idx_account_attention_notes_sale_id
            ON account_attention_notes (sale_id, created_at DESC);
        """,
    ),
    (
        2,
        "sales_reps, users, account_setup_tokens, needs_attention_tracking + audit columns (auth/roles/admin, 2026-08-18)",
        """
        -- The first real, persisted Sales Rep entity -- previously a rep
        -- was only ever a string derived at read time from SourceToken
        -- (extract_rep_name() in sales_metrics.py). Rows are auto-synced
        -- from source data (see user_store.sync_sales_reps()), never
        -- created by hand -- a rep always already exists here by the
        -- time an Admin maps a user to one.
        CREATE TABLE IF NOT EXISTS sales_reps (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- A User is NEVER the same thing as a Sales Rep -- sales_rep_id
        -- is an optional mapping, not an identity. A rep can exist with
        -- zero users; a user (Admin/Customer Success/Other) can exist
        -- with no rep mapping at all.
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            role TEXT NOT NULL,
            sales_rep_id INTEGER REFERENCES sales_reps(id),
            status TEXT NOT NULL DEFAULT 'pending',
            last_login_at TIMESTAMPTZ,
            last_needs_attention_activity_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Covers both first-time account setup and password reset --
        -- one table, `purpose` distinguishes them, since the two flows
        -- are structurally identical (random token -> hashed -> expires
        -- -> single-use). token_hash stores only a hash; the raw token
        -- exists solely in the emailed URL, never persisted.
        CREATE TABLE IF NOT EXISTS account_setup_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_account_setup_tokens_user_id
            ON account_setup_tokens (user_id);

        -- The missing needs_attention_since -- Needs Attention status is
        -- otherwise recomputed fresh from source data on every request
        -- (build_sales_dataset()), so without this there is nowhere to
        -- read "how long has this account actually been in Needs
        -- Attention" from. One row per account currently in Needs
        -- Attention; deleted (not updated) when an account leaves Needs
        -- Attention, so a later re-entry starts a fresh 15-day clock --
        -- see permissions.py / user_store.sync_needs_attention_tracking().
        CREATE TABLE IF NOT EXISTS needs_attention_tracking (
            sale_id BIGINT PRIMARY KEY,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- account_attention_notes becomes the Needs Attention audit log
        -- required by the spec, rather than a second, largely duplicate
        -- table -- it already has previous/new status, note text, and a
        -- timestamp; these three columns are what it was missing.
        ALTER TABLE account_attention_notes ADD COLUMN IF NOT EXISTS acting_user_id INTEGER REFERENCES users(id);
        ALTER TABLE account_attention_notes ADD COLUMN IF NOT EXISTS owner_sales_rep TEXT;
        ALTER TABLE account_attention_notes ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'note';
        """,
    ),
]

_schema_ready = False


def ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_MIGRATIONS_TABLE_SQL)
                cur.execute("SELECT version FROM schema_migrations")
                applied = {row[0] for row in cur.fetchall()}

        for version, description, sql in MIGRATIONS:
            if version in applied:
                continue
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, description) VALUES (%s, %s)",
                        (version, description),
                    )
        _schema_ready = True
    except Exception:
        _schema_ready = False
        raise
