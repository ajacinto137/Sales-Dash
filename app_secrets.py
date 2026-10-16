"""Secrets an Admin enters in the Admin Portal rather than .env -- today
just the HubSpot service key (Admin > HubSpot Integration > Service Key).

Stored in appdb's app_secrets table (db_migrations.py migration 6),
encrypted with Fernet (AES-128-CBC + HMAC-SHA256) under a key derived
from the app's SECRET_KEY, so a database dump alone doesn't reveal it.
Plaintext only ever exists in this process's memory: it is never
returned to the browser, rendered, or logged.

If SECRET_KEY changes, previously saved values can no longer be
decrypted; get_secret() then reports `undecryptable` so the Admin page
can ask for the key to be re-entered.

Reads are cached for CACHE_SECONDS (the HubSpot client asks for the key
on every request and every sanitize() call); writes clear the cache."""

import base64
import hashlib
import os
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

import db
import db_migrations

CACHE_SECONDS = 60

_cache = {}
_cache_lock = threading.Lock()


def _fernet():
    secret_key = os.environ.get("SECRET_KEY", "")
    if not secret_key:
        raise RuntimeError("SECRET_KEY is not set; can't encrypt stored secrets.")
    digest = hashlib.sha256(b"sales-dash-app-secrets:" + secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def get_secret(name):
    """{'value': str|None, 'updated_at', 'updated_by_email', 'undecryptable': bool}.
    value None when nothing is saved, appdb is unreachable, or it can't
    be decrypted. Never raises."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(name)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

    result = {"value": None, "updated_at": None, "updated_by_email": None, "undecryptable": False}
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.encrypted_value, s.updated_at, u.email
                FROM app_secrets s LEFT JOIN users u ON u.id = s.updated_by
                WHERE s.name = %s
                """,
                (name,),
            )
            row = cur.fetchone()
        if row:
            result["updated_at"], result["updated_by_email"] = row[1], row[2]
            try:
                result["value"] = _fernet().decrypt(row[0].encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError):
                result["undecryptable"] = True
    except Exception:
        # appdb down -- don't cache, so the next call retries.
        return result
    finally:
        if conn is not None:
            conn.close()

    with _cache_lock:
        _cache[name] = (now, result)
    return result


def set_secret(name, value, user_id):
    """Returns (ok, error)."""
    conn = None
    try:
        token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_secrets (name, encrypted_value, updated_by) VALUES (%s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value,
                        updated_at = now(), updated_by = EXCLUDED.updated_by
                    """,
                    (name, token, user_id),
                )
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
        clear_cache(name)


def delete_secret(name):
    conn = None
    try:
        conn = db.get_appdb_connection()
        db_migrations.ensure_schema(conn)
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_secrets WHERE name = %s", (name,))
        return True, None
    except Exception as exc:
        return False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
        clear_cache(name)


def clear_cache(name=None):
    with _cache_lock:
        if name is None:
            _cache.clear()
        else:
            _cache.pop(name, None)
