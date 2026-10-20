"""Staffing calendar (staffing_store.py).

Unit tests run anywhere. The integration tests need a REAL Postgres and
run only when STAFFING_INTEGRATION_TEST_DB is set (that database is
created, migrated and dropped -- the app's own appdb is never touched):

    docker compose exec -e STAFFING_INTEGRATION_TEST_DB=staffing_it dashboard \\
        python -m pytest tests/test_staffing.py -v
"""

import os
from datetime import date, time

import pytest

import permissions
import staffing_store as store

TEST_DB = os.environ.get("STAFFING_INTEGRATION_TEST_DB")
integration = pytest.mark.skipif(not TEST_DB, reason="STAFFING_INTEGRATION_TEST_DB not set")

TODAY = date(2026, 10, 20)  # a Tuesday
SATURDAY = date(2026, 10, 24)

ADMIN = {"id": 1, "email": "admin@planet.net", "role": "Admin", "status": "active", "sales_rep_id": None}
REP_A = {"id": 2, "email": "a@planet.net", "role": "Sales Rep", "status": "active", "sales_rep_id": 1}
REP_B = {"id": 3, "email": "b@planet.net", "role": "Sales Rep", "status": "active", "sales_rep_id": 2}
UNLINKED = {"id": 4, "email": "cs@planet.net", "role": "Customer Success", "status": "active", "sales_rep_id": None}


def test_permissions():
    assert permissions.can_join_events(UNLINKED) and permissions.can_join_events(ADMIN)
    assert not permissions.can_join_events(None)
    assert store.staff_key(REP_A) == "rep:1" and store.staff_key(ADMIN) == "user:1"
    assert permissions.can_manage_staffing(ADMIN)
    assert not permissions.can_manage_staffing(REP_A)
    assert not permissions.can_manage_staffing({**ADMIN, "status": "disabled"})
    assert permissions.can_staff_self(REP_A)
    assert not permissions.can_staff_self(UNLINKED)
    assert not permissions.can_staff_self(None)


def test_time_formatting():
    assert store.format_time(time(10, 0)) == "10 AM"
    assert store.format_time(time(13, 30)) == "1:30 PM"
    assert store.format_range(time(10, 0), time(14, 0)) == "10 AM – 2 PM"
    assert store.format_range(None, None) == ""


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
        cur.execute("""INSERT INTO users (id, email, role, sales_rep_id) VALUES
            (1, 'admin@planet.net', 'Admin', NULL), (2, 'a@planet.net', 'Sales Rep', 1),
            (3, 'b@planet.net', 'Sales Rep', 2), (4, 'cs@planet.net', 'Customer Success', NULL)""")
    conn.close()
    yield
    db.APPDB_DATABASE = original
    db_migrations._schema_ready = False
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    admin.close()


EVENT = {"title": "Fall Festival booth", "event_date": SATURDAY.isoformat(), "start_time": "10:00",
         "end_time": "16:00", "location": "Hoboken", "staff_needed": "2", "notes": ""}


def _event_id():
    cal = store.get_calendar(2026, 10, SATURDAY, TODAY)
    return cal["day"]["events"][0]["id"]


@integration
def test_only_admins_post_events_and_never_in_the_past(test_db):
    assert store.create_event(EVENT, REP_A, TODAY) == (False, "Only Admins can post events.")
    ok, error = store.create_event({**EVENT, "event_date": "2026-10-19"}, ADMIN, TODAY)
    assert not ok and "passed" in error
    ok, error = store.create_event({**EVENT, "end_time": "09:00"}, ADMIN, TODAY)
    assert not ok and "after the start" in error
    ok, error = store.create_event({**EVENT, "staff_needed": "0"}, ADMIN, TODAY)
    assert not ok
    assert store.create_event(EVENT, ADMIN, TODAY) == (True, None)


