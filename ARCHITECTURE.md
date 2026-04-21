# Architecture

## Philosophy

Minimal. Sovereign. Deterministic. No frameworks, no ORM, no magic.

Every script is a pure function: read from disk or API, compute, write events via one path, exit. Re-run anything safely. Audit with `sqlite3`.

## The core loop

```
                  ┌─────────────────────────────────────┐
   SOURCES ───▶   │         Ingest Adapters              │  ──▶  events.db
                  │  (one per source, all idempotent)    │       (source of truth)
                  └─────────────────────────────────────┘            │
                                                                      │
                     ┌────────────────────────────────────────────────┼──────────┐
                     ▼                                                 ▼          ▼
              ┌─────────────┐                             ┌──────────────┐  ┌────────────┐
              │ Compile     │                             │ Embed        │  │ MCP Server │
              │ (events →   │                             │ (events →    │  │ (tools for │
              │  Obsidian)  │                             │  Qdrant)     │  │  agents)   │
              └─────────────┘                             └──────────────┘  └────────────┘
                     │                                            │                │
                     ▼                                            ▼                │
              Human-readable                             Semantic search ◀─────────┘
              (Obsidian vault)                           (Qdrant collection)
```

**Directions of data:**

- Sources → adapters → `events.db` (write-only, append-only).
- `events.db` → compile step → Obsidian (rebuildable view).
- `events.db` → embed step → Qdrant (rebuildable cache).
- Agents → MCP server → `events.db` + Qdrant (read + write via `record_event`).

Obsidian and Qdrant are **views**. If either gets corrupted, delete it and rebuild from `events.db`. Never the other way around.

## The schema

One table. That's the whole data model.

```sql
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,  -- ISO 8601 UTC: when the event happened
    source          TEXT    NOT NULL,  -- e.g. clickup, gdrive, stripe, claude, git
    type            TEXT    NOT NULL,  -- e.g. task, document, invoice.paid, session, commit
    actor           TEXT,               -- person/entity + dedup fingerprint
    payload_json    TEXT    NOT NULL,  -- anything. MUST include "summary" field.
    attachment_uri  TEXT,               -- file://, s3://, gdrive://... for blobs
    ingested_at     TEXT    NOT NULL   -- when the row hit the DB
);

CREATE INDEX idx_events_ts          ON events(ts);
CREATE INDEX idx_events_source_type ON events(source, type);
CREATE INDEX idx_events_actor       ON events(actor);
CREATE INDEX idx_events_ingested    ON events(ingested_at);
```

**Invariants:**

1. **Append-only.** No updates. No deletes. Corrections are new events that reference old `id`s in their payload.
2. **`ts` ≠ `ingested_at`.** `ts` is when it happened; `ingested_at` is when you found out. Lets you replay history.
3. **`actor` as dedup key.** Adapters write a fingerprint here (commit SHA, file hash, ClickUp task ID). Unique constraint via `(source, actor)` check in adapter code. Re-run is idempotent.
4. **`payload_json` always has `summary`.** That's what the embedder indexes and what agents see first. Full payload is on demand.

Typical size: ~1-50 KB per event. 100k events ≈ 50 MB DB.

## Adapters

Every adapter follows the same shape:

```python
#!/usr/bin/env python3
"""
ingest_<source>.py — pull <source> into events.db.
"""
def main():
    known_actors = load_existing_actors_for_source("<source>")

    for item in fetch_from_source():
        actor = fingerprint(item)
        if actor in known_actors:
            continue

        payload = {
            "summary": short_human_text(item),
            # ... everything else
        }

        record_event(
            source="<source>",
            type=classify(item),
            actor=actor,
            ts=item.timestamp_iso,
            payload=payload,
        )
```

