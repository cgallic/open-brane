#!/usr/bin/env python3
"""
mcp_server.py — stdio MCP server exposing the event log to agents.

This is the ONLY interface agents should use. Direct DB access is reserved
for cron jobs and humans with sqlite3.

Tools exposed:
- record_event      — the write API
- query_events      — filtered SQL
- semantic_search   — vector search + payload join (writes recall trace if schema present)
- report_usage      — close the recall-trace feedback loop (recall_traces schema)
- propose_edge      — propose a typed reasoning edge (edges schema)
- health_check      — probe infrastructure
- search_events     — literal text search without a vector service
- check_outreach / outreach_history / find_contact / log_outreach — CRM contact context
- upsert_contact / upsert_deal / get_pipeline — event-backed CRM snapshots

Wrap with mcp-proxy if you want HTTP/SSE (see systemd/brain-mcp.service).

Dependencies:
    pip install mcp httpx qdrant-client
"""
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from mcp.server import FastMCP
import customer_runtime as customer
import artifacts

SCRIPT_DIR = Path(__file__).resolve().parent
BRAIN_ROOT = Path(os.environ.get("BRAIN_ROOT", SCRIPT_DIR.parent))
DB = Path(os.environ.get("BRAIN_DB", BRAIN_ROOT / "events.db"))
RECORD_SCRIPT = SCRIPT_DIR / "record_event.py"
SEMANTIC_SCRIPT = SCRIPT_DIR / "semantic_search.py"
HEALTH_SCRIPT = SCRIPT_DIR / "health_check.py"

mcp = FastMCP("brain")


def _run(cmd: list[str], stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
    )


@mcp.tool()
def record_event(
    source: str,
    type: str,
    payload: dict,
    actor: str | None = None,
    ts: str | None = None,
    attachment_uri: str | None = None,
    connection_id: str | None = None,
) -> dict:
    """Append one event to the event log. The canonical write path.

    Args:
        source: e.g. "clickup", "gdrive", "stripe", "manual"
        type: e.g. "task", "document", "invoice.paid", "note"
        payload: dict. MUST include a "summary" field for semantic indexing.
        actor: optional person/entity + dedup fingerprint
        ts: optional ISO 8601 UTC (default: now)
        attachment_uri: optional pointer to external blob
    """
    auth = customer._connection(connection_id, write=True) if connection_id else None
    if auth:
        payload = {**payload, "_server": {"owner_id": auth["owner_id"], "connection_id": auth["connection_id"]}}
    args = [
        sys.executable, str(RECORD_SCRIPT),
        "--source", source,
        "--type", type,
        "--payload-stdin",
    ]
    if actor:
        args.extend(["--actor", actor])
    if ts:
        args.extend(["--ts", ts])
    if attachment_uri:
        args.extend(["--attachment", attachment_uri])

    proc = _run(args, stdin_text=json.dumps(payload))
    if proc.returncode != 0:
        return {"status": "error", "error": proc.stderr.strip()}
    return json.loads(proc.stdout)


@mcp.tool()
def query_events(
    source: str | None = None,
    type: str | None = None,
    actor: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    order: str = "desc",
) -> list[dict]:
    """Filtered SQL query over events. Returns list of events."""
    if order.lower() not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    where, params = [], []
    if source:
        where.append("source = ?")
        params.append(source)
    if type:
        where.append("type = ?")
        params.append(type)
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if since:
        where.append("ts >= ?")
        params.append(since)
    if until:
        where.append("ts <= ?")
        params.append(until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, ts, source, type, actor, payload_json, attachment_uri, ingested_at "
        "FROM events" + clause + f" ORDER BY ts {order.upper()} LIMIT ?"
    )
    params.append(limit)

    conn = sqlite3.connect(DB)
    try:
        rows = list(conn.execute(sql, params))
    finally:
        conn.close()

    results: list[dict] = []
    for r in rows:
        results.append({
            "id": r[0],
            "ts": r[1],
            "source": r[2],
            "type": r[3],
            "actor": r[4],
            "payload": json.loads(r[5]) if r[5] else None,
            "attachment_uri": r[6],
            "ingested_at": r[7],
        })
    return results


