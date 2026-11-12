"""Independent ancestor-enumeration oracle.

This module deliberately uses a *different* algorithm from the engine
(Warshall-style transitive closure on an adjacency matrix plus brute-force
path enumeration) so the cross-check in tests/differential_test.py is a
genuine independent implementation rather than a tautology.

It is pure-Python and has no dependency on the application code.
"""


def transitive_closure(nodes, edges):
    """Warshall closure. ``edges`` maps node -> iterable of parent nodes.

    Returns reach[n] = set of nodes reachable from n by >= 1 edge.
    """
    reach = {n: set(edges.get(n, ())) for n in nodes}
    for mid in nodes:
        for src in nodes:
            if mid in reach[src]:
                reach[src] |= reach.get(mid, set())
    return reach


def enumerate_ancestors(node, defined, edges):
    """Brute-force ancestor enumeration for one node.

    Only defined nodes count as ancestors; edges into placeholder nodes
    stop the walk. Returns the set of defined ancestor ids.
    """
    closure = transitive_closure(_all_nodes(edges), edges)
    return {a for a in closure.get(node, set()) if a in defined}


def _all_nodes(edges):
    out = set()
    for k, vs in edges.items():
        out.add(k)
        out.update(vs)
    return out


def independent_visibility(defined, edges):
    """Independent fixed-point visibility: visible iff defined and every
    parent is defined and visible. Recomputed by repeated sweeps."""
    visible = set()
    pending = set(defined)
    while pending:
        progressed = False
        for n in list(pending):
            parents = edges.get(n, ())
            if all(p in defined and p in visible for p in parents):
                visible.add(n)
                pending.discard(n)
                progressed = True
        if not progressed:
            break
    return visible


def independent_maxima(candidate_ids, edges, defined):
    """Maximal elements computed from the independent Warshall closure.

    x is maximal iff no other candidate z has x among its ancestors."""
    closure = transitive_closure(_all_nodes(edges), edges)
    cset = set(candidate_ids)
    with_candidate_descendant = set()
    for z in cset:
        with_candidate_descendant |= {
            a for a in closure.get(z, set())
            if a in defined and a in cset}
    return [x for x in candidate_ids
            if x not in with_candidate_descendant]
