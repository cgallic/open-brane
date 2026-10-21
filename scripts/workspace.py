"""Owner workspace discovery and explicit, revisioned session handoffs."""
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
                                     'next_action': _text(payload, 'next_action'),
                                     'revision': _revision(conn, pid, owner)})
            if 'artifacts' in tables:
                documents = [dict(artifact_id=r[0], slug=r[1], format=r[2], revision=r[3], updated_at=r[4])
                    for r in conn.execute('SELECT artifact_id,slug,format,current_revision,updated_at FROM artifacts WHERE owner_id=? ORDER BY updated_at DESC,artifact_id LIMIT ?', (owner, limit))]
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'total_events': total,
            'recent_events': events, 'projects': projects, 'documents': documents,
            'workspace_available': available, 'limit_per_section': limit,
            'activity_since': since, 'activity_order': 'recorded_event_id_desc',
            'scope': 'this_deployment_only', 'progress_inferred': False}


def _revision(conn, project_id, owner):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='checkpoint_revisions'").fetchone():
        return 1
    return conn.execute('SELECT COALESCE(MAX(revision),1) FROM checkpoint_revisions WHERE project_id=? AND owner_id=?', (project_id, owner)).fetchone()[0]


def read_project(project_id):
    _, auth_db, _, owner = _paths()
    if not isinstance(project_id, str) or not project_id:
        raise ValueError('project_id is required')
    if not auth_db.is_file():
        raise FileNotFoundError('Saved project not found')
    with closing(_read(auth_db)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone():
            raise FileNotFoundError('Saved project not found')
        conn.execute('BEGIN')
        row = conn.execute('SELECT payload_json,connection_id,updated_at FROM checkpoints WHERE project_id=? AND owner_id=?', (project_id, owner)).fetchone()
        revision = _revision(conn, project_id, owner)
    if row is None:
        raise FileNotFoundError('Saved project not found')
    return {'project_id': project_id, 'payload': json.loads(row[0]),
            'saved_by_connection_id': row[1], 'updated_at': row[2], 'revision': revision}


def read_document(artifact_id, revision=None):
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        raise ValueError('revision must be positive')
    _, auth_db, artifact_root, owner = _paths()
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError('artifact_id is required')
    if not auth_db.is_file():
        raise FileNotFoundError('Saved document not found')
    with closing(_read(auth_db)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone():
            raise FileNotFoundError('Saved document not found')
        row = conn.execute('SELECT a.slug,a.format,r.revision_id,r.path,r.sha256,r.created_at '
            'FROM artifacts a JOIN artifact_revisions r ON r.artifact_id=a.artifact_id AND r.revision_id=COALESCE(?,a.current_revision) '
            'WHERE a.artifact_id=? AND a.owner_id=?', (revision, artifact_id, owner)).fetchone()
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


def save_session(project_id, summary, next_action, expected_revision, connection_id,
                 decisions=None, blockers=None, event_ids=None, artifact_ids=None):
    """Update explicit session facts, preserving other checkpoint fields. CAS is required."""
    import customer_runtime as customer
    customer._connection(connection_id, write=True)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError('expected_revision must be nonnegative; use 0 for a new project')
    for value in (summary, next_action):
        if not isinstance(value, str) or not value.strip() or len(value) > 16000:
            raise ValueError('summary and next_action must be nonempty text up to 16000 characters')
    for values in (decisions, blockers):
        if values is not None and (not isinstance(values, list) or len(values) > 50 or
                any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in values)):
            raise ValueError('decisions and blockers must be lists of up to 50 nonempty strings')
    event_ids = [] if event_ids is None else event_ids
    artifact_ids = [] if artifact_ids is None else artifact_ids
    if not isinstance(event_ids, list) or len(event_ids) > 50 or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in event_ids):
        raise ValueError('event_ids must contain up to 50 positive integers')
    if not isinstance(artifact_ids, list) or len(artifact_ids) > 20 or any(not isinstance(v, str) or not v for v in artifact_ids):
        raise ValueError('artifact_ids must contain up to 20 document IDs')
    try:
        current = read_project(project_id)
        if current['revision'] != expected_revision:
            raise RuntimeError('checkpoint revision conflict; reopen the project before saving')
        payload = current['payload'].copy()
    except FileNotFoundError:
        if expected_revision != 0:
            raise RuntimeError('checkpoint revision conflict; project no longer exists')
        payload = {}
    db, _, _, _ = _paths()
    with closing(_read(db)) as conn:
        for eid in event_ids:
            if not conn.execute('SELECT 1 FROM events WHERE id=?', (eid,)).fetchone():
                raise ValueError('Referenced event does not exist in this deployment')
    documents = []
    for aid in dict.fromkeys(artifact_ids):
        doc = read_document(aid)
        documents.append({k: doc[k] for k in ('artifact_id', 'revision', 'sha256')})
    payload.update(summary=summary.strip(), next_action=next_action.strip(),
                   decisions=decisions or [], blockers=blockers or [],
                   session_sources={'event_ids': list(dict.fromkeys(event_ids)), 'documents': documents})
    return customer.save_checkpoint(project_id, payload, connection_id, expected_revision, summary.strip())


def resume_session(project_id):
    """Return saved facts with source IDs and pinned document versions, never inferred progress."""
    project = read_project(project_id)
    payload = project['payload']
    refs = payload.get('session_sources', {})
    if not isinstance(refs, dict):
        refs = {}
    events, documents, warnings = [], [], []
    db, _, _, _ = _paths()
    ids = refs.get('event_ids', [])
    with closing(_read(db)) as conn:
        for eid in (ids[:50] if isinstance(ids, list) else []):
            if isinstance(eid, bool) or not isinstance(eid, int):
                warnings.append('Invalid saved event reference'); continue
            row = conn.execute('SELECT ts,source,type,payload_json FROM events WHERE id=?', (eid,)).fetchone()
            if row is None:
                warnings.append(f'Event #{eid} is unavailable'); continue
            event = json.loads(row[3])
            summary = _text(event, 'summary')
            events.append({'event_id': eid, 'ts': row[0], 'source': row[1], 'type': row[2],
                           'summary': summary[:2000] if summary else None})
    docs = refs.get('documents', [])
    for ref in (docs[:20] if isinstance(docs, list) else []):
        try:
            if not isinstance(ref, dict) or not isinstance(ref.get('revision'), int) or isinstance(ref['revision'], bool):
                raise ValueError('Invalid document reference')
            doc = read_document(ref.get('artifact_id'), ref['revision'])
            if doc['sha256'] != ref.get('sha256'):
                raise ValueError('Saved reference hash mismatch')
            current = read_document(ref['artifact_id'])
            documents.append({k: doc[k] for k in ('artifact_id', 'slug', 'revision', 'sha256')})
            if current['revision'] != doc['revision']:
                warnings.append(f"Document {doc['slug']} has changed since this session; saved revision {doc['revision']} is still available")
        except (FileNotFoundError, ValueError, OSError):
            warnings.append('A saved document reference is unavailable or failed integrity checks')
    return {**project, 'events': events, 'documents': documents, 'warnings': warnings,
            'progress_inferred': False, 'scope': 'this_deployment_only'}
