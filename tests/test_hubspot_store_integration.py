"""Integration tests for hubspot_store.py -- migrations, owner mapping,
initial + incremental sync, dedupe/update counting, the single-sync lock,
failure handling and the analytics SQL -- against a REAL Postgres, using
a fake HubSpot client (no network).

Runs only when HUBSPOT_INTEGRATION_TEST_DB is set. It creates that
database on the configured APPDB server, runs every migration in it, and
drops it afterwards -- the app's own appdb is never touched. E.g. inside
the dashboard container:

    docker compose exec -e HUBSPOT_INTEGRATION_TEST_DB=hubspot_it dashboard \\
        python -m pytest tests/test_hubspot_store_integration.py -v
"""

import os
from datetime import date, datetime, timezone

import pytest

TEST_DB = os.environ.get("HUBSPOT_INTEGRATION_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="HUBSPOT_INTEGRATION_TEST_DB not set")

import psycopg2  # noqa: E402

import db  # noqa: E402
import db_migrations  # noqa: E402
import hubspot_client  # noqa: E402
import hubspot_logic  # noqa: E402
import hubspot_store  # noqa: E402

CONNECTED = hubspot_logic.DEFAULT_CONNECTED_DISPOSITION_ID
NO_ANSWER = "73a0d17f-1163-4015-bdd5-ec830791da20"


def call(call_id, owner, ts, direction="OUTBOUND", duration="60000", disposition=NO_ANSWER,
         modified="2026-10-01T00:00:00Z", status="COMPLETED", recording=None):
    return {"id": call_id, "properties": {
        "hs_call_recording_url": recording,
        "hs_object_id": call_id, "hubspot_owner_id": owner, "hs_timestamp": ts,
        "hs_call_direction": direction, "hs_call_duration": duration, "hs_call_disposition": disposition,
        "hs_call_status": status, "hs_call_to_number": "+15550000000", "hs_lastmodifieddate": modified,
    }}


class FakeClient:
    def __init__(self, owners, pages, incremental_pages=None, fail=None):
        self.owners, self.pages, self.incremental_pages, self.fail = owners, pages, incremental_pages or [], fail
        self.incremental_since = None

    def list_all_owners(self):
        if self.fail:
            raise self.fail
        return self.owners

    def get_call_contact_ids(self, call_ids):
        links = {"1": "501", "2": "502"}  # 502 = a since-deleted contact
        return {c: links[c] for c in call_ids if c in links}

    def get_contacts(self, contact_ids):
        known = {"501": {"name": "Pat Lee", "email": "pat@example.com"}}
        return {c: known[c] for c in contact_ids if c in known}

    def get_account_details(self):
        return {"portalId": 12345, "uiDomain": "app.hubspot.com"}

    def list_call_dispositions(self):
        return [{"id": CONNECTED, "label": "Connected"}, {"id": NO_ANSWER, "label": "No answer"}]

    def iter_calls_modified_since(self, since_ms, extra_filters=None):
        self.extra_filters = extra_filters
        if since_ms == 0:  # initial sync
            yield from self.pages
            return
        self.incremental_since = since_ms
        yield from self.incremental_pages


OWNERS = [
    {"id": "101", "email": "REP.ONE@planet.net", "firstName": "Rep", "lastName": "One", "userId": 9001, "archived": False},
    {"id": "102", "email": "x@elsewhere.com", "firstName": "Rep", "lastName": "Two", "userId": 9002, "archived": False},
    {"id": "103", "email": "ghost@elsewhere.com", "firstName": "Nobody", "lastName": "Known", "userId": 9003, "archived": True},
]

INITIAL_PAGES = [
    [
        # 2026-10-02 02:00Z is still Oct 1 in America/New_York.
        call("1", "101", "2026-10-02T02:00:00Z", disposition=CONNECTED, duration="120000",
             recording="https://api-na1.hubspot.com/recording/auth/provider/abc"),
        call("2", "101", "2026-10-02T15:00:00Z", duration="30000", recording="javascript:alert(1)"),
        call("3", "101", "2026-10-02T16:00:00Z", direction="INBOUND", disposition=CONNECTED),
    ],
    [
        call("4", "101", "2026-10-05T13:00:00Z", duration=None, disposition=None),
        call("2", "101", "2026-10-02T15:00:00Z", duration="30000"),  # duplicate across pages
        call("5", "102", "2026-10-05T14:00:00Z", disposition=CONNECTED),
    ],
]


@pytest.fixture(scope="module")
def test_db():
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
        cur.execute("INSERT INTO sales_reps (id, name) VALUES (1, 'Rep One'), (2, 'Rep Two'), (3, 'Someone Else')")
        cur.execute("INSERT INTO users (id, email, role, sales_rep_id) VALUES (1, 'rep.one@planet.net', 'Sales Rep', 1)")
    conn.close()
    yield
    db.APPDB_DATABASE = original
    db_migrations._schema_ready = False
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    admin.close()


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv("HUBSPOT_SERVICE_KEY", "test-key-not-real")


def q(sql, params=()):
    conn = db.get_appdb_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def test_initial_sync(test_db):
    client = FakeClient(OWNERS, INITIAL_PAGES)
    ran, summary = hubspot_store.run_sync("manual", client=client)
    # Only calls on/after HUBSPOT_SYNC_START_DATE are requested.
    assert client.extra_filters == [{"propertyName": "hs_timestamp", "operator": "GTE",
                                     "value": str(int(hubspot_store.SYNC_START.timestamp() * 1000))}]
    assert ran and summary["error"] is None
    assert summary["kind"] == "initial"
    assert summary["owners_retrieved"] == 3
    assert summary["new_calls"] == 5  # 6 records, one duplicate id
    assert summary["updated_calls"] == 0
    assert q("SELECT count(*) FROM hubspot_calls")[0][0] == 5

    mappings = {r[0]: r[1:] for r in q(
        "SELECT hubspot_owner_id, sales_rep_id, dashboard_user_id, mapping_method, mapping_status FROM hubspot_user_mappings")}
    assert mappings["101"] == (1, 1, "email_normalized", "matched")
    assert mappings["102"] == (2, None, "name", "matched")
    assert mappings["103"] == (None, None, "none", "unmatched")

    state = q("SELECT initial_sync_completed_at, last_success_at, connection_ok, last_error FROM hubspot_sync_state")[0]
    assert state[0] and state[1] and state[2] is True and state[3] is None
    assert q("SELECT status, new_calls FROM hubspot_sync_runs ORDER BY id DESC LIMIT 1")[0] == ("success", 5)


def test_summary_counts_only_outbound_and_uses_eastern_days(test_db):
    s = hubspot_store.get_call_summary("Rep One", date(2026, 10, 1), date(2026, 10, 5), "daily")
    assert s["total_calls"] == 3          # calls 1, 2, 4 (3 is inbound)
    assert s["connected_calls"] == 1      # call 1
    assert s["connection_rate"] == pytest.approx(100 / 3)
    assert s["total_talk_ms"] == 150000   # 120s + 30s; call 4 has no duration
    assert s["calls_with_duration"] == 2
    assert s["avg_duration_ms"] == 75000
    assert [b["count"] for b in s["series"]] == [1, 1, 0, 0, 1]  # call 1 lands on Oct 1 Eastern
    assert s["last_call_at"] == datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)
    # Three worked days, one call each -> each counts the 15-min floor.
    assert s["working_days"] == 3 and s["working_seconds"] == 3 * 15 * 60
    assert s["calls_per_hour"] == pytest.approx(4.0)


