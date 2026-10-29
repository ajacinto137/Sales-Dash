"""Lead Feeder (main nav, added 2026-10-28) -- the shared HubSpot SQL
call list that used to run as a claude.ai artifact on each rep's own
machine (Lead Feeder/lead-feeder-code.txt), moved onto the dashboard.

What replaced each claude.ai runtime piece:
  * HubSpot connector (mcp)  -> the dashboard's own server-side service
    key via hubspot_client.HubSpotClient. Reps never connect anything.
  * shared db "served"       -> lead_feeder_claims (+ lead_feeder_events
    audit trail), db_migrations.py migration 21. One claim per contact
    per Eastern day; the UNIQUE key makes claiming race-safe.
  * shared db "pas_sweep" / "priority" / "learning/weights"
                             -> lead_feeder_sweep / lead_feeder_priority /
    lead_feeder_weights, loaded by an Admin (Admin > Lead Feeder).
  * user identity            -> the dashboard login.
  * localStorage settings    -> lead_feeder_prefs, per user.
  * downloads                -> a plain CSV response.
  * Planet Maps userscript   -> PlanetWeb place/order lookup by address
    from the data this app already loads (passings_cache, order_search).

Instead of every rep's browser downloading the whole pool, ONE background
thread pulls it from HubSpot every REFRESH_SECONDS (the page's 3 minutes)
and each rep's filters are applied to that shared copy. Rules and scoring:
lead_feeder_logic.py. Reads return safe empty values when a database is
down; a failed HubSpot refresh keeps serving the last good pool."""

import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import db
import db_migrations
import hubspot_client
import hubspot_logic
import lead_feeder_logic as L
import order_search
import passings_cache
import passings_classification
from queries import pas_place_url

REFRESH_SECONDS = 3 * 60
INTEL_SECONDS = 60 * 60
MAX_BACKOFF_SECONDS = 15 * 60
MAX_TAKE_ATTEMPTS = 25        # leads tried per "Take the next lead" before giving up
LIST_LIMIT = 60               # rows shown on the board (the page showed 60)
ACTIVE_SECONDS = 5 * 60       # "using Lead Feeder now" on the admin page
LIST_ROWS_MAX = 1000          # the page read up to 1000 sweep / call-first rows

# Permissive prefs for the shared pool: keep everything ANY rep's
# filters could show; each rep's own prefs narrow it per request.
_POOL_PREFS = dict(L.DEFAULT_PREFS, cool_days=1, quiet_on=True, sweep_on=True,
                   sweep_active_on=True, sweep_manual_on=True)

_state = {
    "entries": [],          # [{"id", "variants": [{"p", "s"}, ...]}], see _build_entries()
    "version": 0,
    "loaded_at": None,      # time.time() of the last successful pool refresh
    "error": None,
    "stats": None,
    "intel": None,          # see _measure_intel()
    "intel_at": None,
    "intel_error": None,
    "weights": dict(L.DEFAULT_WEIGHTS),
    "weights_info": None,
}
_refresh_lock = threading.Lock()
_list_cache = {}
# app.py sets this to a callable returning its loaded Vision packages
# DataFrame (VISION_PACKAGES_QUERY) -- the household check's "current"
# test. Until it returns data the household check is skipped.
vision_packages_source = None
_current_cache = {}           # (user_id, contact_id) -> lead details shown on the "current" card


def _log(message):
    print(f"[lead-feeder] {hubspot_client.sanitize(message)}", flush=True)


def _connect():
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    return conn


def _now():
    return datetime.now(timezone.utc)


def _today():
    return L.day_start(_now()).date()


def _display_name(user):
    if not user:
        return None
    return user.get("display_name") or user.get("sales_rep_name") or user.get("email")


# ================================================================
# HubSpot pool (shared, refreshed in the background)
# ================================================================

def _pool_queries(now):
    """The page's loadPool() filter groups, one set per state so each
    query stays under search's 10,000-result cap. HubSpot allows at most
    6 filters per group."""
    day_iso = L.day_start(now).astimezone(timezone.utc).isoformat()
    quiet_iso = (now - timedelta(days=L.QUIET_DAYS)).astimezone(timezone.utc).isoformat()
    av = {"propertyName": "availability", "operator": "EQ", "value": "1"}
    ls = {"propertyName": "hs_lead_status", "operator": "NOT_IN", "values": L.BAD_STATUS}
    sql_st = {"propertyName": "lifecyclestage", "operator": "EQ", "value": "salesqualifiedlead"}
    no_owner = {"propertyName": "hubspot_owner_id", "operator": "NOT_HAS_PROPERTY"}
    has_owner = {"propertyName": "hubspot_owner_id", "operator": "HAS_PROPERTY"}
    queries = []
    for state in ("NJ", "VA"):
        st = {"propertyName": "state", "operator": "IN", "values": L.STATE_VALS[state]}
        base = [st, sql_st, no_owner, av, ls]
        # 1) every open, unowned SQL not called today (each rep's longer cool-down is applied per request)
        queries.append(("sql", [
            {"filters": [*base, {"propertyName": "notes_last_contacted", "operator": "LT", "value": day_iso}]},
            {"filters": [*base, {"propertyName": "notes_last_contacted", "operator": "NOT_HAS_PROPERTY"}]},
        ]))
        # 3) owned SQLs nobody has called in 30+ days
        qbase = [st, sql_st, has_owner, av, ls]
        queries.append(("quiet", [
            {"filters": [*qbase, {"propertyName": "notes_last_contacted", "operator": "LT", "value": quiet_iso}]},
            {"filters": [*qbase, {"propertyName": "notes_last_contacted", "operator": "NOT_HAS_PROPERTY"}]},
        ]))
    return queries


def _load_list_rows(table):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if table == "priority":
                cur.execute("SELECT contact_id, rank, reason FROM lead_feeder_priority ORDER BY rank NULLS LAST LIMIT %s",
                            (LIST_ROWS_MAX,))
                return {str(r[0]): {"rank": r[1], "reason": r[2]} for r in cur.fetchall()}
            cur.execute("SELECT contact_id, data FROM lead_feeder_sweep ORDER BY added_at DESC LIMIT %s", (LIST_ROWS_MAX,))
            return {str(r[0]): dict(r[1] or {}) for r in cur.fetchall()}
    finally:
        conn.close()


