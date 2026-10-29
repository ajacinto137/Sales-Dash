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
from datetime import date, datetime, timedelta, timezone

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


# ---------------- lead libraries ----------------

def test_library_pref_is_remembered_and_validated():
    assert L.clean_prefs({})["library"] is None                       # no pick yet -> picker
    assert L.clean_prefs({"library": "reiss"})["library"] == "reiss"
    assert L.clean_prefs({"library": "nope"})["library"] is None
    assert L.library_of({}) == L.library_of({"library": "nope"}) == "reiss"
    assert L.LIBRARIES["reiss"]["name"] == "Reiss's List"


def test_board_and_claims_are_scoped_to_the_library(monkeypatch):
    import lead_feeder_store as store
    monkeypatch.setitem(L.LIBRARIES, "va_only", {"name": "VA", "description": "", "includes": [], "excludes": [],
                                                  "match": lambda p, now: L.in_state(p, "VA")})
    nj, va = lead(), lead(state="VA", city="Farmville", phone="(434) 555-0100".replace("555", "392"))
    store._state.update(entries=[{"id": "1", "variants": [{"p": nj, "s": L.score_lead(nj, NOW, None)}]},
                                 {"id": "2", "variants": [{"p": va, "s": L.score_lead(va, NOW, None)}]}],
                        version=1234, loaded_at=1.0)
    store._list_cache.clear()
    assert [x["id"] for x in store.open_leads(L.clean_prefs({"library": "reiss"}), set(), NOW)] == ["1", "2"]
    assert [x["id"] for x in store.open_leads(L.clean_prefs({"library": "va_only"}), set(), NOW)] == ["2"]
    # A lead taken in any library is gone from all of them.
    assert store.open_leads(L.clean_prefs({"library": "reiss"}), {"2"}, NOW)[0]["id"] == "1"

    monkeypatch.setattr(store, "_library_results", lambda: ({"reiss": {"worked": 10, "called": 8, "connected": 2,
                                                                        "sales": 1}}, None))
    monkeypatch.setattr(store, "_active_by_library", lambda: {"va_only": 1})
    claims = {"9": {"library_id": "reiss", "user_id": 1, "outcome": "Talked"},
              "8": {"library_id": "reiss", "user_id": 1, "outcome": "Auto-skipped"}}
    cards = {c["id"]: c for c in store.library_summaries(L.clean_prefs({}), claims, NOW)}
    assert cards["reiss"]["open"] == 2 and cards["reiss"]["nj"] == 1 and cards["reiss"]["va"] == 1
    assert cards["reiss"]["called_today"] == 1 and cards["reiss"]["reps_today"] == 1    # auto-skips don't count
    assert cards["reiss"]["sales_30"] == 1 and cards["reiss"]["connect_30"] == 0.25
    assert cards["reiss"]["conversion_30"] == 0.1
    assert cards["va_only"]["open"] == 1 and cards["va_only"]["active_now"] == 1
    assert cards["va_only"]["sales_30"] == 0 and cards["va_only"]["connect_30"] is None


@integration
def test_claim_records_its_library(test_db):
    import lead_feeder_store as store
    p = lead()
    assert store.try_claim("5004", REP_A, p, _scored(p), library="reiss")
    assert store.try_claim("5005", REP_A, p, _scored(p))
    ok, claims = store.get_claims_today()
    assert claims["5004"]["library_id"] == "reiss" and claims["5005"]["library_id"] == "reiss"


def test_only_active_reps_and_admins_use_lead_feeder():
    import permissions
    assert permissions.can_use_lead_feeder({"role": "Sales Rep", "status": "active"})
    assert permissions.can_use_lead_feeder({"role": "Admin", "status": "active"})
    assert not permissions.can_use_lead_feeder({"role": "Customer Success", "status": "active"})
    assert not permissions.can_use_lead_feeder({"role": "Sales Rep", "status": "pending"})
    assert not permissions.can_use_lead_feeder(None)


# ---------------- household check (family order at the address) ----------------

TODAY = date(2026, 10, 28)


def order(**overrides):
    o = {"sale_id": 501, "name": "Jose Lopez", "last_name": "Lopez", "subscriber_uuid": "abc", "order_date": "Mar 3, 2026",
         "install_date": None}
    o.update(overrides)
    return o


