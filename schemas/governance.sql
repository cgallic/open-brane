-- governance.sql — agent-memory governance sidecar for open-brane
--
-- Adds a trust/use-policy layer on top of the raw events table. Every event
-- can have one governance row that says: how much do we trust this, who
-- confirmed it, and is the agent allowed to act on it as an instruction
-- (vs. cite it only as evidence)?
--
-- The headline invariant (CHECK constraint below):
--
--   An event can only be promoted to instruction-grade if its provenance is
--   user_confirmed or imported. Agents cannot self-promote what they wrote.
--
-- Apply with:  sqlite3 events.db < schemas/governance.sql
-- Adapted from Nate B. Jones's OB1 schemas/agent-memory.

PRAGMA foreign_keys = ON;

------------------------------------------------------------------------------
-- 1:1 governance row per event
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_governance (
    event_id                    INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,

    -- Where did this come from, epistemically?
    --   observed         — sensor / capture / external system observed it
    --   inferred         — derived by an LLM or rule from other events
    --   user_confirmed   — a human explicitly affirmed it
    --   imported         — bulk-loaded from a trusted source (ClickUp, git, your own notes)
    --   generated        — an agent wrote it from scratch (low trust by default)
    --   superseded       — replaced by a newer event
    --   disputed         — flagged as questionable, kept for audit
    provenance_status   TEXT NOT NULL DEFAULT 'observed'
        CHECK (provenance_status IN
              ('observed','inferred','user_confirmed','imported','generated','superseded','disputed')),

    -- Where is this in the review workflow?
    --   pending          — written, awaiting review
    --   confirmed        — human looked, approved
    --   evidence_only    — confirmed for retrieval but NOT for instruction-grade use
    --   restricted       — confirmed but with retrieval restrictions (sensitive)
    --   rejected         — reviewed and not promoted; still queryable, flagged
    --   stale            — past stale_after, needs re-confirmation
    --   merged           — merged into another event
    review_status       TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN
              ('pending','confirmed','evidence_only','restricted','rejected','stale','merged')),

    -- Where is this in its lifecycle?
    --   active           — current truth
    --   stale            — should be re-checked
    --   superseded       — replaced by a newer event
    --   disputed         — contradicted by other evidence
    --   rejected         — review concluded this was wrong
    lifecycle_status    TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_status IN ('active','stale','superseded','disputed','rejected')),

    -- The use-policy. Defaults assume "evidence yes, instruction no" until reviewed.
    can_use_as_instruction       INTEGER NOT NULL DEFAULT 0 CHECK (can_use_as_instruction IN (0,1)),
    can_use_as_evidence          INTEGER NOT NULL DEFAULT 1 CHECK (can_use_as_evidence    IN (0,1)),
    requires_user_confirmation   INTEGER NOT NULL DEFAULT 1 CHECK (requires_user_confirmation IN (0,1)),

    -- How sensitive is this content?
    --   public, internal, confidential, restricted
    sensitivity_tier    TEXT NOT NULL DEFAULT 'internal'
        CHECK (sensitivity_tier IN ('public','internal','confidential','restricted')),

    -- How confident is the writer that this is correct? 0.00-1.00
    confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),

    -- Memory type — what kind of thing is this?
    --   decision, lesson, constraint, open_question, failure, artifact_ref, work_log,
    --   observation, output
    memory_type         TEXT,

    -- Re-confirmation cadence
    stale_after         TEXT,        -- ISO 8601; null = never auto-stale
    last_confirmed_at   TEXT,        -- ISO 8601

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),

    --------------------------------------------------------------------------
    -- THE HEADLINE INVARIANT:
    -- An event is only allowed to be instruction-grade if a human or trusted
    -- import said so. Agents cannot self-promote.
    --------------------------------------------------------------------------
    CHECK (
        can_use_as_instruction = 0
        OR provenance_status IN ('user_confirmed','imported')
    )
);

CREATE INDEX IF NOT EXISTS idx_gov_review_status    ON event_governance(review_status);
CREATE INDEX IF NOT EXISTS idx_gov_lifecycle        ON event_governance(lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_gov_sensitivity      ON event_governance(sensitivity_tier);
CREATE INDEX IF NOT EXISTS idx_gov_stale_after      ON event_governance(stale_after);
CREATE INDEX IF NOT EXISTS idx_gov_instruction_ok   ON event_governance(can_use_as_instruction);

------------------------------------------------------------------------------
-- Relations between events (supersedes / contradicts / merged_into / etc.)
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_relations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    from_event_id       INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    to_event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    relation            TEXT NOT NULL
        CHECK (relation IN
              ('supersedes','superseded_by','contradicts','merged_into','related_to')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    created_by          TEXT,        -- 'connor', 'agent:scout', 'agent:kai', etc.
    rationale           TEXT,        -- one-line why
    UNIQUE (from_event_id, to_event_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_rel_from  ON event_relations(from_event_id);
CREATE INDEX IF NOT EXISTS idx_rel_to    ON event_relations(to_event_id);
CREATE INDEX IF NOT EXISTS idx_rel_kind  ON event_relations(relation);

------------------------------------------------------------------------------
-- Audit trail for every governance change. Append-only.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_review_actions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    action              TEXT NOT NULL,   -- 'confirm', 'reject', 'mark_stale', 'promote', 'demote', 'merge', 'restrict'
    actor               TEXT NOT NULL,   -- 'connor', 'agent:scout', etc.
    before_json         TEXT,            -- snapshot of governance row before
    after_json          TEXT,            -- snapshot after
    note                TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_review_event   ON event_review_actions(event_id);
CREATE INDEX IF NOT EXISTS idx_review_actor   ON event_review_actions(actor);
CREATE INDEX IF NOT EXISTS idx_review_ts      ON event_review_actions(created_at);

------------------------------------------------------------------------------
-- Convenience views
------------------------------------------------------------------------------

-- Events that an agent is allowed to act on as instructions
CREATE VIEW IF NOT EXISTS instruction_grade_events AS
SELECT e.*
FROM events e
JOIN event_governance g ON g.event_id = e.id
WHERE g.can_use_as_instruction = 1
  AND g.lifecycle_status = 'active'
  AND g.review_status IN ('confirmed','restricted');

-- Events safe to retrieve as evidence (most things)
CREATE VIEW IF NOT EXISTS evidence_grade_events AS
SELECT e.*
FROM events e
LEFT JOIN event_governance g ON g.event_id = e.id
WHERE (g.can_use_as_evidence = 1 OR g.event_id IS NULL)
  AND (g.lifecycle_status IN ('active','stale') OR g.lifecycle_status IS NULL)
  AND (g.review_status IN ('pending','confirmed','evidence_only','restricted','stale') OR g.review_status IS NULL);

-- Events past their stale_after date
CREATE VIEW IF NOT EXISTS stale_events AS
SELECT e.*, g.stale_after, g.last_confirmed_at
FROM events e
JOIN event_governance g ON g.event_id = e.id
WHERE g.stale_after IS NOT NULL
  AND g.stale_after < datetime('now')
  AND g.review_status NOT IN ('stale','rejected','merged');
