"""Read-only owner workspace: recent events, saved projects, and documents."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent.parent


def _paths():
    storage = Path(os.environ.get('BRAIN_ROOT', ROOT))
    return (Path(os.environ.get('BRAIN_DB', storage / 'events.db')).resolve(),
            Path(os.environ.get('BRAIN_AUTH_DB', storage / 'data/auth.db')).resolve(),
            Path(os.environ.get('BRAIN_ARTIFACT_ROOT', storage / 'data/artifacts')).resolve(),
            os.environ.get('BRAIN_OWNER_ID', 'local-owner'))


def _read(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError('limit must be between 1 and 100')


def _text(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def overview(limit=20, since=None):
    """Read saved state without inferring progress, priorities, or completion."""
    _limit(limit)
    db, auth_db, _, owner = _paths()
    params = []
    where = ''
    if since is not None:
        if not isinstance(since, str):
            raise ValueError('since must be an ISO-8601 UTC timestamp')
        stamp = datetime.fromisoformat(since.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError('since must explicitly use UTC')
        where = ' WHERE julianday(ingested_at) >= julianday(?)'
        params.append(stamp.isoformat())
    with closing(_read(db)) as conn:
        conn.execute('BEGIN')
        total = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        rows = conn.execute('SELECT id,ts,source,type,payload_json,ingested_at FROM events' + where +
                            ' ORDER BY id DESC LIMIT ?', [*params, limit]).fetchall()
    events = [{'event_id': r[0], 'ts': r[1], 'source': r[2], 'type': r[3],
               'payload': json.loads(r[4]), 'recorded_at': r[5]} for r in rows]
    projects, documents = [], []
    available = auth_db.is_file()
    if available:
        with closing(_read(auth_db)) as conn:
            conn.execute('BEGIN')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'checkpoints' in tables:
                for pid, raw, updated in conn.execute('SELECT project_id,payload_json,updated_at FROM checkpoints WHERE owner_id=? ORDER BY updated_at DESC,project_id LIMIT ?', (owner, limit)):
                    payload = json.loads(raw)
                    projects.append({'project_id': pid, 'updated_at': updated,
                                     'summary': _text(payload, 'summary') or _text(payload, 'goal'),
                                     'next_action': _text(payload, 'next_action')})
            if 'artifacts' in tables:
                documents = [dict(artifact_id=r[0], slug=r[1], format=r[2], revision=r[3], updated_at=r[4])
                    for r in conn.execute('SELECT artifact_id,slug,format,current_revision,updated_at FROM artifacts WHERE owner_id=? ORDER BY updated_at DESC,artifact_id LIMIT ?', (owner, limit))]
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'total_events': total,
            'recent_events': events, 'projects': projects, 'documents': documents,
            'workspace_available': available, 'limit_per_section': limit,
            'activity_since': since, 'activity_order': 'recorded_event_id_desc',
            'scope': 'this_deployment_only', 'progress_inferred': False}


def read_project(project_id):
    _, auth_db, _, owner = _paths()
    if not isinstance(project_id, str) or not project_id:
        raise ValueError('project_id is required')
    if not auth_db.is_file():
        raise FileNotFoundError('Saved project not found')
    with closing(_read(auth_db)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone():
            raise FileNotFoundError('Saved project not found')
        row = conn.execute('SELECT payload_json,connection_id,updated_at FROM checkpoints WHERE project_id=? AND owner_id=?', (project_id, owner)).fetchone()
    if row is None:
        raise FileNotFoundError('Saved project not found')
    return {'project_id': project_id, 'payload': json.loads(row[0]),
            'saved_by_connection_id': row[1], 'updated_at': row[2]}


def read_document(artifact_id):
    _, auth_db, artifact_root, owner = _paths()
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError('artifact_id is required')
    if not auth_db.is_file():
        raise FileNotFoundError('Saved document not found')
    with closing(_read(auth_db)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone():
            raise FileNotFoundError('Saved document not found')
        row = conn.execute('SELECT a.slug,a.format,r.revision_id,r.path,r.sha256,r.created_at '
            'FROM artifacts a JOIN artifact_revisions r ON r.artifact_id=a.artifact_id AND r.revision_id=a.current_revision '
            'WHERE a.artifact_id=? AND a.owner_id=?', (artifact_id, owner)).fetchone()
    if row is None:
        raise FileNotFoundError('Saved document not found')
    path = Path(row[3]).resolve()
    if not path.is_relative_to(artifact_root):
        raise ValueError('Saved document is outside the configured artifact root')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row[4]:
        raise ValueError('Saved document integrity check failed')
    return {'artifact_id': artifact_id, 'slug': row[0], 'format': row[1], 'revision': row[2],
            'content': raw.decode('utf-8'), 'sha256': row[4], 'created_at': row[5]}
