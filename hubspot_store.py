"""HubSpot Outbound Call Analytics -- appdb persistence, owner mapping,
call synchronization and the analytics queries behind the Rep Profile's
"Outbound Call Performance" section and the Admin Portal's "HubSpot
Integration" page. See README.md "HubSpot Outbound Call Analytics".

Data flow: HubSpot (read-only, hubspot_client.py) -> appdb tables from
db_migrations.py migration 5 -> profile/admin routes. Page requests only
ever read appdb; HubSpot is called solely by the sync job, so a HubSpot
outage leaves the last synced data in place (and the UI says the sync is
failing rather than showing zero calls).

Sync concurrency: a Postgres session advisory lock (SYNC_LOCK_KEY) held
for the whole run guarantees one sync at a time across threads,
processes and containers -- the 15-minute scheduler and the Admin "Sync
HubSpot Calls" button can't overlap.

Same raw-psycopg2 + graceful-degradation conventions as user_store.py /
attention_store.py: reads return safe defaults when appdb is down."""

import os
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg2.extras import execute_values

import db
import db_migrations
import hubspot_client
import hubspot_logic as logic

BUSINESS_TZ_NAME = "America/New_York"
BUSINESS_TZ = ZoneInfo(BUSINESS_TZ_NAME)

# How often new/changed calls are pulled. HUBSPOT_SYNC_INTERVAL_SECONDS
# (default 60, floor 30) wins over the older HUBSPOT_SYNC_INTERVAL_MINUTES.
SYNC_INTERVAL_SECONDS = max(30, int(
    os.environ.get("HUBSPOT_SYNC_INTERVAL_SECONDS")
    or int(os.environ.get("HUBSPOT_SYNC_INTERVAL_MINUTES", "0")) * 60
    or 60
))
# Owners (+ mappings) and call outcomes change rarely, so a frequent call
# sync only re-reads them this often (or on a manual/initial sync).
OWNER_REFRESH_SECONDS = 15 * 60
# After a failed automatic sync, wait interval x 2^failures (capped) so a
# HubSpot outage or rate limit isn't hammered once a minute.
MAX_BACKOFF_SECONDS = 15 * 60
# hubspot_sync_runs keeps this much history (a 60s cadence adds ~1,440 rows/day).
RUN_HISTORY_DAYS = 3
# Only calls on/after this business-timezone date are synced or kept
# (by request, 2026-10-16: "only need to sync calls from 2026 onward").
SYNC_START_DATE = date.fromisoformat(os.environ.get("HUBSPOT_SYNC_START_DATE", "2026-01-01"))
SYNC_START = datetime.combine(SYNC_START_DATE, dt_time.min, tzinfo=BUSINESS_TZ)
SYNC_LOCK_KEY = 0x48554253  # "HUBS"
# Incremental sync re-reads this much before the watermark so a call
# that reached HubSpot's search index late is never skipped.
WATERMARK_OVERLAP = timedelta(minutes=5)

HISTORY_PAGE_SIZE = 25
# Calls per Hour: floor for one day's first-to-last-call span.
MIN_WORKING_DAY_SECONDS = 15 * 60
NO_OUTCOME = "__none__"


def interval_display():
    if SYNC_INTERVAL_SECONDS % 60:
        return f"{SYNC_INTERVAL_SECONDS} seconds"
    minutes = SYNC_INTERVAL_SECONDS // 60
    return "minute" if minutes == 1 else f"{minutes} minutes"


def _connect():
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    return conn


def _log(message):
    print(f"[hubspot] {hubspot_client.sanitize(message)}")


# ================================================================
# Status
# ================================================================

