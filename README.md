# Open Brane

**Your projects, research, and decisions, available to every connected agent.**

Open Brane is a self-hosted event-log brain for keeping work across sessions and tools. Save notes, resume project checkpoints, open versioned documents, and find what happened without rebuilding context from scratch. Your browser opens to saved work and recent activity; customer follow-up tools are optional.

One SQLite event store, owner-controlled storage, authenticated HTTP, and MCP. Core recording and text search need no LLM or vector database. Existing ingestion, research, governance, CRM, checkpoints, and document tools remain available.

**For everyday use:** open a saved project, read its explicitly saved next action, or copy its context into another session. Agents can call `workspace_overview`, `read_saved_project`, and `read_saved_document` to find the same work. See [Your workspace](docs/WORKSPACE.md) for the read-only dashboard and its limits.

**Customer workflow example:** after [setup](#quickstart), run `python3 brane.py demo`. Two scripted MCP clients share a call summary, check recent outreach and stale contact facts, and reject a stale document edit. This optional demo uses temporary synthetic data and sends nothing. See the [customer handoff guide](docs/CUSTOMER-HANDOFF.md).

## Quickstart

**Python 3.11+ and Git are the only prerequisites.** No model, API key, Ollama, or Qdrant is needed for notes, text search, CRM, or MCP.

```bash
git clone https://github.com/cgallic/open-brane.git
cd open-brane
python3 brane.py setup
python3 brane.py serve
```

Open **http://127.0.0.1:7778**, paste the token from `data/http-token`, and start saving and searching notes. Setup creates the virtual environment, installs the core dependencies, generates a private access token, and initializes your database. Running setup again preserves your configuration, token, and events.

On Windows, replace `python3` with `py -3.11`. No environment activation is required.

### Docker

Only Docker with Compose is required:

```bash
git clone https://github.com/cgallic/open-brane.git
cd open-brane
docker compose -f compose.json up --build -d --wait
docker compose -f compose.json exec brane cat /data/http-token
```

Open **http://127.0.0.1:7778** and paste that token. The container runs as a non-root user; its named volume preserves your database, credentials, checkpoints, and artifacts. Only localhost is published by default. JSON is used for the Compose configuration.

### Connect an agent

For a local stdio MCP client, generate ready-to-paste configuration with absolute paths:

```bash
python3 brane.py mcp-config
```

The browser also includes optional **Customer follow-up tools** to look up contact facts, interaction history, and follow-up checks. Connect your existing workflow through the [authenticated customer API](docs/CUSTOMER-HANDOFF.md).

For an HTTP MCP client that supports Bearer headers, use `http://127.0.0.1:7778/mcp` with `Authorization: Bearer YOUR_ACCESS_TOKEN`. This is Streamable HTTP, not the older SSE proxy. Both interfaces expose the same tools and database. The HTTP token grants full owner access; separate trust boundaries need separate deployments. This is not an OAuth login flow.

Check your installation:

```bash
python3 brane.py doctor
```

See **[Self-hosting](docs/SELF-HOSTING.md)** for a VPS, HTTPS, backups, upgrades, token rotation, troubleshooting, and optional semantic search. The existing [starter guide](docs/STARTER.md) covers advanced configuration and provider contracts; existing `starter.py` commands remain supported.

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
- **Owner-controlled access.** Use local stdio by default. The included HTTP server requires an owner Bearer token; business filters are not multi-user authentication.
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