@pytest.mark.parametrize("lead_last, order_last, same", [
    ("Lopez", "Lopez", True),
    ("lopez", "LOPEZ ", True),
    ("Lopez-Garcia", "Garcia", True),
    ("Lopez Jr.", "Lopez", True),
    ("Lopez", "Smith", False),
    ("Jr", "Jr", False),
    ("", "Lopez", False),
])
def test_same_family(lead_last, order_last, same):
    assert L.same_family(lead_last, order_last) is same


def test_family_order_current_in_vision_screens_lead_out():
    p = lead()
    p["__household"] = L.household_check(p, [order()], {"abc"}, TODAY)
    assert p["__household"]["status"] == "current"
    assert L.prescreen(p, NOW, PREFS) is False


def test_family_order_not_current_is_called_first():
    p = lead(hs_v2_date_entered_salesqualifiedlead="2026-06-01T14:00:00Z")    # ~150 days: P4/P5 on its own
    p["__household"] = L.household_check(p, [order()], {"someone-else"}, TODAY)
    assert p["__household"]["status"] == "not_current"
    assert L.prescreen(p, NOW, PREFS) == "sql"
    s = L.score_lead(p, NOW, None)
    assert s["tier"] == 2
    assert any("isn't current in Vision" in w for w in s["why"])
    assert "notInstalled" in L.sig_list(p)


def test_order_without_vision_account_counts_as_not_current():
    assert L.household_check(lead(), [order(subscriber_uuid="")], {"abc"}, TODAY)["status"] == "not_current"


def test_any_current_family_order_wins():
    orders = [order(sale_id=2, subscriber_uuid="old"), order(sale_id=1, subscriber_uuid="abc")]
    assert L.household_check(lead(), orders, {"abc"}, TODAY)["order"]["sale_id"] == 1


def test_different_last_name_at_address_is_ignored():
    p = lead()
    assert L.household_check(p, [order(name="Ann Smith", last_name="Smith")], {"abc"}, TODAY) is None
    assert L.prescreen(p, NOW, PREFS) == "sql"


def test_sweep_lead_with_current_family_is_screened_out():
    p = L.sweep_fill(lead(), {"batch": ""})
    p["__household"] = L.household_check(p, [order()], {"abc"}, TODAY)
    assert L.sweep_reason(p, NOW, PREFS) == "Family at this address is a current customer"


@pytest.mark.parametrize("install, dropped", [
    (date(2026, 10, 28), True),     # today
    (date(2026, 11, 4), True),      # later
    (date(2026, 10, 27), False),    # yesterday: falls through to the Vision check
])
def test_install_today_or_later_at_address_screens_lead_out(install, dropped):
    p = lead()
    # Different last name on purpose: a scheduled install drops the lead whoever ordered.
    p["__household"] = L.household_check(p, [order(name="Ann Smith", last_name="Smith", install_date=install)], set(), TODAY)
    assert (L.prescreen(p, NOW, PREFS) is False) is dropped
    if dropped:
        assert p["__household"]["status"] == "scheduled"


def test_scheduled_install_wins_over_not_current_family_order():
    orders = [order(sale_id=9, subscriber_uuid="", install_date=None), order(sale_id=8, install_date=date(2026, 11, 2))]
    result = L.household_check(lead(), orders, set(), TODAY)
    assert result["status"] == "scheduled" and result["order"]["sale_id"] == 8
    assert "on Nov 2, 2026" in result["why"]


def test_sweep_lead_with_scheduled_install_is_screened_out():
    p = L.sweep_fill(lead(), {"batch": ""})
    p["__household"] = L.household_check(p, [order(install_date=TODAY)], set(), TODAY)
    assert L.sweep_reason(p, NOW, PREFS) == "Install scheduled at this address"