def get_integration_status():
    """Everything the Admin "HubSpot Integration" panel and the profile
    section's freshness line need. `available` False means appdb itself
    is unreachable."""
    status = {
        "available": False,
        "configured": hubspot_client.is_configured(),
        "state": {},
        "latest_run": None,
        "recent_runs": [],
        "owners_total": 0,
        "owners_mapped": 0,
        "owners_unmatched": 0,
        "outbound_calls_total": 0,
        "calls_total": 0,
        "sync_interval_display": interval_display(),
    }
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT calls_modified_watermark, initial_sync_completed_at, last_success_at,
                       last_attempt_at, last_error, connection_ok
                FROM hubspot_sync_state WHERE id = 1
            """)
            row = cur.fetchone()
            if row:
                keys = ["calls_modified_watermark", "initial_sync_completed_at", "last_success_at",
                        "last_attempt_at", "last_error", "connection_ok"]
                status["state"] = dict(zip(keys, row))
            cur.execute("""
                SELECT id, trigger, kind, status, started_at, finished_at, owners_retrieved,
                       records_processed, new_calls, updated_calls, error
                FROM hubspot_sync_runs ORDER BY started_at DESC LIMIT 10
            """)
            run_keys = ["id", "trigger", "kind", "status", "started_at", "finished_at", "owners_retrieved",
                        "records_processed", "new_calls", "updated_calls", "error"]
            status["recent_runs"] = [dict(zip(run_keys, r)) for r in cur.fetchall()]
            status["latest_run"] = status["recent_runs"][0] if status["recent_runs"] else None
            cur.execute("""
                SELECT count(*),
                       count(*) FILTER (WHERE sales_rep_id IS NOT NULL),
                       count(*) FILTER (WHERE sales_rep_id IS NULL)
                FROM hubspot_user_mappings
            """)
            status["owners_total"], status["owners_mapped"], status["owners_unmatched"] = cur.fetchone()
            cur.execute("""
                SELECT count(*), count(*) FILTER (WHERE upper(direction) = 'OUTBOUND') FROM hubspot_calls
            """)
            status["calls_total"], status["outbound_calls_total"] = cur.fetchone()
        status["available"] = True
    except Exception as exc:
        status["error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
    return status


# ================================================================
# Owners + mapping
# ================================================================

def list_mappings():
    """Admin table rows, unmatched/ambiguous first then by name."""
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.hubspot_owner_id, m.hubspot_user_id, m.hubspot_email, m.hubspot_name,
                       m.hubspot_archived, m.dashboard_user_id, u.email, m.sales_rep_id, r.name,
                       m.mapping_method, m.mapping_status, m.last_synced_at, m.updated_at,
                       (SELECT count(*) FROM hubspot_calls c
                         WHERE c.hubspot_owner_id = m.hubspot_owner_id AND upper(c.direction) = 'OUTBOUND')
                FROM hubspot_user_mappings m
                LEFT JOIN users u ON u.id = m.dashboard_user_id
                LEFT JOIN sales_reps r ON r.id = m.sales_rep_id
                ORDER BY (m.sales_rep_id IS NOT NULL), m.hubspot_archived, lower(coalesce(m.hubspot_name, m.hubspot_email))
            """)
            keys = ["hubspot_owner_id", "hubspot_user_id", "hubspot_email", "hubspot_name", "hubspot_archived",
                    "dashboard_user_id", "dashboard_user_email", "sales_rep_id", "sales_rep_name",
                    "mapping_method", "mapping_status", "last_synced_at", "updated_at", "outbound_calls"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    except Exception as exc:
        _log(f"list_mappings failed: {db.sanitize_error(exc)}")
        return []
    finally:
        if conn is not None:
            conn.close()


def _load_users_and_reps(cur):
    cur.execute("SELECT id, email, sales_rep_id FROM users")
    users = [{"id": r[0], "email": r[1], "sales_rep_id": r[2]} for r in cur.fetchall()]
    cur.execute("SELECT id, name FROM sales_reps")
    reps = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    return users, reps


def _apply_owner_mappings(conn, owners):
    """Upserts one hubspot_user_mappings row per owner. HubSpot identity
    columns always refresh; mapping columns are recomputed by
    logic.match_owners() except on rows an Admin set manually."""
    with conn:
        with conn.cursor() as cur:
            users, reps = _load_users_and_reps(cur)
            matches = logic.match_owners(owners, users, reps)
            for owner in owners:
                owner_id = str(owner["id"])
                match = matches[owner_id]
                cur.execute(
                    """
                    INSERT INTO hubspot_user_mappings
                        (hubspot_owner_id, hubspot_user_id, hubspot_email, hubspot_name, hubspot_archived,
                         dashboard_user_id, sales_rep_id, mapping_method, mapping_status, last_synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (hubspot_owner_id) DO UPDATE SET
                        hubspot_user_id = EXCLUDED.hubspot_user_id,
                        hubspot_email = EXCLUDED.hubspot_email,
                        hubspot_name = EXCLUDED.hubspot_name,
                        hubspot_archived = EXCLUDED.hubspot_archived,
                        last_synced_at = now(),
                        dashboard_user_id = CASE WHEN hubspot_user_mappings.mapping_method = 'manual'
                            THEN hubspot_user_mappings.dashboard_user_id ELSE EXCLUDED.dashboard_user_id END,
                        sales_rep_id = CASE WHEN hubspot_user_mappings.mapping_method = 'manual'
                            THEN hubspot_user_mappings.sales_rep_id ELSE EXCLUDED.sales_rep_id END,
                        mapping_status = CASE WHEN hubspot_user_mappings.mapping_method = 'manual'
                            THEN hubspot_user_mappings.mapping_status ELSE EXCLUDED.mapping_status END,
                        updated_at = CASE WHEN hubspot_user_mappings.mapping_method <> 'manual'
                                           AND (hubspot_user_mappings.sales_rep_id, hubspot_user_mappings.mapping_method)
                                               IS DISTINCT FROM (EXCLUDED.sales_rep_id, EXCLUDED.mapping_method)
                            THEN now() ELSE hubspot_user_mappings.updated_at END,
                        mapping_method = CASE WHEN hubspot_user_mappings.mapping_method = 'manual'
                            THEN 'manual' ELSE EXCLUDED.mapping_method END
                    """,
                    (
                        owner_id,
                        str(owner["userId"]) if owner.get("userId") is not None else None,
                        owner.get("email") or None,
                        logic.owner_full_name(owner) or None,
                        bool(owner.get("archived")),
                        match["dashboard_user_id"],
                        match["sales_rep_id"],
                        match["method"],
                        match["status"],
                    ),
                )


def rematch_existing_owners():
    """Re-runs automatic matching over the owners already stored (no
    HubSpot call) -- the Admin "Refresh Mappings" button, useful right
    after adding a user or linking a user to a Sales Rep."""
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT hubspot_owner_id, hubspot_user_id, hubspot_email, hubspot_name, hubspot_archived
                FROM hubspot_user_mappings
            """)
            owners = []
            for owner_id, user_id, email, name, archived in cur.fetchall():
                first, _, last = (name or "").partition(" ")
                owners.append({"id": owner_id, "userId": user_id, "email": email,
                               "firstName": first, "lastName": last, "archived": archived})
        _apply_owner_mappings(conn, owners)
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def set_manual_mapping(owner_id, sales_rep_id, mode):
    """mode: 'assign' (sales_rep_id required), 'unassign' (manually mark
    as not a rep -- stays unmatched, auto-matching won't touch it) or
    'auto' (drop the manual override; the next match run decides).
    Returns (ok, error)."""
    if mode not in ("assign", "unassign", "auto"):
        return False, "Unknown mapping action."
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM hubspot_user_mappings WHERE hubspot_owner_id = %s", (owner_id,))
                if cur.fetchone() is None:
                    return False, "That HubSpot owner isn't known -- run a sync first."
                if mode == "assign":
                    cur.execute("SELECT id FROM sales_reps WHERE id = %s", (sales_rep_id,))
                    if cur.fetchone() is None:
                        return False, "That Sales Rep doesn't exist."
                    # Link the dashboard user too when exactly one user is
                    # mapped to this rep, so dashboard_user_id stays meaningful.
                    cur.execute("SELECT id FROM users WHERE sales_rep_id = %s", (sales_rep_id,))
                    linked = [r[0] for r in cur.fetchall()]
                    cur.execute(
                        """
                        UPDATE hubspot_user_mappings
                        SET sales_rep_id = %s, dashboard_user_id = %s, mapping_method = 'manual',
                            mapping_status = 'matched', updated_at = now()
                        WHERE hubspot_owner_id = %s
                        """,
                        (sales_rep_id, linked[0] if len(linked) == 1 else None, owner_id),
                    )
                elif mode == "unassign":
                    cur.execute(
                        """
                        UPDATE hubspot_user_mappings
                        SET sales_rep_id = NULL, dashboard_user_id = NULL, mapping_method = 'manual',
                            mapping_status = 'unmatched', updated_at = now()
                        WHERE hubspot_owner_id = %s
                        """,
                        (owner_id,),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE hubspot_user_mappings
                        SET mapping_method = 'none', mapping_status = 'unmatched', sales_rep_id = NULL,
                            dashboard_user_id = NULL, updated_at = now()
                        WHERE hubspot_owner_id = %s
                        """,
                        (owner_id,),
                    )
        if mode == "auto":
            return rematch_existing_owners()
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


# ================================================================
# Sync
# ================================================================

_CALL_COLUMNS = [
    "hubspot_call_id", "hubspot_owner_id", "call_timestamp", "direction", "duration_ms", "disposition",
    "status", "to_number", "from_number", "title", "hubspot_created_at", "hubspot_modified_at",
    "recording_url",
]

_UPSERT_CALLS_SQL = f"""
    INSERT INTO hubspot_calls ({", ".join(_CALL_COLUMNS)}) VALUES %s
    ON CONFLICT (hubspot_call_id) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in _CALL_COLUMNS[1:])},
        synced_at = now()
    WHERE ({", ".join(f"hubspot_calls.{c}" for c in _CALL_COLUMNS[1:])})
          IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in _CALL_COLUMNS[1:])})
    RETURNING (xmax = 0) AS inserted
"""


def upsert_calls(conn, records):
    """Parses + upserts one page of HubSpot call objects. Returns
    (processed, new, updated, newest_modified). Unchanged rows are
    neither rewritten nor counted as updated."""
    by_id = {}
    for record in records:
        row = logic.parse_call(record)
        if row["hubspot_call_id"]:
            by_id[row["hubspot_call_id"]] = row
    if not by_id:
        return len(records), 0, 0, None
    rows = list(by_id.values())
    with conn:
        with conn.cursor() as cur:
            results = execute_values(
                cur, _UPSERT_CALLS_SQL, [tuple(r[c] for c in _CALL_COLUMNS) for r in rows], fetch=True,
            )
    new = sum(1 for (inserted,) in results if inserted)
    updated = len(results) - new
    modified = [r["hubspot_modified_at"] for r in rows if r["hubspot_modified_at"]]
    return len(records), new, updated, max(modified) if modified else None


def _sync_call_contacts(conn, client, records):
    """Sets contact_id for one page of calls (v4 associations) and caches
    names for any contacts not seen before. Raises HubSpotError; the
    caller treats that as non-fatal so calls still sync."""
    call_ids = [str(r.get("id")) for r in records if r.get("id")]
    if not call_ids:
        return
    contact_by_call = client.get_call_contact_ids(call_ids)
    with conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                UPDATE hubspot_calls c SET contact_id = v.cid
                FROM (VALUES %s) AS v(id, cid)
                WHERE c.hubspot_call_id = v.id AND c.contact_id IS DISTINCT FROM v.cid
                """,
                [(cid, contact_by_call.get(cid)) for cid in call_ids],
                template="(%s, %s::text)",
            )
            wanted = sorted(set(contact_by_call.values()))
            if not wanted:
                return
            cur.execute("SELECT contact_id FROM hubspot_contacts WHERE contact_id = ANY(%s)", (wanted,))
            known = {r[0] for r in cur.fetchall()}
    missing = [c for c in wanted if c not in known]
    if not missing:
        return
    contacts = client.get_contacts(missing)
    with conn:
        with conn.cursor() as cur:
            # Deleted contacts get a nameless row too, so they aren't
            # re-requested on every sync.
            execute_values(
                cur,
                """
                INSERT INTO hubspot_contacts (contact_id, name, email) VALUES %s
                ON CONFLICT (contact_id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, updated_at = now()
                """,
                [(c, (contacts.get(c) or {}).get("name"), (contacts.get(c) or {}).get("email")) for c in missing],
            )


def _sync_account_details(conn, client):
    try:
        details = client.get_account_details()
    except hubspot_client.HubSpotError as exc:
        _log(f"Could not read HubSpot account details (contact links disabled): {exc}")
        return
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hubspot_sync_state SET portal_id = %s, ui_domain = %s WHERE id = 1",
                (str(details.get("portalId") or "") or None, details.get("uiDomain") or None),
            )


def _sync_dispositions(conn, client):
    try:
        dispositions = client.list_call_dispositions()
    except hubspot_client.HubSpotError as exc:
        # Labels are a nicety -- the built-in Connected id still works
        # (logic.DEFAULT_CONNECTED_DISPOSITION_ID), so don't fail the sync.
        _log(f"Could not fetch call outcomes (using HubSpot's default labels): {exc}")
        return
    with conn:
        with conn.cursor() as cur:
            for d in dispositions:
                if d.get("id") and d.get("label"):
                    cur.execute(
                        """
                        INSERT INTO hubspot_dispositions (disposition_id, label) VALUES (%s, %s)
                        ON CONFLICT (disposition_id) DO UPDATE SET label = EXCLUDED.label, updated_at = now()
                        """,
                        (str(d["id"]), d["label"]),
                    )


def _update_run(conn, run_id, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    with conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE hubspot_sync_runs SET {sets} WHERE id = %s", (*fields.values(), run_id))


def run_sync(trigger="auto", client=None):
    """One complete sync: owners -> mappings -> outcomes -> calls (full
    history the first time, then incremental by hs_lastmodifieddate).
    Returns (ran, summary_or_reason). ran=False means another sync holds
    the lock (or HubSpot isn't configured) -- not an error."""
    if not hubspot_client.is_configured():
        return False, "HUBSPOT_SERVICE_KEY is not configured."

    lock_conn = _connect()
    try:
        lock_conn.autocommit = True
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
            if not cur.fetchone()[0]:
                return False, "A HubSpot sync is already running."
            # Holding the lock means no other sync is alive -- any row
            # still 'running' is from a process that died mid-sync.
            cur.execute("""
                UPDATE hubspot_sync_runs SET status = 'failed', finished_at = now(),
                       error = coalesce(error, 'Interrupted (process stopped mid-sync)')
                WHERE status = 'running'
            """)
        return True, _run_sync_locked(trigger, client)
    finally:
        try:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        except Exception:
            pass
        lock_conn.close()


def _run_sync_locked(trigger, client):
    conn = _connect()
    started_at = datetime.now(timezone.utc)
    summary = {"owners_retrieved": None, "records_processed": 0, "new_calls": 0, "updated_calls": 0, "error": None}
    run_id = None
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE hubspot_sync_state SET last_attempt_at = now() WHERE id = 1")
                cur.execute("SELECT calls_modified_watermark, initial_sync_completed_at FROM hubspot_sync_state WHERE id = 1")
                watermark, initial_done = cur.fetchone()
                kind = "incremental" if initial_done and watermark else "initial"
                cur.execute(
                    "INSERT INTO hubspot_sync_runs (trigger, kind, status) VALUES (%s, %s, 'running') RETURNING id",
                    (trigger, kind),
                )
                run_id = cur.fetchone()[0]
        summary["kind"] = kind
        quiet = trigger == "auto" and kind == "incremental"
        if not quiet:
            _log(f"Sync #{run_id} started ({kind}, {trigger})")

        client = client or hubspot_client.HubSpotClient()

        if kind == "initial" or trigger != "auto" or _owners_stale(conn):
            owners = client.list_all_owners()
            summary["owners_retrieved"] = len(owners)
            _apply_owner_mappings(conn, owners)
            _update_run(conn, run_id, owners_retrieved=len(owners))
            _sync_dispositions(conn, client)
            _sync_account_details(conn, client)

        # Drop anything older than the sync window (e.g. after
        # HUBSPOT_SYNC_START_DATE moves forward), then fetch only calls
        # inside it -- both the first full pass and every incremental one.
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM hubspot_calls WHERE call_timestamp < %s OR call_timestamp IS NULL", (SYNC_START,))
                if cur.rowcount:
                    _log(f"Removed {cur.rowcount} stored calls before {SYNC_START_DATE.isoformat()}")
        window = [{"propertyName": "hs_timestamp", "operator": "GTE",
                   "value": str(int(SYNC_START.timestamp() * 1000))}]
        since = 0 if kind == "initial" else (watermark - WATERMARK_OVERLAP).timestamp() * 1000
        pages = client.iter_calls_modified_since(int(since), extra_filters=window)

        newest_modified = watermark
        contacts_warned = False
        for page in pages:
            processed, new, updated, page_newest = upsert_calls(conn, page)
            try:
                _sync_call_contacts(conn, client, page)
            except hubspot_client.HubSpotError as exc:
                # Contact links are secondary -- never block call data.
                if not contacts_warned:
                    _log(f"Could not sync call contacts (calls still synced): {exc}")
                    contacts_warned = True
            summary["records_processed"] += processed
            summary["new_calls"] += new
            summary["updated_calls"] += updated
            if page_newest and (newest_modified is None or page_newest > newest_modified):
                newest_modified = page_newest
            _update_run(conn, run_id, records_processed=summary["records_processed"],
                        new_calls=summary["new_calls"], updated_calls=summary["updated_calls"])

        if kind == "initial":
            # Anything modified while the (long) full listing ran is
            # re-read by the next incremental sync from here.
            new_watermark = started_at - WATERMARK_OVERLAP
        else:
            new_watermark = newest_modified or watermark

        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE hubspot_sync_state SET calls_modified_watermark = %s,
                        initial_sync_completed_at = coalesce(initial_sync_completed_at, now()),
                        last_success_at = now(), last_error = NULL, connection_ok = true
                    WHERE id = 1
                    """,
                    (new_watermark,),
                )
        _update_run(conn, run_id, status="success", finished_at=datetime.now(timezone.utc))
        if not quiet or summary["new_calls"] or summary["updated_calls"]:
            _log(f"Sync #{run_id} finished: {summary['records_processed']} processed, "
                 f"{summary['new_calls']} new, {summary['updated_calls']} updated")
        _prune_run_history(conn)
    except Exception as exc:
        message = hubspot_client.sanitize(db.sanitize_error(exc))
        summary["error"] = message
        _log(f"Sync #{run_id} failed: {message}")
        try:
            # connection_ok only flips on a HubSpot-side failure; an appdb
            # hiccup says nothing about HubSpot connectivity.
            hubspot_failed = isinstance(exc, hubspot_client.HubSpotError)
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE hubspot_sync_state SET last_error = %s,
                            connection_ok = CASE WHEN %s THEN false ELSE connection_ok END
                        WHERE id = 1
                        """,
                        (message, hubspot_failed),
                    )
            if run_id is not None:
                _update_run(conn, run_id, status="failed", finished_at=datetime.now(timezone.utc), error=message)
        except Exception:
            pass
    finally:
        conn.close()
    return summary


def _owners_stale(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT max(last_synced_at) FROM hubspot_user_mappings")
        last = cur.fetchone()[0]
    conn.rollback()
    return last is None or datetime.now(timezone.utc) - last > timedelta(seconds=OWNER_REFRESH_SECONDS)


def _prune_run_history(conn):
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM hubspot_sync_runs WHERE status <> 'running' AND started_at < now() - %s * interval '1 day'",
                    (RUN_HISTORY_DAYS,),
                )
    except Exception as exc:
        _log(f"Pruning sync history failed: {db.sanitize_error(exc)}")


_manual_thread = None
_manual_thread_lock = threading.Lock()


def start_sync_in_background(trigger="manual"):
    """Admin button: kicks off run_sync() on a thread so the request
    returns immediately; the page polls get_integration_status()."""
    global _manual_thread
    if not hubspot_client.is_configured():
        return False, "HUBSPOT_SERVICE_KEY is not configured on the server."
    with _manual_thread_lock:
        if _manual_thread is not None and _manual_thread.is_alive():
            return False, "A HubSpot sync is already running."
        _manual_thread = threading.Thread(target=_safe_run_sync, args=(trigger,), name="hubspot-sync-manual", daemon=True)
        _manual_thread.start()
    return True, None


def _safe_run_sync(trigger):
    """Never raises. Returns False only when a sync actually ran and
    failed (drives the scheduler's backoff); 'another sync holds the
    lock' counts as fine."""
    try:
        ran, result = run_sync(trigger)
        if not ran:
            # Another process/thread is mid-sync -- routine at short
            # intervals, so only worth logging for a manual request.
            if trigger != "auto":
                _log(result)
            return True
        return result.get("error") is None
    except Exception as exc:
        _log(f"Sync crashed: {db.sanitize_error(exc)}")
        return False


_scheduler_started = False
_scheduler_lock = threading.Lock()


def _scheduler_loop():
    # First run shortly after startup (does the initial sync on a fresh
    # database), then every SYNC_INTERVAL_SECONDS measured from the END of
    # the previous run -- runs can never pile up. Consecutive failures
    # back off exponentially up to MAX_BACKOFF_SECONDS.
    time.sleep(10)
    failures = 0
    while True:
        failures = 0 if _safe_run_sync("auto") else failures + 1
        delay = SYNC_INTERVAL_SECONDS if not failures else min(MAX_BACKOFF_SECONDS, SYNC_INTERVAL_SECONDS * 2 ** failures)
        if failures:
            _log(f"Automatic sync failed {failures}x in a row; next attempt in {delay}s")
        time.sleep(delay)


def start_scheduler():
    """Idempotent. No-op without a service key. Runs in the same single
    process as app.py's data-refresh thread (gunicorn --workers 1); the
    advisory lock in run_sync() keeps it safe even if that ever changes."""
    global _scheduler_started
    if _scheduler_started or not hubspot_client.is_configured():
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=_scheduler_loop, name="hubspot-sync", daemon=True).start()
        _scheduler_started = True
        _log(f"Automatic sync scheduled every {interval_display()}")


# ================================================================
# Analytics (Rep Profile)
# ================================================================

def _range_bounds(start_date, end_date):
    start = datetime.combine(start_date, dt_time.min, tzinfo=BUSINESS_TZ)
    end = datetime.combine(end_date + timedelta(days=1), dt_time.min, tzinfo=BUSINESS_TZ)
    return start, end


def _disposition_labels(cur):
    labels = dict(logic.DEFAULT_DISPOSITION_LABELS)
    cur.execute("SELECT disposition_id, label FROM hubspot_dispositions")
    labels.update({d_id: label for d_id, label in cur.fetchall()})
    return labels


def get_rep_owner_ids(cur, rep_name):
    cur.execute(
        """
        SELECT m.hubspot_owner_id FROM hubspot_user_mappings m
        JOIN sales_reps r ON r.id = m.sales_rep_id WHERE r.name = %s
        """,
        (rep_name,),
    )
    return [r[0] for r in cur.fetchall()]


def get_rep_call_context(rep_name):
    """Profile-page context: is this rep mapped, and when was data last
    synced. Never raises."""
    ctx = {"available": False, "configured": hubspot_client.is_configured(), "mapped": False,
           "owner_count": 0, "last_success_at": None, "last_error": None, "ever_synced": False,
           "sync_start_display": SYNC_START_DATE.strftime("%b %-d, %Y")}
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            owner_ids = get_rep_owner_ids(cur, rep_name)
            ctx["owner_count"] = len(owner_ids)
            ctx["mapped"] = bool(owner_ids)
            cur.execute("SELECT last_success_at, last_error, initial_sync_completed_at FROM hubspot_sync_state WHERE id = 1")
            row = cur.fetchone()
            if row:
                ctx["last_success_at"], ctx["last_error"], initial = row
                ctx["ever_synced"] = initial is not None
        ctx["available"] = True
    except Exception as exc:
        ctx["error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
    return ctx


_OUTBOUND = "upper(direction) = 'OUTBOUND'"


def get_call_summary(rep_name, start_date, end_date, scale):
    """KPIs + zero-filled volume series for one rep's outbound calls in
    [start_date, end_date] (business-timezone days). Raises on appdb
    failure -- the API route turns that into an error response, never
    into zeros."""
    start, end = _range_bounds(start_date, end_date)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            owner_ids = get_rep_owner_ids(cur, rep_name)
            labels = _disposition_labels(cur)
            connected_ids = sorted(logic.connected_disposition_ids(labels))
            cur.execute(
                f"""
                SELECT count(*),
                       count(*) FILTER (WHERE disposition = ANY(%s)),
                       sum(duration_ms),
                       count(duration_ms),
                       max(call_timestamp)
                FROM hubspot_calls
                WHERE hubspot_owner_id = ANY(%s) AND {_OUTBOUND}
                  AND call_timestamp >= %s AND call_timestamp < %s
                """,
                (connected_ids, owner_ids, start, end),
            )
            total, connected, duration_sum, duration_count, last_call_at = cur.fetchone()
            # Working day = first call's start -> last call's end, per
            # business-tz day. Gaps between calls count; time after the
            # last call (rep went home) doesn't. Each worked day counts at
            # least MIN_WORKING_DAY_SECONDS so a lone call can't produce
            # an absurd rate.
            cur.execute(
                f"""
                SELECT count(*), coalesce(sum(greatest(extract(epoch FROM last_end - first_start), %s)), 0)
                FROM (
                    SELECT min(call_timestamp) AS first_start,
                           max(call_timestamp + coalesce(duration_ms, 0) * interval '1 millisecond') AS last_end
                    FROM hubspot_calls
                    WHERE hubspot_owner_id = ANY(%s) AND {_OUTBOUND}
                      AND call_timestamp >= %s AND call_timestamp < %s
                    GROUP BY (call_timestamp AT TIME ZONE %s)::date
                ) days
                """,
                (MIN_WORKING_DAY_SECONDS, owner_ids, start, end, BUSINESS_TZ_NAME),
            )
            working_days, working_seconds = cur.fetchone()
            working_seconds = float(working_seconds)
            series = _volume_series(cur, owner_ids, start_date, end_date, scale)
    finally:
        conn.close()

    return {
        "owner_count": len(owner_ids),
        "total_calls": total,
        "connected_calls": connected,
        "connection_rate": (connected / total * 100) if total else None,
        "total_talk_ms": int(duration_sum) if duration_sum is not None else 0,
        "calls_with_duration": duration_count,
        "avg_duration_ms": (int(duration_sum) / duration_count) if duration_count else None,
        "last_call_at": last_call_at,
        "working_days": working_days,
        "working_seconds": working_seconds,
        "calls_per_hour": (total / (working_seconds / 3600)) if working_seconds else None,
        "series": series,
    }


def _volume_series(cur, owner_ids, start_date, end_date, scale):
    start, end = _range_bounds(start_date, end_date)
    cur.execute(
        f"""
        SELECT date_trunc(%s, call_timestamp AT TIME ZONE %s) AS bucket, count(*)
        FROM hubspot_calls
        WHERE hubspot_owner_id = ANY(%s) AND {_OUTBOUND}
          AND call_timestamp >= %s AND call_timestamp < %s
        GROUP BY bucket
        """,
        (logic.SCALE_TRUNC_UNITS[scale], BUSINESS_TZ_NAME, owner_ids, start, end),
    )
    return logic.fill_buckets(dict(cur.fetchall()), start_date, end_date, scale)


def get_call_volume(rep_name, start_date, end_date, scale):
    """Just the series -- what a time-scale change re-fetches."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            return _volume_series(cur, get_rep_owner_ids(cur, rep_name), start_date, end_date, scale)
    finally:
        conn.close()


def get_call_history(rep_name, start_date, end_date, page=1, outcome=None, newest_first=True):
    start, end = _range_bounds(start_date, end_date)
    page = max(1, int(page))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            owner_ids = get_rep_owner_ids(cur, rep_name)
            labels = _disposition_labels(cur)
            connected_ids = logic.connected_disposition_ids(labels)

            where = f"hubspot_owner_id = ANY(%s) AND {_OUTBOUND} AND call_timestamp >= %s AND call_timestamp < %s"
            params = [owner_ids, start, end]

            # Outcome filter options are every outcome present in range,
            # before the outcome filter itself is applied.
            cur.execute(f"SELECT disposition, count(*) FROM hubspot_calls WHERE {where} GROUP BY disposition", params)
            outcome_counts = cur.fetchall()

            if outcome == NO_OUTCOME:
                where += " AND disposition IS NULL"
            elif outcome:
                where += " AND disposition = %s"
                params.append(outcome)

            cur.execute(f"SELECT count(*) FROM hubspot_calls WHERE {where}", params)
            total = cur.fetchone()[0]
            order = "DESC" if newest_first else "ASC"
            cur.execute(
                f"""
                SELECT c.hubspot_call_id, c.call_timestamp, c.duration_ms, c.disposition, c.status,
                       c.to_number, c.from_number, c.title, c.recording_url, c.contact_id, hc.name, hc.email
                FROM hubspot_calls c LEFT JOIN hubspot_contacts hc ON hc.contact_id = c.contact_id
                WHERE {where}
                ORDER BY c.call_timestamp {order}, c.hubspot_call_id {order}
                LIMIT %s OFFSET %s
                """,
                (*params, HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE),
            )
            rows = cur.fetchall()
            cur.execute("SELECT portal_id, ui_domain FROM hubspot_sync_state WHERE id = 1")
            portal_id, ui_domain = cur.fetchone() or (None, None)
    finally:
        conn.close()

    calls = []
    for (call_id, ts, duration_ms, disposition, status, to_number, from_number, title,
         recording_url, contact_id, contact_name, contact_email) in rows:
        local = ts.astimezone(BUSINESS_TZ) if ts else None
        calls.append({
            "id": call_id,
            "date": local.strftime("%b %-d, %Y") if local else None,
            "time": local.strftime("%-I:%M %p") if local else None,
            "duration": logic.format_duration_ms(duration_ms),
            "outcome": labels.get(disposition, "Unknown outcome") if disposition else None,
            "connected": disposition in connected_ids,
            "status": logic.humanize_status(status),
            "to_number": to_number,
            "from_number": from_number,
            "title": title,
            "recording_url": logic.safe_https_url(recording_url),
            "contact_name": contact_name or contact_email or (f"Contact {contact_id}" if contact_id else None),
            "contact_url": logic.hubspot_contact_url(ui_domain, portal_id, contact_id),
        })

    outcomes = sorted(
        ({"value": d if d else NO_OUTCOME,
          "label": (labels.get(d, "Unknown outcome") if d else "No outcome logged"),
          "count": n} for d, n in outcome_counts),
        key=lambda o: (-o["count"], o["label"]),
    )
    return {
        "calls": calls,
        "total": total,
        "page": page,
        "page_size": HISTORY_PAGE_SIZE,
        "pages": max(1, -(-total // HISTORY_PAGE_SIZE)),
        "outcomes": outcomes,
    }


# ================================================================
# Team Outbound Calls (Team Leaderboard /dashboard, added 2026-10-20)
# ================================================================

TEAM_CALLS_DAY_START_HOUR = 8
# Team Outbound Calls chart's time-scale tabs: (key, label, days back
# including today, grouping). First is the default.
TEAM_CALLS_VIEWS = [
    ("today", "Today", 1, "hourly"),
    ("7d", "7 Days", 7, "daily"),
    ("30d", "30 Days", 30, "daily"),
    ("90d", "90 Days", 90, "weekly"),
    ("12m", "12 Months", 365, "monthly"),
]

# Calls per Hour's shift model (by request, 2026-10-20): reps work 8-5 or
# 9-5 with a 30-minute lunch. A rep's day starts at their first call, or
# 9 AM if that came later (a 9 AM starter who dials at 9:40 still started
# at 9); it ends now, capped at 5 PM unless they were still calling after.
SHIFT_LATEST_START = dt_time(9, 0)
SHIFT_END = dt_time(17, 0)
LUNCH_MINUTES = 30
# Lunch is only deducted once the shift is long enough to include it.
LUNCH_AFTER_HOURS = 4
# Floor on the hours divisor, so a rep's first few calls of the morning
# can't read as an absurd rate.
MIN_SHIFT_HOURS = 1


def shift_hours_worked(first_call_at, last_call_at, now):
    """Hours a rep has worked so far today under the shift model above,
    or None if they haven't called today. All args are aware datetimes."""
    if first_call_at is None:
        return None
    first = first_call_at.astimezone(BUSINESS_TZ)
    day = first.date()
    start = min(first, datetime.combine(day, SHIFT_LATEST_START, tzinfo=BUSINESS_TZ))
    end = min(now.astimezone(BUSINESS_TZ), datetime.combine(day, SHIFT_END, tzinfo=BUSINESS_TZ))
    end = max(end, last_call_at.astimezone(BUSINESS_TZ))
    hours = (end - start).total_seconds() / 3600
    if hours > LUNCH_AFTER_HOURS:
        hours -= LUNCH_MINUTES / 60
    return max(hours, MIN_SHIFT_HOURS)


def _last_call_display(call_at, today):
    """"Today 2:14 PM" / "Yesterday 4:05 PM" / "Mon 10/12 3:00 PM", in
    the business timezone."""
    local = call_at.astimezone(BUSINESS_TZ)
    clock = local.strftime("%-I:%M %p")
    if local.date() == today:
        return f"Today {clock}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    return local.strftime("%a %-m/%-d ") + clock


def get_team_call_overview(today, last_call_team="NJ - Sales Reps"):
    """Dashboard context: the whole sales team's outbound calls (every
    HubSpot owner mapped to a sales rep, zero-filled) for each
    TEAM_CALLS_VIEWS time scale, plus each `last_call_team`
    rep's most recent outbound call. Reads appdb only -- never HubSpot
    live -- and never raises: {"available": False, ...} when appdb is
    unreachable."""
    ctx = {"available": False, "ever_synced": False, "configured": hubspot_client.is_configured(),
           "series_views": [], "last_calls": [],
           "last_call_team": last_call_team, "last_success_display": None}
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT last_success_at, initial_sync_completed_at FROM hubspot_sync_state WHERE id = 1")
            row = cur.fetchone()
            if row:
                ctx["ever_synced"] = row[1] is not None
                if row[0]:
                    ctx["last_success_display"] = _last_call_display(row[0], today)
            cur.execute("SELECT hubspot_owner_id FROM hubspot_user_mappings WHERE sales_rep_id IS NOT NULL")
            team_owner_ids = [r[0] for r in cur.fetchall()]
            # One pre-rendered series per time-scale tab (by request,
            # 2026-10-21; was Today only) -- the chart's toggle switches
            # between them client-side.
            for key, label, days, scale in TEAM_CALLS_VIEWS:
                start_date = max(today - timedelta(days=days - 1), SYNC_START_DATE)
                buckets = _volume_series(cur, team_owner_ids, start_date, today, scale)
                if scale == "hourly":
                    # Show from 8 AM (or the first call, if earlier) up to
                    # the current hour -- future hours would plot as a
                    # misleading drop to zero.
                    called = [i for i, b in enumerate(buckets) if b["count"]]
                    first = min([TEAM_CALLS_DAY_START_HOUR] + called[:1])
                    last = max([datetime.now(BUSINESS_TZ).hour] + called[-1:])
                    series = [
                        {"label": datetime.fromisoformat(b["start"]).strftime("%-I %p"), "value": b["count"]}
                        for b in buckets[first:last + 1]
                    ]
                else:
                    series = [{"label": b["label"], "value": b["count"]} for b in buckets]
                ctx["series_views"].append({
                    "key": key, "label": label, "series": series,
                    "total": sum(b["count"] for b in buckets),
                })

            # Last call (any day) plus today's calls/connected calls per
            # rep -- Calls per Sale is added by the caller, which owns the
            # sales data.
            day_start, day_end = _range_bounds(today, today)
            connected_ids = sorted(logic.connected_disposition_ids(_disposition_labels(cur)))
            cur.execute(
                f"""
                SELECT r.name, bool_or(m.hubspot_owner_id IS NOT NULL), max(c.call_timestamp),
                       count(c.hubspot_call_id) FILTER (WHERE c.call_timestamp >= %s AND c.call_timestamp < %s),
                       count(c.hubspot_call_id) FILTER (WHERE c.call_timestamp >= %s AND c.call_timestamp < %s
                                                        AND c.disposition = ANY(%s)),
                       min(c.call_timestamp) FILTER (WHERE c.call_timestamp >= %s AND c.call_timestamp < %s)
                FROM sales_reps r
                LEFT JOIN hubspot_user_mappings m ON m.sales_rep_id = r.id
                LEFT JOIN hubspot_calls c ON c.hubspot_owner_id = m.hubspot_owner_id AND {_OUTBOUND}
                WHERE r.team = %s
                GROUP BY r.name
                ORDER BY max(c.call_timestamp) DESC NULLS LAST, r.name
                """,
                (day_start, day_end, day_start, day_end, connected_ids, day_start, day_end, last_call_team),
            )
            now = datetime.now(BUSINESS_TZ)
            ctx["last_calls"] = []
            for name, mapped, call_at, calls_today, connected_today, first_today in cur.fetchall():
                # first_today is only set when they called today, so
                # call_at (their latest call) is today's last call too.
                hours = shift_hours_worked(first_today, call_at, now)
                ctx["last_calls"].append({
                    "rep": name, "mapped": mapped,
                    "display": _last_call_display(call_at, today) if call_at else None,
                    "is_today": bool(call_at) and call_at.astimezone(BUSINESS_TZ).date() == today,
                    "calls_today": calls_today, "connected_today": connected_today,
                    "hours_today": hours,
                    "hours_display": logic.format_hours_minutes(hours * 3600) if hours else None,
                    "calls_per_hour": calls_today / hours if hours else None,
                })
        ctx["available"] = True
    except Exception as exc:
        ctx["error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
    return ctx


# ================================================================
# Connectivity diagnostic (Admin "Test Connection")
# ================================================================

def test_connection():
    """Hits each endpoint the sync needs with a minimal read and reports
    per-endpoint results -- the precise "which endpoint / status / scope"
    diagnostic the Admin page shows."""
    if not hubspot_client.is_configured():
        return {"ok": False, "checks": [], "error": "HUBSPOT_SERVICE_KEY is not set in the backend environment."}
    client = hubspot_client.HubSpotClient()
    checks = []
    probes = [
        ("GET /crm/v3/owners", lambda: client._request("GET", "/crm/v3/owners", params={"limit": 1})),
        ("GET /crm/v3/objects/calls", lambda: client._request(
            "GET", "/crm/v3/objects/calls", params={"limit": 1, "properties": "hs_call_direction"})),
        ("POST /crm/v3/objects/calls/search", lambda: client._search_calls({"limit": 1, "properties": ["hs_call_direction"]})),
        ("GET /calling/v1/dispositions", client.list_call_dispositions),
    ]
    for endpoint, probe in probes:
        try:
            result = probe()
            detail = None
            if isinstance(result, dict) and "total" in result:
                detail = f"{result['total']} total"
            checks.append({"endpoint": endpoint, "ok": True, "status": 200, "detail": detail})
        except hubspot_client.HubSpotError as exc:
            checks.append({"endpoint": endpoint, "ok": False, "status": exc.status,
                           "required_scopes": exc.required_scopes, "detail": str(exc)})
    required = checks[:3]
    return {"ok": all(c["ok"] for c in required), "checks": checks}
