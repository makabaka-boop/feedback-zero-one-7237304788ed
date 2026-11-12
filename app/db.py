"""Database connection helpers and schema bootstrap."""
import os

import psycopg2
from psycopg2.extras import Json  # noqa: F401  (re-exported for callers)

DEFAULT_DSN = "postgresql://app@/appdb?host=/tmp&port=55432"


def connect(dsn=None):
    dsn = dsn or os.environ.get("APP_DSN", DEFAULT_DSN)
    # Isolation level is managed per-transaction in the engine.
    return psycopg2.connect(dsn)


def init_schema(conn, schema_path):
    with open(schema_path, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def set_serializable(conn):
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
