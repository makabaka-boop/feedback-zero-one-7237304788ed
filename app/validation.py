"""Batch-level validation for offline collection events.

Every rule in here is a *whole batch rejection* rule: if any event in a
submission violates one, the batch is refused and no state changes.
"""
import re

ID_RE = re.compile(r"^[A-Za-z0-9]+$")
FIELDS = ("a", "b")
OPS = ("patch", "erase")


class RejectBatch(Exception):
    """Raised when an entire submission must be refused.

    ``reason`` is a stable, machine-readable code; ``detail`` adds context.
    """

    def __init__(self, reason, detail=""):
        super().__init__(reason + (": " + detail if detail else ""))
        self.reason = reason
        self.detail = detail


def _require(cond, reason, detail=""):
    if not cond:
        raise RejectBatch(reason, detail)


def _is_str(x):
    return isinstance(x, str)


def _normalise_event(ev):
    """Validate one raw event object and return its canonical dict.

    Canonical dict keeps key order fixed so identical JSON structures are
    byte-identical after normalisation.
    """
    _require(isinstance(ev, dict), "event_not_object", repr(type(ev).__name__))

    # Identifier rules: non-empty ASCII alphanumeric strings.
    eid = ev.get("id")
    _require(_is_str(eid) and ID_RE.match(eid) is not None,
             "bad_id", str(eid))
    entity = ev.get("entity")
    _require(_is_str(entity) and ID_RE.match(entity) is not None,
             "bad_entity", str(entity))

    parents = ev.get("parents")
    _require(isinstance(parents, list), "parents_not_array", eid)
    for p in parents:
        _require(_is_str(p) and ID_RE.match(p) is not None,
                 "bad_parent_id", "%s -> %s" % (eid, p))
    _require(len(set(parents)) == len(parents),
             "duplicate_parent", eid)

    op = ev.get("op")
    _require(_is_str(op) and op in OPS, "bad_op", "%s: %s" % (eid, op))

    # No unknown top-level keys: id/entity/parents/op/set/unset only.
    extra = set(ev) - {"id", "entity", "parents", "op", "set", "unset"}
    _require(not extra, "unknown_field", "%s: %s" % (eid, sorted(extra)))

    if op == "patch":
        # patch must carry a top-level set object and an unset array.
        _require("set" in ev, "patch_missing_set", eid)
        _require("unset" in ev, "patch_missing_unset", eid)
        setv = ev["set"]
        unsetv = ev["unset"]
        _require(isinstance(setv, dict), "set_not_object", eid)
        _require(isinstance(unsetv, list), "unset_not_array", eid)

        for k in setv:
            _require(k in FIELDS, "unknown_data_field", "%s.set.%s" % (eid, k))
        for k, v in setv.items():
            _require(v is None or _is_str(v),
                     "bad_set_value", "%s.set.%s" % (eid, k))

        for u in unsetv:
            _require(_is_str(u) and u in FIELDS,
                     "bad_unset_field", "%s: %r" % (eid, u))
        _require(len(set(unsetv)) == len(unsetv),
                 "duplicate_unset", eid)
        _require(not (set(setv) & set(unsetv)),
                 "set_unset_overlap", eid)

        return {
            "id": eid,
            "entity": entity,
            "parents": sorted(parents),  # order-independent identity
            "op": "patch",
            "set": {k: setv[k] for k in FIELDS if k in setv},
            "unset": sorted(f for f in FIELDS if f in unsetv),
        }

    # erase must carry neither set nor unset.
    _require("set" not in ev and "unset" not in ev,
             "erase_with_set_unset", eid)
    return {
        "id": eid,
        "entity": entity,
        "parents": sorted(parents),  # order-independent identity
        "op": "erase",
    }


def normalise_batch(raw):
    """Validate a whole submission.

    Accepts either a bare JSON array of events or ``{"events": [...]}``.
    Returns a list of canonical event dicts. Intra-batch duplicate ids are
    only allowed when the full structure is identical (idempotent retry).
    """
    if isinstance(raw, dict) and isinstance(raw.get("events"), list) \
            and len(raw) == 1:
        items = raw["events"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise RejectBatch("bad_batch_shape")

    events = [_normalise_event(it) for it in items]

    seen = {}
    for ev in events:
        if ev["id"] in seen:
            _require(seen[ev["id"]] == ev,
                     "conflicting_redefinition", ev["id"])
        else:
            seen[ev["id"]] = ev
    return events
