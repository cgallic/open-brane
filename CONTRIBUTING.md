# Contributing

Open Brane is a pattern more than a product. Most valuable contributions are new adapters and new renderers — not core changes.

## Contributing a new adapter

1. Fork.
2. Copy `scripts/ingest_gdrive.py` as a starting point. Rename to `scripts/ingest_<source>.py`.
3. Follow the rules in `docs/ADAPTERS.md`:
   - One source per adapter
   - Dedup via `actor` column
   - Write through `record_event.py`, never direct SQL
   - Emit JSON summary on stdout
   - Idempotent re-run
4. Add a `.example` config file if the adapter needs config.
5. Document it in `README.md` adapter table.
6. Open a PR.

Adapters worth writing (open to PRs): ClickUp, Linear, Jira, Stripe, Fathom, Otter, Gmail, Google Calendar, Slack, Discord, Notion, Airtable, HubSpot, Shopify, GitHub Issues, Twitter/X, LinkedIn, YouTube (channel analytics).

## Contributing a renderer

Renderers turn events into views. They live outside this repo — each renderer should be its own small repo or script. If you build one that might be generally useful (e.g. "events → Obsidian vault compiler"), link it from the README.

## Contributing to the core

The core (record_event, query_events, embed_events, semantic_search, mcp_server) is meant to stay small. Changes that add meaningful complexity will be pushed back unless they solve a clearly-scoped problem.

The invariants that will NOT change:
- Single-table append-only schema
- One write path (`record_event.py`)
- SQLite as the source of truth
- No delete path
- MCP as the agent interface

Good core changes:
- Performance improvements that don't add abstractions
- Bug fixes
- Better error messages
- Tests (there aren't any yet — PRs welcome)
- Documentation improvements

## Style

- Python 3.11+, stdlib-first, minimal dependencies.
- No classes where a function will do.
- No logging framework — `print(..., file=sys.stderr)` is fine.
- No YAML — JSON everywhere for config.
- No pytest fixtures — stdlib `unittest` if tests are needed.

## Questions

Open a GitHub issue. Describe what you're trying to do and what you've tried. Don't ask permission to contribute — just open the PR.
