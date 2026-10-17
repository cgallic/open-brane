"""Portable customer identity, connection and project-continuation primitives."""
from __future__ import annotations
import json, os, secrets, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

OWNER = os.environ.get("BRAIN_OWNER_ID", "local-owner")
AUTH_DB = Path(os.environ.get("BRAIN_AUTH_DB", Path(os.environ.get("BRAIN_ROOT", ".")) / "data/auth.db"))

def now(): return datetime.now(timezone.utc).isoformat()

def ensure_schema():
    AUTH_DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(AUTH_DB)) as c:
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript((Path(__file__).resolve().parent.parent / "schemas/customer.sql").read_text(encoding="utf-8"))
        c.commit()

def _connection(connection_id, write=False):
    ensure_schema()
    with closing(sqlite3.connect(AUTH_DB)) as c:
        row = c.execute("SELECT connection_id,owner_id,scopes_json,read_only,revoked_at FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
        if not row or row[1] != OWNER or row[4]: raise PermissionError("connection is missing, revoked, or belongs to another owner")
        if write and row[3]: raise PermissionError("connection is read-only")
        if write and "brain:write" not in json.loads(row[2]): raise PermissionError("connection lacks brain:write")
        c.execute("UPDATE connections SET last_used_at=? WHERE connection_id=?", (now(), connection_id))
        c.commit()
    return {"connection_id": row[0], "owner_id": row[1], "scopes": json.loads(row[2]), "read_only": bool(row[3])}

def create_connection(provider, label, scopes=None, read_only=None):
    if not provider or not label: raise ValueError("provider and label are required")
    scopes = sorted(set(scopes or ["brain:read", "brain:write"]))
    if any(s not in {"brain:read", "brain:write"} for s in scopes): raise ValueError("unsupported customer scope")
    if read_only is None: read_only = os.environ.get("BRAIN_READ_ONLY_DEFAULT", "false").lower() == "true"
    cid = "conn_" + secrets.token_urlsafe(18)
    ensure_schema()
    with closing(sqlite3.connect(AUTH_DB)) as c:
        c.execute("INSERT INTO connections VALUES (?,?,?,?,?,?,?,?,?)", (cid, OWNER, provider, label, json.dumps(scopes), int(bool(read_only)), None, now(), None))
        c.commit()
    return {"connection_id": cid, "owner_id": OWNER, "provider": provider, "label": label, "scopes": scopes, "read_only": bool(read_only)}

def revoke_connection(connection_id):
    ensure_schema()
    with closing(sqlite3.connect(AUTH_DB)) as c:
        changed = c.execute("UPDATE connections SET revoked_at=? WHERE connection_id=? AND owner_id=? AND revoked_at IS NULL", (now(), connection_id, OWNER)).rowcount
        c.commit()
    if not changed: raise PermissionError("connection not found or already revoked")
    return {"connection_id": connection_id, "revoked": True}

def save_checkpoint(project_id, payload, connection_id):
    auth = _connection(connection_id, write=True)
    if not isinstance(payload, dict) or not project_id: raise ValueError("project_id and object payload are required")
    with closing(sqlite3.connect(AUTH_DB)) as c:
        c.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET payload_json=excluded.payload_json, connection_id=excluded.connection_id, updated_at=excluded.updated_at", (project_id, OWNER, json.dumps(payload, ensure_ascii=False, sort_keys=True), auth["connection_id"], now()))
        c.commit()
    return {"project_id": project_id, "owner_id": OWNER, "connection_id": auth["connection_id"], "saved": True}

def resume_checkpoint(project_id, connection_id):
    auth = _connection(connection_id)
    with closing(sqlite3.connect(AUTH_DB)) as c:
        row = c.execute("SELECT project_id,payload_json,connection_id,updated_at FROM checkpoints WHERE project_id=? AND owner_id=?", (project_id, OWNER)).fetchone()
    if not row: return {"project_id": project_id, "found": False, "connection_id": auth["connection_id"]}
    return {"project_id": row[0], "payload": json.loads(row[1]), "saved_by_connection_id": row[2], "updated_at": row[3], "connection_id": auth["connection_id"], "found": True}
