"""Edge cases not covered elsewhere: empty batches, batch atomicity,
null/string distinctions and HTTP envelope shapes."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from harness import fresh_engine, submit, check, PASS, FAIL  # noqa: E402
from app.validation import normalise_batch, RejectBatch  # noqa: E402


def main():
    engine, conn = fresh_engine()

    # empty array is a valid no-op submission
    ok, r = submit(engine, [])
    check("empty array accepted as no-op", ok and r["retransmitted"] is True)
    check("empty array does not bump version", engine.read_version() == 0)

    # {"events": []} wrapper shape
    ok, r = submit(engine, normalise_batch({"events": []}))
    check("wrapper empty accepted", ok and r["retransmitted"] is True)

    # malformed wrappers rejected at validation
    for bad in [{}, {"events": "x"}, 42, "str", None, {"events": [], "x": 1}]:
        try:
            normalise_batch(bad)
            check("reject shape %r" % bad, False)
        except RejectBatch:
            check("reject shape %r" % bad, True)

    # batch atomicity: one bad event rejects the whole batch, nothing lands
    good = {"id": "k1", "entity": "K", "parents": [], "op": "patch",
            "set": {"a": "ok"}, "unset": []}
    bad = {"id": "k2", "entity": "K", "parents": [], "op": "patch",
           "set": {"a": 7}, "unset": []}  # integer value illegal
    ok, reason = submit(engine, [good, bad])
    check("mixed batch rejected", not ok and reason == "bad_set_value")
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM events")
    check("no events persisted from rejected batch", cur.fetchone()[0] == 0)
    cur.execute("SELECT count(*) FROM nodes")
    check("no nodes persisted from rejected batch", cur.fetchone()[0] == 0)
    check("version unchanged after rejects", engine.read_version() == 0)

    # null vs empty string are distinct values, both legal
    ok, _ = submit(engine, [
        {"id": "n1", "entity": "N", "parents": [], "op": "patch",
         "set": {"a": None, "b": ""}, "unset": []}])
    body = engine.read_snapshot("N")[1]
    check("null value retained", body["a"] == [
        {"id": "n1", "present": True, "value": None}])
    check("empty string retained", body["b"] == [
        {"id": "n1", "present": True, "value": ""}])

    # idempotency is structural: semantically equal but differently ordered
    # parents/unset normalise to the same canonical structure
    canonical = normalise_batch([
        {"id": "i1", "entity": "I", "parents": ["x", "y"], "op": "patch",
         "set": {}, "unset": ["b", "a"]}])
    canonical2 = normalise_batch([
        {"id": "i1", "entity": "I", "parents": ["y", "x"], "op": "patch",
         "unset": ["a", "b"], "set": {}}])
    check("canonical normalisation makes structures equal",
          canonical == canonical2)

    # erase followed by a later patch: existence maxima flips back to true
    submit(engine, [{"id": "r1", "entity": "R", "parents": [],
                     "op": "erase"}])
    check("erase-only entity published with present=false",
          engine.read_snapshot("R")[1]["existence"] == [
              {"id": "r1", "present": False}])
    submit(engine, [{"id": "r2", "entity": "R", "parents": ["r1"],
                     "op": "patch", "set": {"a": "back"}, "unset": []}])
    body = engine.read_snapshot("R")[1]
    check("later patch wins existence back",
          body["existence"] == [{"id": "r2", "present": True}])
    check("later patch field present",
          body["a"] == [{"id": "r2", "present": True, "value": "back"}])

    conn.close()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