@integration
def test_reps_sign_themselves_up(test_db):
    event_id = _event_id()
    assert store.sign_up(event_id, {}, REP_A, TODAY) == (True, None)
    assert store.sign_up(event_id, {}, REP_A, TODAY) == (False, "Already signed up for this event.")
    # A rep can't sign someone else up -- sales_rep_id is ignored for non-admins.
    assert store.sign_up(event_id, {"sales_rep_id": "2"}, REP_A, TODAY)[0] is False
    day = store.get_calendar(2026, 10, SATURDAY, TODAY)["day"]
    event = day["events"][0]
    assert (event["filled"], event["open_spots"], [p["name"] for p in event["staff"]]) == (1, 1, ["Alex Rivera"])
    assert event["staff_keys"] == {"rep:1"}
    assert store.get_event_date(event_id) == SATURDAY

    # An account with no linked rep (here Customer Success) staffs as itself.
    assert store.sign_up(event_id, {}, UNLINKED, TODAY) == (True, None)
    assert store.sign_up(event_id, {}, UNLINKED, TODAY) == (False, "Already signed up for this event.")
    cal = store.get_calendar(2026, 10, SATURDAY, TODAY)
    event = cal["day"]["events"][0]
    assert event["staff_keys"] == {"rep:1", "user:4"} and event["staff"][1]["name"] == "cs@planet.net"
    assert event["open_spots"] == 0 and cal["open_events"] == []

    # Admin adds Blake (overbooks) and removes the CS user by user id.
    assert store.sign_up(event_id, {"sales_rep_id": "2"}, ADMIN, TODAY) == (True, None)
    assert store.leave_event(event_id, {"staff_user_id": "4"}, ADMIN, TODAY) == (True, None)
    assert store.get_calendar(2026, 10, SATURDAY, TODAY)["day"]["events"][0]["staff_keys"] == {"rep:1", "rep:2"}

    # The admin themselves (no linked rep) can join too.
    assert store.sign_up(event_id, {}, ADMIN, TODAY) == (True, None)
    assert store.leave_event(event_id, {}, ADMIN, TODAY) == (True, None)

    assert store.leave_event(event_id, {}, REP_A, TODAY) == (True, None)
    assert store.leave_event(event_id, {}, REP_A, TODAY) == (False, "Not signed up for this event.")
    assert [e["title"] for e in store.get_calendar(2026, 10, SATURDAY, TODAY)["open_events"]] == ["Fall Festival booth"]

    # Once the day has passed, reps can't sign up or back out.
    assert "passed" in store.sign_up(event_id, {}, REP_A, date(2026, 10, 25))[1]
    assert store.leave_event(event_id, {}, REP_B, date(2026, 10, 25)) == (False, "This event has already happened.")


SHIFT = {"shift_date": SATURDAY.isoformat(), "area": "Uptown Hoboken"}


@integration
def test_door_knock_shifts(test_db):
    assert store.add_shift(SHIFT, REP_A, TODAY) == (True, None)
    # One shift per rep per day -- no times to tell two apart.
    ok, error = store.add_shift({**SHIFT, "area": "Downtown"}, REP_A, TODAY)
    assert not ok and "already a door-knocking shift" in error
    assert store.add_shift({**SHIFT, "shift_date": "2026-10-25"}, REP_A, TODAY) == (True, None)
    assert store.add_shift(SHIFT, REP_B, TODAY) == (True, None)  # same day, other rep
    # Times sent by an old form are simply ignored.
    assert store.add_shift({**SHIFT, "shift_date": "2026-10-31", "start_time": "10:00"}, REP_B, TODAY) == (True, None)
    assert "passed" in store.add_shift({**SHIFT, "shift_date": "2026-10-01"}, REP_A, TODAY)[1]
    assert "knocking" in store.add_shift({**SHIFT, "area": " "}, REP_A, TODAY)[1]
    assert "isn't linked" in store.add_shift(SHIFT, UNLINKED, TODAY)[1]

    cal = store.get_calendar(2026, 10, SATURDAY, TODAY)
    shifts = cal["day"]["shifts"]
    assert [(s["rep_name"], s["area"]) for s in shifts] == [("Alex Rivera", "Uptown Hoboken"), ("Blake Chen", "Uptown Hoboken")]
    assert "time_display" not in shifts[0]
    saturday_cell = next(c for w in cal["weeks"] for c in w if c and c["date"] == SATURDAY)
    assert saturday_cell["is_weekend"] and len(saturday_cell["shifts"]) == 2 and len(saturday_cell["events"]) == 1

    blakes = next(s for s in shifts if s["rep_name"] == "Blake Chen")
    assert store.delete_shift(blakes["id"], REP_A, TODAY) == (False, "You can only remove your own shifts.")
    assert store.delete_shift(blakes["id"], REP_B, TODAY) == (True, None)
    alexs = shifts[0]
    assert store.delete_shift(alexs["id"], REP_A, date(2026, 10, 25)) == (False, "This shift has already happened.")
    assert store.delete_shift(alexs["id"], ADMIN, date(2026, 10, 25)) == (True, None)
    # Removing the day's shift frees the day up again.
    assert store.add_shift({**SHIFT, "area": "Downtown"}, REP_A, TODAY) == (True, None)


@integration
def test_calendar_grid_and_event_delete(test_db):
    cal = store.get_calendar(2026, 10, SATURDAY, TODAY)
    assert cal["month_label"] == "October 2026"
    assert cal["weeks"][0][:3] == [None, None, None]  # Oct 1 2026 is a Thursday
    assert cal["weeks"][0][3]["day"] == 1
    assert (cal["prev"], cal["next"]) == (date(2026, 9, 1), date(2026, 11, 1))
    event_id = _event_id()
    assert store.delete_event(event_id, REP_A) == (False, "Only Admins can remove events.")
    assert store.delete_event(event_id, ADMIN) == (True, None)
    assert store.get_calendar(2026, 10, SATURDAY, TODAY)["day"]["events"] == []
