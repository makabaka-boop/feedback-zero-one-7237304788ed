#!/usr/bin/env python3
"""Administration entry points.

  python -m scripts.admin init-schema
  python -m scripts.admin replay
  python -m scripts.admin reset          # wipe everything (dev/test only)
"""
import sys

from app.db import connect, init_schema
from app.engine import Engine
import os

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "app", "schema.sql")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "init-schema"
    conn = connect()
    try:
        if cmd == "init-schema":
            init_schema(conn, os.path.abspath(SCHEMA))
            print("schema ready")
        elif cmd == "replay":
            engine = Engine(conn)
            results = engine.replay()
            version = engine.read_version()
            print("replayed %d accepted batches; version=%d"
                  % (len(results), version))
        elif cmd == "reset":
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE event_parents, events, nodes, "
                    "snapshot_entities, change_batches, meta, "
                    "accepted_batches RESTART IDENTITY")
                cur.execute("INSERT INTO meta (id, version) VALUES (1, 0)")
            conn.commit()
            print("state reset to empty (version 0)")
        else:
            print("unknown command: %s" % cmd, file=sys.stderr)
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