def _load_weights():
    """learning/weights overrides, clamped exactly like the page did."""
    weights = dict(L.DEFAULT_WEIGHTS)
    info = None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT weights, note, updated_at FROM lead_feeder_weights WHERE id = 1")
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        _log(f"Couldn't read ranking weights: {db.sanitize_error(exc)}")
        return weights, None
    if row:
        for key, value in (row[0] or {}).items():
            if key not in weights:
                continue
            try:
                n = float(value)
            except (TypeError, ValueError):
                continue
            lo, hi = (0, 0.3) if key == "repeatStep" else (0, 1) if key == "repeatMax" else (0.5, 2.5)
            weights[key] = min(hi, max(lo, n))
        info = {"note": row[1] or "", "at": row[2].astimezone(L.BUSINESS_TZ).strftime("%b %-d") if row[2] else ""}
    return weights, info


def _build_entries(client, now, intel, weights):
    """The page's loadPool() + loadSweep() + loadPriority(), against the
    permissive _POOL_PREFS. Each contact keeps up to two variants: as a PAS
    sweep lead and as a regular lead -- a rep with sweep leads turned off
    (or whose sweep screen fails) still gets the regular one, like the page."""
    base, stats = {}, {"capped": False, "sweep": {"stored": 0, "out": {}}, "priority": {"stored": 0, "out": 0}}
    household = _household_checker()
    stats["household"] = {"ready": household is not None, "scheduled": 0, "current": 0, "not_current": 0}

    def check(p):
        if household:
            result = household(p)
            if result:
                stats["household"][result["status"]] += 1
        return p

    for _kind, groups in _pool_queries(now):
        rows, capped = client.search_all("contacts", groups, L.PROPS,
                                         sorts=[{"propertyName": "prequal_date", "direction": "DESCENDING"}])
        stats["capped"] |= capped
        for row in rows:
            cid, p = str(row["id"]), dict(row.get("properties") or {})
            if cid not in base and L.prescreen(check(p), now, _POOL_PREFS):
                base[cid] = p

    sweep = {}
    sweep_rows = _load_list_rows("sweep")
    stats["sweep"]["stored"] = len(sweep_rows)
    if sweep_rows:
        found, missing = client.batch_read("contacts", list(sweep_rows), L.PROPS)
        for cid, props in found.items():
            p = check(L.sweep_fill(dict(props), sweep_rows[cid]))
            why = L.sweep_reason(p, now, _POOL_PREFS)
            if why:
                stats["sweep"]["out"][why] = stats["sweep"]["out"].get(why, 0) + 1
                continue
            sweep[cid] = p
        if missing:
            stats["sweep"]["out"]["Not found in HubSpot"] = len(missing)

    prio_rows = _load_list_rows("priority")
    stats["priority"]["stored"] = len(prio_rows)
    if prio_rows:
        found, _missing = client.batch_read("contacts", list(prio_rows), L.PROPS)
        for cid, props in found.items():
            prio = prio_rows[cid]
            if cid in sweep:
                sweep[cid]["__prio"] = prio
            p = base.get(cid) or check(dict(props))
            p["__prio"] = prio
            if L.prescreen(p, now, _POOL_PREFS):
                base[cid] = p
            elif cid not in sweep:
                stats["priority"]["out"] += 1

    entries = []
    for cid in dict.fromkeys([*sweep, *base]):
        variants = [p for p in (sweep.get(cid), base.get(cid)) if p is not None]
        entries.append({"id": cid, "variants": [{"p": p, "s": L.score_lead(p, now, intel, weights)} for p in variants]})
    return entries, stats


def refresh():
    """One pool refresh (and an intelligence re-measure once an hour).
    (ok, error). A failure leaves the previous pool in place."""
    if not hubspot_client.is_configured():
        _state["error"] = "HubSpot isn't connected (no service key in Admin > HubSpot Integration)."
        return False, _state["error"]
    if not _refresh_lock.acquire(blocking=False):
        return True, None
    try:
        client = hubspot_client.HubSpotClient()
        now = _now()
        if not _state["intel_at"] or time.time() - _state["intel_at"] > INTEL_SECONDS:
            _refresh_intel(client)
        weights, info = _load_weights()
        entries, stats = _build_entries(client, now, _state["intel"], weights)
        _state.update(entries=entries, stats=stats, weights=weights, weights_info=info,
                      loaded_at=time.time(), error=None, version=_state["version"] + 1)
        _list_cache.clear()
        return True, None
    except Exception as exc:
        _state["error"] = hubspot_client.sanitize(db.sanitize_error(exc))
        _log(f"Pool refresh failed: {_state['error']}")
        return False, _state["error"]
    finally:
        _refresh_lock.release()


def _refresh_intel(client):
    try:
        _state["intel"] = _measure_intel(client)
        _state["intel_at"] = time.time()
        _state["intel_error"] = None
    except Exception as exc:
        # Like the page: keep ranking on the last (or Oct 21) numbers.
        _state["intel_error"] = hubspot_client.sanitize(db.sanitize_error(exc))
        _state["intel_at"] = time.time()
        _log(f"Lead intelligence measurement failed: {_state['intel_error']}")


_scheduler_started = False
_scheduler_lock = threading.Lock()


def _scheduler_loop():
    time.sleep(15)
    failures = 0
    while True:
        ok, _ = refresh()
        failures = 0 if ok else failures + 1
        time.sleep(REFRESH_SECONDS if not failures else min(MAX_BACKOFF_SECONDS, REFRESH_SECONDS * 2 ** failures))


def start_scheduler():
    """Idempotent. No-op without a HubSpot service key."""
    global _scheduler_started
    if _scheduler_started or not hubspot_client.is_configured():
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        threading.Thread(target=_scheduler_loop, name="lead-feeder-pool", daemon=True).start()
        _scheduler_started = True
        _log(f"Shared lead pool refresh scheduled every {REFRESH_SECONDS // 60} minutes")


def pool_status():
    loaded = _state["loaded_at"]
    return {
        "loaded": loaded is not None,
        "loaded_at": datetime.fromtimestamp(loaded, tz=timezone.utc) if loaded else None,
        "error": _state["error"],
        "configured": hubspot_client.is_configured(),
        "weights_info": _state["weights_info"],
    }


# ================================================================
# Lead intelligence (the page's loadInsights() + loadHours())
# ================================================================

