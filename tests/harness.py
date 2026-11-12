"""Shared pytest-style helpers with no third-party dependency."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import connect  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.validation import RejectBatch, normalise_batch  # noqa: E402


def fresh_engine():
    conn = connect()
    conn.autocommit = False
    engine = Engine(conn)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE event_parents, events, nodes, snapshot_entities, "
            "change_batches, meta, accepted_batches RESTART IDENTITY")
        cur.execute("INSERT INTO meta (id, version) VALUES (1, 0)")
    conn.commit()
    return engine, conn


def submit(engine, raw_events):
    """Validate then submit; returns (ok, result_or_reason)."""
    try:
        canonical = normalise_batch(raw_events)
    except RejectBatch as exc:
        return False, exc.reason
    try:
        return True, engine.submit(canonical)
    except RejectBatch as exc:
        engine.conn.rollback()
        return False, exc.reason


PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print("FAIL %s %s" % (name, detail))


def expect_reject(engine, name, raw_events):
    ok, res = submit(engine, raw_events)
    check(name, not ok, "expected rejection got %r" % (res,))
    return res
