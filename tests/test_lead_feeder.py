"""Lead Feeder (lead_feeder_logic.py / lead_feeder_store.py, 2026-10-28).

The logic tests pin the rules ported from the original claude.ai page.
The claim tests need a REAL Postgres and run only when
LEAD_FEEDER_INTEGRATION_TEST_DB is set (that database is created, migrated
and dropped -- the app's own appdb is never touched):

    docker compose exec -e LEAD_FEEDER_INTEGRATION_TEST_DB=lead_feeder_it dashboard \\
        python -m pytest tests/test_lead_feeder.py -v
"""

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

import lead_feeder_logic as L

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)      # 11:00 Eastern
PREFS = L.clean_prefs({})


def lead(**overrides):
    p = {
        "firstname": "Maria", "lastname": "Lopez", "phone": "(973) 412-8890", "address": "12 Oak St",
        "city": "Sparta", "state": "NJ", "zip": "07871", "lifecyclestage": "salesqualifiedlead",
        "availability": "1", "hs_v2_date_entered_salesqualifiedlead": "2026-10-25T14:00:00Z", "of_calls": "1",
    }
    p.update(overrides)
    return p


# ---------------- screening ----------------

def test_open_unowned_sql_is_callable():
    assert L.prescreen(lead(), NOW, PREFS) == "sql"


@pytest.mark.parametrize("overrides", [
    {"lifecyclestage": "marketingqualifiedlead"},          # SQL-only rule
    {"lifecyclestage": "90871183"},                         # non-ordering is off under the SQL-only rule
    {"firstname": "Test", "lastname": "Lead"},              # junk name
    {"lastname": "Smith LLC"},                              # business name
    {"phone": "973-555-1234"},                              # 555
    {"phone": "2222222222"},                                # repeated digits
    {"hs_email_optout": "true"},
    {"sakari_message_sent_message": "Your install appointment is Tuesday"},
    {"has_service": "Yes"},
    {"client_id": "C-12"},
    {"hs_v2_date_entered_customer": "2025-01-01"},
    {"recent_deal_amount": "70"},
    {"availability": "0"},                                  # not serviceable
    {"state": "PA"},
    {"address": ""},
    {"hs_lead_status": "OBJ Cable"},
    {"hs_lead_status": "OBJ Price", "notes_last_contacted": "2026-10-01T15:00:00Z"},   # price objection < 60 days
])
def test_screened_out(overrides):
    assert L.prescreen(lead(**overrides), NOW, PREFS) is False


def test_cooldown_is_per_rep_and_eastern():
    # Called 2 days ago (Eastern): out with the default 3-day cool-down, in with "today".
    p = lead(notes_last_contacted="2026-10-26T18:00:00Z")
    assert L.prescreen(p, NOW, PREFS) is False
    assert L.prescreen(p, NOW, L.clean_prefs({"cool_days": 1})) == "sql"
    # 11pm Eastern last night is "yesterday" even though it's today in UTC.
    late = lead(notes_last_contacted="2026-10-28T03:00:00Z")
    assert L.prescreen(late, NOW, L.clean_prefs({"cool_days": 1})) == "sql"
    assert L.prescreen(late, NOW, L.clean_prefs({"cool_days": 2})) is False


def test_owned_sql_only_when_quiet_and_toggle_on():
    owned = lead(hubspot_owner_id="77", notes_last_contacted="2026-10-20T15:00:00Z")
    assert L.prescreen(owned, NOW, PREFS) is False                              # touched 8 days ago
    quiet = lead(hubspot_owner_id="77", notes_last_contacted="2026-09-01T15:00:00Z")
    assert L.prescreen(quiet, NOW, PREFS) == "quiet"
    assert L.prescreen(quiet, NOW, L.clean_prefs({"quiet_on": False})) is False


def test_call_first_skips_availability_but_not_sql_rule():
    prio = lead(availability="", __prio={"rank": 1, "reason": "Manager pick"})
    assert L.prescreen(prio, NOW, PREFS) == "sql"
    assert L.prescreen(dict(prio, lifecyclestage="lead"), NOW, PREFS) is False


def test_sweep_leads_and_their_toggles():
    m = {"hot": "yes", "verified": "2026-10-20", "batch": "check manually", "owner_name": "Pat"}
    p = L.sweep_fill(lead(availability="", hubspot_owner_id="5"), m)
    assert L.sweep_reason(p, NOW, PREFS) == "Marked check manually"
    prefs = L.clean_prefs({"sweep_manual_on": True})
    assert L.sweep_reason(p, NOW, prefs) == "Owned by an active rep"
    assert L.prescreen(p, NOW, L.clean_prefs({"sweep_manual_on": True, "sweep_active_on": True})) == "sweep"
    assert L.sweep_reason(p, NOW, L.clean_prefs({"sweep_on": False})) == "PAS sweep leads turned off"
    inactive = L.sweep_fill(lead(hubspot_owner_id="5"), {"owner_name": "Pat", "owner_inactive": True})
    assert L.sweep_reason(inactive, NOW, PREFS) is None