def _measure_intel(client):
    """Measured from HubSpot (search totals) and the dashboard's own
    synced HubSpot call table. The page used HubSpot's SQL tool
    (query_crm_data), which has no REST equivalent; every number below is
    the same count expressed as search filters instead."""
    day_ms = 86400000
    t = "hs_time_to_move_from_salesqualifiedlead_to_customer"
    intel = {"live": True}

    n = client.count_objects("contacts", [{"filters": [{"propertyName": t, "operator": "HAS_PROPERTY"}]}])
    le = {}
    for d in L.AGE_PTS:
        le[d] = client.count_objects("contacts", [{"filters": [
            {"propertyName": t, "operator": "HAS_PROPERTY"},
            {"propertyName": t, "operator": "LTE", "value": str(d * day_ms)},
        ]}])
    intel["conv"] = {"n": n, "le": le} if n else L.FALLBACK_CONV

    def by_calls(stage_filters):
        counts = []
        for lo, hi, _label in L.CALL_BUCKETS:
            if lo == 0:
                groups = [{"filters": [*stage_filters, {"propertyName": "of_calls", "operator": "EQ", "value": "0"}]},
                          {"filters": [*stage_filters, {"propertyName": "of_calls", "operator": "NOT_HAS_PROPERTY"}]}]
            elif hi >= 10 ** 9:
                groups = [{"filters": [*stage_filters, {"propertyName": "of_calls", "operator": "GTE", "value": str(lo)}]}]
            else:
                groups = [{"filters": [*stage_filters, {"propertyName": "of_calls", "operator": "BETWEEN",
                                                         "value": str(lo), "highValue": str(hi)}]}]
            counts.append(client.count_objects("contacts", groups))
        return counts

    cust = by_calls([{"propertyName": "lifecyclestage", "operator": "EQ", "value": "customer"},
                     {"propertyName": "hs_v2_date_entered_salesqualifiedlead", "operator": "HAS_PROPERTY"}])
    open_ = by_calls([{"propertyName": "lifecyclestage", "operator": "EQ", "value": "salesqualifiedlead"}])
    intel["calls"] = {"cust": cust, "open": open_, "rate": L.call_rates(cust, open_)}

    snapshot = []
    try:
        options = (client.get_property("contacts", "lifecyclestage") or {}).get("options") or []
        for opt in options:
            count = client.count_objects("contacts", [{"filters": [
                {"propertyName": "lifecyclestage", "operator": "EQ", "value": opt["value"]}]}])
            snapshot.append([opt.get("label") or opt["value"], opt["value"], count])
    except hubspot_client.HubSpotError as exc:
        _log(f"Stage snapshot skipped: {exc}")
    intel["snapshot"] = snapshot

    intel["connect"], intel["hours"] = _call_stats()
    intel["at"] = _now()
    return intel


def _call_stats():
    """Pick-up rate per dial by quarter (since Jan 1 last year) and by
    Eastern hour/weekday (last 30 days), from hubspot_calls."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT to_char(date_trunc('quarter', h.call_timestamp AT TIME ZONE 'America/New_York'), 'YYYY-MM-DD'),
                       count(*), count(*) FILTER (WHERE d.label = 'Connected')
                FROM hubspot_calls h LEFT JOIN hubspot_dispositions d ON d.disposition_id = h.disposition
                WHERE upper(h.direction) = 'OUTBOUND'
                  AND h.call_timestamp >= make_date(extract(year FROM now())::int - 1, 1, 1)
                GROUP BY 1 ORDER BY 1
            """)
            connect = [{"quarter": q, "tot": tot, "conn": conn_n} for q, tot, conn_n in cur.fetchall() if tot]
            cur.execute("""
                SELECT extract(hour FROM h.call_timestamp AT TIME ZONE 'America/New_York')::int,
                       extract(dow FROM h.call_timestamp AT TIME ZONE 'America/New_York')::int,
                       count(*), count(*) FILTER (WHERE d.label = 'Connected')
                FROM hubspot_calls h LEFT JOIN hubspot_dispositions d ON d.disposition_id = h.disposition
                WHERE upper(h.direction) = 'OUTBOUND' AND h.disposition IS NOT NULL AND h.disposition <> ''
                  AND h.call_timestamp >= now() - interval '30 days'
                GROUP BY 1, 2
            """)
            hrs = [{"tot": 0, "conn": 0} for _ in range(24)]
            wd = [{"tot": 0, "conn": 0} for _ in range(7)]
            total = 0
            for hour, dow, tot, conn_n in cur.fetchall():
                hrs[hour]["tot"] += tot
                hrs[hour]["conn"] += conn_n
                wd[dow]["tot"] += tot
                wd[dow]["conn"] += conn_n
                total += tot
    finally:
        conn.close()
    return connect, ({"hrs": hrs, "wd": wd, "n": total} if total >= 200 else None)


def time_hint(intel, now=None):
    """The page's "Pickup right now / Best hours" line, or None."""
    hours = (intel or {}).get("hours")
    if not hours:
        return None
    ok = [{"h": h, "r": v["conn"] / v["tot"], "tot": v["tot"]} for h, v in enumerate(hours["hrs"]) if v["tot"] >= 40]
    if not ok:
        return None
    avg = sum(x["r"] * x["tot"] for x in ok) / sum(x["tot"] for x in ok)
    hour = (now or _now()).astimezone(L.BUSINESS_TZ).hour
    cur = next((x for x in ok if x["h"] == hour), None)
    best = sorted(sorted(ok, key=lambda x: -x["r"])[:3], key=lambda x: x["h"])
    return {"current": cur["r"] if cur else None, "above": bool(cur and cur["r"] >= avg), "avg": avg,
            "best": [{"label": hour_label(x["h"]), "r": x["r"]} for x in best]}


def hour_label(h):
    return f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}"


def intel_view():
    """Everything the Lead intelligence panel shows."""
    intel = _state["intel"] or {}
    conv = intel.get("conv") or L.FALLBACK_CONV
    view = {
        "live": bool(intel.get("live")),
        "at": intel.get("at"),
        "error": _state["intel_error"],
        "conv": [{"days": d, "share": conv["le"][d] / conv["n"]} for d in sorted(conv["le"])] if conv.get("n") else [],
        "conv_n": conv.get("n") or 0,
        "calls": None, "snapshot": None, "connect": [], "hours": None, "hint": time_hint(intel),
    }
    if intel.get("calls"):
        view["calls"] = [{"label": L.CALL_BUCKETS[i][2], "rate": r} for i, r in enumerate(intel["calls"]["rate"]) if r is not None]
    snap = intel.get("snapshot") or []
    if snap:
        prospects = {"lead", "marketingqualifiedlead", "salesqualifiedlead", "opportunity", "other"}
        view["snapshot"] = {
            "total": sum(r[2] for r in snap),
            "prospects": sum(r[2] for r in snap if r[1] in prospects),
            "sql": next((r[2] for r in snap if r[1] == "salesqualifiedlead"), 0),
        }
    for row in intel.get("connect") or []:
        d = datetime.fromisoformat(row["quarter"])
        view["connect"].append({"label": f"Q{(d.month - 1) // 3 + 1} ’{str(d.year)[2:]}", "rate": row["conn"] / row["tot"]})
    hours = intel.get("hours")
    if hours:
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        view["hours"] = {
            "n": hours["n"],
            "hrs": [{"label": hour_label(h), "rate": v["conn"] / v["tot"]} for h, v in enumerate(hours["hrs"]) if v["tot"] >= 40],
            "wd": [{"label": names[d], "rate": v["conn"] / v["tot"]} for d, v in enumerate(hours["wd"]) if v["tot"] >= 40],
        }
    return view


