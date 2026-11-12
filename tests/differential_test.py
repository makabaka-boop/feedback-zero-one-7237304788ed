"""Differential tests: engine vs the independent Warshall oracle.

Random graphs are generated and fed into the database-backed engine in
randomised batches; every ancestor set, visibility set and causal-maximum
set is cross-checked against app/oracle.py, which uses separate algorithms
and never imports the engine.
"""
import os
import random
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from harness import fresh_engine, submit, check, PASS, FAIL  # noqa: E402
from app.engine import (ancestor_sets, compute_visibility,  # noqa: E402
                        causal_maxima, build_entity_snapshot)
from app.oracle import (enumerate_ancestors, independent_visibility,  # noqa: E402
                        independent_maxima, transitive_closure)


def random_event(rng, eid, entity, known_ids):
    r = rng.random()
    if r < 0.25:
        return {"id": eid, "entity": entity, "parents": [], "op": "erase"}
    parents = []
    if known_ids and rng.random() < 0.6:
        k = rng.randint(1, min(3, len(known_ids)))
        parents = rng.sample(known_ids, k)  # distinct by sampling
    set_, unset = {}, []
    if rng.random() < 0.7:
        for f in ("a", "b"):
            choice = rng.choice(("set", "unset", "skip"))
            if choice == "set":
                set_[f] = None if rng.random() < 0.2 else "v%s" % rng.randint(0, 9)
            elif choice == "unset":
                unset.append(f)
    # keep set/unset disjoint
    unset = [f for f in unset if f not in set_]
    return {"id": eid, "entity": entity, "parents": parents,
            "op": "patch", "set": set_, "unset": unset}


def main():
    rng = random.Random(20261112)
    for trial in range(40):
        engine, conn = fresh_engine()
        entity = "E"
        n = rng.randint(4, 14)
        ids = ["id%03d" % i for i in range(n)]
        rng.shuffle(ids)
        # build acyclic random parent edges from later->earlier in shuffle
        edges = {}
        raw_events = []
        for i, eid in enumerate(ids):
            parents = []
            if i > 0:
                k = rng.randint(0, min(3, i))
                parents = rng.sample(ids[:i], k)
            ev = random_event(rng, eid, entity, parents)
            edges[eid] = list(parents)
            raw_events.append(ev)

        # submit in random batch sizes, some batches repeated (idempotency)
        idx = 0
        while idx < len(raw_events):
            step = rng.randint(1, 4)
            batch = raw_events[idx:idx + step]
            ok, res = submit(engine, batch)
            check("trial%d random batch accepted" % trial, ok, str(res))
            if rng.random() < 0.3:
                ok2, res2 = submit(engine, batch)  # exact retransmission
                check("trial%d retransmit idempotent" % trial,
                      ok2 and res2.get("retransmitted") is True, str(res2))
            idx += step

        defined = set(ids)
        # --- ancestor cross-check, every node -----------------------------
        engine_anc = ancestor_sets(defined, edges)
        for eid in ids:
            ora = enumerate_ancestors(eid, defined, edges)
            check("trial%d ancestors %s" % (trial, eid),
                  engine_anc[eid] == ora,
                  "%r != %r" % (engine_anc[eid], ora))

        # --- visibility cross-check ---------------------------------------
        eng_vis = compute_visibility(defined, edges)
        ora_vis = independent_visibility(defined, edges)
        check("trial%d visibility" % trial, eng_vis == ora_vis,
              "%r != %r" % (eng_vis, ora_vis))

        # --- maxima cross-check over random candidate subsets -------------
        for _ in range(6):
            k = rng.randint(1, len(ids))
            cands = rng.sample(ids, k)
            eng_m = set(causal_maxima(cands, engine_anc))
            ora_m = set(independent_maxima(cands, edges, defined))
            check("trial%d maxima" % trial, eng_m == ora_m,
                  "%r != %r" % (eng_m, ora_m))

        # --- whole-snapshot cross-check built from engine primitives ------
        cur = conn.cursor()
        cur.execute("SELECT payload FROM events ORDER BY id")
        payloads = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT child_id, parent_id FROM event_parents")
        db_edges = {}
        for child, parent in cur.fetchall():
            db_edges.setdefault(child, []).append(parent)
        db_defined = set(db_edges) | {p["id"] for p in payloads}
        db_visible = compute_visibility(db_defined, db_edges)
        db_ancestors = ancestor_sets(db_defined, db_edges)
        body = build_entity_snapshot(entity, payloads,
                                     db_ancestors, db_visible)
        _, db_body = engine.read_snapshot(entity)
        check("trial%d stored snapshot matches recomputation" % trial,
              body == db_body, "%r != %r" % (body, db_body))

        # closure sanity: Warshall closure equals per-node ancestor union
        closure = transitive_closure(list(db_defined), db_edges)
        check("trial%d closure sanity" % trial,
              all(closure[x] == db_ancestors[x] for x in db_defined))

        conn.close()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
