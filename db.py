import os

import psycopg2
import pyodbc
from dotenv import load_dotenv

load_dotenv()

PLANETWEB_HOST = os.environ.get("PLANETWEB_HOST", "")
PLANETWEB_DATABASE = os.environ.get("PLANETWEB_DATABASE", "")
PLANETWEB_USERNAME = os.environ.get("PLANETWEB_USERNAME", "")
PLANETWEB_PASSWORD = os.environ.get("PLANETWEB_PASSWORD", "")

KPI_HOST = os.environ.get("KPI_HOST", "")
KPI_PORT = int(os.environ.get("KPI_PORT", "5432"))
KPI_DATABASE = os.environ.get("KPI_DATABASE", "")
KPI_USERNAME = os.environ.get("KPI_USERNAME", "")
KPI_PASSWORD = os.environ.get("KPI_PASSWORD", "")

CONNECT_TIMEOUT_SECONDS = 10


def get_planetweb_connection():
    return pyodbc.connect(
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={PLANETWEB_HOST};"
        f"DATABASE={PLANETWEB_DATABASE};"
        f"UID={PLANETWEB_USERNAME};"
        f"PWD={PLANETWEB_PASSWORD};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;",
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


def get_kpi_connection():
    return psycopg2.connect(
        host=KPI_HOST,
        port=KPI_PORT,
        dbname=KPI_DATABASE,
        user=KPI_USERNAME,
        password=KPI_PASSWORD,
        sslmode="require",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def sanitize_error(message):
    """Strips known secrets out of a raw driver error message before it is
    shown in the UI or logs."""
    text = str(message)
    for secret in (PLANETWEB_PASSWORD, KPI_PASSWORD):
        if secret:
            text = text.replace(secret, "***")
    return text


def test_planetweb_connection():
    conn = None
    try:
        conn = get_planetweb_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        return True, None
    except Exception as exc:
        return False, sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def test_kpi_connection():
    conn = None
    try:
        conn = get_kpi_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        return True, None
    except Exception as exc:
        return False, sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()