# ================================================================
# Per-rep lists
# ================================================================

def get_prefs(user_id):
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT prefs FROM lead_feeder_prefs WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        _log(f"Couldn't read prefs: {db.sanitize_error(exc)}")
        row = None
    return L.clean_prefs(row[0] if row else None)


def save_prefs(user_id, raw):
    """(ok, error, prefs). Only the rep's own row is ever written."""
    prefs = L.clean_prefs(dict(get_prefs(user_id), **(raw or {})))
    try:
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lead_feeder_prefs (user_id, prefs) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = now()
                """, (user_id, json.dumps(prefs)))
        finally:
            conn.close()
    except Exception as exc:
        return False, f"Couldn't save your settings ({db.sanitize_error(exc)}).", prefs
    return True, None, prefs


def touch(user_id):
    """Marks the rep as using Lead Feeder right now (admin page)."""
    try:
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO lead_feeder_prefs (user_id, last_seen_at) VALUES (%s, now())
                    ON CONFLICT (user_id) DO UPDATE SET last_seen_at = now()
                """, (user_id,))
        finally:
            conn.close()
    except Exception:
        pass


def _ranked_for(prefs, now):
    """[(id, p, s)] callable for these prefs, ranked -- before claims.
    Cached per pool version, prefs and Eastern day."""
    key = (_state["version"], json.dumps(prefs, sort_keys=True), L.day_start(now).date())
    cached = _list_cache.get(key)
    if cached is not None:
        return cached
    leads = []
    for entry in _state["entries"]:
        for variant in entry["variants"]:
            p = variant["p"]
            if L.prescreen(p, now, prefs) and L.in_state(p, prefs["state"]) and L.in_area(p, prefs["area"]):
                leads.append({"id": entry["id"], "p": p, "s": variant["s"]})
                break
    leads.sort(key=L.rank_key)
    if len(_list_cache) > 200:
        _list_cache.clear()
    _list_cache[key] = leads
    return leads


def open_leads(prefs, claimed_ids, now=None):
    now = now or _now()
    return [lead for lead in _ranked_for(prefs, now) if lead["id"] not in claimed_ids]


def lead_row(lead, now=None):
    """What the board / CSV / current card need about one lead."""
    p, s = lead["p"], lead["s"]
    now = now or _now()
    kind = L.lead_kind(p, now)
    sg = L.signals(p)
    owner = L.sweep_owner(p) if p.get("__sweep") else None
    m = p.get("__sweep") or {}
    verified = str(m.get("verified") or "")[5:10].split("-")
    return {
        "id": lead["id"],
        "name": L.full_name(p),
        "where": ", ".join(str(p[k]) for k in ("address", "city", "state") if p.get(k)),
        "address": L.lead_addr(p),
        "phone": L.fmt_phone(p.get("phone")),
        "tel": ("tel:+1" + L.digits(p.get("phone"))) if len(L.digits(p.get("phone"))) == 10 else "tel:" + L.digits(p.get("phone")),
        "tier": s["tier"], "tier_label": L.TIERS[s["tier"]], "score": s["score"], "why": s["why"],
        "replied": s["replied"],
        "kind": kind, "source": L.SOURCE_LABELS.get(kind, ""),
        "stage_name": L.STAGE_NAME.get(p.get("lifecyclestage"), p.get("lifecyclestage")),
        "quiet_days": L.quiet_days(p, now),
        "calls": L.calls_of(p),
        "last_called": p.get("notes_last_contacted") or None,
        "never_called": not p.get("notes_last_contacted"),
        "prequal_date": p.get("prequal_date") or None,
        "sql_date": str(p.get("hs_v2_date_entered_salesqualifiedlead") or p.get("prequal_date") or "")[:10],
        "lead_status": p.get("hs_lead_status") or "",
        "no_texts": bool(p.get("sakari_opt_out")),
        "signals": {k: v for k, v in sg.items()},
        "prio": p.get("__prio"),
        "prio_check_pas": bool(p.get("__prio")) and str(p.get("availability") or "").strip() != "1",
        "sweep": {"date": f"{int(verified[0])}/{int(verified[1])}" if len(verified) == 2 and all(x.isdigit() for x in verified) else "",
                  "owner": owner, "manual": m.get("batch") == "check manually", "notes": m.get("notes") or ""} if m else None,
        "street": p.get("address") or "", "city": p.get("city") or "", "state": p.get("state") or "",
        "zip": p.get("zip") or "",
    }


# ================================================================
# Claims (shared, one per contact per Eastern day)
# ================================================================

_CLAIM_COLUMNS = "contact_id, user_id, user_name, claimed_at, outcome, outcome_at, contact_name, town"


def _claim_dict(row):
    contact_id, user_id, user_name, claimed_at, outcome, outcome_at, name, town = row
    return {"contact_id": contact_id, "user_id": user_id, "user_name": user_name, "claimed_at": claimed_at,
            "outcome": outcome, "outcome_at": outcome_at, "name": name, "town": town}


def get_claims_today():
    """(ok, {contact_id: claim}) for today's claims, newest first."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_CLAIM_COLUMNS} FROM lead_feeder_claims WHERE claim_date = %s ORDER BY claimed_at DESC",
                            (_today(),))
                return True, {r[0]: _claim_dict(r) for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as exc:
        _log(f"Couldn't read claims: {db.sanitize_error(exc)}")
        return False, {}


def _event(cur, contact_id, user_id, event, detail=None):
    cur.execute("INSERT INTO lead_feeder_events (contact_id, user_id, event, detail) VALUES (%s, %s, %s, %s)",
                (contact_id, user_id, event, detail))


def try_claim(contact_id, user, p, s, now=None):
    """True if THIS user now holds the lead. Race-safe: the UNIQUE
    (claim_date, contact_id) key lets exactly one INSERT win."""
    now = now or _now()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lead_feeder_claims
                    (claim_date, contact_id, user_id, user_name, contact_name, town, contact_email, contact_phone, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_date, contact_id) DO NOTHING
                RETURNING id
            """, (L.day_start(now).date(), str(contact_id), user["id"], _display_name(user), L.full_name(p),
                  p.get("city") or "", (p.get("email") or "").strip().lower() or None, L.digits(p.get("phone")) or None,
                  json.dumps(L.lead_meta(p, s, now))))
            won = cur.fetchone() is not None
            if won:
                _event(cur, str(contact_id), user["id"], "claimed")
        return won
    finally:
        conn.close()