def test_sql_buckets_match_python_for_every_scale(test_db):
    for scale in hubspot_logic.TIME_SCALES:
        series = hubspot_store.get_call_volume("Rep One", date(2026, 10, 1), date(2026, 10, 5), scale)
        assert sum(b["count"] for b in series) == 3, scale


def test_history_paging_filter_and_order(test_db):
    h = hubspot_store.get_call_history("Rep One", date(2026, 10, 1), date(2026, 10, 5))
    assert h["total"] == 3
    assert [c["id"] for c in h["calls"]] == ["4", "2", "1"]
    assert h["calls"][2]["outcome"] == "Connected" and h["calls"][2]["connected"] is True
    assert h["calls"][2]["date"] == "Oct 1, 2026" and h["calls"][2]["time"] == "10:00 PM"
    assert h["calls"][0]["outcome"] is None
    by_id = {c["id"]: c for c in h["calls"]}
    assert by_id["1"]["recording_url"] == "https://api-na1.hubspot.com/recording/auth/provider/abc"
    assert by_id["1"]["contact_name"] == "Pat Lee"
    assert by_id["1"]["contact_url"] == "https://app.hubspot.com/contacts/12345/record/0-1/501"
    assert by_id["2"]["recording_url"] is None          # non-https URL never rendered as a link
    assert by_id["2"]["contact_name"] == "Contact 502"  # deleted contact: still linked, no name
    assert by_id["4"]["contact_url"] is None
    oldest_first = hubspot_store.get_call_history("Rep One", date(2026, 10, 1), date(2026, 10, 5), newest_first=False)
    assert [c["id"] for c in oldest_first["calls"]] == ["1", "2", "4"]
    only_connected = hubspot_store.get_call_history("Rep One", date(2026, 10, 1), date(2026, 10, 5), outcome=CONNECTED)
    assert [c["id"] for c in only_connected["calls"]] == ["1"]
    no_outcome = hubspot_store.get_call_history("Rep One", date(2026, 10, 1), date(2026, 10, 5), outcome=hubspot_store.NO_OUTCOME)
    assert [c["id"] for c in no_outcome["calls"]] == ["4"]


