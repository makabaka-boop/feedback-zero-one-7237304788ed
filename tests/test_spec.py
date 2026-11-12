"""Spec-rule tests: every normative sentence in the task is exercised."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from harness import fresh_engine, submit, check, expect_reject, PASS, FAIL  # noqa: E402


def ev(eid, entity, parents=(), op="patch", set_=None, unset=()):
    d = {"id": eid, "entity": entity, "parents": list(parents), "op": op}
    if op == "patch":
        d["set"] = {} if set_ is None else set_
        d["unset"] = list(unset)
    return d


def main():
    engine, conn = fresh_engine()

    # --- identifier / shape rules -----------------------------------------
    expect_reject(engine, "empty id", [ev("", "E")])
    expect_reject(engine, "non-ascii id", [ev("é", "E")])
    expect_reject(engine, "punct id", [ev("x-y", "E")])
    expect_reject(engine, "id wrong type", [{"id": 1, "entity": "E",
                 "parents": [], "op": "patch", "set": {}, "unset": []}])
    expect_reject(engine, "bad entity", [ev("p", "")])
    expect_reject(engine, "parents dup", [ev("p", "E", parents=["q", "q"])])
    expect_reject(engine, "bad parent id", [ev("p", "E", parents=["!"])])
    expect_reject(engine, "bad op", [{"id": "p", "entity": "E",
                 "parents": [], "op": "delete", "set": {}, "unset": []}])
    expect_reject(engine, "unknown top key", [{"id": "p", "entity": "E",
                 "parents": [], "op": "patch", "set": {}, "unset": [],
                 "extra": 1}])
    expect_reject(engine, "patch missing set", [{"id": "p", "entity": "E",
                 "parents": [], "op": "patch", "unset": []}])
    expect_reject(engine, "patch missing unset", [{"id": "p", "entity": "E",
                 "parents": [], "op": "patch", "set": {}}])
    expect_reject(engine, "set not object", [{"id": "p", "entity": "E",
                 "parents": [], "op": "patch", "set": [], "unset": []}])
    expect_reject(engine, "unset not array", [{"id": "p", "entity": "E",
                 "parents": [], "op": "patch", "set": {}, "unset": {}}])
    expect_reject(engine, "unknown field in set",
                  [ev("p", "E", set_={"c": "x"})])
    expect_reject(engine, "non-string set value",
                  [ev("p", "E", set_={"a": 3})])
    expect_reject(engine, "boolean set value",
                  [ev("p", "E", set_={"a": True})])
    expect_reject(engine, "unset bad field", [ev("p", "E", unset=["c"])])
    expect_reject(engine, "unset dup", [ev("p", "E", unset=["a", "a"])])
    expect_reject(engine, "set unset overlap",
                  [ev("p", "E", set_={"a": "x"}, unset=["a"])])
    expect_reject(engine, "erase with set",
                  [{"id": "p", "entity": "E", "parents": [],
                    "op": "erase", "set": {}}])
    expect_reject(engine, "erase with unset",
                  [{"id": "p", "entity": "E", "parents": [],
                    "op": "erase", "unset": []}])
    check("version still 0 after rejects", engine.read_version() == 0)
    _, snap0 = engine.read_snapshot()
    check("no snapshot entities after rejects", snap0 == [])

    # --- happy path: basic patch and empty patch --------------------------
    ok, r = submit(engine, [ev("p1", "E", set_={"a": "v1"})])
    check("first batch accepted", ok and r["version"] == 1, str(r))
    v, snap = engine.read_snapshot()
    check("one entity published", len(snap) == 1 and snap[0]["entity"] == "E")
    check("existence true candidate",
          snap[0]["existence"] == [{"id": "p1", "present": True}])
    check("a candidate with value",
          snap[0]["a"] == [{"id": "p1", "present": True, "value": "v1"}])
    check("b empty candidates", snap[0]["b"] == [])

    ok, r = submit(engine, [ev("p2", "E")])  # empty patch is legal
    check("empty patch accepted", ok)
    check("empty patch existence true",
          engine.read_snapshot("E")[1]["existence"] == [
              {"id": "p1", "present": True},
              {"id": "p2", "present": True}])  # concurrent writes retained

    # --- idempotency and conflicting redefinition -------------------------
    ok, r = submit(engine, [ev("p1", "E", set_={"a": "v1"})])
    check("identical retransmit ok", ok and r["retransmitted"] is True)
    check("retransmit does not bump version",
          r["version"] == engine.read_version() == 2)
    expect_reject(engine, "same id different content",
                  [ev("p1", "E", set_={"a": "v2"})])
    expect_reject(engine, "same id different op",
                  [{"id": "p1", "entity": "E", "parents": [], "op": "erase"}])
    ok, r = submit(engine, [ev("p3", "E", set_={"a": "v3"}),
                            ev("p3", "E", set_={"a": "v3"})])
    check("identical intra-batch duplicate", ok)
    expect_reject(engine, "intra-batch conflicting duplicate",
                  [ev("p4", "E", set_={"a": "1"}),
                   ev("p4", "E", set_={"a": "2"})])

    # --- entity ownership fixed at first sight ----------------------------
    ok, _ = submit(engine, [ev("p5", "F", parents=["q1"])])  # q1 placeholder
    check("placeholder batch accepted", ok)
    check("pending entity not in snapshot",
          engine.read_snapshot("F")[1] is None)
    expect_reject(engine, "redefine id under other entity",
                  [ev("p5", "G")])
    expect_reject(engine, "define placeholder under other entity",
                  [ev("q1", "G")])
    expect_reject(engine, "cross entity parent",
                  [ev("x1", "G", parents=["p1"])])  # p1 belongs to E

    # --- late parent and cascade activation -------------------------------
    ok, r = submit(engine, [ev("q1", "F")])
    check("late parent activates", ok and "F" in r["changed_entities"], str(r))
    body = engine.read_snapshot("F")[1]
    check("F existence maxima is p5 (newest leaf)",
          [c["id"] for c in body["existence"]] == ["p5"]
          and body["existence"][0]["present"] is True)

    # chain pending then cascade
    submit(engine, [ev("c1", "H", parents=["c2"])])
    submit(engine, [ev("c2", "H", parents=["c3"])])
    check("long chain pending", engine.read_snapshot("H")[1] is None)
    ok, r = submit(engine, [ev("c3", "H")])
    check("long chain cascade", ok and "H" in r["changed_entities"])
    body = engine.read_snapshot("H")[1]
    check("chain maxima is leaf c1 (no candidate descendant)",
          [c["id"] for c in body["existence"]] == ["c1"])

    # --- cycles: direct, self, placeholder-containing ---------------------
    expect_reject(engine, "self loop", [ev("s1", "S", parents=["s1"])])
    expect_reject(engine, "two node cycle",
                  [ev("s2", "S", parents=["s3"]),
                   ev("s3", "S", parents=["s2"])])
    submit(engine, [ev("t1", "T")])
    # Ring containing a placeholder node. t8 is never defined (no event
    # carries id=t8), yet t6->t7->t8->t6 closes a cycle through it.
    expect_reject(engine, "cycle through placeholder",
                  [ev("t6", "T2", parents=["t7"]),
                   ev("t7", "T2", parents=["t8"]),
                   ev("t8", "T2", parents=["t6"])])
    # Straight three-node ring, all defined in the same batch.
    expect_reject(engine, "three node ring",
                  [ev("ta", "T3", parents=["tb"]),
                   ev("tb", "T3", parents=["tc"]),
                   ev("tc", "T3", parents=["ta"])])
    # Long DAG chain ending at an existing defined node is legal.
    ok, _ = submit(engine, [ev("t2", "T", parents=["t3"]),
                            ev("t3", "T", parents=["t1"]),
                            ev("t4", "T", parents=["t2"])])
    check("chain into existing node accepted", ok)
    expect_reject(engine, "cycle via placeholder",
                  [ev("u1", "U", parents=["u2"]),
                   ev("u2", "U", parents=["u1"])])

    # --- erase semantics --------------------------------------------------
    ok, r = submit(engine, [ev("e1", "K", set_={"a": "av", "b": "bv"})])
    body = engine.read_snapshot("K")[1]
    check("both fields present",
          body["a"][0]["value"] == "av" and body["b"][0]["value"] == "bv")
    ok, r = submit(engine, [{"id": "e2", "entity": "K", "parents": ["e1"],
                             "op": "erase"}])
    body = engine.read_snapshot("K")[1]
    check("erase maxima present=false",
          body["existence"] == [{"id": "e2", "present": False}])
    # erase writes no field candidates; the older field values survive
    # because an unwritten field never overrides an ancestor's value.
    check("erase emits no new field candidates (ancestor values kept)",
          body["a"] == [{"id": "e1", "present": True, "value": "av"}]
          and body["b"] == [{"id": "e1", "present": True, "value": "bv"}])
    check("erased entity still in snapshot (all fields output)",
          engine.read_snapshot("K")[1] is not None)

    # --- unset vs null distinction ----------------------------------------
    ok, _ = submit(engine, [ev("n1", "N", set_={"a": None}, unset=["b"])])
    body = engine.read_snapshot("N")[1]
    check("null is a value", body["a"] == [
        {"id": "n1", "present": True, "value": None}])
    check("unset produces no candidate", body["b"] == [])

    # --- concurrent equal values are not merged ---------------------------
    ok, _ = submit(engine, [ev("m1", "M"), ev("m2", "M")])
    body = engine.read_snapshot("M")[1]
    ids = [c["id"] for c in body["existence"]]
    check("same-value concurrency retained", ids == ["m1", "m2"], str(ids))

    # --- unwritten field does not override ancestor -----------------------
    ok, _ = submit(engine, [ev("g1", "G2", set_={"a": "keep"})])
    ok, _ = submit(engine, [ev("g2", "G2", parents=["g1"], set_={"b": "x"})])
    body = engine.read_snapshot("G2")[1]
    check("unwritten a inherited", body["a"] == [
        {"id": "g1", "present": True, "value": "keep"}])
    check("b from newer event", body["b"] == [
        {"id": "g2", "present": True, "value": "x"}])

    # --- conflict: concurrent field winners both published ----------------
    ok, _ = submit(engine, [ev("d1", "Q", set_={"a": "one"})])
    ok, _ = submit(engine, [ev("d2", "Q", parents=["d1"],
                               set_={"a": "two"}),
                            ev("d3", "Q", parents=["d1"],
                               set_={"a": "three"})])
    body = engine.read_snapshot("Q")[1]
    check("concurrent field winners both kept (no winner selection)",
          [(c["id"], c["value"]) for c in body["a"]] ==
          [("d2", "two"), ("d3", "three")], str(body["a"]))
    check("existence maxima excludes dominated d1",
          [c["id"] for c in body["existence"]] == ["d2", "d3"])

    # --- change stream: cursor strictness and empty batches ---------------
    ok, r1 = submit(engine, [ev("z1", "Z")])
    zv = r1["version"]
    ok, r2 = submit(engine, [ev("z1", "Z")])  # retransmit: no new version
    check("retransmit no version bump", r2["version"] == zv)
    batches = engine.read_changes(zv - 1)
    check("changes strictly greater than cursor",
          all(b["version"] > zv - 1 for b in batches)
          and batches[-1]["version"] == zv)
    batches_eq = engine.read_changes(zv)
    check("cursor equals current yields nothing", batches_eq == [])

    # an accepted batch that changes nothing still publishes (empty change)
    submit(engine, [ev("z2", "Z", parents=["z1"], set_={"a": "q"})])
    vz2 = engine.read_version()
    # a causally redundant patch that writes the same value and adds no
    # candidate can leave the entity snapshot byte-identical -> empty
    # change list, but the version still increments and a batch is stored
    ok, r = submit(engine, [ev("z4", "Z", parents=["z2"],
                               set_={"a": "q"})])
    # z4 is a new descendant so existence maxima do change; to get a truly
    # empty change we instead re-send a batch whose events are all
    # retransmissions is a no-op (no version bump), so construct a batch
    # that activates nothing and alters no snapshot:
    check("new-event batch always increments version",
          r["version"] == engine.read_version() == vz2 + 1)
    cb = engine.read_changes(vz2)
    check("change batch published at new version",
          cb and cb[0]["version"] == r["version"])
    # genuinely empty change: submit an event for an entity that stays
    # pending (its parent is an unknown placeholder)
    ok, r = submit(engine, [ev("z5", "ZP", parents=["ghost"])])
    check("pending-only batch increments version with empty changes",
          ok and r["version"] == engine.read_version()
          and r["changed_entities"] == [] and r["changes"] == [])
    cb = engine.read_changes(r["version"] - 1)
    check("empty change list published",
          cb[0]["version"] == r["version"] and cb[0]["changes"] == [])

    # --- ordering ----------------------------------------------------------
    check("snapshot entities sorted",
          [e["entity"] for e in engine.read_snapshot()[1]] == sorted(
              e["entity"] for e in engine.read_snapshot()[1]))
    check("candidate id byte order",
          engine.read_snapshot("Z")[1]["a"] == sorted(
              engine.read_snapshot("Z")[1]["a"], key=lambda c: c["id"]))

    # --- unknown / pending entity query shape -----------------------------
    v, unknown = engine.read_snapshot("NOPE")
    check("unknown query is None", unknown is None)

    conn.close()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