**Rules:**
- One source per adapter.
- Dedup via the `actor` column. Adapter picks the fingerprint (hash, ID, URL, whatever's stable).
- Write through `record_event.py` via subprocess, not direct SQL. That way the write path stays singular even as adapters multiply.
- Emit a JSON summary on stdout (`{ok, skipped, error, ...}`). Cron greps for errors.
- Exit 0 if the adapter ran cleanly (even with zero new items). Exit 1 only on infra failure.

**The canonical adapter pattern** is in `scripts/ingest_gdrive.py`. Copy it, rename, rewire the fetch step.

## Multi-machine sync

Sources live on multiple machines. A typical setup: your laptop has Google Drive mounted, maybe a second laptop for travel, and a Linux server (VPS or home box) that hosts `events.db` and the MCP server.

The sync pattern is **two-phase**:

```
LAPTOP                                    VPS (events.db lives here)
──────                                    ───
~/.claude/sessions/    ─── phase 1 ───▶   ~/brain/external/laptop/.claude/sessions/
~/.codex/sessions/       (tar + ssh)      ~/brain/external/laptop/.codex/sessions/
~/.gemini/tmp/                            ~/brain/external/laptop/.gemini/tmp/
G:/My Drive                               (raw snapshots, for eval & replay)

                       ─── phase 2 ───▶   events.db
                         (extractor on               (appended)
                          laptop → scp →
                          ingester on VPS)
```

**Phase 1** preserves raw files. Re-ingestion is always possible.

**Phase 2** normalizes into events. Extractor runs locally (has access to Google Drive API / local files), emits JSONL, scp to VPS, ingester on VPS loads into `events.db`.

Orchestrated by `scripts/sync-all.sh`. Windows Task Scheduler or cron fires it every 12 hours.

**Why two phases:** If phase 2 fails (bad parse, schema drift), you re-extract from the raw snapshot. No data loss.

**Connection:** I use Tailscale for `laptop ↔ agent-box` because it's free, encrypted, works behind NAT. You'll use Tailscale or straight SSH over your VPS's public IP with key auth.

## The MCP server

One service. The only way agents interact with the brain.

Tools exposed (names match Python functions in `mcp_server.py`):

| Tool | What it does |
|---|---|
| `record_event(source, type, payload, [actor], [ts])` | Append one event. The write API. |
| `query_events(source?, type?, actor?, since?, until?, limit?)` | Filtered SQL. Returns list of events. |
| `semantic_search(query, limit?, source?)` | Embed query, cosine top-k from Qdrant, join payload from events.db. |
| `get_context_for_task(task)` | Semantic search + recency + actor bias → one context packet for the agent. |
| `build_memory_packet(type, subject)` | Longer-form packet: events + wiki summaries + decision log refs. |
| `compile_journal(date?)` | Recompile a day's journal from events. |
| `health_check()` | Probe infra. |

**Deployment:** `systemd/brain-mcp.service` wraps `mcp-proxy` to expose stdio MCP over HTTP/SSE. Agents connect to `http://<tailscale-ip>:7778`. On your VPS that'll be the Tailscale IP of the host (or `127.0.0.1` if agents run on the same box).

**Why one server:** Every agent hits the same API. No divergent access patterns. Rate limits, auth, observability all in one place.

## Failure & retry

**There is no retry code in the scripts.** Cron is the retry loop.

```
*/5 * * * * cd /var/lib/open-brane && ./scripts/ingest_clickup.py >> logs/cron.log 2>&1
*/5 * * * * cd /var/lib/open-brane && ./scripts/embed_events.py  >> logs/cron.log 2>&1
*/15 * * * * cd /var/lib/open-brane && ./scripts/health_check.py --record >> logs/cron.log 2>&1
```

**What breaks and what happens:**

- Ollama down → `embed_events.py` exits 1, nothing embedded this tick, next tick retries. Zero data loss.
- Qdrant corrupts → delete the collection, re-run `embed_events.py`. Rebuilds from `events.db`.
- ClickUp API 500s → adapter exits 1, cron retries. Idempotent dedup means no duplicates even if half the fetch succeeded.
- `events.db` locked → SQLite WAL handles it. Writers serialize in microseconds.
- `record_event.py` fails → adapter logs to stderr, skips that item, continues. Next cron tick retries (because `actor` isn't in `known_actors`).

**Monitoring:** `health_check.py --record` writes its own probe result as an event. You can query your infra uptime from `events.db`:

```sql
SELECT ts, json_extract(payload_json, '$.all_healthy') AS healthy
FROM events
WHERE source = 'health' AND type = 'probe'
ORDER BY ts DESC LIMIT 20;
```

## What lives where

```
/var/lib/open-brane/              ← BRAIN_ROOT
├── events.db                     ← the source of truth
├── .venv/                        ← Python virtualenv
├── scripts/                      ← this package
├── config/                       ← your config (from .example templates)
├── logs/
│   ├── cron.log
│   └── brain-mcp.log
├── external/                     ← raw phase-1 snapshots from remote machines
│   └── laptop/
│       ├── .claude/
│       └── G_My_Drive/
└── vault/                        ← Obsidian vault (compile target, not source)
    ├── clients/
    ├── projects/
    └── topics/
```

**Note on `vault/`:** If you compile events into an Obsidian vault (or any other human-readable view), the compiled files go here. Humans never write to `vault/` directly — it's always an output of a compile step, never a source. If you want to edit, edit the source event or write a new one.

## Things that are NOT in this architecture

- No message queue. Cron is the queue.
- No worker pool. Scripts run linearly, one per cron tick.
- No webhook router. Use a tiny FastAPI on `/webhooks/*` that calls `record_event`.
- No observability stack. `health_check.py` + `events.db` queries are the observability.
- No auth. MCP server binds to Tailscale IP or localhost — the network is the auth boundary.
- No multi-tenancy. Each client gets their own `events.db` if they need isolation. (You probably don't — they can share one DB with a `client` field in payloads.)

Keep it this small. The temptation is to layer orchestration on top. Don't. When something breaks, you want a single file to grep.

## Extending

**To add a new source:**
1. Copy `scripts/ingest_gdrive.py` → `scripts/ingest_<newsource>.py`.
2. Rewrite the fetch loop.
3. Pick a stable `actor` fingerprint.
4. Add cron line.
5. Done.

**To add a new compile target (PDF, HTML, Slack digest):**
1. Copy any `compile_*` pattern.
2. Query events, format output, write to target.
3. Cron it.

**To add a new MCP tool:**
1. Edit `mcp_server.py`. Add a `@mcp.tool()` function.
2. Restart the systemd service.
3. Agents see the new tool on next MCP handshake.

**To migrate to Postgres:**
1. Export: `sqlite3 events.db .dump > events.sql`
2. Clean up SQLite-specific syntax.
3. Import into Postgres.
4. Change `sqlite3.connect()` to `psycopg.connect()` in ~5 files.
5. Don't bother unless SQLite is actually hurting.

That's the whole system.
