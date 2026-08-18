"""Tracks WHEN each account entered Needs Attention (`needs_attention_since`,
missing from the data model before 2026-08-18) and aggregates activity
for the Admin Portal's accountability metrics. Same appdb/psycopg2
pattern as attention_store.py/user_store.py.

Needs Attention status itself is still computed purely from source data,
fresh on every request (build_sales_dataset() in sales_metrics.py) --
this module never decides *whether* an account is Needs Attention, only
*since when*, for accounts the caller already knows are. Nothing here is
imported by sales_metrics.py.

AGE_THRESHOLD_DAYS is the ONE place the "15 days" number lives --
permissions.py imports it rather than hardcoding its own copy, per the
project's explicit "don't duplicate the 15-day rule across components"
requirement."""

from datetime import datetime, timezone

import db
import db_migrations

AGE_THRESHOLD_DAYS = 15


def sync_tracking(current_sale_ids):
    """Upserts a first_seen_at row (defaulted to now(), never overwritten
    once set) for every sale_id newly observed as Needs Attention, and
    removes tracking for any sale_id no longer in that population -- so
    an account that leaves Needs Attention (installed, reclassified) and
    later re-enters starts a fresh 15-day clock rather than inheriting a
    stale timestamp. Call once per data refresh with the FULL current
    Needs Attention sale_id population (see app.py's load_all_data()).
    Silently no-ops on failure -- a tracking-sync failure must never
    block the dashboard itself from loading."""
    ids = sorted({int(s) for s in current_sale_ids if s is not None})
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                if ids:
                    cur.execute(
                        """
                        INSERT INTO needs_attention_tracking (sale_id)
                        SELECT unnest(%s::bigint[])
                        ON CONFLICT (sale_id) DO NOTHING
                        """,
                        (ids,),
                    )
                    cur.execute(
                        "DELETE FROM needs_attention_tracking WHERE sale_id != ALL(%s::bigint[])",
                        (ids,),
                    )
                else:
                    cur.execute("DELETE FROM needs_attention_tracking")
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def get_first_seen_map(sale_ids):
    """{sale_id: first_seen_at datetime}. Empty dict (never raises) if
    appdb is unreachable or sale_ids is empty. A sale_id missing from the
    result (tracking not yet synced this cycle) should be treated as
    "just entered" (0 days), never as an error."""
    ids = sorted({int(s) for s in sale_ids if s is not None})
    if not ids:
        return {}
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sale_id, first_seen_at FROM needs_attention_tracking WHERE sale_id = ANY(%s)",
                (ids,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def get_needs_attention_since(sale_id):
    """Single-account convenience wrapper around get_first_seen_map(),
    used by permissions.can_work_account()."""
    return get_first_seen_map([sale_id]).get(int(sale_id))


def days_since(first_seen_at):
    """Fractional days elapsed since `first_seen_at` (None -> 0.0, i.e.
    "just entered," never an error). Exact elapsed time, not calendar-
    date rounding -- "15 full days" per the spec."""
    if first_seen_at is None:
        return 0.0
    return (datetime.now(timezone.utc) - first_seen_at).total_seconds() / 86400.0


def get_activity_counts(user_ids):
    """{user_id: {"last_7_days": N, "last_30_days": N}} -- counts of
    meaningful Needs Attention actions (account_attention_notes rows)
    PERFORMED BY each user (acting_user_id), not accounts they own.
    Empty dict (never raises) if appdb is unreachable."""
    ids = sorted({int(u) for u in user_ids if u is not None})
    if not ids:
        return {}
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT acting_user_id,
                       count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS last_7,
                       count(*) FILTER (WHERE created_at >= now() - interval '30 days') AS last_30
                FROM account_attention_notes
                WHERE acting_user_id = ANY(%s)
                GROUP BY acting_user_id
                """,
                (ids,),
            )
            return {row[0]: {"last_7_days": row[1], "last_30_days": row[2]} for row in cur.fetchall()}
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def get_rep_needs_attention_stats(rep_sale_ids):
    """Given {rep_name: [sale_id, ...]} for the CURRENT Needs Attention
    population (computed by the caller from sales_metrics.py -- this
    module never decides who's in Needs Attention, only how long),
    returns {rep_name: {"count", "aged_15_plus", "oldest_days"}} using
    needs_attention_tracking. A rep with accounts not yet synced this
    cycle treats them as 0 days old rather than erroring."""
    all_ids = [sid for ids in rep_sale_ids.values() for sid in ids]
    first_seen = get_first_seen_map(all_ids)
    stats = {}
    for rep, sale_ids in rep_sale_ids.items():
        ages = [days_since(first_seen.get(sid)) for sid in sale_ids]
        stats[rep] = {
            "count": len(sale_ids),
            "aged_15_plus": sum(1 for age in ages if age >= AGE_THRESHOLD_DAYS),
            "oldest_days": int(max(ages)) if ages else 0,
        }
    return stats
