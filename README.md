# Open Brane

**An event-log brain for people who keep losing context.**

Your calendar, your Drive, your Stripe, your git commits, your Claude sessions, your Fathom recordings — they all live in different tools, with different schemas, and every time you want to reason across them you copy-paste or give up. Open Brane is the minimum viable fix: one SQLite file, one append-only table, one write path, one MCP server, and a pattern for writing adapters that takes about an hour per source.

No framework. No ORM. No workflow engine. Fits on a USB stick.

## The idea in one paragraph

Every source you care about gets normalized into **one append-only SQLite table called `events`**. That table is the source of truth. Everything downstream — Obsidian pages, dashboards, semantic search, agent context packets — is rebuilt from it. One write path (`record_event.py`). Re-run anything and nothing breaks. If a view gets corrupted, delete it and rebuild from events. The brain itself never gets corrupted because it's append-only.

Agents hit a local [MCP](https://modelcontextprotocol.io) server, never the DB directly. They can `record_event`, `query_events`, or `semantic_search` — and that's the whole API.

## What you get

```
open-brane/
├── README.md                  ← this file
├── ARCHITECTURE.md            ← data flow, schema, failure patterns
├── SCHEMA.sql                 ← the one table that runs the whole system
├── LICENSE                    ← MIT
├── requirements.txt
├── scripts/
│   ├── record_event.py        ← the only write path
│   ├── query_events.py        ← filtered reader
│   ├── embed_events.py        ← events → Qdrant (incremental, idempotent)
│   ├── semantic_search.py     ← vector search + payload join
│   ├── health_check.py        ← probe Ollama + Qdrant + events.db
│   ├── policy_check.py        ← unsafe-write filter (secrets, size, code blobs)
│   ├── ingest_gdrive.py       ← Drive extract → events (canonical adapter)
│   ├── ingest_claude.py       ← Claude Code sessions → events
│   ├── ingest_git.py          ← git log / gh api → events
│   ├── gdrive_extractor.py    ← runs on the machine with Drive mounted
│   └── mcp_server.py          ← MCP stdio server
├── schemas/                   ← optional sidecar schemas, apply à la carte
│   ├── governance.sql         ← provenance / review / use-policy invariant
│   ├── recall_traces.sql      ← log every retrieval + which items the agent used
│   └── edges.sql              ← typed reasoning edges with temporal validity
├── config/
│   ├── gdrive-sources.json.example
│   ├── git-repos.json.example
│   └── web-sources.json.example
├── systemd/
│   └── brain-mcp.service      ← template (swap BRAIN_ROOT + bind host)
└── docs/
    ├── ADAPTERS.md            ← how to write a new ingest adapter
    ├── STRUCTURED-OUTPUTS.md  ← the skill-output-contract pattern
    ├── CONDUCTOR-FLOW.md      ← long-running multi-agent orchestration
    ├── GOVERNANCE.md          ← agent-memory trust layer (apply with governance.sql)
    ├── RECALL-TRACE.md        ← closing the retrieval feedback loop
    └── EDGES.md               ← typed reasoning edges, supports/contradicts/etc.
```

## Quickstart

On Linux (Ubuntu/Debian):

```bash
# System deps
sudo apt install -y sqlite3 python3-venv
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text
docker run -d --name qdrant -p 6333:6333 \
    -v $HOME/qdrant_storage:/qdrant/storage qdrant/qdrant

# Clone and install
git clone https://github.com/cgallic/open-brane /var/lib/open-brane
cd /var/lib/open-brane
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Initialize the DB
sqlite3 events.db < SCHEMA.sql

# Smoke test
export BRAIN_DB=$(pwd)/events.db
echo '{"summary":"first event"}' | ./scripts/record_event.py \
    --source manual --type note --payload-stdin
./scripts/query_events.py --limit 5
./scripts/health_check.py
```

If `health_check.py` prints `"all_healthy": true`, you're running. Wire up adapters from there — see `docs/ADAPTERS.md` for the pattern.

## The three hard problems this solves

**1. Context that doesn't drift between agents.**
Agents don't pass markdown to each other. They write events. They read events. The markdown (or Google Doc, or Notion page, or Obsidian vault) is a rendered view, always rebuildable. If two agents disagree, you diff events.

**2. Definitions that don't fight each other.**
Store raw source data, compute derived metrics on read. Stripe's MRR calculation and yours will differ — that's fine. The events are raw. A definition layer query joins over them when someone asks. You never pre-compute and store a derived number that drifts from source.

**3. Pipelines that break silently.**
Every adapter is idempotent. Re-run is a no-op. Cron is the retry loop. `health_check.py --record` writes its own probe as an event, so you can query your own uptime history from the same table.

## Design principles

- **Append-only.** No UPDATEs. No DELETEs. Corrections reference prior event IDs.
- **One write path.** Everything writes through `record_event.py`. Everything.
- **Scripts are pure functions.** Read, compute, write, exit. No state machines, no background workers.
- **Cron is the orchestrator.** If a script fails, the next tick retries. No queues, no DLQs.
- **Network boundary = auth.** Bind MCP to localhost or a tailnet IP. Don't build an auth layer you'll regret.
- **Views are rebuildable.** Obsidian pages, wiki pages, vector DB — all derived. Delete and rebuild anytime.

## Stack

| Component | Why |
|---|---|
| SQLite (WAL) | Fits on a USB stick. Handles 10k writes/sec. Never an operational problem at personal/SMB scale. |
| Ollama + nomic-embed-text | Local, free, sovereign. 768-dim vectors, good enough for short factual events. |
| Qdrant | Single Docker container, handles persistence itself. Swap for pgvector if you prefer. |
| MCP (Model Context Protocol) | The lingua franca for agent↔tool interfaces. Claude Code, Cursor, custom agents all speak it. |
| Python 3.11+ | stdlib does 90% of the work. External deps: httpx, qdrant-client, mcp. |

## Optional sidecar schemas

The core is one `events` table. That's the contract. But once you have agents *writing* events (not just adapters), three additional schemas are worth applying:

| Schema | What it adds | Doc |
|---|---|---|
| `schemas/governance.sql` | Per-event provenance + review state + use-policy. Enforces "agents cannot self-promote their own writes to instruction-grade" via a SQL CHECK constraint. | `docs/GOVERNANCE.md` |
| `schemas/recall_traces.sql` | Every retrieval writes a trace; agent reports back which items it used. Surfaces dead-weight events and bad queries. | `docs/RECALL-TRACE.md` |
| `schemas/edges.sql` | Typed reasoning edges (supports / contradicts / supersedes / depends_on) with weight, decay, and temporal validity. Powers a `questioned_decisions` view: "active decisions that have surfaced contradictions". | `docs/EDGES.md` |

All three are additive and idempotent — apply à la carte. `scripts/policy_check.py` is the matching write-side filter: catches secrets, oversized payloads, and code blobs before they hit the brain.

## Extending

**To add a source** (ClickUp, Stripe, Fathom, Slack, anything): copy `scripts/ingest_gdrive.py`, rewrite the fetch loop, pick a stable dedup fingerprint for the `actor` field, add a cron line. See `docs/ADAPTERS.md`.

**To add a view** (dashboard, PDF export, Slack digest): query `events.db`, format, write. No brain-side changes needed.

**To add an MCP tool**: edit `scripts/mcp_server.py`, add a `@mcp.tool()` function, restart the service.

**To add a daily reflection layer**: see `docs/PATTERN-SCANNER.md` — a four-stage loop (scan → rate → research → promote) that surfaces cross-project tensions, lets you rate them with one click, and auto-suppresses noise. Reference impl, not yet ported as standalone scripts.

## License

MIT. See `LICENSE`.

## Credits

Built as the persistent-memory layer for a personal agent mesh. Inspired by Karpathy's wiki-style knowledge indexes, the [Ralph autonomous-loop pattern](https://ghuntley.com/ralph/), and a general exhaustion with frameworks that do 20% of what you need in 80% of the complexity.

## Related links

- [MeetKai](https://meetkai.xyz) — the operator layer behind Kai CMO workflows.
- [KaiCalls](https://kaicalls.com) — AI voice agents for small-business phone answering and lead capture.
- [Connor Gallic](https://connorgallic.com) — founder building Kai, KaiCalls, and AI automation systems.


---

*Built and maintained by [Connor Gallic](https://pr.linkedin.com/in/cgallic) — connect on LinkedIn.*