@mcp.tool()
def semantic_search(
    query: str,
    limit: int = 10,
    source: str | None = None,
    agent: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Natural-language search over events via vector similarity.

    Returns {"trace_id": int|None, "results": [{...hit}, ...]}.
    If schemas/recall_traces.sql is applied, a trace row is written and
    trace_id is returned — call report_usage() afterwards to close the
    loop on which results you actually used. If the schema isn't there,
    trace_id is null and behavior matches the pre-trace version.
    """
    if os.environ.get("BRAIN_VECTORS_ENABLED", "true").lower() == "false":
        return {"status": "backend_unavailable", "error": "Vector search is disabled in deployment config", "trace_id": None, "results": []}
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    args = [
        sys.executable, str(SEMANTIC_SCRIPT),
        "--query", query,
        "--limit", str(limit),
    ]
    if source:
        args.extend(["--source", source])

    proc = _run(args)
    if proc.returncode != 0:
        return {"status": "backend_unavailable", "error": proc.stderr.strip(), "trace_id": None, "results": []}

    hits: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                hits.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    trace_id = _maybe_write_trace(query, hits, source, limit, agent, session_id)
    return {"trace_id": trace_id, "results": hits}


def _maybe_write_trace(
    query: str,
    hits: list[dict],
    source: str | None,
    limit: int,
    agent: str | None,
    session_id: str | None,
) -> int | None:
    """Write a recall_traces row + recall_items rows if the schema exists.

    Snapshots each item's governance row (if present) into use_policy_snapshot_json
    so we can reconstruct what the agent saw even if governance is updated later.
    """
    conn = sqlite3.connect(DB)
    try:
        # Schema present?
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recall_traces'"
        ).fetchone()
        if not row:
            return None

        cur = conn.execute(
            "INSERT INTO recall_traces (agent, query, source_filter, requested_limit, "
            "returned_count, context_session_id) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, query, source, limit, len(hits), session_id),
        )
        trace_id = cur.lastrowid

        for rank, hit in enumerate(hits, start=1):
            event_id = hit.get("id") or hit.get("event_id")
            if not event_id:
                continue
            similarity = hit.get("score") or hit.get("similarity")
            gov = conn.execute(
                "SELECT provenance_status, review_status, lifecycle_status, "
                "can_use_as_instruction, can_use_as_evidence, sensitivity_tier, "
                "confidence, memory_type "
                "FROM event_governance WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            snapshot = None
            if gov:
                snapshot = json.dumps({
                    "provenance_status": gov[0],
                    "review_status": gov[1],
                    "lifecycle_status": gov[2],
                    "can_use_as_instruction": gov[3],
                    "can_use_as_evidence": gov[4],
                    "sensitivity_tier": gov[5],
                    "confidence": gov[6],
                    "memory_type": gov[7],
                })
            conn.execute(
                "INSERT INTO recall_items (trace_id, event_id, rank, similarity, "
                "use_policy_snapshot_json) VALUES (?, ?, ?, ?, ?)",
                (trace_id, event_id, rank, similarity, snapshot),
            )
        conn.commit()
        return trace_id
    except sqlite3.Error:
        # Don't let trace-write failures break retrieval.
        return None
    finally:
        conn.close()


@mcp.tool()
def report_usage(
    trace_id: int,
    used_event_ids: list[int] | None = None,
    ignored: list[dict] | None = None,
) -> dict:
    """Close the recall-trace feedback loop.

    Call after answering a question that was informed by semantic_search.
    Tells the brain which retrieved events actually informed the answer.

    Args:
        trace_id: from the semantic_search() return value.
        used_event_ids: list of event ids that informed your answer.
        ignored: list of {"event_id": int, "reason": str}. Reasons: irrelevant,
                 stale, restricted, duplicate, lower_priority, wrong_subject.
    """
    used_event_ids = used_event_ids or []
    ignored = ignored or []

    conn = sqlite3.connect(DB)
    try:
        # Verify schema present
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recall_items'"
        ).fetchone()
        if not row:
            return {"status": "error", "error": "recall_traces schema not applied"}

        for eid in used_event_ids:
            conn.execute(
                "UPDATE recall_items SET used = 1, used_in = 'final_answer', "
                "reported_at = datetime('now') WHERE trace_id = ? AND event_id = ?",
                (trace_id, eid),
            )
        for entry in ignored:
            conn.execute(
                "UPDATE recall_items SET used = 0, ignored_reason = ?, "
                "reported_at = datetime('now') WHERE trace_id = ? AND event_id = ?",
                (entry.get("reason"), trace_id, entry.get("event_id")),
            )

        used_count = conn.execute(
            "SELECT COUNT(*) FROM recall_items WHERE trace_id = ? AND used = 1",
            (trace_id,),
        ).fetchone()[0]

        conn.execute(
            "UPDATE recall_traces SET used_count = ?, closed_at = datetime('now') "
            "WHERE id = ?",
            (used_count, trace_id),
        )
        conn.commit()
        return {"status": "ok", "trace_id": trace_id, "used_count": used_count}
    except sqlite3.Error as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@mcp.tool()
def propose_edge(
    from_event_id: int,
    to_event_id: int,
    relation: str,
    weight: float = 0.5,
    rationale: str | None = None,
    valid_until: str | None = None,
    created_by: str = "agent:unspecified",
) -> dict:
    """Propose a typed reasoning edge between two events.

    Allowed relations: supports, contradicts, evolved_into, supersedes,
    depends_on, related_to, informs, triggered_by.

    Agent-proposed edges start at weight <= 0.5 by convention so they can
    be reviewed. UNIQUE constraint on (from, to, relation) makes this
    idempotent — re-proposing the same edge is a no-op.

    Args:
        from_event_id: source event id
        to_event_id: target event id
        relation: one of the 8 allowed relations
        weight: 0.0-1.0 (default 0.5 for agent proposals)
        rationale: one-line why
        valid_until: optional ISO 8601 expiration
        created_by: who/what is proposing this edge
    """
    allowed = {"supports", "contradicts", "evolved_into", "supersedes",
               "depends_on", "related_to", "informs", "triggered_by"}
    if relation not in allowed:
        return {"status": "error", "error": f"relation must be one of {sorted(allowed)}"}
    if not 0.0 <= weight <= 1.0:
        return {"status": "error", "error": "weight must be between 0.0 and 1.0"}
    if from_event_id == to_event_id:
        return {"status": "error", "error": "self-loops not allowed"}

    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_edges'"
        ).fetchone()
        if not row:
            return {"status": "error", "error": "edges schema not applied"}

        try:
            cur = conn.execute(
                "INSERT INTO event_edges (from_event_id, to_event_id, relation, "
                "weight, rationale, valid_until, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (from_event_id, to_event_id, relation, weight, rationale,
                 valid_until, created_by),
            )
            conn.commit()
            return {"status": "ok", "edge_id": cur.lastrowid, "created": True}
        except sqlite3.IntegrityError:
            # UNIQUE constraint — edge already exists
            return {"status": "ok", "created": False, "reason": "edge already exists"}
    except sqlite3.Error as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@mcp.tool()
def health_check() -> dict:
    """Probe Ollama, Qdrant, and events.db. Returns health report."""
    if os.environ.get("BRAIN_VECTORS_ENABLED", "true").lower() == "false":
        try:
            with closing(sqlite3.connect(f"{DB.resolve().as_uri()}?mode=ro", uri=True)) as conn:
                total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            return {"all_healthy": True, "scope": "core", "vectors_enabled": False,
                    "semantic_search_available": False, "targets": [
                        {"name": "brain_event_log", "healthy": True, "total_events": total},
                        {"name": "vectors", "status": "disabled"}]}
        except sqlite3.Error as exc:
            return {"all_healthy": False, "scope": "core", "vectors_enabled": False, "error": str(exc)}
    proc = _run([sys.executable, str(HEALTH_SCRIPT)])
    if proc.returncode != 0:
        return {"status": "error", "error": proc.stderr.strip()}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": "health_check returned non-JSON output"}


@mcp.tool()
def search_events(query: str, source: str | None = None, type: str | None = None,
                  limit: int = 50, offset: int = 0) -> list[dict]:
    """Literal text search over event summaries and payloads; no vector service needed."""
    if not query.strip() or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("provide a query, limit 1..1000, and nonnegative offset")
    where = ["payload_json LIKE ? ESCAPE '\\'"]
    literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    params = [f"%{literal}%"]
    for column, value in (("source", source), ("type", type)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    params.extend([limit, offset])
    with closing(sqlite3.connect(DB)) as conn:
        rows = conn.execute("SELECT id, ts, source, type, actor, payload_json, attachment_uri, ingested_at FROM events WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ? OFFSET ?", params).fetchall()
    return [dict(id=r[0], ts=r[1], source=r[2], type=r[3], actor=r[4], payload=json.loads(r[5]), attachment_uri=r[6], ingested_at=r[7]) for r in rows]


@mcp.tool()
def check_outreach(contact: str, cooldown_days: int = 7, mailbox: str | None = None,
                   business_id: str | None = None) -> dict:
    """Check contact policy and recent outreach before taking any outbound action."""
    import crm
    return crm.check_outreach(contact=contact, cooldown_days=cooldown_days, mailbox=mailbox, business_id=business_id)


@mcp.tool()
def outreach_history(contact: str | None = None, mailbox: str | None = None,
                     agent: str | None = None, since: str | None = None,
                     limit: int = 50, business_id: str | None = None) -> list[dict]:
    """Read recorded outreach history for this deployment's business."""
    import crm
    return crm.outreach_history(contact=contact, mailbox=mailbox, agent=agent, since=since, limit=limit, business_id=business_id)


@mcp.tool()
def find_contact(query: str, limit: int = 10, business_id: str | None = None) -> list[dict]:
    """Look up recorded contact snapshots for this deployment's business."""
    import crm
    return crm.find_contact(query=query, limit=limit, business_id=business_id)


@mcp.tool()
def log_outreach(contact: str, mailbox: str, agent: str, channel: str = "email",
                 subject: str = "", message_id: str | None = None,
                 thread_id: str | None = None, ts: str | None = None,
                 direction: str = "outbound", business_id: str | None = None) -> dict:
    """Record a reported interaction; does not send or verify provider delivery."""
    import crm
    return crm.log_outreach(contact=contact, mailbox=mailbox, agent=agent, channel=channel, subject=subject, message_id=message_id, thread_id=thread_id, ts=ts, direction=direction, business_id=business_id)


@mcp.tool()
def upsert_contact(contact_id: str, email: str | None = None, phone: str | None = None,
                   display_name: str | None = None, kind: str = "contact",
                   contact_policy: str = "unknown", business_id: str | None = None,
                   observed_at: str | None = None, valid_until: str | None = None) -> dict:
    """Append a contact snapshot; contact policy defaults to unknown."""
    import crm
    return crm.upsert_contact(contact_id=contact_id, email=email, phone=phone, display_name=display_name, kind=kind, contact_policy=contact_policy, business_id=business_id, observed_at=observed_at, valid_until=valid_until)


@mcp.tool()
def upsert_deal(deal_id: str, contact_id: str, title: str, stage: str = "open",
                owner: str | None = None, next_action: str | None = None,
                value_cents: int | None = None, currency: str | None = None,
                business_id: str | None = None) -> dict:
    """Append a deal snapshot; external CRM provider is not mutated."""
    import crm
    return crm.upsert_deal(deal_id=deal_id, contact_id=contact_id, title=title, stage=stage, owner=owner, next_action=next_action, value_cents=value_cents, currency=currency, business_id=business_id)


@mcp.tool()
def get_pipeline(limit: int = 50, business_id: str | None = None) -> list[dict]:
    """Read current deal snapshots for this business."""
    import crm
    return crm.get_pipeline(limit=limit, business_id=business_id)


@mcp.tool()
def create_connection(provider: str, label: str, scopes: list[str] | None = None,
                      read_only: bool | None = None) -> dict:
    """Create a server-stamped customer connection; no provider credential is stored."""
    return customer.create_connection(provider, label, scopes, read_only)


@mcp.tool()
def revoke_connection(connection_id: str) -> dict:
    """Revoke one customer connection and all future operations using it."""
    return customer.revoke_connection(connection_id)


@mcp.tool()
def save_project_checkpoint(project_id: str, payload: dict, connection_id: str) -> dict:
    """Save scoped project context/checkpoint with authenticated connection provenance."""
    return customer.save_checkpoint(project_id, payload, connection_id)


@mcp.tool()
def resume_project_checkpoint(project_id: str, connection_id: str) -> dict:
    """Resume a project checkpoint from another authorized customer connection."""
    return customer.resume_checkpoint(project_id, connection_id)


@mcp.tool()
def create_artifact(slug: str, content: str, connection_id: str, format: str = "markdown",
                    provenance: dict | None = None) -> dict:
    """Create a UTF-8 artifact in the customer root with immutable revision 1."""
    return artifacts.create(slug, content, connection_id, format, provenance)


@mcp.tool()
def read_artifact(artifact_id: str, connection_id: str) -> dict:
    """Read the current customer-scoped artifact revision."""
    return artifacts.read(artifact_id, connection_id)


@mcp.tool()
def edit_artifact(artifact_id: str, content: str, expected_revision: int,
                  connection_id: str, provenance: dict | None = None) -> dict:
    """CAS edit: stale expected revisions fail without changing the artifact."""
    return artifacts.edit(artifact_id, content, expected_revision, connection_id, provenance)


@mcp.tool()
def artifact_history(artifact_id: str, connection_id: str) -> list[dict]:
    """List immutable artifact revisions and provenance."""
    return artifacts.history(artifact_id, connection_id)


@mcp.tool()
def restore_artifact(artifact_id: str, revision: int, expected_revision: int,
                     connection_id: str) -> dict:
    """Restore a prior revision by creating a new revision, never rewriting history."""
    return artifacts.restore(artifact_id, revision, expected_revision, connection_id)


@mcp.tool()
def export_artifact(artifact_id: str, connection_id: str) -> dict:
    """Export the current artifact bytes and revision metadata."""
    return artifacts.export(artifact_id, connection_id)


if __name__ == "__main__":
    mcp.run()
