# Your everyday workspace

Open Brane remains a general-purpose Brain for projects, research, notes, decisions, and agent handoffs. The customer handoff integration is optional. Nothing in the workspace view migrates, overwrites, seeds, or reclassifies existing records.

After setup, start `python3 brane.py serve` and unlock the browser. The home screen shows:

- Saved project checkpoints, most recently updated first. A saved `summary` or `goal` appears when present. A `next_action` appears only if that string was explicitly stored in the checkpoint payload.
- Saved documents, with their current revisions. Open a document to read its content; the server checks its hash before returning it.
- Quick note capture, literal text search, and recent events across sources.
- Customer follow-up tools inside a collapsed optional section.

Open a project or document and use **Copy context** to bring the saved content into another session. Nothing is sent to another service by that button. If the browser blocks clipboard access, select and copy the displayed text manually. Use Refresh saved work to pick up new checkpoints and documents saved by agents.

The screen reads the configured deployment only. It does not automatically connect to a different personal Brain server, import cloud accounts, or prove that an external integration is healthy. Empty sections mean there are no matching saved records in this deployment; they do not mean your projects or files do not exist elsewhere. No personal project names or sample memories are inserted.

## Agent tools

- `workspace_overview(limit=20, since=None)` returns recent recorded events, project summaries, saved next actions, and document metadata. Each section is limited to 1–100 items. It does not infer progress, priorities, overdue work, or completion.
- `read_saved_project(project_id)` reads the exact current saved checkpoint payload and its save timestamp.
- `read_saved_document(artifact_id)` reads the current document revision and checks its SHA-256 against the revision metadata.

These tools use the same full-owner trust boundary as the existing general event-query tools. They do not create customer connection records or change connection last-used timestamps. Project and document reads are filtered to the configured owner. Existing connection-aware checkpoint and artifact APIs remain supported for writes and revision management.

A practical request to a connected agent is: “Read my workspace, open the saved checkpoint for the project I name, and tell me its saved next action. Distinguish saved facts from your suggestions.” This is an example prompt; the dashboard itself does not call a model.

## HTTP equivalents

All routes require the existing full-owner Bearer token:

- `GET /api/workspace?limit=20`
- `GET /api/workspace?since=2026-10-20T00:00:00Z`
- `GET /api/workspace/project?project_id=YOUR_PROJECT_ID`
- `GET /api/workspace/document?artifact_id=YOUR_ARTIFACT_ID`

URL-encode query values. Unknown query parameters are rejected. `since` must explicitly use UTC and filters newly **recorded** events by `ingested_at`, including imported events whose original event timestamp is older. It does not filter the saved projects or documents. Recent events are ordered by event ID, newest first. The normal event-search tools are still available for older records.

The event log and workspace sidecar are read in separate transactions; this is not an atomic snapshot across both files. Missing optional workspace storage is reported as `workspace_available: false`; it is not silently created. Opening a document outside the configured artifact root or with a mismatched hash fails instead of returning content. All of these new routes and tools are read-only.
