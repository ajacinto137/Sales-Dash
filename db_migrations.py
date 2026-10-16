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
    (
        3,
        "sales_reps.team (Sales Rep Roles/Teams -- Junior/NJ/NY/VA, 2026-08-19)",
        """
        -- A rep's Sales Rep Group for org/permissions/dashboard filtering
        -- (Sales Volume Over Time's team selector -- see
        -- calculate_sales_volume_trend() in sales_metrics.py). Lives on
        -- sales_reps, NOT users -- a rep can (and often does) have zero
        -- linked users (see README.md "User Records vs Sales Rep
        -- Records"), so a users-table column could never cover every
        -- rep that needs a team. Nullable and no default on purpose: an
        -- existing rep with no team assigned yet must stay unassigned
        -- (excluded from every team-filtered view) rather than being
        -- silently defaulted into a team that could misrepresent their
        -- reporting -- an Admin must assign it explicitly (Admin
        -- Portal's Sales Reps tab). Validated against
        -- user_store.SALES_REP_TEAMS in application code only, same
        -- convention as users.role/ROLE_SET -- no DB-level CHECK.
        -- Changing this value never touches sale rows, account_attention,
        -- or needs_attention_tracking -- Needs Attention/Install Rate/etc.
        -- are computed straight from source data, unaware this column
        -- exists.
        ALTER TABLE sales_reps ADD COLUMN IF NOT EXISTS team TEXT;
        """,
    ),
    (
        4,
        "passing_activity (Passings & Leads outreach workflow, 2026-08-20)",
        """
        -- Append-only outreach history for the Passings & Leads feature
        -- (README.md "Passings & Leads"), keyed by PlanetWeb's
        -- View_Places.ID ("PlaceID"/AuxVar5) rather than a sale_id --
        -- Passings are not sales, so this is deliberately a new table
        -- rather than reusing account_attention[_notes]. No mutable
        -- "current status" row like account_attention has: every logged
        -- activity is a permanent fact, and Last Activity is always
        -- derived from the latest row (see
        -- passings_activity_store.get_last_activity_map()), never stored
        -- redundantly. Pure workflow metadata -- see
        -- passings_activity_store.py's module docstring: this table is
        -- NEVER allowed to influence a Passing's classification.
        CREATE TABLE IF NOT EXISTS passing_activity (
            id SERIAL PRIMARY KEY,
            place_id BIGINT NOT NULL,
            activity_type TEXT NOT NULL,
            activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            note TEXT,
            user_id INTEGER REFERENCES users(id),
            rep_display_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Supports get_last_activity_map()/get_activity_history()'s
        -- "latest row per place_id" queries and the Last Activity
        -- date-range filter.
        CREATE INDEX IF NOT EXISTS idx_passing_activity_place_id_activity_at
            ON passing_activity (place_id, activity_at DESC);

        -- Supports the Called/Visited filters (spec #15) without a full
        -- table scan.
        CREATE INDEX IF NOT EXISTS idx_passing_activity_activity_type
            ON passing_activity (activity_type);

        -- Supports the Rep filter.
        CREATE INDEX IF NOT EXISTS idx_passing_activity_rep_display_name
            ON passing_activity (rep_display_name);
        """,
    ),
    (
        5,
        "hubspot_user_mappings, hubspot_calls, hubspot_dispositions, hubspot_sync_state, hubspot_sync_runs (HubSpot Outbound Call Analytics, 2026-10-16)",
        """
        -- One row per HubSpot OWNER (hubspot_owner_id is the owner `id`
        -- from /crm/v3/owners -- what a call's hubspot_owner_id points
        -- at -- never the separate HubSpot user id, kept only for
        -- reference in hubspot_user_id). sales_rep_id is what puts calls
        -- on a Rep Profile (profiles are per sales_reps row, see
        -- migration 2); dashboard_user_id records the users row an email
        -- match came from. Both nullable: an owner can be unmatched.
        -- mapping_method 'manual' is never overwritten by auto-matching
        -- (see hubspot_store.refresh_owners_and_mappings()).
        CREATE TABLE IF NOT EXISTS hubspot_user_mappings (
            id SERIAL PRIMARY KEY,
            hubspot_owner_id TEXT NOT NULL UNIQUE,
            hubspot_user_id TEXT,
            hubspot_email TEXT,
            hubspot_name TEXT,
            hubspot_archived BOOLEAN NOT NULL DEFAULT false,
            dashboard_user_id INTEGER REFERENCES users(id),
            sales_rep_id INTEGER REFERENCES sales_reps(id),
            mapping_method TEXT NOT NULL DEFAULT 'none',
            mapping_status TEXT NOT NULL DEFAULT 'unmatched',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_synced_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_hubspot_user_mappings_sales_rep_id
            ON hubspot_user_mappings (sales_rep_id);

        -- Every call HubSpot returns, all directions (outbound filtering
        -- happens at query time, so a direction fix in HubSpot is picked
        -- up by the next incremental sync instead of being lost).
        -- call_timestamp is hs_timestamp as UTC; grouping/filtering
        -- converts to America/New_York in SQL.
        CREATE TABLE IF NOT EXISTS hubspot_calls (
            hubspot_call_id TEXT PRIMARY KEY,
            hubspot_owner_id TEXT,
            call_timestamp TIMESTAMPTZ,
            direction TEXT,
            duration_ms BIGINT,
            disposition TEXT,
            status TEXT,
            to_number TEXT,
            from_number TEXT,
            title TEXT,
            hubspot_created_at TIMESTAMPTZ,
            hubspot_modified_at TIMESTAMPTZ,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_hubspot_calls_owner_timestamp
            ON hubspot_calls (hubspot_owner_id, call_timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_hubspot_calls_timestamp
            ON hubspot_calls (call_timestamp);

        -- hs_call_disposition is a GUID; labels come from HubSpot's
        -- dispositions endpoint so "Connected" is read off HubSpot's own
        -- definition rather than guessed from a hardcoded id.
        CREATE TABLE IF NOT EXISTS hubspot_dispositions (
            disposition_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Single-row (id = 1) integration state: the incremental-sync
        -- watermark and the last successful/attempted sync.
        CREATE TABLE IF NOT EXISTS hubspot_sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            calls_modified_watermark TIMESTAMPTZ,
            initial_sync_completed_at TIMESTAMPTZ,
            last_success_at TIMESTAMPTZ,
            last_attempt_at TIMESTAMPTZ,
            last_error TEXT,
            connection_ok BOOLEAN
        );
        INSERT INTO hubspot_sync_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

        CREATE TABLE IF NOT EXISTS hubspot_sync_runs (
            id SERIAL PRIMARY KEY,
            trigger TEXT NOT NULL,
            kind TEXT,
            status TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            owners_retrieved INTEGER,
            records_processed INTEGER NOT NULL DEFAULT 0,
            new_calls INTEGER NOT NULL DEFAULT 0,
            updated_calls INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hubspot_sync_runs_started_at
            ON hubspot_sync_runs (started_at DESC);
        """,
    ),
    (
        6,
        "app_secrets (HubSpot service key entered in the Admin Portal, 2026-10-16)",
        """
        -- Secrets an Admin enters in the UI instead of .env (today only
        -- the HubSpot service key). encrypted_value is Fernet ciphertext
        -- keyed from SECRET_KEY (see app_secrets.py) -- never plaintext.
        CREATE TABLE IF NOT EXISTS app_secrets (
            name TEXT PRIMARY KEY,
            encrypted_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by INTEGER REFERENCES users(id)
        );
        """,
    ),
    (
        7,
        "hubspot_calls recording/contact, hubspot_contacts, portal info + one-time call re-read (Call History links, 2026-10-16)",
        """
        -- Call History's Recording + Contact links. recording_url is
        -- hs_call_recording_url (HubSpot-hosted, opens for a signed-in
        -- HubSpot user); contact_id is the call's associated contact
        -- (first one, via the v4 associations API).
        ALTER TABLE hubspot_calls ADD COLUMN IF NOT EXISTS recording_url TEXT;
        ALTER TABLE hubspot_calls ADD COLUMN IF NOT EXISTS contact_id TEXT;

        CREATE TABLE IF NOT EXISTS hubspot_contacts (
            contact_id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Portal id + UI domain build https://<ui_domain>/contacts/<portal_id>/record/0-1/<contact>.
        ALTER TABLE hubspot_sync_state ADD COLUMN IF NOT EXISTS portal_id TEXT;
        ALTER TABLE hubspot_sync_state ADD COLUMN IF NOT EXISTS ui_domain TEXT;

        -- Backfill: calls already synced have no recording/contact yet.
        -- Clearing this makes the next sync a full re-read of the sync
        -- window (upserts only -- existing data stays visible meanwhile).
        UPDATE hubspot_sync_state SET initial_sync_completed_at = NULL WHERE id = 1;
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
