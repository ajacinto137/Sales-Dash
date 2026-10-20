"""External Rep Sales (external_sales_store.py).

The unit tests run anywhere. The integration tests (sync, filtering,
summary cards, export, mapping) need a REAL Postgres and run only when
EXTERNAL_SALES_INTEGRATION_TEST_DB is set: that database is created on
the configured APPDB server, migrated, and dropped afterwards -- the
app's own appdb is never touched. E.g. inside the dashboard container:

    docker compose exec -e EXTERNAL_SALES_INTEGRATION_TEST_DB=external_sales_it dashboard \\
        python -m pytest tests/test_external_sales.py -v
"""

import os
import re
from datetime import date, datetime

import pytest
from werkzeug.datastructures import MultiDict

import external_sales_store as store
from queries import EXTERNAL_REP_SALES_QUERY

TEST_DB = os.environ.get("EXTERNAL_SALES_INTEGRATION_TEST_DB")
integration = pytest.mark.skipif(not TEST_DB, reason="EXTERNAL_SALES_INTEGRATION_TEST_DB not set")


# ================================================================
# Unit tests
# ================================================================

def test_query_matches_vba_source():
    codes = re.search(r"fd\.ReferralData IN \((.*?)\)", EXTERNAL_REP_SALES_QUERY, re.S).group(1)
    codes = re.findall(r"'([^']+)'", codes)
    assert len(codes) == 91 and len(set(codes)) == 91
    assert codes[0] == "mritchie" and codes[-1] == "joakley"
    assert {"mastle", "mastle2", "repperson"} <= set(codes)
    excluded = re.search(r"fd\.ID NOT IN \((.*?)\)", EXTERNAL_REP_SALES_QUERY, re.S).group(1)
    assert re.findall(r"'([^']+)'", excluded) == ["201443", "203769", "214436"]
    assert "WHERE fd.QuoteID IS NOT NULL" in EXTERNAL_REP_SALES_QUERY
    assert "r2.ServiceName LIKE '%Voice%'" in EXTERNAL_REP_SALES_QUERY


def test_columns_cover_all_21_fields_with_spec_labels():
    assert len(store.SOURCE_COLUMNS) == 21
    assert [c[3] for c in store.COLUMNS if not c[1]] == [
        "Install Date", "Order Cancellation Date", "Service Cancellation Date"]
    labels = {src: label for _key, src, _col, label, _shown in store.SOURCE_COLUMNS}
    assert labels == {
        "ID": "Order ID", "Speed": "Package Speed", "FirstName": "First Name", "LastName": "Last Name",
        "EmailAddress": "Email", "Address": "Address", "ApartmentSuite": "Apartment / Suite", "City": "City",
        "State": "State", "Zipcode": "ZIP Code", "ReferralData": "External Rep", "Date": "Sale Date", "Q1": "Q1",
        "PhoneNumber": "Phone Number", "Municipality": "Municipality", "Cc_PayBy": "Payment Method",
        "vi_subscriber_id": "Subscriber ID", "vi_subscriber_uuid": "Subscriber UUID", "Voice": "Voice",
        "Scheduled": "Scheduling Status", "StartDate": "Installation Date",
    }


def test_parse_filters_defaults_newest_first():
    f = store.parse_filters(MultiDict())
    assert (f["sort"], f["dir"], f["page"], f["per_page"]) == ("sale_date", "desc", 1, 50)
    assert not f["include_removed"]


def test_parse_filters_rejects_bad_values():
    f = store.parse_filters(MultiDict({
        "sort": "order_id; DROP TABLE users", "dir": "sideways", "page": "-4", "per_page": "100000",
        "speed": "1000abc", "scheduled": "Maybe", "date_from": "not-a-date",
    }))
    assert f["sort"] == "sale_date" and f["dir"] == "desc"
    assert f["page"] == 1 and f["per_page"] == 50
    assert f["speed"] is None and f["scheduled"] == "" and f["date_from"] is None


def test_parse_filters_swaps_reversed_dates_and_text_sort_defaults_ascending():
    f = store.parse_filters(MultiDict({"date_from": "2026-10-01", "date_to": "2026-09-01", "sort": "last_name"}))
    assert (f["date_from"], f["date_to"]) == (date(2026, 9, 1), date(2026, 10, 1))
    assert f["dir"] == "asc"


def test_search_escapes_like_wildcards():
    where, params = store._build_where(store.parse_filters(MultiDict({"search": "100%_x"})))
    assert "ILIKE" in where
    assert params[0] == "%100\\%\\_x%"