def set_outcome(contact_id, user, outcome, event_detail=None):
    """(ok, error). Only the rep holding today's claim can set it."""
    allowed = set(L.OUTCOMES) | {"Called", "Tab closed", "Auto-skipped"}
    if outcome not in allowed:
        return False, "Unknown outcome."
    try:
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    UPDATE lead_feeder_claims SET outcome = %s, outcome_at = now()
                    WHERE claim_date = %s AND contact_id = %s AND user_id = %s
                    RETURNING id
                """, (outcome, _today(), str(contact_id), user["id"]))
                if cur.fetchone() is None:
                    return False, "That lead isn't yours today."
                _event(cur, str(contact_id), user["id"], "outcome", outcome if not event_detail else f"{outcome}: {event_detail}")
        finally:
            conn.close()
    except Exception as exc:
        return False, f"Couldn't save that ({db.sanitize_error(exc)})."
    _current_cache.pop((user["id"], str(contact_id)), None)
    return True, None


def release(contact_id, user, why):
    """Drops this user's claim (only used when HubSpot failed mid-take,
    so the lead isn't lost for the day)."""
    try:
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("DELETE FROM lead_feeder_claims WHERE claim_date = %s AND contact_id = %s AND user_id = %s",
                            (_today(), str(contact_id), user["id"]))
                _event(cur, str(contact_id), user["id"], "released", why)
        finally:
            conn.close()
    except Exception as exc:
        _log(f"Couldn't release a claim: {db.sanitize_error(exc)}")


def my_current(user):
    """The contact id this rep is still on ('Calling' today), or None."""
    ok, claims = get_claims_today()
    mine = [c for c in claims.values() if c["user_id"] == user["id"] and c["outcome"] == "Calling"]
    return mine[0]["contact_id"] if mine else None


# ================================================================
# Verify right before calling + take
# ================================================================

def _recent_activity(client, contact_id):
    """The contact's 5 latest calls and 5 latest notes, newest first
    ([{ts, text}]). Notes are skipped if the key can't read them."""
    items = []
    for to_type, body_prop in (("calls", "hs_call_body"), ("notes", "hs_note_body")):
        try:
            ids = client.associated_ids("contacts", contact_id, to_type)
            if not ids:
                continue
            found, _ = client.batch_read(to_type, ids, [body_prop, "hs_timestamp"])
        except hubspot_client.HubSpotError as exc:
            if exc.status in (403, 404) and to_type == "notes":
                continue
            raise
        rows = sorted(found.values(), key=lambda r: str(r.get("hs_timestamp") or ""), reverse=True)[:5]
        items.extend({"ts": r.get("hs_timestamp"), "text": L.body_text(r.get(body_prop))} for r in rows)
    items.sort(key=lambda it: str(it["ts"] or ""), reverse=True)
    return items


def verify(client, contact_id, pooled_p, prefs, now):
    """The page's verify(): fresh contact + latest calls/notes. Returns
    {"ok", "why", "p", "notes"}."""
    found, _ = client.batch_read("contacts", [contact_id], L.PROPS)
    props = found.get(str(contact_id))
    if props is None:
        return {"ok": False, "why": "Couldn't find this contact", "p": pooled_p}
    p = dict(props)
    if pooled_p.get("__prio"):
        p["__prio"] = pooled_p["__prio"]
    household = _household_checker()
    if household:
        household(p)
    elif pooled_p.get("__household"):
        p["__household"] = pooled_p["__household"]
    if L.household_drop(p):
        return {"ok": False, "why": p["__household"]["why"], "p": p}
    if pooled_p.get("__sweep"):
        L.sweep_fill(p, pooled_p["__sweep"])
        why = L.sweep_reason(p, now, prefs)
        if why:
            return {"ok": False, "why": why, "p": p}
    if L.is_customer(p):
        return {"ok": False, "why": "Already a Planet customer", "p": p}
    if not L.lead_kind(p, now):
        return {"ok": False, "why": "Stage or owner changed, no longer up for grabs", "p": p}
    if not L.prescreen(p, now, prefs):
        return {"ok": False, "why": "Called today or no longer callable", "p": p}
    items = _recent_activity(client, contact_id)
    ok, why = L.verify_activity(items, now, prefs["cool_days"])
    if not ok:
        return {"ok": False, "why": why, "p": p}
    return {"ok": True, "why": None, "p": p, "notes": [i for i in items if i["text"]][:3]}


def take(user, prefs, contact_id=None):
    """Claims and verifies a lead for `user`: the given one, or the
    highest-ranked open lead for their filters (trying the next one when
    a lead was just taken or fails the pre-dial check, like the page's
    takeNext()). Returns {"ok", "lead", "messages", "error"}."""
    now = _now()
    current = my_current(user)
    if current:
        return {"ok": True, "lead": current_lead(user, current), "messages": ["You're still on this lead. Pick how it went first."]}
    if not hubspot_client.is_configured():
        return {"ok": False, "error": "HubSpot isn't connected.", "messages": []}
    client = hubspot_client.HubSpotClient()
    _ok, claims = get_claims_today()
    if contact_id is not None:
        contact_id = str(contact_id)
        match = next((lead for lead in _ranked_for(prefs, now) if lead["id"] == contact_id), None)
        if match is None:
            return {"ok": False, "error": "That lead isn't on your list anymore.", "messages": []}
        candidates = [match]
    else:
        candidates = open_leads(prefs, set(claims), now)[:MAX_TAKE_ATTEMPTS]
    messages = []
    for lead in candidates:
        p = lead["p"]
        name = L.full_name(p)
        try:
            if not try_claim(lead["id"], user, p, lead["s"], now):
                messages.append(f"{name} was just taken by a teammate.")
                continue
        except Exception as exc:
            return {"ok": False, "error": f"Couldn't claim leads right now ({db.sanitize_error(exc)}).", "messages": messages}
        try:
            v = verify(client, lead["id"], p, prefs, now)
        except hubspot_client.HubSpotError as exc:
            release(lead["id"], user, f"HubSpot error: {exc}")
            return {"ok": False, "error": f"Couldn't reach HubSpot: {exc}", "messages": messages}
        if not v["ok"]:
            set_outcome(lead["id"], user, "Auto-skipped", v["why"])
            messages.append(f"Skipped {name}: {v['why']}.")
            continue
        details = lead_row({"id": lead["id"], "p": v["p"], "s": L.score_lead(v["p"], now, _state["intel"], _state["weights"])}, now)
        details["notes"] = v["notes"]
        _current_cache[(user["id"], lead["id"])] = details
        return {"ok": True, "lead": details, "messages": messages}
    if contact_id is None and len(candidates) == MAX_TAKE_ATTEMPTS:
        messages.append(f"Tried {MAX_TAKE_ATTEMPTS} leads without one passing the pre-dial check. Press Take the next lead again.")
    return {"ok": False, "lead": None, "messages": messages, "error": None if messages else "No open leads match your filters."}


