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

The event log and workspace sidecar are read in separate transactions; this is not an atomic snapshot across both files. Missing optional workspace storage is reported as `workspace_available: false`; it is not silently created. Opening a document outside the configured artifact root or with a mismatched hash fails instead of returning content. The discovery routes and tools above are read-only; explicit session writes are described below.

## Save → resume → continue

Use **Save a session** in the browser to create a project. Record what changed, a next action, decisions, and blockers. Source event IDs and document IDs are optional; they must exist in this deployment. Open an existing project first to update it. Other checkpoint fields (including custom agent context) are retained. Saving replaces the session fields and source list with the supplied values.

A save uses the revision you opened. If another session updates the project first, the browser reports a conflict and keeps your draft. Copy the draft before reopening the project, compare it with the latest saved context, then reconcile and save. Drafts are held in tab memory and cleared by locking or reloading. They are not automatically backed up.

Connected agents use the same workflow:

1. `resume_work_session(project_id)` returns the saved payload, current revision, source event summaries, document revision references, and warnings. Explicit decisions, blockers, and next action are in `payload`; the server does not infer completion.
2. Continue the work using the original tools. Read referenced events with the event tools, and use `read_saved_document(artifact_id, revision)` for the exact saved source version.
3. `save_work_session(project_id, summary, next_action, expected_revision, connection_id, decisions=[], blockers=[], event_ids=[], artifact_ids=[])` saves a new revision. Use `expected_revision=0` only for a new project. For existing projects, use the revision returned by resume. Supply the full source list you want retained.

Documents are pinned to their revision and SHA-256 at save time. Resume flags newer document versions and missing or corrupt references. Event summaries are limited to 2,000 characters; they are saved observations, not independently verified evidence. Document content is read separately, keeping the handoff packet smaller. Each session supports up to 50 event references and 20 documents. References apply to this deployment, not portable IDs across installations.

Every checkpoint write now retains revision history. `project_checkpoint_history(project_id, connection_id, limit=50)` reads previous payloads; `restore_project_checkpoint(project_id, revision, expected_revision, connection_id)` restores an earlier payload as a **new** revision. Existing checkpoints appear as revision 1 and are preserved before their first edit. History begins at this upgrade; overwritten checkpoints from older versions cannot be recovered. Existing `save_project_checkpoint` callers remain supported; pass `expected_revision` to opt into stale-save protection. The new session workflow always requires it.

HTTP: `GET /api/workspace/session?project_id=...` resumes; `POST /api/workspace/session` saves with the same fields as `save_work_session` except `connection_id`. The full-owner token authorizes the browser write; the server records a reusable browser connection for provenance, not separate authentication. Stale saves return HTTP 409. `GET /api/workspace/document?artifact_id=...&revision=1` reads a historical document. Discovery and resume remain read-only; the session POST is an explicit write through the canonical checkpoint path. Browser history/restore controls are not included; recovery is available through MCP.
