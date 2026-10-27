"""Passings & Leads Check Out + Rep Profile Checked Out buckets
(passings_activity_store.py, 2026-10-27).

Needs a REAL Postgres; runs only when CHECKOUT_INTEGRATION_TEST_DB is set
(that database is created, migrated and dropped -- the app's own appdb is
never touched):

    docker compose exec -e CHECKOUT_INTEGRATION_TEST_DB=checkout_it dashboard \\
        python -m pytest tests/test_passing_checkouts.py -v
"""

import os

import pytest

import passings_activity_store as store

TEST_DB = os.environ.get("CHECKOUT_INTEGRATION_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="CHECKOUT_INTEGRATION_TEST_DB not set")

USER = {"id": 1, "email": "a@planet.net", "sales_rep_name": "Alex Rivera"}


@pytest.fixture(scope="module", autouse=True)
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
        cur.execute("INSERT INTO users (id, email, role, sales_rep_id) VALUES (1, 'a@planet.net', 'Sales Rep', 1)")
    conn.close()
    yield
    db.APPDB_DATABASE = original
    db_migrations._schema_ready = False
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    admin.close()


def test_checkout_buckets_and_return():
    assert store.check_out([], 1, USER)[0] is False
    assert store.check_out([10], 999, USER)[1] == "Choose a sales rep."
    assert store.check_out([10, "11", 11, None], 1, USER) == (True, None, 2)

    ok, checkouts = store.get_checkouts("Alex Rivera")
    assert ok and set(checkouts) == {10, 11}
    assert checkouts[10]["checked_out_by_name"] == "Alex Rivera" and checkouts[10]["bucket_id"] is None

    # Buckets belong to one rep's profile.
    ok, _, hot = store.create_bucket("Alex Rivera", "  Call   back ")
    assert ok and hot["name"] == "Call back"
    assert store.create_bucket("Alex Rivera", "call BACK")[1] == "That bucket already exists."
    assert store.create_bucket("Blake Chen", "Call back")[0]  # same name, other rep: fine
    assert [b["name"] for b in store.get_buckets("Blake Chen")[1]] == ["Call back"]

    assert store.set_checkout_bucket("Alex Rivera", 10, hot["id"]) == (True, None)
    blake_bucket = store.get_buckets("Blake Chen")[1][0]["id"]
    assert store.set_checkout_bucket("Alex Rivera", 11, blake_bucket)[1] == "Invalid bucket."
    assert store.set_checkout_bucket("Blake Chen", 10, blake_bucket)[0] is False  # not Blake's card
    assert store.get_checkouts("Alex Rivera")[1][10]["bucket_id"] == hot["id"]

    # Re-checking out to the same rep keeps the bucket; moving to another rep clears it.
    store.check_out([10], 1, USER)
    assert store.get_checkouts("Alex Rivera")[1][10]["bucket_id"] == hot["id"]
    store.check_out([10], 2, USER)
    assert set(store.get_checkouts("Alex Rivera")[1]) == {11}
    assert store.get_checkouts("Blake Chen")[1][10]["bucket_id"] is None

    # Deleting a bucket sends its cards back to Unsorted; another rep can't delete it.
    store.check_out([12], 1, USER)
    store.set_checkout_bucket("Alex Rivera", 12, hot["id"])
    store.delete_bucket("Blake Chen", hot["id"])
    assert store.get_checkouts("Alex Rivera")[1][12]["bucket_id"] == hot["id"]
    store.delete_bucket("Alex Rivera", hot["id"])
    assert store.get_checkouts("Alex Rivera")[1][12]["bucket_id"] is None

    assert store.return_checkout(12) == (True, None)
    assert set(store.get_checkouts()[1]) == {10, 11}
