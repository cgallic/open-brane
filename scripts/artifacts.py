"""Customer-scoped immutable artifact revisions with locked atomic writes."""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, time, uuid
from contextlib import contextmanager, closing
from pathlib import Path
from customer_runtime import AUTH_DB, OWNER, _connection, now

MAX_BYTES = 256 * 1024
ROOT = Path(os.environ.get("BRAIN_ARTIFACT_ROOT", Path(os.environ.get("BRAIN_ROOT", ".")) / "data/artifacts")).resolve()
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,96}$")

def _safe(slug):
    if not SLUG.fullmatch(slug): raise ValueError("artifact slug must be lowercase alphanumeric with hyphens")
    path = (ROOT / slug).resolve()
    if path.parent != ROOT: raise ValueError("artifact path escapes customer root")
    return path

@contextmanager
def _lock(path):
    path.mkdir(parents=True, exist_ok=True); lock = path / ".lock"
    started = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd); break
        except FileExistsError:
            if time.monotonic() - started > 10: raise TimeoutError("artifact is busy")
            time.sleep(.01)
    try: yield
    finally:
        try: lock.unlink()
        except FileNotFoundError: pass

def _db():
    with closing(sqlite3.connect(AUTH_DB)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS artifacts (artifact_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, format TEXT NOT NULL, current_revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS artifact_revisions (artifact_id TEXT NOT NULL, revision_id INTEGER NOT NULL, parent_revision INTEGER, sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL, path TEXT NOT NULL, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL, connection_id TEXT NOT NULL, PRIMARY KEY(artifact_id,revision_id))")
        c.commit()
    return sqlite3.connect(AUTH_DB, timeout=30)

@contextmanager
def _managed_db():
    c = _db()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def _bytes(content):
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if len(raw) > MAX_BYTES: raise ValueError(f"artifact exceeds {MAX_BYTES} UTF-8 bytes")
    return raw

def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try: tmp.write_bytes(raw); os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()

def create(slug, content, connection_id, fmt="markdown", provenance=None):
    auth = _connection(connection_id, write=True); raw = _bytes(content); root = _safe(slug); aid = "art_" + uuid.uuid4().hex
    with _lock(root):
        with _managed_db() as c:
            if c.execute("SELECT 1 FROM artifacts WHERE slug=?", (slug,)).fetchone(): raise FileExistsError("artifact slug already exists")
            revpath = root / "revisions" / "1.bin"; _write(revpath, raw); stamp = now(); digest = hashlib.sha256(raw).hexdigest()
            c.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)", (aid, OWNER, slug, fmt, 1, stamp, stamp)); c.execute("INSERT INTO artifact_revisions VALUES (?,?,?,?,?,?,?,?,?)", (aid,1,None,digest,len(raw),str(revpath),json.dumps(provenance or {},sort_keys=True),stamp,auth["connection_id"]))
    return read(aid, connection_id)

def _get(c, artifact_id):
    row = c.execute("SELECT artifact_id,slug,format,current_revision,owner_id FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    if not row or row[4] != OWNER: raise FileNotFoundError("artifact not found")
    return row

def read(artifact_id, connection_id):
    auth = _connection(connection_id); 
    with _managed_db() as c:
        a = _get(c, artifact_id); r = c.execute("SELECT revision_id,sha256,byte_length,path,provenance_json,created_at,connection_id FROM artifact_revisions WHERE artifact_id=? AND revision_id=?", (artifact_id,a[3])).fetchone()
    raw = Path(r[3]).resolve().read_bytes(); return {"artifact_id": a[0], "slug": a[1], "format": a[2], "revision": r[0], "content": raw.decode("utf-8"), "sha256": r[1], "byte_length": r[2], "provenance": json.loads(r[4]), "created_at": r[5], "connection_id": auth["connection_id"]}

def edit(artifact_id, content, expected_revision, connection_id, provenance=None):
    auth = _connection(connection_id, write=True); raw = _bytes(content)
    with _managed_db() as c: a = _get(c, artifact_id)
    root = _safe(a[1])
    with _lock(root):
        with _managed_db() as c:
            a = _get(c, artifact_id)
            if a[3] != expected_revision: raise RuntimeError(f"artifact revision conflict: expected {expected_revision}, current {a[3]}")
            rev = a[3] + 1; path = root / "revisions" / f"{rev}.bin"; _write(path, raw); stamp=now(); digest=hashlib.sha256(raw).hexdigest()
            c.execute("INSERT INTO artifact_revisions VALUES (?,?,?,?,?,?,?,?,?)", (artifact_id,rev,a[3],digest,len(raw),str(path),json.dumps(provenance or {},sort_keys=True),stamp,auth["connection_id"])); c.execute("UPDATE artifacts SET current_revision=?,updated_at=? WHERE artifact_id=?", (rev,stamp,artifact_id))
    return read(artifact_id, connection_id)

def history(artifact_id, connection_id):
    _connection(connection_id)
    with _managed_db() as c: a=_get(c,artifact_id); rows=c.execute("SELECT revision_id,parent_revision,sha256,byte_length,provenance_json,created_at,connection_id FROM artifact_revisions WHERE artifact_id=? ORDER BY revision_id",(artifact_id,)).fetchall()
    return [{"artifact_id":artifact_id,"revision":r[0],"parent_revision":r[1],"sha256":r[2],"byte_length":r[3],"provenance":json.loads(r[4]),"created_at":r[5],"connection_id":r[6]} for r in rows]

def restore(artifact_id, revision, expected_revision, connection_id):
    _connection(connection_id, write=True)
    with _managed_db() as c:
        a=_get(c,artifact_id); row=c.execute("SELECT path FROM artifact_revisions WHERE artifact_id=? AND revision_id=?",(artifact_id,revision)).fetchone()
    if not row: raise FileNotFoundError("revision not found")
    return edit(artifact_id, Path(row[0]).resolve().read_bytes(), expected_revision, connection_id, {"restored_from":revision})

def export(artifact_id, connection_id):
    result=read(artifact_id,connection_id); result["export_bytes"]=result.pop("content").encode("utf-8"); return result