def test_incremental_sync_updates_changed_and_adds_new(test_db):
    pages = [[
        call("2", "101", "2026-10-02T15:00:00Z", duration="30000"),  # unchanged
        call("4", "101", "2026-10-05T13:00:00Z", duration="45000", disposition=CONNECTED,
             modified="2026-10-06T00:00:00Z"),  # changed in HubSpot
        call("6", "101", "2026-10-06T13:00:00Z", modified="2026-10-06T13:05:00Z"),  # new
    ]]
    client = FakeClient(OWNERS, [], incremental_pages=pages)
    watermark_before = q("SELECT calls_modified_watermark FROM hubspot_sync_state")[0][0]
    ran, summary = hubspot_store.run_sync("auto", client=client)
    assert ran and summary["error"] is None
    assert summary["kind"] == "incremental"
    assert client.incremental_since is not None
    assert (summary["new_calls"], summary["updated_calls"], summary["records_processed"]) == (1, 1, 3)
    assert q("SELECT duration_ms, disposition FROM hubspot_calls WHERE hubspot_call_id = '4'")[0] == (45000, CONNECTED)
    # Re-reads from (watermark - overlap) and never moves the watermark
    # backwards for older modifications.
    assert client.incremental_since == int((watermark_before - hubspot_store.WATERMARK_OVERLAP).timestamp() * 1000)
    watermark = q("SELECT calls_modified_watermark FROM hubspot_sync_state")[0][0]
    assert watermark == max(watermark_before, datetime(2026, 10, 6, 13, 5, tzinfo=timezone.utc))


def test_manual_mapping_survives_rematch_and_sync(test_db):
    ok, err = hubspot_store.set_manual_mapping("103", 3, "assign")
    assert ok, err
    ok, err = hubspot_store.set_manual_mapping("102", None, "unassign")
    assert ok, err
    hubspot_store.rematch_existing_owners()
    hubspot_store.run_sync("auto", client=FakeClient(OWNERS, [], incremental_pages=[]))
    rows = {r[0]: r[1:] for r in q("SELECT hubspot_owner_id, sales_rep_id, mapping_method, mapping_status FROM hubspot_user_mappings")}
    assert rows["103"] == (3, "manual", "matched")
    assert rows["102"] == (None, "manual", "unmatched")
    # Back to automatic -> name match restores Rep Two.
    ok, err = hubspot_store.set_manual_mapping("102", None, "auto")
    assert ok, err
    assert q("SELECT sales_rep_id, mapping_method FROM hubspot_user_mappings WHERE hubspot_owner_id = '102'")[0] == (2, "name")


def test_calls_before_sync_start_are_removed(test_db):
    conn = db.get_appdb_connection()
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO hubspot_calls (hubspot_call_id, hubspot_owner_id, call_timestamp, direction) "
                    "VALUES ('old-1', '101', '2025-12-31 23:30:00-05', 'OUTBOUND')")
    conn.close()
    before = q("SELECT count(*) FROM hubspot_calls")[0][0]
    hubspot_store.run_sync("auto", client=FakeClient(OWNERS, [], incremental_pages=[]))
    assert q("SELECT count(*) FROM hubspot_calls WHERE hubspot_call_id = 'old-1'")[0][0] == 0
    assert q("SELECT count(*) FROM hubspot_calls")[0][0] == before - 1


def test_manual_mapping_rejects_unknown_rep(test_db):
    ok, err = hubspot_store.set_manual_mapping("101", 99999, "assign")
    assert not ok and "doesn't exist" in err


def test_only_one_sync_at_a_time(test_db):
    holder = db.get_appdb_connection()
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (hubspot_store.SYNC_LOCK_KEY,))
        ran, reason = hubspot_store.run_sync("manual", client=FakeClient(OWNERS, []))
        assert ran is False and "already running" in reason
    finally:
        holder.close()