def current_lead(user, contact_id):
    """The current card for a lead this rep already holds -- from memory,
    or re-read from HubSpot after a restart / on another device."""
    details = _current_cache.get((user["id"], str(contact_id)))
    if details:
        return details
    now = _now()
    pooled = next((v["p"] for e in _state["entries"] if e["id"] == str(contact_id) for v in e["variants"]), None)
    p, notes = dict(pooled or {}), []
    if hubspot_client.is_configured():
        try:
            client = hubspot_client.HubSpotClient()
            found, _ = client.batch_read("contacts", [contact_id], L.PROPS)
            if str(contact_id) in found:
                fresh = dict(found[str(contact_id)])
                for key in ("__prio", "__sweep", "__household"):
                    if p.get(key):
                        fresh[key] = p[key]
                p = fresh
            notes = [i for i in _recent_activity(client, contact_id) if i["text"]][:3]
        except hubspot_client.HubSpotError as exc:
            _log(f"Couldn't re-read the current lead: {exc}")
    details = lead_row({"id": str(contact_id), "p": p, "s": L.score_lead(p, now, _state["intel"], _state["weights"])}, now)
    details["notes"] = notes
    _current_cache[(user["id"], str(contact_id))] = details
    return details


def hubspot_url(contact_id):
    """HubSpot contact record, opened ready to call (?interaction=call)."""
    portal_id, ui_domain = L.PORTAL_ID, None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT portal_id, ui_domain FROM hubspot_sync_state WHERE id = 1")
                row = cur.fetchone()
                if row and row[0]:
                    portal_id, ui_domain = row
        finally:
            conn.close()
    except Exception:
        pass
    return hubspot_logic.hubspot_contact_url(ui_domain, portal_id, contact_id) + "?interaction=call"


# ================================================================
# PlanetWeb (replaces the Planet Maps userscript)
# ================================================================

_address_index = {"df": None, "index": None}


def _street_key(street, zipcode):
    return " ".join(order_search.normalize_words(street or "")), str(zipcode or "").strip()[:5]


def _passings_by_address():
    df = passings_cache._state.get("df")
    if df is None:
        return None, None
    if _address_index["df"] is not df:
        index = {}
        for i, (street, zipcode) in enumerate(zip(df["Street"], df["ZipCode"])):
            index.setdefault(_street_key(street, zipcode), []).append(i)
        _address_index.update(df=df, index=index)
    return df, _address_index["index"]


_orders_index = {"df": None, "by_zip": None, "by_city": None}
_vision_current = {"source": None, "uuids": None}


def _orders_by_address():
    """order_search's index keyed by (street words, ZIP) and (street words,
    city), rebuilt when the index refreshes. (None, None) until it loads."""
    df = order_search.get_index()
    if df is None:
        return None, None
    if _orders_index["df"] is not df:
        by_zip, by_city = {}, {}
        for i, (street, zip5, city) in enumerate(zip(df["address"], df["zip5"], df["city"])):
            words = " ".join(order_search.normalize_words(street))
            if not words:
                continue
            if zip5:
                by_zip.setdefault((words, zip5), []).append(i)
            if city:
                by_city.setdefault((words, city.casefold()), []).append(i)
        _orders_index.update(df=df, by_zip=by_zip, by_city=by_city)
    return df, _orders_index


def current_vision_uuids(vision, today):
    """Lowercased subscriber uuids with an active, invoiced Vision package
    not past its estimated cancellation -- "current in Vision"."""
    if vision is None or vision.empty:
        return set()
    active = vision["active"].fillna(False).astype(bool)
    cancel = pd.to_datetime(vision["estimated_cancellation_date"], errors="coerce")
    live = active & (cancel.isna() | (cancel.dt.date > today))
    return set(vision.loc[live, "subscriber_uuid"].dropna().astype(str).str.strip().str.lower())


def _current_uuids():
    vision = vision_packages_source() if vision_packages_source else None
    if vision is None or "active" not in vision.columns:
        return None
    if _vision_current["source"] is not vision:
        _vision_current.update(source=vision, uuids=current_vision_uuids(vision, _today()))
    return _vision_current["uuids"]


def orders_at(df, idx, street, zipcode, city):
    """Order rows at exactly this street address (ZIP, else city), newest first."""
    words = " ".join(order_search.normalize_words(street or ""))
    if not words:
        return []
    zip5, town = str(zipcode or "").strip()[:5], str(city or "").strip().casefold()
    rows = idx["by_zip"].get((words, zip5)) if zip5 else idx["by_city"].get((words, town))
    out = []
    for i in rows or []:
        hit = df.iloc[i]
        out.append({
            "sale_id": int(hit["sale_id"]),
            "name": f"{hit['first_name']} {hit['last_name']}".strip(),
            "last_name": hit["last_name"],
            "subscriber_uuid": hit["subscriber_uuid"],
            "order_date": hit["order_date"].strftime("%b %-d, %Y") if hit["order_date"] == hit["order_date"] else "",
            "install_date": hit["install_date"].date() if hit["install_date"] == hit["install_date"] else None,
        })
    return out


def _household_checker():
    """A function that sets p["__household"] (L.household_check) and
    returns it, or None while PlanetWeb orders or Vision aren't loaded."""
    df, idx = _orders_by_address()
    uuids = _current_uuids()
    if df is None or uuids is None:
        return None
    today = _today()

    def check(p):
        result = L.household_check(p, orders_at(df, idx, p.get("address"), p.get("zip"), p.get("city")), uuids, today)
        if result:
            p["__household"] = result
        else:
            p.pop("__household", None)
        return result
    return check


