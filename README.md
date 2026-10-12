# Open Brane

**An event-log brain for people who keep losing context.**

Your calendar, your Drive, your Stripe, your git commits, your Claude sessions, your Fathom recordings — they all live in different tools, with different schemas, and every time you want to reason across them you copy-paste or give up. Open Brane is the minimum viable fix: one SQLite file, one append-only table, one write path, one MCP server, and a pattern for writing adapters that takes about an hour per source.

No framework. No ORM. No workflow engine. Fits on a USB stick.

## The idea in one paragraph

Every source you care about gets normalized into **one append-only SQLite table called `events`**. That table is the source of truth. Everything downstream — Obsidian pages, dashboards, semantic search, agent context packets — can be rebuilt from it. One write path (`record_event.py`). Corrections append new events. Append-only is an application convention; keep backups of the database and external attachments.

Agents use a local [MCP](https://modelcontextprotocol.io) server to write and retrieve events, report recall usage, propose reasoning edges, and work with contacts, deals, and outreach history. The reusable starter loads this existing server from one JSON configuration. See [the starter guide](docs/STARTER.md) for configuration and provider contracts.

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
│   ├── starter.py             ← validate / initialize / diagnose / run / embed
│   ├── runtime_config.py      ← deployment JSON → existing environment API
│   ├── crm.py                 ← event-backed contacts, deals, outreach checks
│   ├── crm_providers.py       ← optional read-only external contact facts
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
│   ├── starter.example.json   ← copy and replace your deployment settings
│   ├── starter.schema.json    ← configuration shape
│   ├── gdrive-sources.json.example
│   ├── git-repos.json.example
│   └── web-sources.json.example
├── systemd/
│   └── brain-mcp.service      ← template (swap BRAIN_ROOT + bind host)
└── docs/
    ├── STARTER.md             ← portable setup, MCP tools, CRM provider ABI
    ├── ADAPTERS.md            ← how to write a new ingest adapter
    ├── STRUCTURED-OUTPUTS.md  ← the skill-output-contract pattern
    ├── CONDUCTOR-FLOW.md      ← long-running multi-agent orchestration
    ├── GOVERNANCE.md          ← agent-memory trust layer (apply with governance.sql)
    ├── RECALL-TRACE.md        ← closing the retrieval feedback loop
    └── EDGES.md               ← typed reasoning edges, supports/contradicts/etc.
```

## Quickstart

Python 3.11+ is required. The default configuration runs the event store and CRM tools without Ollama or Qdrant.

Clone and create an environment:

```bash
git clone https://github.com/cgallic/open-brane.git
cd open-brane
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config/starter.example.json config/starter.json
```

On Windows PowerShell, use these environment commands after cloning:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config/starter.example.json config/starter.json
```

Replace `business_id` in `config/starter.json` with your business identifier. The example database path resolves to `data/events.db` inside this copy. Then run, on either platform:

```text
python scripts/starter.py validate --config config/starter.json
python scripts/starter.py init --config config/starter.json
python scripts/starter.py doctor --config config/starter.json
python scripts/starter.py run --config config/starter.json
```

`init` creates the database and governance, recall, and edge sidecars. `doctor` checks the local database and, when enabled, vector services. `run` starts the stdio MCP server and waits for a client; no web page opens. Configure your MCP client with the environment's absolute Python path and the absolute script/config paths shown in [STARTER.md](docs/STARTER.md).

For another system, copy the public package or clone again, create a separate database, and replace `config/starter.json` and any external provider settings. Keep credentials in environment variables. This is an owner-controlled deployment: CRM tools bind to one business, while general Brain tools can read its entire database. Use separate installations for separate trust boundaries.

## The three hard problems this solves

**1. Context that doesn't drift between agents.**
Agents don't pass markdown to each other. They write events. They read events. The markdown (or Google Doc, or Notion page, or Obsidian vault) is a rendered view, always rebuildable. If two agents disagree, you diff events.

**2. Definitions that don't fight each other.**
Store raw source data, compute derived metrics on read. Stripe's MRR calculation and yours will differ — that's fine. The events are raw. A definition layer query joins over them when someone asks. You never pre-compute and store a derived number that drifts from source.

**3. Pipelines that break silently.**
Adapters should use stable source identities and explicitly deduplicate retries. The canonical writer supports `--dedupe-key`; legacy writes without a key append again, and existing adapters do not all deduplicate. The vector embedder skips already indexed event IDs. `health_check.py --record` writes its own probe as an event, so you can query your uptime history from the same table.

## Design principles

- **Append-only.** No UPDATEs. No DELETEs. Corrections reference prior event IDs.
- **One write path.** Everything writes through `record_event.py`. Everything.
- **Scripts are pure functions.** Read, compute, write, exit. No state machines, no background workers.
- **Cron is the orchestrator.** If a script fails, the next tick retries. No queues, no DLQs.
- **Owner-controlled access.** Use local stdio by default. The optional HTTP/SSE template needs its own access controls; business filters are not multi-user authentication.
- **Views are rebuildable.** Obsidian pages, wiki pages, vector DB — all derived. Delete and rebuild anytime.

## Stack

| Component | Why |
|---|---|
| SQLite (WAL) | A portable event store. Back up and verify your database like any other persistent data. |
| Ollama + nomic-embed-text | Optional semantic search. The model and dimensions are configurable. |
| Qdrant | Optional vector index. The included scripts use the Qdrant API; another vector engine requires an adapter. |
| MCP (Model Context Protocol) | The lingua franca for agent↔tool interfaces. Claude Code, Cursor, custom agents all speak it. |
| Python 3.11+ | stdlib does 90% of the work. External deps: httpx, qdrant-client, mcp. |

## Optional sidecar schemas

The core is one `events` table. That's the contract. But once you have agents *writing* events (not just adapters), three additional schemas are worth applying:

| Schema | What it adds | Doc |
|---|---|---|
| `schemas/governance.sql` | Per-event provenance + review state + use-policy. Enforces "agents cannot self-promote their own writes to instruction-grade" via a SQL CHECK constraint. | `docs/GOVERNANCE.md` |
| `schemas/recall_traces.sql` | Every retrieval writes a trace; agent reports back which items it used. Surfaces dead-weight events and bad queries. | `docs/RECALL-TRACE.md` |
| `schemas/edges.sql` | Typed reasoning edges (supports / contradicts / supersedes / depends_on) with weight, decay, and temporal validity. Powers a `questioned_decisions` view: "active decisions that have surfaced contradictions". | `docs/EDGES.md` |

All three are additive and idempotent. The starter applies them in dependency order; manual installations can apply them à la carte. `scripts/policy_check.py` checks known secret patterns, oversized payloads, and code blobs before writes. It is not a complete sensitive-data scanner.

## Extending

**To add a source** (ClickUp, Stripe, Fathom, Slack, anything): copy `scripts/ingest_gdrive.py`, rewrite the fetch loop, pick a stable dedup fingerprint for the `actor` field, add a cron line. See `docs/ADAPTERS.md`.

**To add a view** (dashboard, PDF export, Slack digest): query `events.db`, format, write. No brain-side changes needed.

**To add an MCP tool**: edit `scripts/mcp_server.py`, add a `@mcp.tool()` function, restart the service.

**To add a daily reflection layer**: see `docs/PATTERN-SCANNER.md` — a four-stage loop (scan → rate → research → promote) that surfaces cross-project tensions, lets you rate them with one click, and auto-suppresses noise. Reference impl, not yet ported as standalone scripts.

## License

MIT. See `LICENSE`.

## Credits

Built as the persistent-memory layer for a personal agent mesh.

## Related links

- [MeetKai](https://meetkai.xyz) — the operator layer behind Kai CMO workflows.
- [KaiCalls](https://kaicalls.com) — AI voice agents for small-business phone answering and lead capture.
- [Connor Gallic](https://connorgallic.com) — founder building Kai, KaiCalls, and AI automation systems.


---

*Built and maintained by [Connor Gallic](https://pr.linkedin.com/in/cgallic) — connect on LinkedIn.*
