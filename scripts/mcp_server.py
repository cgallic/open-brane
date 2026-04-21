#!/usr/bin/env python3
"""
mcp_server.py — stdio MCP server exposing the event log to agents.

This is the ONLY interface agents should use. Direct DB access is reserved
for cron jobs and humans with sqlite3.

Tools exposed:
- record_event      — the write API
- query_events      — filtered SQL
- semantic_search   — vector search + payload join
- health_check      — probe infrastructure

Wrap with mcp-proxy if you want HTTP/SSE (see systemd/brain-mcp.service).

Dependencies:
    pip install mcp httpx qdrant-client
"""
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from mcp.server import FastMCP

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
    args = [
        "python3", str(RECORD_SCRIPT),
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
) -> list[dict]:
    """Natural-language search over events via vector similarity.

    Returns ranked hits with summary, score, and full payload.
    """
    args = [
        "python3", str(SEMANTIC_SCRIPT),
        "--query", query,
        "--limit", str(limit),
    ]
    if source:
        args.extend(["--source", source])

    proc = _run(args)
    if proc.returncode != 0:
        return [{"status": "error", "error": proc.stderr.strip()}]

    hits: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                hits.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return hits


@mcp.tool()
def health_check() -> dict:
    """Probe Ollama, Qdrant, and events.db. Returns health report."""
    proc = _run(["python3", str(HEALTH_SCRIPT)])
    if proc.returncode != 0:
        return {"status": "error", "error": proc.stderr.strip()}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": "health_check returned non-JSON output"}


if __name__ == "__main__":
    mcp.run()