def test_sweep_fill_never_overwrites_hubspot():
    p = L.sweep_fill({"firstname": "Ann", "phone": "", "mobilephone": "9735551000"},
                     {"first_name": "Annie", "last_name": "Lee", "phone": "111"})
    assert (p["firstname"], p["lastname"], p["phone"]) == ("Ann", "Lee", "9735551000")


def test_area_pins_state():
    prefs = L.clean_prefs({"area": "farmville", "state": "NJ"})
    assert prefs["state"] == "VA"
    assert L.in_area({"city": "  Hampden  Sydney "}, "farmville")
    assert not L.in_area({"city": "Richmond"}, "farmville")
    assert L.in_state({"state": "Virginia"}, "VA") and not L.in_state({"state": "NJ"}, "VA")


# ---------------- signals + scoring ----------------

def test_signals():
    s = L.signals(lead(prequal_count="3", likely_to_switch="10", prequal_date="2026-10-20",
                       notes_last_contacted="2026-10-10T12:00:00Z", starting_avail_status="2"))
    assert s == {"newly": True, "repeat": 3, "lts": 10, "cameBack": True}


def test_still_ahead_matches_page_fallback():
    conv = L.FALLBACK_CONV
    assert L.still_ahead(0, conv) == 1
    assert L.still_ahead(7, conv) == pytest.approx(1 - 8977 / 18196)
    assert L.still_ahead(500, conv) == pytest.approx(1 - 13650 / 18196)
    assert L.still_ahead(None, conv) == pytest.approx(1 - 13650 / 18196)


def test_score_and_tiers():
    fresh = L.score_lead(lead(), NOW, None)
    # 3 days old: interpolated still-ahead x no call data (factor 1) -> JS Math.round
    expected = L.still_ahead(fresh["age"], L.FALLBACK_CONV)
    assert fresh["tier"] == 1 and fresh["score"] == int(100 * expected + 0.5)
    old = L.score_lead(lead(hs_v2_date_entered_salesqualifiedlead="2026-05-01T00:00:00Z"), NOW, None)
    assert old["tier"] == 5                                    # no call curve -> P5
    came_back = lead(hs_v2_date_entered_salesqualifiedlead="2026-05-01T00:00:00Z",
                     prequal_date="2026-10-20", notes_last_contacted="2026-10-01T00:00:00Z")
    assert L.score_lead(came_back, NOW, None)["tier"] == 2    # buying signal pulls it up


def test_call_curve_and_p4():
    cust = [500, 300, 200, 150, 100, 80, 60, 40, 20, 10, 5]
    open_ = [500, 300, 300, 300, 300, 300, 300, 300, 300, 300, 300]
    intel = {"calls": {"rate": L.call_rates(cust, open_)}}
    assert L.call_rate(0, intel["calls"]["rate"]) == L.call_rate(1, intel["calls"]["rate"])   # never called = 1 call
    old = lead(hs_v2_date_entered_salesqualifiedlead="2026-01-01T00:00:00Z", of_calls="1")
    assert L.score_lead(old, NOW, intel)["tier"] == 4
    heavy = dict(old, of_calls="25")
    assert L.score_lead(heavy, NOW, intel)["tier"] == 5


def test_rank_order_call_first_then_tier_score_calls():
    def row(cid, tier, score, calls, prio=None):
        p = {"of_calls": str(calls)}
        if prio:
            p["__prio"] = prio
        return {"id": cid, "p": p, "s": {"tier": tier, "score": score}}
    rows = [row("a", 1, 50, 3), row("b", 1, 50, 1), row("c", 1, 80, 9), row("d", 3, 99, 0),
            row("e", 5, 1, 9, {"rank": 2}), row("f", 5, 1, 9, {"rank": 1})]
    assert [r["id"] for r in sorted(rows, key=L.rank_key)] == ["f", "e", "c", "b", "a", "d"]


def test_verify_activity_cooldown_and_bad_notes():
    ok, why = L.verify_activity([{"ts": "2026-10-28T13:00:00Z", "text": "LVM"}], NOW, 3)
    assert not ok and why == "Someone called them in the last 3 days"
    ok, why = L.verify_activity([{"ts": "2026-09-01T13:00:00Z", "text": "Customer said do not call again"}], NOW, 3)
    assert not ok and why.startswith('Note says: "Customer said do not call')
    assert L.verify_activity([{"ts": "2026-09-01T13:00:00Z", "text": "Interested, call back"}], NOW, 3) == (True, None)


def test_body_text_strips_html():
    assert L.body_text("<p>Left&nbsp;VM &amp; text</p>") == "Left VM & text"


# ---------------- claims (real Postgres) ----------------

TEST_DB = os.environ.get("LEAD_FEEDER_INTEGRATION_TEST_DB")
integration = pytest.mark.skipif(not TEST_DB, reason="LEAD_FEEDER_INTEGRATION_TEST_DB not set")