def test_orders_at_matches_street_and_zip_and_current_vision_uuids():
    import datetime as dt

    import pandas as pd

    import lead_feeder_store as S
    import order_search

    raw = pd.DataFrame([
        {"ID": 1, "FirstName": "Jose", "LastName": "Lopez", "Address": "12 Oak Street", "ApartmentSuite": "",
         "City": "Sparta", "State": "NJ", "Zipcode": "07871-1234", "InsertDate": "2026-03-03", "ReferralData": None,
         "SourceToken": None, "InsertByEmployeeID": None, "vi_subscriber_uuid": "ABC ", "InstallDate": "2026-11-04 08:00"},
        {"ID": 2, "FirstName": "Ann", "LastName": "Lopez", "Address": "14 Oak St", "ApartmentSuite": "",
         "City": "Sparta", "State": "NJ", "Zipcode": "07871", "InsertDate": "2026-03-04", "ReferralData": None,
         "SourceToken": None, "InsertByEmployeeID": None, "vi_subscriber_uuid": None, "InstallDate": None},
    ])
    df = order_search.build_index(raw)
    S._orders_index["df"] = None
    orig = order_search.get_index
    order_search.get_index = lambda: df
    try:
        df_, idx = S._orders_by_address()
        hits = S.orders_at(df_, idx, "12 Oak St.", "07871", "Sparta")
        assert [h["sale_id"] for h in hits] == [1] and hits[0]["subscriber_uuid"] == "abc"
        assert hits[0]["install_date"] == dt.date(2026, 11, 4)
        assert S.orders_at(df_, idx, "14 Oak St", "07871", "Sparta")[0]["install_date"] is None
        assert [h["sale_id"] for h in S.orders_at(df_, idx, "12 oak st", "", "SPARTA")] == [1]
    finally:
        order_search.get_index = orig
        S._orders_index["df"] = None

    vision = pd.DataFrame([
        {"subscriber_uuid": "ABC", "active": True, "estimated_cancellation_date": None},
        {"subscriber_uuid": "gone", "active": True, "estimated_cancellation_date": "2026-10-01"},
        {"subscriber_uuid": "off", "active": False, "estimated_cancellation_date": None},
        {"subscriber_uuid": "later", "active": True, "estimated_cancellation_date": "2026-12-01"},
    ])
    assert S.current_vision_uuids(vision, dt.date(2026, 10, 28)) == {"abc", "later"}


def test_planetweb_lookup_matches_house_number_and_unit(monkeypatch):
    """Passings' Street column has no house number; the lookup keys on the
    FullAddress line instead, with or without an apartment/unit."""
    import pandas as pd
    import lead_feeder_store as S
    import passings_cache as pc

    df = pd.DataFrame.from_records(
        [(7, None, None, "12 Main St", "Main St", "NEWARK", "NJ", "07101", 1, None, 1, 0, None, None, None),
         (8, None, None, "40 Oak Avenue Apt 3", "Oak Ave", "NEWARK", "NJ", "07101", 1, None, 4, 0, None, None, None)],
        columns=pc._SNAPSHOT_COLUMNS)
    monkeypatch.setitem(pc._state, "df", pc._prepare(df))
    monkeypatch.setattr(S.order_search, "get_index", lambda: None)

    hit = S.planetweb_lookup({"street": "12 Main Street", "zip": "07101-1234", "address": ""})
    assert hit["place"]["place_id"] == 7 and hit["place"]["pas_url"]
    assert S.planetweb_lookup({"street": "40 Oak Ave", "zip": "07101"})["place"]["place_id"] == 8
    assert S.planetweb_lookup({"street": "40 Oak Ave Unit 3", "zip": "07101"})["place"]["place_id"] == 8
    assert S.planetweb_lookup({"street": "99 Main St", "zip": "07101"})["place"] is None


def test_every_library_obeys_the_household_rules(monkeypatch):
    """A library's recipe can't bring back a lead the order-at-address
    check dropped, and the not-current family lead stays, at P2."""
    import lead_feeder_store as store
    monkeypatch.setitem(L.LIBRARIES, "everyone", {"name": "Everyone", "description": "", "includes": [],
                                                   "excludes": [], "match": lambda p, now: True})
    scheduled, current, family, clean = lead(), lead(), lead(hs_v2_date_entered_salesqualifiedlead="2026-03-01T14:00:00Z"), lead()
    scheduled["__household"] = L.household_check(
        scheduled, [order(name="Ann Smith", last_name="Smith", install_date=date(2026, 11, 4))], set(), TODAY)
    current["__household"] = L.household_check(current, [order()], {"abc"}, TODAY)
    family["__household"] = L.household_check(family, [order(subscriber_uuid="")], {"abc"}, TODAY)
    entries = [{"id": cid, "variants": [{"p": p, "s": L.score_lead(p, NOW, None)}]}
               for cid, p in (("1", scheduled), ("2", current), ("3", family), ("4", clean))]
    store._state.update(entries=entries, version=4321, loaded_at=1.0)
    store._list_cache.clear()
    assert "Nov 4, 2026" in scheduled["__household"]["why"]
    for library in L.LIBRARIES:
        leads = {x["id"]: x for x in store.open_leads(L.clean_prefs({"library": library}), set(), NOW)}
        assert set(leads) == {"3", "4"}, library
        assert leads["3"]["s"]["tier"] == 2 and "notInstalled" in L.sig_list(leads["3"]["p"])
