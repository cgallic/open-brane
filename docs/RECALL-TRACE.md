# Recall Trace

By default, `semantic_search` is a black box: the agent gets results and we never learn which ones it used. Recall traces close the loop.

Every retrieval writes a trace + N item rows. After the agent finishes its turn, it POSTs back which items it actually used. Over time you can answer:

- Which events get retrieved a lot but never used? (Low signal — consider re-summarizing or removing.)
- Which events get retrieved + used reliably? (High signal — protect from staleness.)
- Which queries return mostly-rejected results? (Bad retrieval, not bad events.)

## Apply

```bash
sqlite3 events.db < schemas/recall_traces.sql
```

## Wiring

### Side 1 — the retrieval tool writes a trace

Edit `semantic_search` (or whatever your read path is) to, after returning results:

```python
def semantic_search(query, limit=10, source=None, agent=None, session_id=None):
    hits = qdrant_search(query, limit=limit, source=source)   # existing
    rows = join_payloads(hits)                                # existing

    # NEW: write the trace
    trace_id = _write_trace(query, agent, source, limit, len(rows), session_id)
    _write_items(trace_id, rows)   # includes rank, similarity, governance snapshot

    # NEW: include trace_id in the response so the agent can report back
    return {"trace_id": trace_id, "results": rows}
```

`_write_items` should snapshot the current `event_governance` row for each event into `recall_items.use_policy_snapshot_json` so the trace is reconstructible later.

### Side 2 — the agent reports usage

After the agent answers, it calls a `report_usage` tool/endpoint with:

```json
{
  "trace_id": 12345,
  "used_event_ids": [9001, 9007],
  "ignored": [
    {"event_id": 9002, "reason": "irrelevant"},
    {"event_id": 9008, "reason": "stale"}
  ]
}
```

Server applies:

```sql
UPDATE recall_items
SET used = 1, used_in = 'final_answer', reported_at = datetime('now')
WHERE trace_id = ? AND event_id IN (?, ?, ...);

UPDATE recall_items
SET used = 0, ignored_reason = ?, reported_at = datetime('now')
WHERE trace_id = ? AND event_id = ?;

UPDATE recall_traces
SET used_count = (SELECT COUNT(*) FROM recall_items WHERE trace_id = ? AND used = 1),
    closed_at  = datetime('now')
WHERE id = ?;
```

### Side 3 — make the agent actually call report_usage

Two options:

1. **Tool-call discipline.** Add `report_usage` to your MCP tool set with a description that tells the agent to call it after `semantic_search` results inform the answer. Anthropic's tool-routing will usually comply for well-described tools.
2. **Heuristic fallback.** If `closed_at` is null after 5 minutes, mark all items `used=null, ignored_reason='unreported'` so the trace doesn't stay open forever. View `open_recall_traces` surfaces these for inspection.

## Querying the loop

**Events that are dead weight (retrieved 5+ times, used <20%):**

```sql
SELECT e.id, e.source, e.type, json_extract(e.payload_json, '$.summary') AS summary,
       s.retrieved_count, s.used_count
FROM event_recall_stats s
JOIN events e ON e.id = s.event_id
WHERE s.retrieved_count >= 5
  AND CAST(s.used_count AS REAL) / s.retrieved_count < 0.2
ORDER BY s.retrieved_count DESC
LIMIT 20;
```

These are candidates for re-summarization (the body might be fine; the summary embedding is weak).

**Queries that consistently fail to find useful results:**

```sql
SELECT * FROM unhelpful_queries;
```

These are candidates for retrieval tuning (re-rank, hybrid search, query rewriting).

**A specific session's retrievals:**

```sql
SELECT t.id, t.query, t.returned_count, t.used_count, t.closed_at
FROM recall_traces t
WHERE t.context_session_id = ?
ORDER BY t.ts;
```

## Unsafe-write filter

The recall layer is read-side. For the write side, `scripts/policy_check.py` is a callable module that catches:

- Payloads over 64 KB
- Missing or oversized `summary`
- API keys / tokens in any string field (AWS, GitHub PAT, OpenAI, Anthropic, Stripe, Twilio, Slack, Linear, ClickUp/Vapi, generic `*_secret`/`*_token` JSON keys)
- Raw transcripts dumped under non-transcript sources
- Code blobs over 50 lines

Wire it into `record_event.py`:

```python
from policy_check import check, PolicyError

try:
    payload = check(source=args.source, etype=args.etype, payload_raw=payload)
except PolicyError as e:
    sys.stderr.write(f"policy_check rejected: {e}\n")
    sys.exit(2)
```

Or for "warn but don't block" behavior:

```python
payload, warnings = check(..., mode="sanitize")
for w in warnings:
    sys.stderr.write(f"warn: {w}\n")
```

Run `python scripts/policy_check.py` with a JSON payload on stdin to smoke-test.

## What recall traces and policy_check have in common

Both encode the same idea: **the brain should know how it's being used, not just what it contains.** Traces tell you which retrievals mattered. Policy_check tells you which writes shouldn't have happened. Together they make the brain self-correcting instead of just self-accumulating.
