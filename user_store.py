"""App-owned persistence for users, roles, Sales Rep mapping, and
first-time-setup/password-reset tokens. Same appdb/psycopg2 pattern as
attention_store.py -- see that module's docstring for why this app has
no ORM and writes to a dedicated `appdb` Postgres database rather than
either read-only source database.

Users and Sales Reps are deliberately separate entities (see
db_migrations.py's migration 2 and README.md "User Records vs Sales Rep
Records"). `sales_reps` rows are auto-synced from the live source data
(sync_sales_reps(), called once per data refresh from app.py) -- never
created by an Admin by hand. A `users` row's `sales_rep_id` is an
optional mapping onto an already-existing rep, not an identity.

Passwords are hashed with werkzeug.security (already a Flask dependency,
no new package) -- generate_password_hash()/check_password_hash(), never
a plaintext password anywhere, including in memory longer than the
single request that sets it.

Every public function here follows attention_store.py's graceful-
degradation convention: read functions return an explicit availability
flag or a safe empty/default value, write functions return
(ok, error, ...) tuples, and nothing here ever raises to a caller that
isn't itself already in a try/except."""

import secrets
import hashlib
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash, check_password_hash

import db
import db_migrations

ROLES = ["Admin", "Sales Rep", "Customer Success", "Other"]
ROLE_SET = set(ROLES)

# A Sales Rep's team/group, for org, permissions where appropriate, and
# dashboard filtering (currently: the Sales Volume Over Time chart's team
# selector -- see calculate_sales_volume_trend() in sales_metrics.py).
# Lives on sales_reps.team, not users -- deliberately distinct from
# users.role/ROLES above (a rep can belong to a team with zero users ever
# logging in). No "unassigned" entry here on purpose -- NULL in the
# database IS unassigned; see list_sales_reps()/update_sales_rep_team()
# below and db_migrations.py migration 3.
SALES_REP_TEAMS = ["Junior", "NJ - Sales Reps", "NY - Sales Reps", "VA - Sales Reps"]
SALES_REP_TEAM_SET = set(SALES_REP_TEAMS)

# Roles an Excel import row's Group column may resolve to -- Admin is
# deliberately excluded (spec: "Admin should be managed separately").
IMPORTABLE_ROLES = ["Sales Rep", "Customer Success", "Other"]

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

SETUP_TOKEN_TTL_HOURS = 72
RESET_TOKEN_TTL_HOURS = 24

_USER_COLUMNS = """
    u.id, u.email, u.password_hash, u.role, u.sales_rep_id, u.status,
    u.last_login_at, u.last_needs_attention_activity_at,
    u.created_at, u.updated_at, sr.name AS sales_rep_name
"""


def _row_to_user(row):
    if row is None:
        return None
    (user_id, email, password_hash, role, sales_rep_id, status,
     last_login_at, last_needs_attention_activity_at, created_at, updated_at,
     sales_rep_name) = row
    return {
        "id": user_id,
        "email": email,
        "password_hash": password_hash,
        "role": role,
        "sales_rep_id": sales_rep_id,
        "sales_rep_name": sales_rep_name,
        "status": status,
        "last_login_at": last_login_at,
        "last_needs_attention_activity_at": last_needs_attention_activity_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "display_name": sales_rep_name or email,
    }


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ================================================================
# Sales Reps
# ================================================================