def test_failed_sync_keeps_data_and_records_error(test_db):
    before = q("SELECT count(*) FROM hubspot_calls")[0][0]
    error = hubspot_client.HubSpotError("HubSpot GET /crm/v3/owners failed (403): permission denied",
                                        endpoint="GET /crm/v3/owners", status=403, required_scopes=["crm.objects.owners.read"])
    ran, summary = hubspot_store.run_sync("manual", client=FakeClient(OWNERS, [], fail=error))
    assert ran and "403" in summary["error"]
    assert q("SELECT count(*) FROM hubspot_calls")[0][0] == before
    state = q("SELECT connection_ok, last_error, last_success_at FROM hubspot_sync_state")[0]
    assert state[0] is False and "403" in state[1] and state[2] is not None
    assert q("SELECT status FROM hubspot_sync_runs ORDER BY id DESC LIMIT 1")[0][0] == "failed"
    # Analytics still served from the last good data.
    assert hubspot_store.get_call_summary("Rep One", date(2026, 10, 1), date(2026, 10, 6), "daily")["total_calls"] == 4


def test_admin_saved_key_is_encrypted_and_overrides_env(test_db, monkeypatch):
    import app_secrets
    monkeypatch.setenv("HUBSPOT_SERVICE_KEY", "env-key-1234567890")
    app_secrets.clear_cache()
    assert hubspot_client.get_service_key() == "env-key-1234567890"
    assert hubspot_client.key_source() == "env"

    ok, err = app_secrets.set_secret(hubspot_client.SECRET_NAME, "pat-admin-key-abcdef", None)
    assert ok, err
    stored = q("SELECT encrypted_value FROM app_secrets WHERE name = %s", (hubspot_client.SECRET_NAME,))[0][0]
    assert "pat-admin-key-abcdef" not in stored
    assert hubspot_client.get_service_key() == "pat-admin-key-abcdef"
    assert hubspot_client.key_source() == "admin"
    assert hubspot_client.sanitize("Bearer pat-admin-key-abcdef env-key-1234567890") == "Bearer *** ***"

    # A different SECRET_KEY can't decrypt it -> falls back, flags it.
    monkeypatch.setenv("SECRET_KEY", "a-completely-different-secret")
    app_secrets.clear_cache()
    assert app_secrets.get_secret(hubspot_client.SECRET_NAME)["undecryptable"] is True
    assert hubspot_client.get_service_key() == "env-key-1234567890"

    ok, err = app_secrets.delete_secret(hubspot_client.SECRET_NAME)
    assert ok, err
    assert hubspot_client.key_source() == "env"


class CountingClient(FakeClient):
    owner_calls = 0

    def list_all_owners(self):
        CountingClient.owner_calls += 1
        return super().list_all_owners()


def test_frequent_auto_sync_skips_owner_refresh_until_stale(test_db):
    CountingClient.owner_calls = 0
    hubspot_store.run_sync("manual", client=CountingClient(OWNERS, []))  # manual always refreshes
    hubspot_store.run_sync("auto", client=CountingClient(OWNERS, []))    # fresh -> skipped
    assert CountingClient.owner_calls == 1
    conn = db.get_appdb_connection()
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE hubspot_user_mappings SET last_synced_at = now() - interval '1 hour'")
    conn.close()
    hubspot_store.run_sync("auto", client=CountingClient(OWNERS, []))    # stale -> refreshed
    assert CountingClient.owner_calls == 2


def test_old_run_history_is_pruned(test_db):
    conn = db.get_appdb_connection()
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO hubspot_sync_runs (trigger, kind, status, started_at) "
                    "VALUES ('auto', 'incremental', 'success', now() - interval '10 days')")
    conn.close()
    hubspot_store.run_sync("auto", client=FakeClient(OWNERS, []))
    assert q("SELECT count(*) FROM hubspot_sync_runs WHERE started_at < now() - interval '3 days'")[0][0] == 0


def test_working_day_spans_first_to_last_call(test_db):
    # Owner 102 (Rep Two): 9:00 and 11:00 calls (10 min each) on Oct 7,
    # plus one 9:00 call on Oct 8. Day 1 span = 9:00 -> 11:10 = 130 min;
    # day 2 = 15-min floor. Time after each day's last call never counts.
    page = [
        call("w1", "102", "2026-10-07T13:00:00Z", duration="600000", modified="2026-10-20T00:00:00Z"),
        call("w2", "102", "2026-10-07T15:00:00Z", duration="600000", modified="2026-10-20T00:00:00Z"),
        call("w3", "102", "2026-10-08T13:00:00Z", duration=None, modified="2026-10-20T00:00:00Z"),
    ]
    conn = db.get_appdb_connection()
    hubspot_store.upsert_calls(conn, page)
    conn.close()
    s = hubspot_store.get_call_summary("Rep Two", date(2026, 10, 7), date(2026, 10, 8), "daily")
    assert s["working_days"] == 2
    assert s["working_seconds"] == (130 + 15) * 60
    assert s["calls_per_hour"] == pytest.approx(3 / ((130 + 15) / 60))
