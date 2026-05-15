# Governance

The raw `events` table is append-only and trust-blind: anything that gets written stays written, regardless of whether it's verified fact, an LLM's best guess, or a typo. That's correct for the source-of-truth log — but it means agents can't safely read from it without an extra layer telling them what they're allowed to *act* on.

`schemas/governance.sql` adds that layer.

## Apply

```bash
sqlite3 events.db < schemas/governance.sql
```

Idempotent — creates tables only if missing.

## The one invariant that matters

A governance row controls whether an event is **instruction-grade** (an agent is allowed to act on it as a directive) vs. **evidence-grade** (an agent can cite it but cannot follow it).

The schema enforces this via a CHECK constraint:

```sql
CHECK (
    can_use_as_instruction = 0
    OR provenance_status IN ('user_confirmed','imported')
)
```

**Translation:** an agent's own writes start at `provenance_status = 'inferred'` or `'generated'`. SQLite will reject any attempt to set `can_use_as_instruction = 1` until a human review action flips `provenance_status` to `user_confirmed`, OR the event was bulk-imported from a trusted source (`imported`).

This is the single biggest difference between an event log and an *agent-safe* event log.

## Default policy per adapter

The schema doesn't know which sources are personal vs. work — your adapters do. Suggested defaults:

| Adapter | Default `provenance_status` | Default `review_status` | Default `can_use_as_instruction` |
|---|---|---|---|
| `manual` (you typed it) | `user_confirmed` | `confirmed` | 1 |
| `clickup` | `imported` | `confirmed` | 1 (work tasks you created count as you) |
| `git` / `gh` | `imported` | `confirmed` | 0 (commits are evidence, not instructions) |
| `gdrive` | `imported` | `confirmed` | 0 |
| `claude` (session log) | `observed` | `pending` | 0 |
| `pendant` (raw transcript) | `observed` | `pending` | 0 |
| `pendant` (LLM summary) | `inferred` | `pending` | 0 |
| `scout` (self-mirror) | `observed` | `pending` | 0 |
| `agent:*` (any agent's analysis) | `generated` | `pending` | 0 |
| `health` (uptime probe) | `observed` | `confirmed` | 0 |
| `stripe`, `kaicalls`, other webhooks | `imported` | `confirmed` | 0 (data, not instructions) |

In adapter code, after `record_event` returns the new `event_id`:

```python
import sqlite3
conn = sqlite3.connect(DB)
conn.execute(
    "INSERT INTO event_governance (event_id, provenance_status, review_status, "
    "can_use_as_instruction, sensitivity_tier, memory_type) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (event_id, "inferred", "pending", 0, "internal", "observation"),
)
conn.commit()
```

(If you skip this insert, the event simply has no governance row — the `evidence_grade_events` view treats that as "evidence allowed", but `instruction_grade_events` excludes it. Backward-compatible.)

## Promoting an event to instruction-grade

```sql
-- Step 1: change provenance (only a human / trusted import path should do this)
UPDATE event_governance
SET provenance_status = 'user_confirmed',
    review_status     = 'confirmed',
    last_confirmed_at = datetime('now'),
    updated_at        = datetime('now')
WHERE event_id = ?;

-- Step 2: flip the use-policy
UPDATE event_governance
SET can_use_as_instruction = 1,
    updated_at             = datetime('now')
WHERE event_id = ?;
```

If you try Step 2 without Step 1, the CHECK constraint rejects the update. That's the whole point.

Every promotion should also write an `event_review_actions` row with the before/after JSON snapshot.

## Querying for retrieval

**For an agent that needs instructions:**

```sql
SELECT * FROM instruction_grade_events WHERE source = 'connor_preferences' ORDER BY ts DESC LIMIT 10;
```

**For an agent that needs context / evidence:**

```sql
SELECT * FROM evidence_grade_events WHERE source IN ('pendant','claude') ORDER BY ts DESC LIMIT 50;
```

**For a re-confirmation cron:**

```sql
SELECT event_id, stale_after, last_confirmed_at FROM stale_events LIMIT 20;
```

Surface those in your Discord digest. Each one is a question: "still true?"

## Relations

`event_relations` lets you record `supersedes`, `superseded_by`, `contradicts`, `merged_into`, `related_to` between events. Combine with `event_edges` (from `schemas/edges.sql`) for richer reasoning-style edges with temporal validity.

## Failure modes

- **You forgot to insert the governance row.** Event is treated as evidence-only by `evidence_grade_events`, invisible to `instruction_grade_events`. Safe default.
- **You set instruction-grade on an LLM-generated event.** SQLite rejects with a CHECK constraint failure. The error is the feature.
- **You promote, then the source is wrong.** Write a new event with `record_event`, then an `event_relations` row with `relation = 'supersedes'`, then update the old one's `lifecycle_status = 'superseded'`.

## Anti-pattern

Don't backfill `provenance_status = 'user_confirmed'` across all your existing events to silence the gate. That removes the whole signal. If you have 10,000 historical events with no governance rows, leave them at evidence-grade by default and only promote the small handful you actually want agents to follow.