def sync_sales_reps(rep_names):
    """Upserts every distinct rep name seen in the current source data
    into `sales_reps` (first_seen_at set once, last_seen_at bumped every
    call) -- the auto-sync that makes sales_reps a live mirror of
    whoever's actually selling, with zero manual data entry. Called once
    per data refresh (see app.py's load_all_data()). Silently no-ops if
    appdb is unreachable -- a rep-sync failure must never block the
    dashboard itself from loading."""
    names = sorted({str(n).strip() for n in rep_names if n and str(n).strip()})
    if not names:
        return
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sales_reps (name)
                    SELECT unnest(%s::text[])
                    ON CONFLICT (name) DO UPDATE SET last_seen_at = now()
                    """,
                    (names,),
                )
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def list_sales_reps():
    """[{id, name, team, last_seen_at}, ...] ordered by name -- team is
    None for a rep with no Sales Rep Group assigned yet (see
    SALES_REP_TEAMS above). Empty list (never raises) if appdb is
    unreachable. Existing callers that only care about id/name (the
    Admin Portal's user->rep mapping dropdown) are unaffected by the
    added keys."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, team, last_seen_at FROM sales_reps ORDER BY name")
            return [{"id": r[0], "name": r[1], "team": r[2], "last_seen_at": r[3]} for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def list_sales_reps_by_team():
    """{team: [rep_name, ...]} for every value in SALES_REP_TEAMS, each
    list sorted by name -- the exact grouping the Sales Volume Over
    Time's team selector filters by (app.py's dashboard_page()). Reps
    with no team assigned (team IS NULL) are omitted entirely, not bucketed
    under a catch-all -- an unassigned rep should show in no team view
    rather than an incorrect one (see SALES_REP_TEAMS above / README.md).
    {} (never raises) if appdb is unreachable, same convention as
    list_sales_reps()."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT team, name FROM sales_reps WHERE team = ANY(%s) ORDER BY team, name",
                (SALES_REP_TEAMS,),
            )
            grouped = {team: [] for team in SALES_REP_TEAMS}
            for team, name in cur.fetchall():
                grouped[team].append(name)
            return grouped
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def update_sales_rep_team(rep_id, team):
    """Sets (or clears, if team is None/empty) one rep's Sales Rep Group.
    Deliberately touches ONLY sales_reps.team -- never sale rows,
    account_attention, or needs_attention_tracking, so a team change can
    never alter a rep's Needs Attention data, Install Rate, or historical
    sales (see db_migrations.py migration 3 and README.md). Returns
    (ok, error)."""
    team = (team or "").strip() or None
    if team is not None and team not in SALES_REP_TEAM_SET:
        return False, "Invalid team."
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sales_reps SET team = %s WHERE id = %s",
                    (team, rep_id),
                )
                if cur.rowcount == 0:
                    return False, "Sales Rep not found."
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def list_sales_rep_role_names():
    """Distinct sales_reps.name values currently mapped (via
    users.sales_rep_id) to a user whose role is literally "Sales Rep" --
    i.e. reps an Admin has explicitly set up as Sales Rep in the Admin
    Portal, not merely any name that happens to appear in the raw sales
    data (a rep can rack up sales having never been given a user account
    at all, or an Admin/Customer Success staffer can appear in the raw
    data under their own name from a manual entry -- see README.md "User
    Records vs Sales Rep Records"). Used only by the Team Leaderboard's
    Sales Volume tab (app.py's dashboard_page()) to keep that one chart
    to genuine Sales Reps; every other reporting number in this app
    intentionally keeps reading straight from the source data, unfiltered
    by role.

    Returns None (never raises) if appdb is unreachable -- deliberately
    distinct from a real empty set (zero users currently hold the Sales
    Rep role), so a caller can fail OPEN (show every rep unfiltered)
    rather than fail closed (silently blank the whole chart) on a
    transient appdb outage. Follow this module's usual
    read-returns-an-availability-flag-or-safe-default convention: check
    for None before treating the result as the source of truth."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT sr.name FROM users u JOIN sales_reps sr ON sr.id = u.sales_rep_id WHERE u.role = %s",
                ("Sales Rep",),
            )
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def find_sales_rep_by_name(name):
    """Exact, case-insensitive match. Returns {id, name} or None -- used
    by the Excel importer to decide Create/Update vs "Needs Review" (an
    unmatched name is never guessed at)."""
    name = (name or "").strip()
    if not name:
        return None
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM sales_reps WHERE lower(name) = lower(%s)", (name,))
            row = cur.fetchone()
            return {"id": row[0], "name": row[1]} if row else None
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


# ================================================================
# Users
# ================================================================

def get_user_by_id(user_id):
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_COLUMNS} FROM users u LEFT JOIN sales_reps sr ON sr.id = u.sales_rep_id WHERE u.id = %s",
                (user_id,),
            )
            return _row_to_user(cur.fetchone())
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def get_user_by_email(email):
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_COLUMNS} FROM users u LEFT JOIN sales_reps sr ON sr.id = u.sales_rep_id WHERE lower(u.email) = %s",
                (email,),
            )
            return _row_to_user(cur.fetchone())
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def list_users():
    """Every user, newest first. Empty list (never raises) if appdb is
    unreachable -- the Admin Portal shows an "unavailable" notice rather
    than crashing (same convention as attention_store.py)."""
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_COLUMNS} FROM users u LEFT JOIN sales_reps sr ON sr.id = u.sales_rep_id ORDER BY u.created_at DESC"
            )
            return [_row_to_user(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def create_user(email, role, sales_rep_id=None):
    """Creates a user with no password (status=pending) -- a real
    password is only ever set via set_password() through a verified
    setup token. Returns (ok, error, user)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "Invalid email address.", None
    if role not in ROLE_SET:
        return False, "Invalid role.", None

    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, role, sales_rep_id, status)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (email, role, sales_rep_id, STATUS_PENDING),
                )
                user_id = cur.fetchone()[0]
        return True, None, get_user_by_id(user_id)
    except Exception as exc:
        message = db.sanitize_error(exc)
        if "unique" in message.lower() or "duplicate" in message.lower():
            return False, "Email is already associated with another user.", None
        return False, message, None
    finally:
        if conn is not None:
            conn.close()


_UNSET = object()


def update_user(user_id, email=None, role=None, sales_rep_id=_UNSET):
    """Partial update -- only fields explicitly passed are changed.
    sales_rep_id uses a sentinel default so `None` can be passed
    deliberately (unmap a rep) without being confused with "not
    provided". Returns (ok, error)."""
    fields = []
    values = []
    if email is not None:
        email = email.strip().lower()
        if not email or "@" not in email:
            return False, "Invalid email address."
        fields.append("email = %s")
        values.append(email)
    if role is not None:
        if role not in ROLE_SET:
            return False, "Invalid role."
        fields.append("role = %s")
        values.append(role)
    if sales_rep_id is not _UNSET:
        fields.append("sales_rep_id = %s")
        values.append(sales_rep_id)
    if not fields:
        return True, None

    fields.append("updated_at = now()")
    values.append(user_id)

    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", values)
                if cur.rowcount == 0:
                    return False, "User not found."
        return True, None
    except Exception as exc:
        message = db.sanitize_error(exc)
        if "unique" in message.lower() or "duplicate" in message.lower():
            return False, "Email is already associated with another user."
        return False, message
    finally:
        if conn is not None:
            conn.close()


def set_user_status(user_id, status):
    if status not in (STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED):
        return False, "Invalid status."
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET status = %s, updated_at = now() WHERE id = %s",
                    (status, user_id),
                )
                if cur.rowcount == 0:
                    return False, "User not found."
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def set_password(user_id, password):
    """Sets a user's password (hashed -- never stored plaintext) and
    activates the account. This is the ONLY place password_hash is ever
    written, reached exclusively through a verified, single-use setup/
    reset token (see verify_setup_token()/consume_token() below) -- an
    Admin can never set or see a user's password directly."""
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters."
    password_hash = generate_password_hash(password)
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET password_hash = %s, status = %s, updated_at = now() WHERE id = %s",
                    (password_hash, STATUS_ACTIVE, user_id),
                )
                if cur.rowcount == 0:
                    return False, "User not found."
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def verify_password(user, password):
    if not user or not user.get("password_hash") or not password:
        return False
    return check_password_hash(user["password_hash"], password)


