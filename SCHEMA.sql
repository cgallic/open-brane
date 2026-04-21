-- events.db schema for the brain ingestion system.
-- Apply with: sqlite3 events.db < SCHEMA.sql

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,        -- ISO 8601 UTC: when the event happened
    source          TEXT    NOT NULL,        -- e.g. clickup, gdrive, stripe, claude, git
    type            TEXT    NOT NULL,        -- e.g. task, document, invoice.paid, session, commit
    actor           TEXT,                     -- person/entity + dedup fingerprint
    payload_json    TEXT    NOT NULL,        -- JSON blob. MUST include "summary" field.
    attachment_uri  TEXT,                     -- file://, s3://, gdrive://... for blobs
    ingested_at     TEXT    NOT NULL         -- ISO 8601 UTC: when the row hit the DB
);

CREATE INDEX IF NOT EXISTS idx_events_ts          ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_source_type ON events(source, type);
CREATE INDEX IF NOT EXISTS idx_events_actor       ON events(actor);
CREATE INDEX IF NOT EXISTS idx_events_ingested    ON events(ingested_at);

-- Convenience view: latest event per (source, actor) for quick dedup checks.
CREATE VIEW IF NOT EXISTS latest_by_actor AS
SELECT source, actor, MAX(ts) AS last_ts, COUNT(*) AS event_count
FROM events
WHERE actor IS NOT NULL
GROUP BY source, actor;