REP_A = {"id": 1, "email": "a@planet.net", "sales_rep_name": "Alex Rivera", "role": "Sales Rep", "status": "active"}
REP_B = {"id": 2, "email": "b@planet.net", "sales_rep_name": "Blake Chen", "role": "Sales Rep", "status": "active"}


@pytest.fixture(scope="module")
def test_db():
    import psycopg2
    import db
    import db_migrations

    admin = psycopg2.connect(host=db.APPDB_HOST, port=db.APPDB_PORT, dbname=db.APPDB_DATABASE,
                             user=db.APPDB_USERNAME, password=db.APPDB_PASSWORD)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cur.execute(f'CREATE DATABASE "{TEST_DB}"')
    original = db.APPDB_DATABASE
    db.APPDB_DATABASE = TEST_DB
    db_migrations._schema_ready = False
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO sales_reps (id, name) VALUES (1, 'Alex Rivera'), (2, 'Blake Chen')")
        cur.execute("INSERT INTO users (id, email, role, status, sales_rep_id) VALUES "
                    "(1, 'a@planet.net', 'Sales Rep', 'active', 1), (2, 'b@planet.net', 'Sales Rep', 'active', 2)")
    conn.close()
    yield
    db.APPDB_DATABASE = original
    db_migrations._schema_ready = False
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    admin.close()


def _scored(p):
    return L.score_lead(p, datetime.now(timezone.utc), None)


@integration
def test_two_reps_racing_for_one_lead(test_db):
    import lead_feeder_store as store
    p = lead()
    winners, barrier = [], threading.Barrier(8)

    def grab(user):
        barrier.wait()
        if store.try_claim("5001", user, p, _scored(p)):
            winners.append(user["id"])

    threads = [threading.Thread(target=grab, args=(REP_A if i % 2 else REP_B,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1                      # exactly one rep gets it

    ok, claims = store.get_claims_today()
    assert ok and claims["5001"]["user_id"] == winners[0] and claims["5001"]["outcome"] == "Calling"
    # It's gone from everyone else's open list.
    store._state.update(entries=[{"id": "5001", "variants": [{"p": p, "s": _scored(p)}]}], version=99)
    store._list_cache.clear()
    assert store.open_leads(L.clean_prefs({}), set(claims)) == []


@integration
def test_outcomes_only_by_holder_and_current_survives_reload(test_db):
    import lead_feeder_store as store
    p = lead()
    assert store.try_claim("5002", REP_A, p, _scored(p))
    assert store.my_current(REP_A) == "5002"                        # a browser refresh finds it again
    assert store.set_outcome("5002", REP_B, "Talked") == (False, "That lead isn't yours today.")
    assert store.set_outcome("5002", REP_A, "Made up") == (False, "Unknown outcome.")
    assert store.set_outcome("5002", REP_A, "Tab closed") == (True, None)
    assert store.my_current(REP_A) is None
    ok, claims = store.get_claims_today()
    assert claims["5002"]["outcome"] == "Tab closed"


@integration
def test_release_after_hubspot_error_frees_the_lead(test_db):
    import lead_feeder_store as store
    p = lead()
    assert store.try_claim("5003", REP_A, p, _scored(p))
    store.release("5003", REP_A, "HubSpot error: 503")
    assert store.try_claim("5003", REP_B, p, _scored(p))


@integration
def test_prefs_are_per_user(test_db):
    import lead_feeder_store as store
    ok, _, prefs = store.save_prefs(1, {"state": "VA", "cool_days": 7, "bogus": 1})
    assert ok and prefs["state"] == "VA" and prefs["cool_days"] == 7 and "bogus" not in prefs
    assert store.get_prefs(2) == L.clean_prefs({})


@integration
def test_list_import(test_db):
    import lead_feeder_store as store
    ok, msg = store.import_list("priority", "Record ID,Rank,Reason\n101,2,Manager pick\nabc,1,x\n", REP_A)
    assert ok and "Loaded 1 rows" in msg and "skipped 1" in msg
    assert store._load_list_rows("priority") == {"101": {"rank": 2, "reason": "Manager pick"}}
    ok, _ = store.import_list("sweep", "hubspot_id,hot,owner_inactive,notes\n202,Yes,true,gate code 12\n", REP_A)
    assert ok and store._load_list_rows("sweep")["202"] == {"hot": "Yes", "owner_inactive": True, "notes": "gate code 12"}


def test_only_active_reps_and_admins_use_lead_feeder():
    import permissions
    assert permissions.can_use_lead_feeder({"role": "Sales Rep", "status": "active"})
    assert permissions.can_use_lead_feeder({"role": "Admin", "status": "active"})
    assert not permissions.can_use_lead_feeder({"role": "Customer Success", "status": "active"})
    assert not permissions.can_use_lead_feeder({"role": "Sales Rep", "status": "pending"})
    assert not permissions.can_use_lead_feeder(None)
