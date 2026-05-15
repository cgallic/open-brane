# Typed Reasoning Edges

The events table is a timeline. `event_relations` (in `governance.sql`) gives basic ontological links — supersedes, contradicts, merged_into. That's enough for housekeeping but not enough for reasoning.

`schemas/edges.sql` adds **typed reasoning edges** with weight and temporal validity. They answer:

- Which events *support* this claim? Which *contradict* it?
- Which decisions are still valid today, and which only applied in a date range?
- Which active decisions have surfaced contradictions and should be re-reviewed?

## Apply

```bash
sqlite3 events.db < schemas/edges.sql
```

## The relations

| Relation | Meaning | Example |
|---|---|---|
| `supports` | from is evidence FOR to | A health-check event supports a decision "agent box is healthy" |
| `contradicts` | from is evidence AGAINST to | A new transcript contradicts a previously-confirmed fact |
| `evolved_into` | from is an earlier draft of to | Day 1 plan → Day 5 plan (same idea, refined) |
| `supersedes` | from REPLACES to | New policy supersedes old policy |
| `depends_on` | from cannot apply unless to is true | Decision X depends on the agent box being available |
| `related_to` | weak generic link | Two threads in the same panning session |
| `informs` | from is context for to (looser than supports) | A meeting log informs a follow-up decision |
| `triggered_by` | from is the cause that produced to | An alert triggered the deploy event |

## Temporal validity

Most edges are forever (`valid_from = null`, `valid_until = null`). But sometimes an edge only applied in a window:

```sql
INSERT INTO event_edges (from_event_id, to_event_id, relation, valid_from, valid_until, weight, rationale)
VALUES (
    9001,    -- "agent kernel 6.17.0-22 unstable"
    9002,    -- decision: "pin grub to 6.17.0-20"
    'supports',
    '2026-05-05',
    '2026-05-20',     -- because the kernel got patched
    0.9,
    'observed 7 crashes in 5 days'
);
```

The `active_edges` view auto-filters by `valid_until > now`, so the pinning rationale will disappear from `event_support` once May 20 passes. The edge is still in the audit trail, just not "currently in force".

## Weight and decay

- `weight` (0.0-1.0) is how strong the relationship is at creation.
- `decay_weight` (0.0-1.0) is how much weight is lost per day past `valid_until`. Defaults to 0 (no decay).

The schema stores these; downstream consumers (your retrieval, your "is this still true?" reports) decide how to use them. Suggestion: when ranking evidence, multiply similarity × edge.weight × (1 - decay_weight × days_past).

## The most useful query

`questioned_decisions` surfaces active decisions that have at least one active contradiction edge:

```sql
SELECT * FROM questioned_decisions ORDER BY contradiction_count DESC;
```

Run this weekly. Each row is a question: "this is still marked as a decision but something contradicts it — re-confirm or supersede?"

Pair with `stale_events` (from governance) for a complete "what needs human attention?" report.

## How edges get created

Three paths:

1. **You write them manually** when you spot a relationship — usually after a panning session.
2. **An agent proposes them.** When the agent reads two events that seem to relate, it can call a `propose_edge` MCP tool. Edges proposed by agents should start at `weight ≤ 0.5` and `created_by = 'agent:<name>'`, with governance left to the human to confirm.
3. **A rule auto-links them.** Examples:
   - "Every health-check event with `all_healthy = true` supports the most recent `decision: agent box is healthy` event."
   - "Every git commit that touches `/srv/scout/` is `related_to` the most recent scout-related decision."

The rule-based path is the cheapest. Write one Python script per rule, run it on the same 4-hour cadence as `embed_events.py`.

## Anti-pattern

Don't create an edge for every pair of related-feeling events. Sparse, deliberate edges are far more useful than a dense mesh. If `event_edges` grows faster than `events`, your rules are over-eager — tighten them.