def planetweb_lookup(lead, billed_uuids=None):
    """What PlanetWeb knows about the lead's address: the place (availability,
    order status, PAS page) from the Passings & Leads snapshot, and any
    existing order at the address from the global-search order index."""
    street, zipcode = lead.get("street") or "", str(lead.get("zip") or "").strip()[:5]
    result = {"place": None, "orders": [], "searched": lead.get("address") or "", "snapshot_ready": passings_cache.ready()}
    df, index = _passings_by_address()
    if df is not None:
        rows = index.get(_street_key(street, zipcode)) or []
        if rows:
            row = df.iloc[rows[0]]
            place_id = int(row["PlaceID"]) if row["PlaceID"] == row["PlaceID"] else None
            statuses = passings_cache.order_statuses(billed_uuids or set()) if billed_uuids is not None else None
            result["place"] = {
                "place_id": place_id,
                "address": row.get("FullAddress") or "",
                "availability": passings_classification.availability_label(int(row["AvailabilityID"]) if row["AvailabilityID"] == row["AvailabilityID"] else None),
                "became_available": row["BecameAvailable"].strftime("%b %-d, %Y") if row["BecameAvailable"] == row["BecameAvailable"] and row["BecameAvailable"] is not None else None,
                "order_status": (statuses.get(place_id) if statuses is not None and place_id is not None else None) or passings_cache.NO_ORDER,
                "pas_url": pas_place_url(place_id),
            }
    index_df = order_search.get_index()
    if index_df is not None and street:
        hits, closest = order_search.search(index_df, f"{street} {zipcode}".strip(), 3)
        if hits is not None and not closest:
            for _, hit in hits.iterrows():
                result["orders"].append({
                    "sale_id": int(hit["sale_id"]),
                    "name": f"{hit['first_name']} {hit['last_name']}".strip(),
                    "address": f"{hit['address']}, {hit['city_state_zip']}",
                    "order_date": hit["order_date"].strftime("%b %-d, %Y") if hit["order_date"] == hit["order_date"] else "",
                })
    return result


# ================================================================
# Call-first / PAS sweep lists and weights (Admin)
# ================================================================

SWEEP_FIELDS = ["hot", "verified", "owner_name", "owner_inactive", "batch", "phone", "first_name", "last_name",
                "state", "city", "street_address", "notes"]


def _csv_rows(text):
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in row.items()} for row in reader]


def _record_id(row):
    value = row.get("hubspot_id") or row.get("record_id") or row.get("contact_id") or row.get("id") or ""
    value = value.strip()
    return value if value.isdigit() else None


def import_list(kind, text, user, replace=True):
    """(ok, message). CSV with a HubSpot Record ID column (record_id /
    hubspot_id / contact_id / id). Call first: + rank, reason. PAS sweep:
    + any of SWEEP_FIELDS."""
    try:
        rows = _csv_rows(text)
    except csv.Error as exc:
        return False, f"Couldn't read that CSV ({exc})."
    parsed, bad = [], 0
    for row in rows:
        cid = _record_id(row)
        if not cid:
            bad += 1
            continue
        if kind == "priority":
            try:
                rank = int(float(row.get("rank") or 0)) or None
            except ValueError:
                rank = None
            parsed.append((cid, rank, row.get("reason") or None))
        else:
            data = {k: row[k] for k in SWEEP_FIELDS if row.get(k)}
            if "owner_inactive" in data:
                data["owner_inactive"] = L.yes(data["owner_inactive"])
            parsed.append((cid, data))
    if not parsed:
        return False, "No rows with a HubSpot Record ID column (record_id, hubspot_id, contact_id or id)."
    try:
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                table = "lead_feeder_priority" if kind == "priority" else "lead_feeder_sweep"
                if replace:
                    cur.execute(f"DELETE FROM {table}")
                for item in parsed:
                    if kind == "priority":
                        cur.execute("""
                            INSERT INTO lead_feeder_priority (contact_id, rank, reason, added_by_user_id) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (contact_id) DO UPDATE SET rank = EXCLUDED.rank, reason = EXCLUDED.reason, added_at = now()
                        """, (*item, user["id"]))
                    else:
                        cur.execute("""
                            INSERT INTO lead_feeder_sweep (contact_id, data, added_by_user_id) VALUES (%s, %s, %s)
                            ON CONFLICT (contact_id) DO UPDATE SET data = EXCLUDED.data, added_at = now()
                        """, (item[0], json.dumps(item[1]), user["id"]))
                _event(cur, "-", user["id"], f"import_{kind}", f"{len(parsed)} rows")
        finally:
            conn.close()
    except Exception as exc:
        return False, f"Couldn't save the list ({db.sanitize_error(exc)})."
    msg = f"Loaded {len(parsed)} rows" + (f", skipped {bad} without a Record ID" if bad else "") + ". They show up on the next refresh."
    return True, msg


def clear_list(kind, user):
    table = "lead_feeder_priority" if kind == "priority" else "lead_feeder_sweep"
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table}")
            _event(cur, "-", user["id"], f"clear_{kind}")
    finally:
        conn.close()


def list_counts():
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT (SELECT count(*) FROM lead_feeder_priority), (SELECT count(*) FROM lead_feeder_sweep)")
                priority, sweep = cur.fetchone()
                cur.execute("SELECT weights, note FROM lead_feeder_weights WHERE id = 1")
                weights = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return {"priority": None, "sweep": None, "weights": None}
    return {"priority": priority, "sweep": sweep, "weights": weights[0] if weights else None,
            "weights_note": weights[1] if weights else None}


def save_weights(raw, note, user):
    """(ok, error). Unknown keys dropped; values clamped when read."""
    weights = {}
    for key in L.DEFAULT_WEIGHTS:
        value = (raw or {}).get(key)
        if value in (None, ""):
            continue
        try:
            weights[key] = float(value)
        except (TypeError, ValueError):
            return False, f"{key} must be a number."
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            if weights:
                cur.execute("""
                    INSERT INTO lead_feeder_weights (id, weights, note, updated_by_user_id) VALUES (1, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET weights = EXCLUDED.weights, note = EXCLUDED.note,
                        updated_by_user_id = EXCLUDED.updated_by_user_id, updated_at = now()
                """, (json.dumps(weights), note or None, user["id"]))
            else:
                cur.execute("DELETE FROM lead_feeder_weights WHERE id = 1")
            _event(cur, "-", user["id"], "weights", json.dumps(weights))
    finally:
        conn.close()
    return True, None


# ================================================================
# Admin report
# ================================================================