def record_login(user_id):
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def record_needs_attention_activity(user_id):
    """Called after a MEANINGFUL Needs Attention action succeeds (setting/
    changing Attention Status, adding a note) -- never for simply viewing
    an account. See app.py's attention_set_status()/attention_add_note()."""
    if not user_id:
        return
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_needs_attention_activity_at = now() WHERE id = %s",
                    (user_id,),
                )
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


# ================================================================
# Setup / password-reset tokens
# ================================================================

def create_token(user_id, purpose, ttl_hours=None):
    """Generates a fresh single-use token for first-time setup or
    password reset, invalidating any earlier unused token of the same
    purpose for this user first (so only the most recently sent link
    ever works). Returns (ok, error, raw_token) -- the raw token is
    returned exactly once, to be embedded in the emailed link; only its
    sha256 hash is ever stored."""
    if purpose not in ("setup", "reset"):
        return False, "Invalid token purpose.", None
    ttl_hours = ttl_hours or (SETUP_TOKEN_TTL_HOURS if purpose == "setup" else RESET_TOKEN_TTL_HOURS)

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE account_setup_tokens SET used_at = now() WHERE user_id = %s AND purpose = %s AND used_at IS NULL",
                    (user_id, purpose),
                )
                cur.execute(
                    """
                    INSERT INTO account_setup_tokens (user_id, token_hash, purpose, expires_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_id, token_hash, purpose, expires_at),
                )
        return True, None, raw_token
    except Exception as exc:
        return False, db.sanitize_error(exc), None
    finally:
        if conn is not None:
            conn.close()


def verify_token(raw_token, purpose):
    """Checks a raw token from a URL against its stored hash -- valid
    only if it exists, matches `purpose`, hasn't expired, and hasn't
    already been used. Returns (ok, user_id, error) -- does NOT mark it
    used (see consume_token()); a form-render (GET /setup/<token>) can
    verify without burning the token before the user actually submits."""
    if not raw_token:
        return False, None, "Invalid or expired link."
    token_hash = _hash_token(raw_token)
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, purpose, expires_at, used_at
                FROM account_setup_tokens
                WHERE token_hash = %s
                """,
                (token_hash,),
            )
            row = cur.fetchone()
        if row is None:
            return False, None, "Invalid or expired link."
        user_id, token_purpose, expires_at, used_at = row
        if token_purpose != purpose:
            return False, None, "Invalid or expired link."
        if used_at is not None:
            return False, None, "This link has already been used."
        if expires_at < datetime.now(timezone.utc):
            return False, None, "This link has expired. Ask an Admin to resend it."
        return True, user_id, None
    except Exception as exc:
        return False, None, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def consume_token(raw_token):
    """Marks a token used -- call only after the password has actually
    been set, so a failed submit doesn't burn a valid link."""
    token_hash = _hash_token(raw_token)
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE account_setup_tokens SET used_at = now() WHERE token_hash = %s AND used_at IS NULL",
                    (token_hash,),
                )
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
