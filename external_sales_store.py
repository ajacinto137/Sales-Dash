"""External Rep Sales -- door-to-door sales made under external referral
codes (Admin Portal > External Rep Sales, templates/admin/
external_rep_sales.html). See README.md "External Rep Sales".

Data flow: PlanetWeb (read-only, queries.EXTERNAL_REP_SALES_QUERY -- the
VBA workbook's Refresh_Main() query, verbatim) -> appdb external_rep_sales
(db_migrations.py migration 8) -> admin page / CSV export. Page requests
only read appdb, so a PlanetWeb outage leaves the last synced data in
place and the page says the sync is failing instead of showing zero sales.

Sync rules:
  - Every sync pulls the full query result (~5k rows, a few seconds) and
    upserts it by PlanetWeb's own order ID -- no duplicates, IDs kept.
  - Nothing is ever deleted. A row a successful refresh no longer returns
    is flagged in_source = false and hidden by default; it is restored if
    it comes back. A failed refresh changes nothing, and a refresh that
    returns suspiciously few rows (< MIN_COMPLETE_RATIO of what's stored)
    still upserts but flags nothing as removed.
  - One sync at a time across threads/processes (Postgres advisory lock,
    same as hubspot_store.py).

Referral codes are mapped to reps/organizations/managers in external_reps
(one row per code, auto-created when a code first appears). A code is
never assumed to be one person -- see migration 8's comments."""

import csv
import io
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from psycopg2.extras import execute_values

import db
import db_migrations
from queries import EXTERNAL_REP_SALES_QUERY

SYNC_INTERVAL_SECONDS = max(60, int(os.environ.get("EXTERNAL_SALES_SYNC_INTERVAL_SECONDS", "300")))
MAX_BACKOFF_SECONDS = 30 * 60
RUN_HISTORY_DAYS = 14
SYNC_LOCK_KEY = 0x45585452  # "EXTR"
# A successful refresh returning fewer rows than this share of the rows
# currently in source is treated as incomplete: nothing gets flagged as
# removed from it.
MIN_COMPLETE_RATIO = 0.5

PAGE_SIZE_OPTIONS = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50

SCHEDULED = "Scheduled"
NOT_SCHEDULED = "Not Scheduled"
UNMAPPED_ORG = "__unmapped__"

# (key, source query column, appdb column, dashboard label, shown by default).
# The ONE place the query's 21 fields, appdb columns, table headers and
# CSV headers come from, so they can't drift apart.
COLUMNS = [
    ("order_id", "ID", "order_id", "Order ID", True),
    ("package_speed", "Speed", "package_speed", "Package Speed", True),
    ("first_name", "FirstName", "first_name", "First Name", True),
    ("last_name", "LastName", "last_name", "Last Name", True),
    ("email", "EmailAddress", "email", "Email", False),
    ("address", "Address", "address", "Address", True),
    ("apartment_suite", "ApartmentSuite", "apartment_suite", "Apartment / Suite", False),
    ("city", "City", "city", "City", True),
    ("state", "State", "state", "State", True),
    ("zipcode", "Zipcode", "zipcode", "ZIP Code", False),
    ("referral_code", "ReferralData", "referral_code", "External Rep", True),
    ("sale_date", "Date", "sale_date", "Sale Date", True),
    ("q1", "Q1", "q1", "Q1", False),
    ("phone_number", "PhoneNumber", "phone_number", "Phone Number", False),
    ("municipality", "Municipality", "municipality", "Municipality", True),
    ("payment_method", "Cc_PayBy", "payment_method", "Payment Method", False),
    ("subscriber_id", "vi_subscriber_id", "subscriber_id", "Subscriber ID", False),
    ("subscriber_uuid", "vi_subscriber_uuid", "subscriber_uuid", "Subscriber UUID", False),
    ("voice", "Voice", "voice", "Voice", True),
    ("scheduled_status", "Scheduled", "scheduled_status", "Scheduling Status", True),
    ("installation_date", "StartDate", "installation_date", "Installation Date", True),
]
COLUMN_KEYS = [c[0] for c in COLUMNS]
_DB_COLUMNS = [c[2] for c in COLUMNS]
_DATA_COLUMNS = [c for c in _DB_COLUMNS if c != "order_id"]

