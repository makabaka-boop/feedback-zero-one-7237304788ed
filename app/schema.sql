-- Persistent schema for the offline-collection replay service.
-- All state lives here; on replay the derived tables (events, parents,
-- nodes, snapshot, change_batches) are rebuilt from accepted_batches.

CREATE TABLE IF NOT EXISTS meta (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version    BIGINT NOT NULL DEFAULT 0
);
INSERT INTO meta (id, version) VALUES (1, 0) ON CONFLICT DO NOTHING;

-- Registry of every identifier that has ever been defined or referenced.
-- Owning entity is fixed the first time an id is seen.
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    entity      TEXT NOT NULL,
    is_defined  BOOLEAN NOT NULL DEFAULT FALSE
);

-- One row per accepted event. payload is the canonical, normalised JSON
-- structure; equal id + equal structure is the idempotency key.
CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    entity     TEXT NOT NULL,
    payload    JSONB NOT NULL,
    created_in BIGINT NOT NULL
);

-- Parent edges (one row per (child, parent) pair; no duplicates by PK).
CREATE TABLE IF NOT EXISTS event_parents (
    child_id  TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    PRIMARY KEY (child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_event_parents_parent ON event_parents(parent_id);

-- Current full snapshot, one row per entity that currently has at least
-- one visible event. body is the complete entity snapshot.
CREATE TABLE IF NOT EXISTS snapshot_entities (
    entity   TEXT PRIMARY KEY,
    body     JSONB NOT NULL,
    version  BIGINT NOT NULL
);

-- Every published change batch, including empty ones (version still grows
-- only when the batch introduced new events).
CREATE TABLE IF NOT EXISTS change_batches (
    version   BIGINT PRIMARY KEY,
    changes   JSONB NOT NULL
);

-- Raw accepted input, retained so a restart can rebuild every derived
-- structure from the empty state.
CREATE TABLE IF NOT EXISTS accepted_batches (
    seq       BIGSERIAL PRIMARY KEY,
    payload   JSONB NOT NULL,
    version   BIGINT NOT NULL
);
