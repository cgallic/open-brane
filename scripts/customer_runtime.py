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

def save_checkpoint(project_id, payload, connection_id, expected_revision=None, change_summary=None):
    auth = _connection(connection_id, write=True)
    if not isinstance(payload, dict) or not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id and object payload are required")
    if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0):
        raise ValueError("expected_revision must be a nonnegative integer")
    if change_summary is not None and (not isinstance(change_summary, str) or not change_summary.strip()):
        raise ValueError("change_summary must be a nonempty string")
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with closing(sqlite3.connect(AUTH_DB, timeout=30)) as c:
        c.execute("BEGIN IMMEDIATE")
        previous = c.execute("SELECT owner_id,payload_json,connection_id,updated_at FROM checkpoints WHERE project_id=?", (project_id,)).fetchone()
        if previous and previous[0] != OWNER:
            raise PermissionError("project belongs to another owner")
        revision = c.execute("SELECT COALESCE(MAX(revision),0) FROM checkpoint_revisions WHERE project_id=? AND owner_id=?", (project_id,OWNER)).fetchone()[0]
        if previous and revision == 0:
            # Preserve an existing legacy checkpoint before its first revisioned edit.
            revision = 1
            c.execute("INSERT INTO checkpoint_revisions VALUES (?,?,?,?,?,?,?)", (project_id,OWNER,revision,previous[1],previous[2],previous[3],"Preserved existing checkpoint"))
        if expected_revision is not None and revision != expected_revision:
            raise RuntimeError(f"checkpoint revision conflict: expected {expected_revision}, current {revision}")
        revision += 1
        stamp = now()
        c.execute("INSERT INTO checkpoint_revisions VALUES (?,?,?,?,?,?,?)", (project_id,OWNER,revision,raw,auth["connection_id"],stamp,change_summary))
        c.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET payload_json=excluded.payload_json, connection_id=excluded.connection_id, updated_at=excluded.updated_at", (project_id, OWNER, raw, auth["connection_id"], stamp))
        c.commit()
    return {"project_id": project_id, "owner_id": OWNER, "connection_id": auth["connection_id"], "saved": True, "revision": revision}


def checkpoint_history(project_id, connection_id, limit=50):
    _connection(connection_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with closing(sqlite3.connect(AUTH_DB)) as c:
        c.execute("BEGIN")
        current = c.execute("SELECT owner_id,payload_json,connection_id,updated_at FROM checkpoints WHERE project_id=?", (project_id,)).fetchone()
        if current is None or current[0] != OWNER:
            raise FileNotFoundError("project not found")
        rows = c.execute("SELECT revision,payload_json,connection_id,saved_at,change_summary FROM checkpoint_revisions WHERE project_id=? AND owner_id=? ORDER BY revision DESC LIMIT ?", (project_id,OWNER,limit)).fetchall()
    if not rows:
        rows = [(1,current[1],current[2],current[3],"Existing checkpoint")]
    return [{"revision":r[0], "payload":json.loads(r[1]), "connection_id":r[2], "saved_at":r[3], "change_summary":r[4]} for r in rows]


def restore_checkpoint(project_id, revision, expected_revision, connection_id):
    _connection(connection_id, write=True)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision must be a positive integer")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ValueError("expected_revision must identify the current checkpoint")
    with closing(sqlite3.connect(AUTH_DB)) as c:
        row = c.execute("SELECT payload_json FROM checkpoint_revisions WHERE project_id=? AND owner_id=? AND revision=?", (project_id,OWNER,revision)).fetchone()
    if row is None:
        raise FileNotFoundError("checkpoint revision not found")
    return save_checkpoint(project_id, json.loads(row[0]), connection_id, expected_revision,
                           f"Restored checkpoint revision {revision}")


def resume_checkpoint(project_id, connection_id):
    auth = _connection(connection_id)
    with closing(sqlite3.connect(AUTH_DB)) as c:
        c.execute("BEGIN")
        row = c.execute("SELECT project_id,payload_json,connection_id,updated_at FROM checkpoints WHERE project_id=? AND owner_id=?", (project_id, OWNER)).fetchone()
        revision = c.execute("SELECT COALESCE(MAX(revision),1) FROM checkpoint_revisions WHERE project_id=? AND owner_id=?", (project_id,OWNER)).fetchone()[0]
    if not row: return {"project_id": project_id, "found": False, "connection_id": auth["connection_id"]}
    return {"revision": revision, "project_id": row[0], "payload": json.loads(row[1]), "saved_by_connection_id": row[2], "updated_at": row[3], "connection_id": auth["connection_id"], "found": True}
