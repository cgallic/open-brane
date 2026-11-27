# How Open Brane compares

This page compares Open Brane with two common ways to give AI agents memory: hosted vector memory services, and plain markdown files such as `CLAUDE.md` or `AGENTS.md`. Services in the middle column differ from each other, and many change quickly, so that column describes the common pattern rather than any one product. Check a specific service's current documentation before deciding.

## Summary

| | Open Brane | Hosted vector memory services | Markdown notes / `CLAUDE.md` files |
|---|---|---|---|
| Needs an API key or model | No. Semantic search is an optional add-on (Ollama + Qdrant). | Usually, for embeddings and often for an LLM that extracts memories. | No |
| Storage | One SQLite file (`events.db`) plus local sidecar files, on a machine you control. | The provider's database, or a vector database you run for a self-hosted edition. | Files in your repository or home directory. |
| How agents connect | MCP over stdio or Streamable HTTP, plus an authenticated HTTP API. | SDK or REST API; some offer an MCP server. | The harness loads the file into the prompt. |
| Search | Ranked full-text search (SQLite FTS5): words in any order, best match first, quoted phrases, and notes with any of the words when none contain all of them. Optional vector search. | Semantic similarity over embeddings. | The agent reads the whole file, or uses grep. |
| Multi-agent handoff | Role task board: `post_task`, `claim_task` with leases, `settle_task` requiring proof for done. | Agents can share a memory store; task handoff is usually left to you. | Agents edit the same file; no claims or leases. |
| Resuming a project | Saved checkpoints with an explicit next action, revision history, and stale-save rejection. | Depends on what was extracted into memory. | Whatever was last written to the file. |
| Audit trail | Append-only event log. Corrections are new events. Checkpoints and documents keep revisions. | Varies; extracted memories may be updated or merged in place. | Git history, if the file is committed. |
| Self-hosted | Yes: local Python or Docker. | Some services offer a self-hosted edition. | Yes |
| Browser UI | Local page for notes, search, saved projects, and documents. | Usually a hosted dashboard. | Your editor. |
| License | MIT | Varies | Not applicable |

## When Open Brane fits

- You want several agents or harnesses (Claude Code, Cursor, Codex, scripts) to read and write the same project history.
- You want to run without an embedding model, API key, or second database.
- You want every write kept, so you can see who recorded what and when, and rebuild views from the log.
- You want agents to hand work to each other by role, with a claim that expires if an agent crashes and a settlement that has to cite proof.

## When something else fits better

- **Fuzzy recall across different wording.** Full-text search finds notes that contain your words. If your queries and notes rarely share words, a vector memory service, or Open Brane's optional semantic search, will recall more.
- **Automatic memory extraction.** Open Brane stores what agents and people explicitly save. It does not run an LLM to decide what to remember from a conversation.
- **A few static instructions.** If an agent only needs a short list of rules for one repository, a `CLAUDE.md` or `AGENTS.md` file is simpler.
- **Multi-user access control.** The HTTP token grants full owner access. Separate trust boundaries need separate deployments. A hosted service with per-user accounts may suit a team better.

## Using them together

These options can be combined. A repository can keep its standing instructions in `AGENTS.md` while agents record decisions, checkpoints, and task outcomes in Open Brane. Open Brane's optional embedding step (`scripts/embed_events.py`) indexes the same events in Qdrant, and the vector index can be deleted and rebuilt from `events.db` at any time.

See the [README](../README.md) for setup, [Your workspace](WORKSPACE.md) for saving and resuming work, and [ROLES.md](ROLES.md) for the task board.
