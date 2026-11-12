"""Core engine: batch acceptance, visibility, snapshots and change stream.

All writes happen in one SERIALIZABLE transaction guarded by a transaction
advisory lock, so concurrent submissions are serialised and every read sees
a complete published version. Rejected batches raise RejectBatch and the
transaction is rolled back: no state changes.
"""
import json

import psycopg2

from .validation import FIELDS, RejectBatch

ADVISORY_KEY = 0x43415553414C  # arbitrary 64-bit constant


# ---------------------------------------------------------------------------
# Pure graph / snapshot helpers (also exercised directly by the test-suite
# and cross-checked by the independent oracle).
# ---------------------------------------------------------------------------

def detect_cycle(edges):
    """Return True if the directed graph given by child -> [parents] has a
    cycle (including a self-loop). Iterative DFS coloring."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    def visit(start):
        stack = [(start, 0)]
        while stack:
            node, idx = stack[-1]
            neigh = edges.get(node, ())
            if idx == 0:
                color[node] = GRAY
            if idx < len(neigh):
                stack[-1] = (node, idx + 1)
                nxt = neigh[idx]
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    return True
                if c == WHITE:
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
        return False

    for n in list(edges):
        if color.get(n, WHITE) == WHITE and visit(n):
            return True
    return False


def ancestor_sets(defined_ids, parent_map):
    """For every defined event id, enumerate its defined ancestors.

    Parent edges that point at ids which are not defined events are
    ignored: an undefined (placeholder) node breaks the chain.
    """
    ancestors = {}
    for root in defined_ids:
        seen = set()
        stack = list(parent_map.get(root, ()))
        while stack:
            node = stack.pop()
            if node in seen or node not in defined_ids:
                continue
            seen.add(node)
            stack.extend(parent_map.get(node, ()))
        ancestors[root] = seen
    return ancestors


def compute_visibility(defined_ids, parent_map):
    """Fixed point: an event is visible when it is defined and every parent
    is itself a defined, visible event. Newly supplied events can
    cascade-activate events that used to be pending."""
    visible = set()
    while True:
        changed = False
        for eid in defined_ids:
            if eid in visible:
                continue
            parents = parent_map.get(eid, ())
            if all(p in defined_ids and p in visible for p in parents):
                visible.add(eid)
                changed = True
        if not changed:
            return visible


def causal_maxima(candidate_ids, ancestors):
    """Causally maximal candidates: x survives unless some other candidate
    z exists that is a (non-zero-length) descendant of x.

    Equivalently, x is maximal when no other candidate lists x among its
    ancestors. Candidate ancestors of x do not eliminate x."""
    cset = set(candidate_ids)
    # Which candidate ids appear as ancestors of another candidate?
    has_candidate_descendant = set()
    for z in cset:
        has_candidate_descendant |= (ancestors.get(z, set()) & cset)
    return [x for x in candidate_ids
            if x not in has_candidate_descendant]


def event_candidates(ev):
    """Return (existence_candidate, field_candidates) for one event.

    existence: (id, present)
    field:     {field: (id, present, value_or_None)}  -- missing writes
               (unset / erase) simply emit no field candidate.
    """
    eid = ev["id"]
    if ev["op"] == "erase":
        return (eid, False), {}
    existence = (eid, True)
    fields = {}
    for f in FIELDS:
        if f in ev["unset"]:
            continue  # unset is a missing write
        if f in ev["set"]:
            fields[f] = (eid, True, ev["set"][f])  # null => present with value null
    return existence, fields


def build_entity_snapshot(entity, entity_events, ancestors, visible):
    """Compute the complete published snapshot body for one entity."""
    visible_events = [ev for ev in entity_events if ev["id"] in visible]
    if not visible_events:
        return None  # entity has no visible events: must not be published

    existence_cands = []
    field_cands = {f: [] for f in FIELDS}
    for ev in visible_events:
        ex, flds = event_candidates(ev)
        existence_cands.append(ex)
        for f, c in flds.items():
            field_cands[f].append(c)

    def max_ids(pairs):
        ids = [c[0] for c in pairs]
        winners = set(causal_maxima(ids, ancestors))
        return [c for c in pairs if c[0] in winners]

    body = {"entity": entity}
    ex_winners = max_ids(existence_cands)
    body["existence"] = sorted(
        ({"id": i, "present": p} for i, p in ex_winners),
        key=lambda c: c["id"],
    )
    for f in FIELDS:  # fixed field order a, b
        winners = max_ids(field_cands[f])
        out = []
        for c in winners:
            eid, present, value = c
            if present:
                out.append({"id": eid, "present": True, "value": value})
            else:
                out.append({"id": eid, "present": False})
        body[f] = sorted(out, key=lambda c: c["id"])
    return body


# ---------------------------------------------------------------------------
# Database-backed engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, conn):
        self.conn = conn

    # -- low level ---------------------------------------------------------

    def _load_state(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, entity, is_defined FROM nodes")
        nodes = {r[0]: {"entity": r[1], "is_defined": r[2]} for r in cur.fetchall()}
        cur.execute("SELECT id, entity, payload FROM events")
        # Normalise stored jsonb payloads to canonical JSON text so the
        # idempotency comparison is independent of jsonb's internal key order.
        events = {r[0]: {"entity": r[1],
                         "payload": json.dumps(r[2], sort_keys=True)}
                  for r in cur.fetchall()}
        cur.execute("SELECT child_id, parent_id FROM event_parents")
        parent_map = {}
        for child, parent in cur.fetchall():
            parent_map.setdefault(child, []).append(parent)
        cur.execute("SELECT entity, body FROM snapshot_entities")
        snapshots = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT version FROM meta WHERE id = 1")
        version = cur.fetchone()[0]
        return nodes, events, parent_map, snapshots, version

    # -- submission --------------------------------------------------------

    def submit(self, canonical_events, durable=True):
        """Apply one already-validated, normalised batch.

        ``durable=False`` is used by replay: the raw batch is already in
        accepted_batches, so it must not be inserted a second time.

        Returns a dict describing the outcome. Raises RejectBatch on any
        semantic conflict; the caller rolls back.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))

        nodes, events, parent_map, old_snap, version = self._load_state()

        # 1) idempotency / conflicting redefinition -----------------------
        fresh = {}
        for ev in canonical_events:
            eid = ev["id"]
            if eid in events:
                if events[eid]["payload"] != json.dumps(ev, sort_keys=True):
                    raise RejectBatch("conflicting_redefinition", eid)
                continue  # identical retransmission
            if eid in fresh:
                continue  # identical intra-batch duplicate (validated)
            fresh[eid] = ev

        if not fresh:
            self.conn.rollback()
            return {"version": version, "new_events": 0,
                    "changed_entities": [], "retransmitted": True}

        # 2) fixed entity ownership (first definition or reference wins) --
        # work on a copy so a rejection leaves the DB transaction untouched
        local_nodes = {k: dict(v) for k, v in nodes.items()}
        for ev in fresh.values():
            eid, ent = ev["id"], ev["entity"]
            if eid in local_nodes:
                if local_nodes[eid]["entity"] != ent:
                    raise RejectBatch("entity_ownership_conflict", eid)
            else:
                local_nodes[eid] = {"entity": ent, "is_defined": True}
            local_nodes[eid]["is_defined"] = True
            for p in ev["parents"]:
                if p in local_nodes:
                    if local_nodes[p]["entity"] != ent:
                        raise RejectBatch("cross_entity_reference",
                                          "%s -> %s" % (eid, p))
                else:
                    # placeholder: ownership fixed by this first reference
                    local_nodes[p] = {"entity": ent, "is_defined": False}

        # 3) cycle detection over the union graph (placeholders included).
        # Start from every persisted edge, then overlay edges of all events
        # in the submission, including idempotent retransmissions: they can
        # form a cycle together with a newly introduced node.
        union_edges = {}
        cur.execute("SELECT child_id, parent_id FROM event_parents")
        for child, parent in cur.fetchall():
            union_edges.setdefault(child, []).append(parent)
        for ev in canonical_events:
            union_edges.setdefault(ev["id"], [])
            for p in ev["parents"]:
                if p not in union_edges[ev["id"]]:
                    union_edges[ev["id"]].append(p)
                # Placeholder parents may appear only as parents; make sure
                # they exist as nodes so DFS can traverse through them.
                union_edges.setdefault(p, [])
        if detect_cycle(union_edges):
            raise RejectBatch("cycle_detected")

        # Visibility *before* this batch, so newly activated (cascade)
        # events can be detected after the inserts below.
        old_visible = compute_visibility(set(events), parent_map)

        # 4) persist new nodes / events / edges ---------------------------
        for nid, info in local_nodes.items():
            if nid not in nodes:
                cur.execute(
                    "INSERT INTO nodes (id, entity, is_defined) VALUES (%s,%s,%s)",
                    (nid, info["entity"], info["is_defined"]))
            elif info["is_defined"] and not nodes[nid]["is_defined"]:
                cur.execute(
                    "UPDATE nodes SET is_defined = TRUE WHERE id = %s", (nid,))
        for ev in fresh.values():
            cur.execute(
                "INSERT INTO events (id, entity, payload, created_in) "
                "VALUES (%s,%s,%s,%s)",
                (ev["id"], ev["entity"], json.dumps(ev), version + 1))
            for p in ev["parents"]:
                cur.execute(
                    "INSERT INTO event_parents (child_id, parent_id) "
                    "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (ev["id"], p))

        # 5) recompute visibility over the full graph ---------------------
        cur.execute("SELECT id, entity, payload FROM events ORDER BY id")
        all_events = []
        for r in cur.fetchall():
            ev = dict(r[2])
            ev["id"] = r[0]
            ev["entity"] = r[1]
            all_events.append(ev)
        cur.execute("SELECT child_id, parent_id FROM event_parents")
        full_parents = {}
        for child, parent in cur.fetchall():
            full_parents.setdefault(child, []).append(parent)
        defined = {ev["id"] for ev in all_events}
        visible = compute_visibility(defined, full_parents)
        ancestors = ancestor_sets(defined, full_parents)

        # 6) snapshots for every entity whose result may change:
        #   - entities owning new events,
        #   - entities that already had a published snapshot (new visible
        #     descendants can change their maxima),
        #   - entities gaining a newly visible event (cascade activation
        #     of old pending events, possibly in an entity that submitted
        #     nothing in this batch).
        affected = {ev["entity"] for ev in fresh.values()}
        affected.update(old_snap.keys())
        entity_of = {ev["id"]: ev["entity"] for ev in all_events}
        for eid in visible - old_visible:
            affected.add(entity_of[eid])
        affected = sorted(affected)
        by_entity = {}
        for ev in all_events:
            by_entity.setdefault(ev["entity"], []).append(ev)

        new_snap = dict(old_snap)
        changed = []
        for ent in affected:
            body = build_entity_snapshot(
                ent, by_entity.get(ent, []), ancestors, visible)
            if body is None:
                new_snap.pop(ent, None)
            else:
                new_snap[ent] = body
            prior = old_snap.get(ent)
            if prior != body:
                changed.append(ent)
                if body is None:
                    cur.execute(
                        "DELETE FROM snapshot_entities WHERE entity = %s",
                        (ent,))
                elif prior is None:
                    cur.execute(
                        "INSERT INTO snapshot_entities (entity, body, version) "
                        "VALUES (%s,%s,%s)",
                        (ent, json.dumps(body), version + 1))
                else:
                    cur.execute(
                        "UPDATE snapshot_entities SET body=%s, version=%s "
                        "WHERE entity=%s",
                        (json.dumps(body), version + 1, ent))

        # 7) version + change batch (may be empty) + durable raw input ----
        new_version = version + 1
        changes = [new_snap[e] for e in sorted(changed)]
        cur.execute("UPDATE meta SET version = %s WHERE id = 1", (new_version,))
        cur.execute(
            "INSERT INTO change_batches (version, changes) VALUES (%s,%s)",
            (new_version, json.dumps(changes)))
        if durable:
            cur.execute(
                "INSERT INTO accepted_batches (payload, version) "
                "VALUES (%s,%s)",
                (json.dumps(canonical_events), new_version))
        self.conn.commit()
        return {"version": new_version, "new_events": len(fresh),
                "changed_entities": changed, "retransmitted": False,
                "changes": changes}

    # -- reads -------------------------------------------------------------

    def _begin_read(self):
        # A read-only snapshot transaction guarantees the whole response
        # reflects one complete published version, even if a writer commits
        # while the query is running. Clear any idle transaction first so
        # the isolation change applies before the first query.
        self.conn.commit()
        self.conn.set_session(
            isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ,
            readonly=True, autocommit=False)

    def _end_read(self):
        self.conn.commit()
        # restore the default read-write session characteristics
        self.conn.set_session(
            isolation_level=psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED,
            readonly=False, autocommit=False)

    def read_version(self):
        cur = self.conn.cursor()
        cur.execute("SELECT version FROM meta WHERE id = 1")
        return cur.fetchone()[0]

    def read_snapshot(self, entity=None):
        try:
            self._begin_read()
            version = self.read_version()
            cur = self.conn.cursor()
            if entity is None:
                cur.execute(
                    "SELECT body FROM snapshot_entities ORDER BY entity")
                result = version, [r[0] for r in cur.fetchall()]
            else:
                cur.execute(
                    "SELECT body FROM snapshot_entities WHERE entity = %s",
                    (entity,))
                row = cur.fetchone()
                result = version, None if row is None else row[0]
            self._end_read()
            return result
        except Exception:
            self.conn.rollback()
            self.conn.set_session(readonly=False)
            raise

    def read_changes(self, cursor_version):
        try:
            self._begin_read()
            cur = self.conn.cursor()
            cur.execute(
                "SELECT version, changes FROM change_batches "
                "WHERE version > %s ORDER BY version", (cursor_version,))
            out = [{"version": v, "changes": c}
                   for v, c in cur.fetchall()]
            self._end_read()
            return out
        except Exception:
            self.conn.rollback()
            self.conn.set_session(readonly=False)
            raise

    # -- replay ------------------------------------------------------------

    def replay(self):
        """Rebuild every derived structure from accepted_batches, starting
        from the empty state. Used after a restart / for verification."""
        cur = self.conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
        cur.execute("SELECT seq, payload FROM accepted_batches ORDER BY seq")
        batches = cur.fetchall()
        # Wipe every derived structure, but keep accepted_batches itself.
        cur.execute("TRUNCATE event_parents, events, nodes, "
                    "snapshot_entities, change_batches, meta RESTART IDENTITY")
        cur.execute("INSERT INTO meta (id, version) VALUES (1, 0)")
        self.conn.commit()

        results = []
        for _, payload in batches:
            # Same code path as a live submission, minus validation and
            # without re-inserting the raw batch we are replaying.
            results.append(self.submit(payload, durable=False))
        return results
