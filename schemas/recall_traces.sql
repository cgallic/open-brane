-- recall_traces.sql — log every retrieval and what the agent actually used.
--
-- Why: semantic_search returns events to an agent. The agent then picks some
-- and ignores others. We almost never know which. This schema closes the loop:
-- every retrieval writes a trace; after the agent answers, it POSTs back
-- which items it used. Over time you can answer:
--
--   - which events get retrieved a lot but never used? (low value)
--   - which events get retrieved + used reliably? (high value)
--   - which queries return mostly-rejected results? (bad retrieval)
--
-- Apply with:  sqlite3 events.db < schemas/recall_traces.sql
-- Adapted from Nate B. Jones's OB1 integrations/agent-memory-api.

PRAGMA foreign_keys = ON;

------------------------------------------------------------------------------
-- One trace per retrieval call.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recall_traces (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL DEFAULT (datetime('now')),
    agent               TEXT,                  -- 'claude-code', 'scout', 'kai', etc.
    query               TEXT NOT NULL,
    source_filter       TEXT,                  -- comma-sep list, null = all
    requested_limit     INTEGER NOT NULL DEFAULT 10,
    returned_count      INTEGER NOT NULL,
    used_count          INTEGER,               -- populated by report_usage; null until then
    closed_at           TEXT,                  -- when report_usage was called
    context_session_id  TEXT                   -- optional: tie traces from same session together
);

CREATE INDEX IF NOT EXISTS idx_trace_ts        ON recall_traces(ts);
CREATE INDEX IF NOT EXISTS idx_trace_agent     ON recall_traces(agent);
CREATE INDEX IF NOT EXISTS idx_trace_session   ON recall_traces(context_session_id);
CREATE INDEX IF NOT EXISTS idx_trace_open      ON recall_traces(closed_at) WHERE closed_at IS NULL;

------------------------------------------------------------------------------
-- One row per (trace, returned event). Captures rank, similarity,
-- whether the agent used it, and why it was ignored if not.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recall_items (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id                 INTEGER NOT NULL REFERENCES recall_traces(id) ON DELETE CASCADE,
    event_id                 INTEGER NOT NULL REFERENCES events(id),
    rank                     INTEGER NOT NULL,        -- 1 = top hit
    similarity               REAL,                    -- cosine similarity, if available

    -- Frozen at retrieval time so we can reconstruct what the agent saw
    -- even if the governance row is updated later.
    use_policy_snapshot_json TEXT,

    -- Populated by report_usage (null until then)
    used                     INTEGER CHECK (used IS NULL OR used IN (0,1)),
    ignored_reason           TEXT,                    -- 'irrelevant', 'stale', 'restricted', 'duplicate', 'lower_priority', 'wrong_subject', null
    used_in                  TEXT,                    -- e.g. 'final_answer', 'reasoning_step', 'cited_in_output'
    reported_at              TEXT,

    UNIQUE (trace_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_trace      ON recall_items(trace_id);
CREATE INDEX IF NOT EXISTS idx_item_event      ON recall_items(event_id);
CREATE INDEX IF NOT EXISTS idx_item_used       ON recall_items(used);
CREATE INDEX IF NOT EXISTS idx_item_unreported ON recall_items(reported_at) WHERE reported_at IS NULL;

------------------------------------------------------------------------------
-- Convenience views
------------------------------------------------------------------------------

-- For each event, how often it gets retrieved vs. how often it gets used.
CREATE VIEW IF NOT EXISTS event_recall_stats AS
SELECT
    event_id,
    COUNT(*)                                              AS retrieved_count,
    SUM(CASE WHEN used = 1 THEN 1 ELSE 0 END)             AS used_count,
    SUM(CASE WHEN used = 0 THEN 1 ELSE 0 END)             AS ignored_count,
    AVG(similarity)                                        AS avg_similarity,
    MAX(ts)                                                AS last_retrieved_at
FROM recall_items i
JOIN recall_traces t ON t.id = i.trace_id
GROUP BY event_id;

-- Open traces — retrievals where the agent never reported back which items it used.
-- High count = a runtime that's not closing the feedback loop.
CREATE VIEW IF NOT EXISTS open_recall_traces AS
SELECT *
FROM recall_traces
WHERE closed_at IS NULL
ORDER BY ts DESC;

-- Queries that consistently return useless results (high retrieval, low usage).
-- Surface in a weekly review to fix retrieval, not the events.
CREATE VIEW IF NOT EXISTS unhelpful_queries AS
SELECT
    t.query,
    COUNT(DISTINCT t.id)                                   AS trace_count,
    SUM(i.returned_count_safe)                             AS total_returned,
    SUM(i.used_count_safe)                                 AS total_used,
    CAST(SUM(i.used_count_safe) AS REAL) /
        NULLIF(SUM(i.returned_count_safe), 0)              AS used_rate
FROM recall_traces t
JOIN (
    SELECT
        trace_id,
        COUNT(*)                                       AS returned_count_safe,
        SUM(CASE WHEN used = 1 THEN 1 ELSE 0 END)      AS used_count_safe
    FROM recall_items
    WHERE used IS NOT NULL
    GROUP BY trace_id
) i ON i.trace_id = t.id
GROUP BY t.query
HAVING trace_count >= 3 AND used_rate < 0.20
ORDER BY trace_count DESC;