# Sort key -> SQL expression. Every sort ends with order_id as a tiebreak
# so paging is stable.
_SORT_SQL = {key: f"s.{col}" for key, _src, col, _label, _shown in COLUMNS}
DEFAULT_SORT = "sale_date"
# Columns whose first click sorts newest/largest first.
_DESC_FIRST = {"order_id", "sale_date", "installation_date", "package_speed", "voice"}

_SEARCH_SQL = [
    "CAST(s.order_id AS TEXT)",
    "s.first_name",
    "s.last_name",
    "concat_ws(' ', s.first_name, s.last_name)",
    "s.email",
    "s.phone_number",
    "s.address",
    "s.apartment_suite",
    "s.city",
    "s.zipcode",
    "s.subscriber_id",
    "s.referral_code",
    "r.rep_name",
]

_FROM_SQL = """
    FROM external_rep_sales s
    LEFT JOIN external_reps r ON r.referral_code = s.referral_code
    LEFT JOIN external_organizations o ON o.id = r.organization_id
"""


def interval_display():
    minutes, seconds = divmod(SYNC_INTERVAL_SECONDS, 60)
    if seconds:
        return f"{SYNC_INTERVAL_SECONDS} seconds"
    return "minute" if minutes == 1 else f"{minutes} minutes"


def _connect():
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    return conn


def _log(message):
    print(f"[external-sales] {db.sanitize_error(message)}")


# ================================================================
# Sync
# ================================================================

