-- edges.sql — typed reasoning edges between events.
--
-- The raw events table is a timeline. event_relations (in governance.sql)
-- gives you basic ontological links (supersedes / contradicts / merged_into).
-- This file adds *reasoning* edges with temporal validity and a decay weight,
-- so the brain can answer questions like:
--
--   - which events support this claim?
--   - which decisions still apply, and which were valid only in a date range?
--   - which observations contradict a still-active decision?
--
-- Apply with:  sqlite3 events.db < schemas/edges.sql
-- Adapted from Nate B. Jones's OB1 schemas/typed-reasoning-edges.

PRAGMA foreign_keys = ON;

------------------------------------------------------------------------------
-- Typed, weighted, time-bounded edges.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_edges (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    from_event_id       INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    to_event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,

    -- Type of reasoning relationship.
    --   supports         — from is evidence FOR to
    --   contradicts      — from is evidence AGAINST to
    --   evolved_into     — from is an earlier draft of to (same idea, refined)
    --   supersedes       — from REPLACES to (to is no longer current)
    --   depends_on       — from cannot be acted on unless to is true
    --   related_to       — weak generic association
    --   informs          — from is context for to (looser than 'supports')
    --   triggered_by     — from is the cause/precondition that produced to
    relation            TEXT NOT NULL
        CHECK (relation IN
              ('supports','contradicts','evolved_into','supersedes',
               'depends_on','related_to','informs','triggered_by')),

    -- Temporal validity. Both null = always valid.
    valid_from          TEXT,    -- ISO 8601; null = since beginning
    valid_until         TEXT,    -- ISO 8601; null = still valid

    -- Edge weight: how strong is this relationship? 0.00-1.00.
    -- decay_weight: how much does the weight decay per day past valid_until? 0 = no decay.
    weight              REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0.0 AND weight <= 1.0),
    decay_weight        REAL NOT NULL DEFAULT 0.0 CHECK (decay_weight >= 0.0 AND decay_weight <= 1.0),

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    created_by          TEXT,        -- 'connor', 'agent:scout', 'rule:auto-link', etc.
    rationale           TEXT,        -- one-line why

    -- An edge is a relation between TWO specific events with ONE relation type.
    -- A second edge of the same relation between the same pair is meaningless.
    UNIQUE (from_event_id, to_event_id, relation),

    -- No self-loops.
    CHECK (from_event_id <> to_event_id)
);

CREATE INDEX IF NOT EXISTS idx_edge_from           ON event_edges(from_event_id);
CREATE INDEX IF NOT EXISTS idx_edge_to             ON event_edges(to_event_id);
CREATE INDEX IF NOT EXISTS idx_edge_relation       ON event_edges(relation);
CREATE INDEX IF NOT EXISTS idx_edge_active         ON event_edges(valid_until) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_edge_created_by     ON event_edges(created_by);

------------------------------------------------------------------------------
-- Convenience views
------------------------------------------------------------------------------

-- Currently active edges (valid_until is null or future)
CREATE VIEW IF NOT EXISTS active_edges AS
SELECT *
FROM event_edges
WHERE valid_until IS NULL
   OR valid_until > datetime('now');

-- For an event, list everything currently supporting it (and weights).
-- Use: SELECT * FROM event_support WHERE to_event_id = <id>;
CREATE VIEW IF NOT EXISTS event_support AS
SELECT
    e.to_event_id,
    e.from_event_id,
    e.weight,
    e.rationale,
    src.source        AS from_source,
    src.type          AS from_type,
    json_extract(src.payload_json, '$.summary') AS from_summary
FROM active_edges e
JOIN events src ON src.id = e.from_event_id
WHERE e.relation = 'supports';

-- For an event, list everything currently contradicting it.
CREATE VIEW IF NOT EXISTS event_contradictions AS
SELECT
    e.to_event_id,
    e.from_event_id,
    e.weight,
    e.rationale,
    src.source        AS from_source,
    src.type          AS from_type,
    json_extract(src.payload_json, '$.summary') AS from_summary
FROM active_edges e
JOIN events src ON src.id = e.from_event_id
WHERE e.relation = 'contradicts';

-- Decisions that have contradicting evidence — "is this still current?" report
CREATE VIEW IF NOT EXISTS questioned_decisions AS
SELECT DISTINCT
    e.id              AS event_id,
    e.source,
    e.type,
    json_extract(e.payload_json, '$.summary') AS summary,
    (SELECT COUNT(*) FROM event_contradictions c WHERE c.to_event_id = e.id)  AS contradiction_count,
    (SELECT COUNT(*) FROM event_support        s WHERE s.to_event_id = e.id)  AS support_count
FROM events e
JOIN event_governance g ON g.event_id = e.id
WHERE g.memory_type = 'decision'
  AND g.lifecycle_status = 'active'
  AND EXISTS (SELECT 1 FROM event_contradictions c WHERE c.to_event_id = e.id);
