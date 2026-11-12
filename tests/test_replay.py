"""Replay test: wipe derived state, replay accepted batches from empty and
verify the snapshot/version/change stream is exactly restored."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from harness import fresh_engine, submit, check, PASS, FAIL  # noqa: E402


def snapshot_dump(engine):
    v, entities = engine.read_snapshot()
    cur = engine.conn.cursor()
    cur.execute("SELECT version, changes FROM change_batches ORDER BY version")
    changes = cur.fetchall()
    return v, entities, changes


def main():
    engine, conn = fresh_engine()

    scenario = [
        [{"id": "a1", "entity": "A", "parents": [], "op": "patch",
          "set": {"a": "x"}, "unset": []}],
        [{"id": "b1", "entity": "B", "parents": ["b2"], "op": "patch",
          "set": {}, "unset": ["a"]}],
        [{"id": "b2", "entity": "B", "parents": [], "op": "patch",
          "set": {"b": "y"}, "unset": []}],   # late parent activates B
        [{"id": "a1", "entity": "A", "parents": [], "op": "patch",
          "set": {"a": "x"}, "unset": []}],   # idempotent retransmit
        [{"id": "a2", "entity": "A", "parents": ["a1"], "op": "erase"}],
        [{"id": "c1", "entity": "C", "parents": [], "op": "patch",
          "set": {"a": None, "b": "z"}, "unset": []}],
        [{"id": "c2", "entity": "C", "parents": ["c1"], "op": "patch",
          "set": {}, "unset": []}],           # empty change batch
    ]
    for i, batch in enumerate(scenario):
        ok, res = submit(engine, batch)
        check("scenario batch %d accepted" % i, ok, str(res))

    before = snapshot_dump(engine)
    check("version 6 after replay scenario (one retransmit)",
          before[0] == 6, str(before[0]))

    # Replay through the same public entry point used after a restart.
    results = engine.replay()
    after = snapshot_dump(engine)
    check("replay returns one result per accepted batch",
          len(results) == 6, str(len(results)))
    check("version restored", before[0] == after[0])
    check("snapshot entities restored", before[1] == after[1])
    check("change stream restored", before[2] == after[2])

    # accepted_batches count unchanged by replay (it is the replay source).
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM accepted_batches")
    check("accepted batches retained", cur.fetchone()[0] == 6)
    cur.execute("SELECT count(*) FROM change_batches")
    check("change batches one per version", cur.fetchone()[0] == 6)

    # Restart simulation: brand new connection, replay again, identical.
    conn.close()
    from app.db import connect
    from app.engine import Engine
    conn2 = connect()
    engine2 = Engine(conn2)
    engine2.replay()
    restarted = snapshot_dump(engine2)
    check("fresh-connection replay identical", restarted == after)
    conn2.close()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