def fetch_source_rows():
    """Runs the spec query against PlanetWeb. Returns a list of dicts
    keyed by appdb column. Raises on any connection/query failure."""
    conn = db.get_planetweb_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(EXTERNAL_REP_SALES_QUERY)
        returned = [d[0] for d in cursor.description]
        expected = [src for _key, src, _col, _label, _shown in COLUMNS]
        if returned != expected:
            raise RuntimeError(f"External Rep Sales query returned unexpected columns: {returned}")
        return [dict(zip(_DB_COLUMNS, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def _normalize(row):
    row = dict(row)
    row["order_id"] = int(row["order_id"])
    row["voice"] = bool(row["voice"])
    return row


def _update_run(conn, run_id, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    with conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE external_sales_sync_runs SET {sets} WHERE id = %s", (*fields.values(), run_id))


def run_sync(trigger="auto", fetch=fetch_source_rows):
    """One full refresh. Returns (ran, summary_or_reason); ran=False means
    another sync already holds the lock (not an error)."""
    lock_conn = _connect()
    try:
        lock_conn.autocommit = True
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
            if not cur.fetchone()[0]:
                return False, "An External Rep Sales refresh is already running."
            cur.execute("""
                UPDATE external_sales_sync_runs SET status = 'failed', finished_at = now(),
                       error = coalesce(error, 'Interrupted (process stopped mid-sync)')
                WHERE status = 'running'
            """)
        return True, _run_sync_locked(trigger, fetch)
    finally:
        try:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        except Exception:
            pass
        lock_conn.close()


def _run_sync_locked(trigger, fetch):
    conn = _connect()
    summary = {"rows_fetched": None, "new_rows": 0, "updated_rows": 0, "removed_rows": 0,
               "restored_rows": 0, "warning": None, "error": None}
    run_id = None
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE external_sales_sync_state SET last_attempt_at = now() WHERE id = 1")
                cur.execute(
                    "INSERT INTO external_sales_sync_runs (trigger, status) VALUES (%s, 'running') RETURNING id",
                    (trigger,),
                )
                run_id = cur.fetchone()[0]

        try:
            source_rows = fetch()
        except Exception as exc:
            raise _SourceError(exc) from exc

        incoming = {}
        for row in source_rows:
            row = _normalize(row)
            incoming[row["order_id"]] = row
        if len(incoming) != len(source_rows):
            summary["warning"] = f"Source returned {len(source_rows) - len(incoming)} duplicate order ID(s); kept one row each."
        summary["rows_fetched"] = len(incoming)

        with conn:
            with conn.cursor() as cur:
                _apply_snapshot(cur, list(incoming.values()), summary)
                cur.execute(
                    """
                    UPDATE external_sales_sync_state SET last_success_at = now(), last_error = NULL,
                        connection_ok = true, source_row_count = %s
                    WHERE id = 1
                    """,
                    (summary["rows_fetched"],),
                )
        _update_run(conn, run_id, status="success", finished_at=datetime.now(timezone.utc),
                    rows_fetched=summary["rows_fetched"], new_rows=summary["new_rows"],
                    updated_rows=summary["updated_rows"], removed_rows=summary["removed_rows"],
                    restored_rows=summary["restored_rows"], warning=summary["warning"])
        changed = summary["new_rows"] or summary["updated_rows"] or summary["removed_rows"] or summary["restored_rows"]
        if trigger != "auto" or changed or summary["warning"]:
            _log(f"Sync #{run_id} ({trigger}) finished: {summary['rows_fetched']} fetched, "
                 f"{summary['new_rows']} new, {summary['updated_rows']} updated, "
                 f"{summary['removed_rows']} no longer in source, {summary['restored_rows']} restored"
                 + (f" -- {summary['warning']}" if summary["warning"] else ""))
        _prune_run_history(conn)
    except Exception as exc:
        source_failed = isinstance(exc, _SourceError)
        message = db.sanitize_error(exc.__cause__ if source_failed else exc)
        summary["error"] = message
        _log(f"Sync #{run_id} ({trigger}) failed{' (PlanetWeb)' if source_failed else ''}: {message}")
        try:
            conn.rollback()
            with conn:
                with conn.cursor() as cur:
                    # connection_ok tracks PlanetWeb only -- an appdb
                    # hiccup says nothing about the source connection.
                    cur.execute(
                        """
                        UPDATE external_sales_sync_state SET last_error = %s,
                            connection_ok = CASE WHEN %s THEN false ELSE connection_ok END
                        WHERE id = 1
                        """,
                        (message, source_failed),
                    )
            if run_id is not None:
                _update_run(conn, run_id, status="failed", finished_at=datetime.now(timezone.utc), error=message)
        except Exception:
            pass
    finally:
        conn.close()
    return summary


class _SourceError(Exception):
    pass


def _apply_snapshot(cur, rows, summary):
    """Merges one complete query result into external_rep_sales inside the
    caller's transaction (all-or-nothing)."""
    cols = ", ".join(_DB_COLUMNS)
    cur.execute(
        "CREATE TEMP TABLE _ext_incoming ON COMMIT DROP AS "
        f"SELECT {cols} FROM external_rep_sales WITH NO DATA"
    )
    if rows:
        execute_values(
            cur,
            f"INSERT INTO _ext_incoming ({cols}) VALUES %s",
            [tuple(row[c] for c in _DB_COLUMNS) for row in rows],
            page_size=1000,
        )

    cur.execute(f"""
        INSERT INTO external_rep_sales ({cols})
        SELECT {cols} FROM _ext_incoming i
        WHERE NOT EXISTS (SELECT 1 FROM external_rep_sales s WHERE s.order_id = i.order_id)
    """)
    summary["new_rows"] = cur.rowcount

    set_list = ", ".join(f"{c} = i.{c}" for c in _DATA_COLUMNS)
    old_row = ", ".join(f"s.{c}" for c in _DATA_COLUMNS)
    new_row = ", ".join(f"i.{c}" for c in _DATA_COLUMNS)
    cur.execute(f"""
        UPDATE external_rep_sales s SET {set_list}, updated_at = now()
        FROM _ext_incoming i
        WHERE s.order_id = i.order_id AND ROW({old_row}) IS DISTINCT FROM ROW({new_row})
    """)
    summary["updated_rows"] = cur.rowcount

    cur.execute("""
        UPDATE external_rep_sales s SET in_source = true, removed_from_source_at = NULL
        FROM _ext_incoming i WHERE s.order_id = i.order_id AND NOT s.in_source
    """)
    summary["restored_rows"] = cur.rowcount

    cur.execute("""
        UPDATE external_rep_sales s SET last_seen_at = now()
        FROM _ext_incoming i WHERE s.order_id = i.order_id
    """)

    cur.execute("SELECT count(*) FROM external_rep_sales WHERE in_source")
    in_source_now = cur.fetchone()[0]
    previously_in_source = in_source_now - summary["new_rows"] - summary["restored_rows"]
    if not rows or (previously_in_source >= 20 and len(rows) < previously_in_source * MIN_COMPLETE_RATIO):
        note = (f"Source returned {len(rows)} rows vs {previously_in_source} stored -- "
                "treated as incomplete, so no rows were marked as removed.")
        summary["warning"] = f"{summary['warning']} {note}" if summary["warning"] else note
    else:
        cur.execute("""
            UPDATE external_rep_sales s SET in_source = false, removed_from_source_at = now()
            WHERE s.in_source AND NOT EXISTS (SELECT 1 FROM _ext_incoming i WHERE i.order_id = s.order_id)
        """)
        summary["removed_rows"] = cur.rowcount

    # Every code that has sales gets a mapping row (unmapped until an
    # Admin fills it in), so no code's sales are ever unattributed.
    cur.execute("""
        INSERT INTO external_reps (referral_code)
        SELECT DISTINCT referral_code FROM _ext_incoming WHERE referral_code IS NOT NULL AND referral_code <> ''
        ON CONFLICT (referral_code) DO NOTHING
    """)


def _prune_run_history(conn):
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM external_sales_sync_runs WHERE status <> 'running' "
                    "AND started_at < now() - %s * interval '1 day'",
                    (RUN_HISTORY_DAYS,),
                )
    except Exception as exc:
        _log(f"Pruning sync history failed: {db.sanitize_error(exc)}")


_manual_thread = None
_manual_thread_lock = threading.Lock()


def start_sync_in_background(trigger="manual"):
    """Admin "Refresh Data" button: runs run_sync() on a thread so the
    request returns immediately; the page polls get_status()."""
    global _manual_thread
    with _manual_thread_lock:
        if _manual_thread is not None and _manual_thread.is_alive():
            return False, "A refresh is already running."
        _manual_thread = threading.Thread(target=_safe_run_sync, args=(trigger,),
                                          name="external-sales-sync-manual", daemon=True)
        _manual_thread.start()
    return True, None


def _safe_run_sync(trigger):
    """Never raises. False only when a sync ran and failed (drives the
    scheduler's backoff)."""
    try:
        ran, result = run_sync(trigger)
        if not ran:
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
    time.sleep(15)
    failures = 0
    while True:
        failures = 0 if _safe_run_sync("auto") else failures + 1
        delay = SYNC_INTERVAL_SECONDS if not failures else min(MAX_BACKOFF_SECONDS, SYNC_INTERVAL_SECONDS * 2 ** failures)
        if failures:
            _log(f"Automatic sync failed {failures}x in a row; next attempt in {delay}s")
        time.sleep(delay)


def start_scheduler():
    """Idempotent. The advisory lock in run_sync() keeps it safe even if
    more than one process ever runs this."""
    global _scheduler_started
    if _scheduler_started:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=_scheduler_loop, name="external-sales-sync", daemon=True).start()
        _scheduler_started = True
        _log(f"Automatic sync scheduled every {interval_display()}")


# ================================================================
# Status
# ================================================================

def get_status():
    """`available` False means appdb itself is unreachable."""
    status = {
        "available": False, "error": None, "state": {}, "latest_run": None,
        "stored_total": 0, "in_source_total": 0, "removed_total": 0,
        "codes_total": 0, "codes_unmapped": 0,
        "sync_interval_display": interval_display(),
    }
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT last_success_at, last_attempt_at, last_error, connection_ok, source_row_count
                FROM external_sales_sync_state WHERE id = 1
            """)
            row = cur.fetchone()
            if row:
                status["state"] = dict(zip(
                    ["last_success_at", "last_attempt_at", "last_error", "connection_ok", "source_row_count"], row))
            cur.execute("""
                SELECT id, trigger, status, started_at, finished_at, rows_fetched, new_rows,
                       updated_rows, removed_rows, restored_rows, warning, error
                FROM external_sales_sync_runs ORDER BY started_at DESC, id DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                status["latest_run"] = dict(zip(
                    ["id", "trigger", "status", "started_at", "finished_at", "rows_fetched", "new_rows",
                     "updated_rows", "removed_rows", "restored_rows", "warning", "error"], row))
            cur.execute("SELECT count(*), count(*) FILTER (WHERE in_source) FROM external_rep_sales")
            status["stored_total"], status["in_source_total"] = cur.fetchone()
            status["removed_total"] = status["stored_total"] - status["in_source_total"]
            cur.execute("""
                SELECT count(*), count(*) FILTER (WHERE rep_name IS NULL AND organization_id IS NULL)
                FROM external_reps
            """)
            status["codes_total"], status["codes_unmapped"] = cur.fetchone()
        status["available"] = True
    except Exception as exc:
        status["error"] = db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
    return status


# ================================================================
# Sales table / summary / export
# ================================================================

def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def parse_filters(args):
    """Normalizes request.args into the filter dict every query below
    takes. Unknown/invalid values are dropped, never passed to SQL."""
    date_from, date_to = _parse_date(args.get("date_from")), _parse_date(args.get("date_to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    speed = (args.get("speed") or "").strip()
    scheduled = (args.get("scheduled") or "").strip()
    sort = args.get("sort") if args.get("sort") in _SORT_SQL else DEFAULT_SORT
    direction = (args.get("dir") or "").lower()
    if direction not in ("asc", "desc"):
        direction = "desc" if sort in _DESC_FIRST else "asc"
    try:
        page = max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(args.get("per_page", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PAGE_SIZE
    return {
        "search": (args.get("search") or "").strip()[:200],
        "date_from": date_from,
        "date_to": date_to,
        "rep": (args.get("rep") or "").strip(),
        "organization": (args.get("organization") or "").strip(),
        "state": (args.get("state") or "").strip(),
        "municipality": (args.get("municipality") or "").strip(),
        "speed": int(speed) if speed.isdigit() else None,
        "scheduled": scheduled if scheduled in (SCHEDULED, NOT_SCHEDULED) else "",
        "include_removed": (args.get("include_removed") or "") in ("1", "true", "on"),
        "sort": sort,
        "dir": direction,
        "page": page,
        "per_page": per_page if per_page in PAGE_SIZE_OPTIONS else DEFAULT_PAGE_SIZE,
    }


def _build_where(f):
    clauses, params = [], []
    if not f["include_removed"]:
        clauses.append("s.in_source")
    if f["date_from"]:
        clauses.append("s.sale_date >= %s")
        params.append(f["date_from"])
    if f["date_to"]:
        clauses.append("s.sale_date <= %s")
        params.append(f["date_to"])
    if f["rep"]:
        clauses.append("s.referral_code = %s")
        params.append(f["rep"])
    if f["organization"] == UNMAPPED_ORG:
        clauses.append("r.organization_id IS NULL")
    elif f["organization"].isdigit():
        clauses.append("r.organization_id = %s")
        params.append(int(f["organization"]))
    if f["state"]:
        clauses.append("s.state = %s")
        params.append(f["state"])
    if f["municipality"]:
        clauses.append("s.municipality = %s")
        params.append(f["municipality"])
    if f["speed"] is not None:
        clauses.append("s.package_speed = %s")
        params.append(f["speed"])
    if f["scheduled"]:
        clauses.append("s.scheduled_status = %s")
        params.append(f["scheduled"])
    if f["search"]:
        like = "%" + re.sub(r"([\\%_])", r"\\\1", f["search"]) + "%"
        clauses.append("(" + " OR ".join(f"{col} ILIKE %s" for col in _SEARCH_SQL) + ")")
        params.extend([like] * len(_SEARCH_SQL))
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _order_by(f):
    direction = "ASC" if f["dir"] == "asc" else "DESC"
    return f"ORDER BY {_SORT_SQL[f['sort']]} {direction} NULLS LAST, s.order_id {direction}"


_ROW_SELECT = ", ".join(f"s.{c}" for c in _DB_COLUMNS) + ", s.in_source, r.rep_name, o.name"


def _row_dict(row):
    d = dict(zip(_DB_COLUMNS, row[:len(_DB_COLUMNS)]))
    d["in_source"], d["rep_name"], d["organization_name"] = row[len(_DB_COLUMNS):]
    return d


def query_sales(f):
    """One page of rows plus summary cards for the current filters.
    Never raises: ok False + error on an appdb failure."""
    result = {"ok": False, "error": None, "rows": [], "total": 0, "page": 1, "total_pages": 1,
              "summary": None}
    where_sql, params = _build_where(f)
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT count(*),
                       count(DISTINCT s.order_id),
                       count(DISTINCT s.referral_code),
                       count(*) FILTER (WHERE s.scheduled_status = %s),
                       count(*) FILTER (WHERE s.scheduled_status = %s),
                       count(*) FILTER (WHERE s.voice),
                       min(s.sale_date), max(s.sale_date)
                {_FROM_SQL} {where_sql}
            """, [SCHEDULED, NOT_SCHEDULED] + params)
            total, orders, reps, scheduled, unscheduled, voice, earliest, latest = cur.fetchone()
            result["summary"] = {
                "total_sales": orders,
                "active_reps": reps,
                "scheduled": scheduled,
                "unscheduled": unscheduled,
                "voice": voice,
                "voice_rate": (voice / total * 100) if total else None,
                "scheduled_rate": (scheduled / total * 100) if total else None,
                "earliest": earliest,
                "latest": latest,
            }
            total_pages = max(1, -(-total // f["per_page"]))
            page = min(f["page"], total_pages)
            cur.execute(
                f"SELECT {_ROW_SELECT} {_FROM_SQL} {where_sql} {_order_by(f)} LIMIT %s OFFSET %s",
                params + [f["per_page"], (page - 1) * f["per_page"]],
            )
            result.update(
                ok=True, rows=[_row_dict(r) for r in cur.fetchall()],
                total=total, page=page, total_pages=total_pages,
            )
    except Exception as exc:
        result["error"] = db.sanitize_error(exc)
        _log(f"Sales query failed: {result['error']}")
    finally:
        if conn is not None:
            conn.close()
    return result


def get_filter_options():
    """Distinct values for the filter dropdowns (all rows still in
    source, not cross-filtered). Empty lists on failure."""
    options = {"reps": [], "organizations": [], "states": [], "municipalities": [], "speeds": []}
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.referral_code, max(r.rep_name), count(*)
                FROM external_rep_sales s LEFT JOIN external_reps r ON r.referral_code = s.referral_code
                WHERE s.in_source AND s.referral_code IS NOT NULL
                GROUP BY s.referral_code ORDER BY s.referral_code
            """)
            options["reps"] = [{"code": c, "name": n, "sales": cnt} for c, n, cnt in cur.fetchall()]
            cur.execute("SELECT id, name FROM external_organizations ORDER BY lower(name)")
            options["organizations"] = [{"id": i, "name": n} for i, n in cur.fetchall()]
            for key, col in (("states", "state"), ("municipalities", "municipality"), ("speeds", "package_speed")):
                cur.execute(f"SELECT DISTINCT {col} FROM external_rep_sales WHERE in_source AND {col} IS NOT NULL ORDER BY {col}")
                options[key] = [r[0] for r in cur.fetchall()]
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return options


EXPORT_HEADERS = [label for _key, _src, _col, label, _shown in COLUMNS] + ["Rep Name", "Organization", "In Source"]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    # Neutralize spreadsheet formulas (CSV injection) in free-text fields.
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def iter_export_csv(f):
    """Yields the CSV (header + every row matching the filters, in the
    table's sort order) in chunks. Raises before the first chunk if appdb
    is unreachable, so the route can return a proper error instead."""
    where_sql, params = _build_where(f)
    conn = _connect()
    try:
        cur = conn.cursor(name="external_sales_export")
        cur.itersize = 1000
        cur.execute(f"SELECT {_ROW_SELECT} {_FROM_SQL} {where_sql} {_order_by(f)}", params)
    except Exception:
        conn.close()
        raise

    def generate():
        try:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(EXPORT_HEADERS)
            count = 0
            for row in cur:
                d = _row_dict(row)
                writer.writerow([_csv_value(d[c]) for c in _DB_COLUMNS]
                                + [_csv_value(d["rep_name"]), _csv_value(d["organization_name"]),
                                   "Yes" if d["in_source"] else "No"])
                count += 1
                if count % 500 == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
            yield buf.getvalue()
        finally:
            cur.close()
            conn.close()

    return generate()


# ================================================================
# Referral code -> rep / organization / manager mapping
# ================================================================

def list_rep_mappings():
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id, r.referral_code, r.rep_name, r.rep_email, r.organization_id, o.name,
                       r.manager_name, r.manager_email, r.dashboard_user_id, u.email, r.active,
                       r.updated_at, ub.email,
                       count(s.order_id) FILTER (WHERE s.in_source), max(s.sale_date) FILTER (WHERE s.in_source)
                FROM external_reps r
                LEFT JOIN external_organizations o ON o.id = r.organization_id
                LEFT JOIN users u ON u.id = r.dashboard_user_id
                LEFT JOIN users ub ON ub.id = r.updated_by
                LEFT JOIN external_rep_sales s ON s.referral_code = r.referral_code
                GROUP BY r.id, o.name, u.email, ub.email
                ORDER BY count(s.order_id) FILTER (WHERE s.in_source) DESC, r.referral_code
            """)
            keys = ["id", "referral_code", "rep_name", "rep_email", "organization_id", "organization_name",
                    "manager_name", "manager_email", "dashboard_user_id", "dashboard_user_email", "active",
                    "updated_at", "updated_by_email", "sales", "last_sale_date"]
            return [dict(zip(keys, r)) for r in cur.fetchall()]
    except Exception as exc:
        _log(f"Listing rep mappings failed: {db.sanitize_error(exc)}")
        return []
    finally:
        if conn is not None:
            conn.close()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def update_rep_mapping(rep_id, form, acting_user_id):
    """Saves one referral code's mapping from the admin form. The
    organization is given by name and created on first use. Returns
    (ok, error)."""
    def clean(name, limit=200):
        value = (form.get(name) or "").strip()
        return value[:limit] or None

    rep_name, rep_email = clean("rep_name"), clean("rep_email")
    manager_name, manager_email = clean("manager_name"), clean("manager_email")
    organization = clean("organization")
    for label, email in (("Rep email", rep_email), ("Manager email", manager_email)):
        if email and not _EMAIL_RE.match(email):
            return False, f"{label} doesn't look like an email address."
    user_raw = (form.get("dashboard_user_id") or "").strip()
    if user_raw and not user_raw.isdigit():
        return False, "Choose a dashboard user."
    dashboard_user_id = int(user_raw) if user_raw else None
    active = (form.get("active") or "") in ("1", "true", "on")

    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM external_reps WHERE id = %s FOR UPDATE", (rep_id,))
                if not cur.fetchone():
                    return False, "That referral code no longer exists."
                if dashboard_user_id is not None:
                    cur.execute("SELECT 1 FROM users WHERE id = %s", (dashboard_user_id,))
                    if not cur.fetchone():
                        return False, "That dashboard user no longer exists."
                organization_id = None
                if organization:
                    cur.execute(
                        """
                        INSERT INTO external_organizations (name) VALUES (%s)
                        ON CONFLICT (name) DO UPDATE SET updated_at = external_organizations.updated_at
                        RETURNING id
                        """,
                        (organization,),
                    )
                    organization_id = cur.fetchone()[0]
                cur.execute(
                    """
                    UPDATE external_reps SET rep_name = %s, rep_email = %s, organization_id = %s,
                        manager_name = %s, manager_email = %s, dashboard_user_id = %s, active = %s,
                        updated_at = now(), updated_by = %s
                    WHERE id = %s
                    """,
                    (rep_name, rep_email, organization_id, manager_name, manager_email,
                     dashboard_user_id, active, acting_user_id, rep_id),
                )
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