def test_order_cancellation_query_excludes_rebooked_orders():
    from queries import EXTERNAL_ORDER_CANCELLATIONS_QUERY as q
    assert q.count("StatusID = 16") == 2 and "live.StatusID <> 16" in q and "NOT EXISTS" in q


def test_enrichment_failures_never_raise(monkeypatch):
    def boom(*_args):
        raise RuntimeError("KPI down")
    monkeypatch.setattr(store, "_fetch_install_and_service_dates", boom)
    monkeypatch.setattr(store, "_fetch_order_cancellations", lambda: {7: date(2026, 5, 1)})
    values, failed, warnings = store.fetch_enrichment([{"order_id": 7, "subscriber_uuid": "u7"}])
    assert failed == {"install_date", "service_cancellation_date"}
    assert values[7]["order_cancellation_date"] == date(2026, 5, 1)
    assert "KPI down" in warnings[0]


def test_csv_neutralizes_formulas():
    assert store._csv_value("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert store._csv_value("@SUM(A1)") == "'@SUM(A1)"
    assert store._csv_value("Main St") == "Main St"
    assert store._csv_value(None) == ""
    assert store._csv_value(True) == "1"
    assert store._csv_value(date(2026, 3, 5)) == "2026-03-05"
    assert store._csv_value(datetime(2026, 3, 5, 15, 0)) == "2026-03-05 15:00"


# ================================================================
# Integration tests (real Postgres, fake PlanetWeb)
# ================================================================

def sale(order_id, code="mritchie", day="2026-10-01", speed=1000, state="NJ", muni="Hoboken",
         voice=False, scheduled=True, first="Pat", last="Lee", **extra):
    row = {
        "order_id": order_id, "package_speed": speed, "first_name": first, "last_name": last,
        "email": f"c{order_id}@example.com", "address": f"{order_id} Main St", "apartment_suite": None,
        "city": "Hoboken", "state": state, "zipcode": "07030", "referral_code": code,
        "sale_date": date.fromisoformat(day), "q1": None, "phone_number": "(555) 555-0100",
        "municipality": muni, "payment_method": "card", "subscriber_id": None, "subscriber_uuid": None,
        "voice": voice, "scheduled_status": "Scheduled" if scheduled else "Not Scheduled",
        "installation_date": datetime(2026, 10, 20, 9, 0) if scheduled else None,
    }
    row.update(extra)
    return row


BASE = [
    sale(1001, "mritchie", "2026-09-01", voice=True),
    sale(1002, "mritchie", "2026-10-05", scheduled=False),
    sale(1003, "jsparks", "2026-10-10", speed=250, state="NY", muni="Albany", first="=cmd"),
    sale(1004, "mastle", "2026-10-15"),
    sale(1005, "mastle2", "2026-10-18", voice=True, scheduled=False),
]


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
        cur.execute("INSERT INTO users (id, email, role) VALUES (1, 'admin@planet.net', 'Admin'), (2, 'rep@outside.com', 'Other')")
    conn.close()
    yield
    db.APPDB_DATABASE = original
    db_migrations._schema_ready = False
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    admin.close()


def q(sql, params=()):
    import db
    conn = db.get_appdb_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def execute(sql, params=()):
    import db
    conn = db.get_appdb_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


ENRICH = {}  # order_id -> {column: value}, what the fake lookups "return"


def fake_enrich(rows, failed=()):
    values = {r["order_id"]: {c: (None if c in failed else ENRICH.get(r["order_id"], {}).get(c))
                              for c in store._ENRICHED_COLUMNS} for r in rows}
    return values, set(failed), [f"{c} lookup failed" for c in failed]


def sync(rows, failed=()):
    ran, summary = store.run_sync("manual", fetch=lambda: [dict(r) for r in rows],
                                  enrich=lambda rs: fake_enrich(rs, failed))
    assert ran
    return summary


def query(**args):
    return store.query_sales(store.parse_filters(MultiDict(args)))


@integration
def test_initial_sync_inserts_and_dedupes(test_db):
    summary = sync(BASE + [sale(1001, "mritchie", "2026-09-01", voice=True)])
    assert summary["error"] is None
    assert summary["rows_fetched"] == 5 and summary["new_rows"] == 5
    assert "duplicate" in summary["warning"]
    assert q("SELECT array_agg(order_id ORDER BY order_id) FROM external_rep_sales")[0][0] == [1001, 1002, 1003, 1004, 1005]
    # Every code with a sale gets an (unmapped) mapping row; mastle and
    # mastle2 stay separate codes.
    assert q("SELECT array_agg(referral_code ORDER BY referral_code) FROM external_reps")[0][0] == \
        ["jsparks", "mastle", "mastle2", "mritchie"]
    status = store.get_status()
    assert status["state"]["connection_ok"] is True and status["state"]["last_success_at"] is not None
    assert status["in_source_total"] == 5 and status["codes_unmapped"] == 4


@integration
def test_resync_counts_only_real_changes(test_db):
    changed = [dict(r) for r in BASE]
    changed[1].update(scheduled_status="Scheduled", installation_date=datetime(2026, 10, 25, 12, 0))
    summary = sync(changed)
    assert (summary["new_rows"], summary["updated_rows"], summary["removed_rows"]) == (0, 1, 0)
    assert q("SELECT scheduled_status FROM external_rep_sales WHERE order_id = 1002")[0][0] == "Scheduled"
    assert sync(changed)["updated_rows"] == 0


@integration
def test_failed_refresh_keeps_data(test_db):
    def boom():
        raise RuntimeError("Login failed for user 'readonly'")

    ran, summary = store.run_sync("manual", fetch=boom)
    assert ran and "Login failed" in summary["error"]
    status = store.get_status()
    assert status["state"]["connection_ok"] is False and "Login failed" in status["state"]["last_error"]
    assert status["state"]["last_success_at"] is not None
    assert status["in_source_total"] == 5 and status["latest_run"]["status"] == "failed"


@integration
def test_missing_rows_flagged_never_deleted_and_restored(test_db):
    rows = [dict(r) for r in BASE]
    rows[1].update(scheduled_status="Scheduled", installation_date=datetime(2026, 10, 25, 12, 0))
    summary = sync([r for r in rows if r["order_id"] != 1004])
    assert summary["removed_rows"] == 1 and summary["error"] is None
    assert q("SELECT in_source, removed_from_source_at IS NOT NULL FROM external_rep_sales WHERE order_id = 1004")[0] == (False, True)
    assert query()["total"] == 4
    assert query(include_removed="1")["total"] == 5

    summary = sync(rows)
    assert summary["restored_rows"] == 1
    assert q("SELECT in_source, removed_from_source_at FROM external_rep_sales WHERE order_id = 1004")[0] == (True, None)


@integration
def test_incomplete_refresh_does_not_flag_removals(test_db):
    many = [sale(2000 + i, "dpond", "2026-08-01") for i in range(40)]
    rows = [dict(r) for r in BASE]
    rows[1].update(scheduled_status="Scheduled", installation_date=datetime(2026, 10, 25, 12, 0))
    sync(rows + many)
    summary = sync(rows[:3])  # 3 of 45 rows -- looks truncated
    assert summary["removed_rows"] == 0 and "incomplete" in summary["warning"]
    assert store.get_status()["in_source_total"] == 45
    summary = sync([])
    assert summary["removed_rows"] == 0 and "incomplete" in summary["warning"]
    sync(rows)  # 5 of 45 is still below MIN_COMPLETE_RATIO -> still guarded
    assert store.get_status()["in_source_total"] == 45


@integration
def test_summary_filters_sorting_and_search(test_db):
    execute("UPDATE external_rep_sales SET in_source = false WHERE order_id >= 2000")
    r = query()
    assert r["ok"] and r["total"] == 5
    assert [row["order_id"] for row in r["rows"]] == [1005, 1004, 1003, 1002, 1001]  # newest first
    s = r["summary"]
    assert (s["total_sales"], s["active_reps"], s["scheduled"], s["unscheduled"], s["voice"]) == (5, 4, 4, 1, 2)  # 1002 was rescheduled above
    assert s["voice_rate"] == pytest.approx(40.0)

    r = query(date_from="2026-10-05", date_to="2026-10-15")  # inclusive on both ends
    assert sorted(row["order_id"] for row in r["rows"]) == [1002, 1003, 1004]
    assert r["summary"]["active_reps"] == 3

    assert [row["order_id"] for row in query(rep="mritchie")["rows"]] == [1002, 1001]
    assert [row["order_id"] for row in query(state="NY")["rows"]] == [1003]
    assert [row["order_id"] for row in query(municipality="Albany")["rows"]] == [1003]
    assert [row["order_id"] for row in query(speed="250")["rows"]] == [1003]
    assert sorted(row["order_id"] for row in query(scheduled="Not Scheduled")["rows"]) == [1005]
    assert [row["order_id"] for row in query(search="1003")["rows"]] == [1003]
    assert [row["order_id"] for row in query(search="1003 main")["rows"]] == [1003]
    assert [row["order_id"] for row in query(sort="order_id", dir="asc")["rows"]] == [1001, 1002, 1003, 1004, 1005]

    page = query(per_page="25", page="9")
    assert page["page"] == 1  # clamped


@integration
def test_mapping_updates_and_rep_search(test_db):
    rep_id = q("SELECT id FROM external_reps WHERE referral_code = 'mastle'")[0][0]
    rep2_id = q("SELECT id FROM external_reps WHERE referral_code = 'mastle2'")[0][0]
    form = {"rep_name": "Mike Astle", "rep_email": "mike@outside.com", "organization": "Door Co",
            "manager_name": "Dana Boss", "manager_email": "dana@outside.com", "dashboard_user_id": "2", "active": "1"}
    assert store.update_rep_mapping(rep_id, form, 1) == (True, None)
    # Two codes, one person -- allowed.
    assert store.update_rep_mapping(rep2_id, form, 1) == (True, None)
    assert q("SELECT count(*) FROM external_organizations")[0][0] == 1

    ok, error = store.update_rep_mapping(rep_id, {**form, "rep_email": "nope"}, 1)
    assert not ok and "email" in error
    ok, error = store.update_rep_mapping(rep_id, {**form, "dashboard_user_id": "999"}, 1)
    assert not ok
    assert store.update_rep_mapping(999999, form, 1)[0] is False

    assert sorted(row["order_id"] for row in query(search="Astle")["rows"]) == [1004, 1005]
    org_id = q("SELECT id FROM external_organizations")[0][0]
    assert sorted(row["order_id"] for row in query(organization=str(org_id))["rows"]) == [1004, 1005]
    assert sorted(row["order_id"] for row in query(organization=store.UNMAPPED_ORG)["rows"]) == [1001, 1002, 1003]
    mapping = {m["referral_code"]: m for m in store.list_rep_mappings()}
    assert mapping["mastle"]["organization_name"] == "Door Co" and mapping["mastle"]["sales"] == 1
    assert store.get_status()["codes_unmapped"] == 3  # jsparks, mritchie, dpond


@integration
def test_export_csv(test_db):
    text = "".join(store.iter_export_csv(store.parse_filters(MultiDict({"state": "NY"}))))
    lines = text.strip().splitlines()
    assert lines[0].startswith("Order ID,Package Speed,First Name")
    assert len(lines) == 2 and lines[1].startswith("1003,250,'=cmd,")


@integration
def test_install_and_cancellation_dates(test_db):
    rows = [dict(r) for r in BASE]
    rows[1].update(scheduled_status="Scheduled", installation_date=datetime(2026, 10, 25, 12, 0))
    ENRICH.update({
        1001: {"install_date": date(2026, 9, 12)},
        1003: {"install_date": date(2026, 10, 20), "service_cancellation_date": date(2026, 10, 21)},
        1005: {"order_cancellation_date": date(2026, 10, 19)},
    })
    summary = sync(rows)
    assert summary["error"] is None and summary["updated_rows"] == 3
    r = query()
    s = r["summary"]
    assert (s["installed"], s["order_cancellations"], s["service_cancellations"]) == (2, 1, 1)
    assert s["install_rate"] == pytest.approx(40.0)
    assert sorted(x["order_id"] for x in query(install="installed")["rows"]) == [1001, 1003]
    assert sorted(x["order_id"] for x in query(install="not_installed")["rows"]) == [1002, 1004, 1005]
    assert [x["order_id"] for x in query(cancellation="order")["rows"]] == [1005]
    assert [x["order_id"] for x in query(cancellation="service")["rows"]] == [1003]
    assert sorted(x["order_id"] for x in query(cancellation="any")["rows"]) == [1003, 1005]
    assert sorted(x["order_id"] for x in query(cancellation="none")["rows"]) == [1001, 1002, 1004]
    assert [x["order_id"] for x in query(sort="install_date")["rows"]][:2] == [1003, 1001]  # newest first

    # A failed Vision lookup keeps the stored install/service dates and warns.
    ENRICH.clear()
    summary = sync(rows, failed=("install_date", "service_cancellation_date"))
    assert summary["error"] is None and "lookup failed" in summary["warning"]
    s = query()["summary"]
    assert (s["installed"], s["service_cancellations"]) == (2, 1)
    assert s["order_cancellations"] == 0  # that lookup worked and returned nothing

    text = "".join(store.iter_export_csv(store.parse_filters(MultiDict({"install": "installed"}))))
    header, *lines = text.strip().splitlines()
    assert header.split(",")[21:25] == ["Install Date", "Order Cancellation Date", "Service Cancellation Date", "Install Status"]
    assert all(",Installed," in line for line in lines) and len(lines) == 2