def _orders_since(since_naive):
    """PlanetWeb orders placed since `since_naive` (Eastern local):
    [(order_id, email_lower, phone_digits, insert_naive)]. Read-only."""
    conn = db.get_planetweb_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fd.ID, fd.EmailAddress, fd.PhoneNumber, fd.InsertDate
            FROM FTTPFormData fd
            WHERE fd.QuoteID IS NOT NULL AND fd.InsertDate >= ?
        """, since_naive)
        return [(int(r[0]), (r[1] or "").strip().lower(), L.digits(r[2]), r[3]) for r in cur.fetchall()]
    finally:
        conn.close()


def admin_report(start_date, end_date):
    """Claims in [start_date, end_date] with HubSpot calls / connected
    calls / PlanetWeb sales attributed to each. A claim is never a call:
    calls come only from HubSpot (hubspot_calls), made by the claiming
    rep's mapped HubSpot owner (Admin > HubSpot Integration) to that
    contact on the claim's day after it was claimed. A sale is a
    PlanetWeb order with the contact's email or phone placed on/after
    the claim."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH owners AS (
                    SELECT u.id AS user_id, m.hubspot_owner_id
                    FROM users u JOIN hubspot_user_mappings m
                      ON m.dashboard_user_id = u.id OR (u.sales_rep_id IS NOT NULL AND m.sales_rep_id = u.sales_rep_id)
                )
                SELECT c.id, c.claim_date, c.contact_id, c.user_id, c.user_name, c.claimed_at, c.outcome, c.outcome_at,
                       c.contact_name, c.town, c.contact_email, c.contact_phone, c.meta,
                       EXISTS (SELECT 1 FROM owners o WHERE o.user_id = c.user_id) AS mapped,
                       calls.n, calls.connected, calls.last_call
                FROM lead_feeder_claims c
                LEFT JOIN LATERAL (
                    SELECT count(*) AS n, count(*) FILTER (WHERE d.label = 'Connected') AS connected,
                           max(h.call_timestamp) AS last_call
                    FROM hubspot_calls h LEFT JOIN hubspot_dispositions d ON d.disposition_id = h.disposition
                    WHERE h.contact_id = c.contact_id AND upper(h.direction) = 'OUTBOUND'
                      AND h.call_timestamp >= c.claimed_at
                      AND (h.call_timestamp AT TIME ZONE 'America/New_York')::date = c.claim_date
                      AND (NOT EXISTS (SELECT 1 FROM owners o WHERE o.user_id = c.user_id)
                           OR h.hubspot_owner_id IN (SELECT o.hubspot_owner_id FROM owners o WHERE o.user_id = c.user_id))
                ) calls ON true
                WHERE c.claim_date BETWEEN %s AND %s
                ORDER BY c.claimed_at DESC
            """, (start_date, end_date))
            cols = [d[0] for d in cur.description]
            claims = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("""
                SELECT p.user_id, coalesce(sr.name, u.email), p.last_seen_at
                FROM lead_feeder_prefs p JOIN users u ON u.id = p.user_id
                LEFT JOIN sales_reps sr ON sr.id = u.sales_rep_id
                WHERE p.last_seen_at >= now() - %s * interval '1 second'
                ORDER BY p.last_seen_at DESC
            """, (ACTIVE_SECONDS,))
            active = [{"user_id": r[0], "name": r[1], "last_seen_at": r[2]} for r in cur.fetchall()]
            cur.execute("""
                SELECT e.created_at, e.event, e.detail, e.contact_id, coalesce(sr.name, u.email), c.contact_name
                FROM lead_feeder_events e
                LEFT JOIN users u ON u.id = e.user_id
                LEFT JOIN sales_reps sr ON sr.id = u.sales_rep_id
                LEFT JOIN lead_feeder_claims c ON c.contact_id = e.contact_id
                     AND c.claim_date = (e.created_at AT TIME ZONE 'America/New_York')::date
                WHERE (e.created_at AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
                ORDER BY e.created_at DESC LIMIT 300
            """, (start_date, end_date))
            events = [{"at": r[0], "event": r[1], "detail": r[2], "contact_id": r[3], "user": r[4], "contact": r[5]}
                      for r in cur.fetchall()]
    finally:
        conn.close()

    sales_error = None
    if claims:
        earliest = min(c["claimed_at"] for c in claims).astimezone(L.BUSINESS_TZ).replace(tzinfo=None)
        try:
            orders = _orders_since(earliest)
        except Exception as exc:
            orders, sales_error = [], db.sanitize_error(exc)
        for c in claims:
            claimed_local = c["claimed_at"].astimezone(L.BUSINESS_TZ).replace(tzinfo=None)
            email, phone = c["contact_email"], c["contact_phone"]
            c["sale_order_id"] = next((oid for oid, o_email, o_phone, at in orders
                                       if at and at >= claimed_local
                                       and ((email and o_email == email) or (phone and len(phone) == 10 and o_phone == phone))), None)
    for c in claims:
        c["called"] = bool(c["n"])
        c["connected_calls"] = c["connected"] or 0
        c["calls"] = c["n"] or 0
        c["hubspot_url"] = None

    reps = {}
    for c in claims:
        r = reps.setdefault(c["user_id"], {"name": c["user_name"], "claims": 0, "skipped": 0, "worked": 0, "called": 0,
                                           "calls": 0, "connected": 0, "sales": 0, "reported_sales": 0, "mapped": c["mapped"]})
        r["claims"] += 1
        if c["outcome"] == "Auto-skipped":
            r["skipped"] += 1
            continue
        r["worked"] += 1
        r["called"] += c["called"]
        r["calls"] += c["calls"]
        r["connected"] += 1 if c["connected_calls"] else 0
        r["sales"] += 1 if c.get("sale_order_id") else 0
        r["reported_sales"] += 1 if c["outcome"] == "Sale" else 0
    rep_rows = sorted(reps.values(), key=lambda r: -r["claims"])
    for r in rep_rows:
        r["conversion"] = r["sales"] / r["worked"] if r["worked"] else None
    totals = {k: sum(r[k] for r in rep_rows) for k in ("claims", "skipped", "worked", "called", "calls", "connected", "sales", "reported_sales")}
    totals["conversion"] = totals["sales"] / totals["worked"] if totals["worked"] else None
    outcomes = {}
    for c in claims:
        outcomes[c["outcome"]] = outcomes.get(c["outcome"], 0) + 1
    return {
        "claims": claims, "reps": rep_rows, "totals": totals, "active": active, "events": events,
        "active_claims": [c for c in claims if c["outcome"] == "Calling" and c["claim_date"] == _today()],
        "outcomes": sorted(outcomes.items(), key=lambda kv: -kv[1]), "sales_error": sales_error,
    }
