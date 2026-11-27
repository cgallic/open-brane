"""Role-based task board: post work for a role, let one agent claim it, settle it with proof.

Every change is an append-only event (source "tasks"), so the board is rebuilt
from the event log on each read, shows up in search, and keeps a full history.

    task.posted   a task for one role, with a brief and a done_when line
    task.claimed  one agent holds the task until its lease ends
    task.renewed  the holder extends its lease on a long job
    task.settled  done (with proof), blocked or failed (with a reason)
    task.verified a different agent checks a done task: verified or rejected

A task posted with followup_to replaces a blocked, failed or rejected task:
tasks that depended on the old one now wait on the follow-up instead.

A claim is decided inside one write transaction, so two agents asking for the
same role at the same moment never get the same task. A claim whose lease has
ended is treated as abandoned and the task can be claimed again.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

from record_event import append_event

SOURCE = 'tasks'
ROLE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')
TASK_ID = re.compile(r'^t_[0-9a-f]{12}$')
SETTLED = ('done', 'blocked', 'failed')
STATUSES = ('open', 'waiting', 'stuck', 'claimed', *SETTLED, 'rejected')
DEAD_END = ('blocked', 'failed', 'rejected')
COMMIT = re.compile(r'^[0-9a-f]{7,40}$')
DEFAULT_LEASE_MINUTES = 120


def _db():
    root = Path(os.environ.get('BRAIN_ROOT', Path(__file__).resolve().parent.parent))
    return Path(os.environ.get('BRAIN_DB', root / 'events.db'))


def _now():
    return datetime.now(timezone.utc)


def _role(value):
    if not isinstance(value, str) or not ROLE.match(value):
        raise ValueError('role must be a short lowercase name such as research, build or review')
    return value


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name} must be nonempty text up to {limit} characters')
    return value.strip()


def _strings(values, name, max_items, max_len):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > max_items or any(
            not isinstance(v, str) or not v.strip() or len(v) > max_len for v in values):
        raise ValueError(f'{name} must be a list of up to {max_items} nonempty strings')
    return [v.strip() for v in values]


def _load(conn):
    """Fold task events into current task state, oldest event first."""
    tasks = {}
    rows = conn.execute("SELECT id,type,payload_json FROM events WHERE source=? AND type LIKE 'task.%' "
                        "ORDER BY id", (SOURCE,)).fetchall()
    for event_id, etype, raw in rows:
        p = json.loads(raw)
        tid = p.get('task_id')
        if etype == 'task.posted' and tid not in tasks:
            tasks[tid] = {'task_id': tid, 'role': p['role'], 'title': p['title'], 'brief': p['brief'],
                          'done_when': p['done_when'], 'project_id': p.get('project_id'),
                          'depends_on': p.get('depends_on', []), 'context_event_ids': p.get('context_event_ids', []),
                          'posted_by': p.get('posted_by'), 'posted_at': p['posted_at'], 'posted_event_id': event_id,
                          'followup_to': p.get('followup_to'), 'require_verified': bool(p.get('require_verified')),
                          'never': p.get('never', []),
                          'status': 'open', 'claim': None, 'attempts': 0, 'settlement': None, 'verification': None}
        elif etype == 'task.verified' and tid in tasks and tasks[tid]['status'] == 'done' and not tasks[tid]['verification']:
            tasks[tid]['verification'] = {'verdict': p['verdict'], 'agent': p['agent'], 'note': p['note'],
                                          'verified_at': p['verified_at'], 'event_id': event_id}
            if p['verdict'] == 'rejected':
                tasks[tid]['status'] = 'rejected'
        elif tid in tasks and tasks[tid]['settlement'] is None:
            task = tasks[tid]
            if etype == 'task.claimed':
                task['claim'] = {'claim_id': p['claim_id'], 'agent': p['agent'],
                                 'claimed_at': p['claimed_at'], 'lease_until': p['lease_until']}
                task['attempts'] += 1
            elif etype == 'task.renewed' and task['claim'] and task['claim']['claim_id'] == p['claim_id']:
                task['claim']['lease_until'] = p['lease_until']
            elif etype == 'task.settled':
                task['settlement'] = {'status': p['status'], 'agent': p['agent'], 'outcome': p['outcome'],
                                      'proof': p.get('proof', []), 'lesson': p.get('lesson'),
                                      'settled_at': p['settled_at'], 'event_id': event_id}
                task['status'] = p['status']
    now = _now()
    for task in tasks.values():
        if task['settlement'] is None and task['claim']:
            live = datetime.fromisoformat(task['claim']['lease_until']) > now
            task['status'] = 'claimed' if live else 'open'
            task['claim']['lease_expired'] = not live
    # The newest follow-up stands in for the task it replaces.
    replaced = {}
    for task in sorted(tasks.values(), key=lambda t: t['posted_event_id']):
        if task['followup_to'] in tasks:
            replaced[task['followup_to']] = task['task_id']
            task_from = tasks[task['followup_to']]
            task_from['replaced_by'] = task['task_id']
    for task in tasks.values():
        if task['status'] != 'open':
            continue
        waiting, stuck = [], []
        for dep in task['depends_on']:
            seen = set()
            while dep in replaced and dep not in seen:
                seen.add(dep)
                dep = replaced[dep]
            upstream = tasks.get(dep, {})
            if upstream.get('status') in DEAD_END:
                stuck.append(dep)
            elif upstream.get('status') != 'done' or (
                    task['require_verified'] and (upstream.get('verification') or {}).get('verdict') != 'verified'):
                waiting.append(dep)
        if stuck:
            task['status'] = 'stuck'
            task['stuck_on'] = stuck
        elif waiting:
            task['status'] = 'waiting'
            task['waiting_on'] = waiting
    return tasks


def _resolved(tasks, dep):
    seen = set()
    while tasks.get(dep, {}).get('replaced_by') and dep not in seen:
        seen.add(dep)
        dep = tasks[dep]['replaced_by']
    return dep


def _write(conn, etype, payload, dedupe_key=None):
    return append_event(source=SOURCE, event_type=etype, payload=payload, actor=payload['task_id'],
                        connection=conn, dedupe_key=dedupe_key)


def _connect():
    db = _db()
    if not db.exists():
        raise FileNotFoundError('database missing; run brane.py setup first')
    conn = sqlite3.connect(db, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def post_task(role, title, brief, done_when, posted_by=None, project_id=None,
              depends_on=None, context_event_ids=None, request_id=None,
              followup_to=None, require_verified=False, never=None):
    """Post one task for a role. request_id makes a retried post return the same task.

    followup_to names a blocked, failed or rejected task this one replaces.
    require_verified makes this task wait until its upstream tasks are verified,
    not only settled. never lists actions the claiming agent must not take.
    """
    role = _role(role)
    title = _text(title, 'title', 200)
    brief = _text(brief, 'brief', 16000)
    done_when = _text(done_when, 'done_when', 2000)
    depends_on = _strings(depends_on, 'depends_on', 20, 20)
    if any(not TASK_ID.match(d) for d in depends_on):
        raise ValueError('depends_on must contain task ids returned by post_task')
    context = context_event_ids or []
    if not isinstance(context, list) or len(context) > 50 or any(
            isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in context):
        raise ValueError('context_event_ids must contain up to 50 positive event ids')
    if project_id is not None:
        project_id = _text(project_id, 'project_id', 200)
    if posted_by is not None:
        posted_by = _text(posted_by, 'posted_by', 100)
    if request_id is not None:
        request_id = _text(request_id, 'request_id', 200)
    if not isinstance(require_verified, bool):
        raise ValueError('require_verified must be true or false')
    never = _strings(never, 'never', 20, 300)
    with closing(_connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if request_id is not None:
                known = conn.execute("SELECT e.payload_json FROM event_dedup d JOIN events e ON e.id=d.event_id "
                                     "WHERE d.source=? AND d.dedupe_key=?", (SOURCE, 'post:' + request_id)).fetchone() \
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='event_dedup'").fetchone() else None
                if known:
                    conn.commit()
                    return {**_load(conn)[json.loads(known[0])['task_id']], 'deduped': True}
            tasks = _load(conn)
            missing = [d for d in depends_on if d not in tasks]
            if missing:
                raise ValueError(f'unknown depends_on task: {missing[0]}')
            if followup_to is not None:
                old = tasks.get(followup_to)
                if old is None:
                    raise ValueError('followup_to must name an existing task')
                if old['status'] not in DEAD_END:
                    raise ValueError(f"only a blocked, failed or rejected task can be followed up; this one is {old['status']}")
                if old.get('replaced_by'):
                    raise ValueError(f"task already has a follow-up: {old['replaced_by']}")
            for eid in context:
                if not conn.execute('SELECT 1 FROM events WHERE id=?', (eid,)).fetchone():
                    raise ValueError(f'context event #{eid} does not exist')
            tid = 't_' + secrets.token_hex(6)
            payload = {'summary': f'Task for {role}: {title}', 'task_id': tid, 'role': role, 'title': title,
                       'brief': brief, 'done_when': done_when, 'project_id': project_id,
                       'depends_on': depends_on, 'context_event_ids': context, 'posted_by': posted_by,
                       'followup_to': followup_to, 'require_verified': require_verified, 'never': never,
                       'posted_at': _now().isoformat()}
            _write(conn, 'task.posted', payload, None if request_id is None else 'post:' + request_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {**_load(conn)[tid], 'deduped': False}


def _recent_outcomes(tasks, role, limit):
    settled = [t for t in tasks.values() if t['role'] == role and t['settlement']]
    settled.sort(key=lambda t: t['settlement']['event_id'], reverse=True)
    return [{'task_id': t['task_id'], 'title': t['title'], 'status': t['status'],
             'outcome': t['settlement']['outcome'], 'proof': t['settlement']['proof'],
             'lesson': t['settlement'].get('lesson'),
             'verification': (t['verification'] or {}).get('verdict'),
             'agent': t['settlement']['agent'], 'settled_at': t['settlement']['settled_at']}
            for t in settled[:limit]]


def _briefing(conn, tasks, task):
    """What an agent in this role should know before starting: saved facts only."""
    brief = {'recent_outcomes_for_role': _recent_outcomes(tasks, task['role'], 5),
             'upstream_results': [], 'already_known': [], 'context_events': [], 'project': None,
             'never': task['never'], 'warnings': []}
    for dep in task['depends_on']:
        dep = _resolved(tasks, dep)
        t = tasks.get(dep)
        if t and t['settlement']:
            brief['upstream_results'].append({'task_id': dep, 'role': t['role'], 'title': t['title'],
                                              'outcome': t['settlement']['outcome'],
                                              'proof': t['settlement']['proof'],
                                              'verification': t['verification']})
    if task['followup_to'] in tasks:
        old = tasks[task['followup_to']]
        brief['replaces'] = {'task_id': old['task_id'], 'status': old['status'],
                             'outcome': (old['settlement'] or {}).get('outcome'),
                             'rejection_note': (old['verification'] or {}).get('note')}
    brief['already_known'] = _prior_work(conn, task)
    for eid in task['context_event_ids']:
        row = conn.execute('SELECT ts,source,type,payload_json FROM events WHERE id=?', (eid,)).fetchone()
        if row:
            summary = json.loads(row[3]).get('summary')
            brief['context_events'].append({'event_id': eid, 'ts': row[0], 'source': row[1], 'type': row[2],
                                            'summary': summary[:2000] if isinstance(summary, str) else None})
    if task['project_id']:
        try:
            import workspace
            saved = workspace.resume_session(task['project_id'])
            payload = saved['payload']
            brief['project'] = {'project_id': task['project_id'], 'revision': saved['revision'],
                                'summary': payload.get('summary') or payload.get('goal'),
                                'next_action': payload.get('next_action'),
                                'decisions': payload.get('decisions', []),
                                'blockers': payload.get('blockers', [])}
        except (FileNotFoundError, ValueError, OSError, sqlite3.Error):
            brief['warnings'].append(f"Saved project {task['project_id']} is not available in this deployment")
    return brief


def _prior_work(conn, task, limit=5):
    """Saved notes that already match this task's title, so agents do not redo known work."""
    import search_index
    try:
        rows = search_index.search(conn, task['title'], limit=limit + 5, exclude=[
            (SOURCE, 'task.posted'), (SOURCE, 'task.claimed'), (SOURCE, 'task.renewed')])
    except sqlite3.Error:
        return []
    known = []
    for row in rows:
        if row.get('match') == 'literal' or row.get('actor') == task['task_id']:
            continue
        known.append({'event_id': row['id'], 'ts': row['ts'], 'source': row['source'], 'type': row['type'],
                      'match': row['match'], 'snippet': row['snippet']})
        if len(known) == limit:
            break
    return known


def claim_task(role, agent, lease_minutes=DEFAULT_LEASE_MINUTES, task_id=None):
    """Claim the oldest open task for a role (or one named task) and return it with a briefing."""
    role = _role(role)
    agent = _text(agent, 'agent', 100)
    if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int) or not 1 <= lease_minutes <= 1440:
        raise ValueError('lease_minutes must be between 1 and 1440')
    with closing(_connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            tasks = _load(conn)
            if task_id is not None:
                task = tasks.get(task_id)
                if task is None or task['role'] != role:
                    raise LookupError('task not found for this role')
                if task['status'] != 'open':
                    raise RuntimeError(f"task is {task['status']}, not open")
            else:
                ready = sorted((t for t in tasks.values() if t['role'] == role and t['status'] == 'open'),
                               key=lambda t: t['posted_event_id'])
                if not ready:
                    conn.commit()
                    waiting = sum(1 for t in tasks.values() if t['role'] == role and t['status'] == 'waiting')
                    return {'task': None, 'role': role, 'waiting_on_dependencies': waiting}
                task = ready[0]
            now = _now()
            claim = {'summary': f"{agent} claimed {task['title']}", 'task_id': task['task_id'],
                     'claim_id': secrets.token_hex(16), 'agent': agent, 'role': role,
                     'claimed_at': now.isoformat(),
                     'lease_until': (now + timedelta(minutes=lease_minutes)).isoformat()}
            _write(conn, 'task.claimed', claim)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        tasks = _load(conn)
        task = tasks[task['task_id']]
        return {'task': task, 'claim_id': claim['claim_id'], 'lease_until': claim['lease_until'],
                'briefing': _briefing(conn, tasks, task), 'progress_inferred': False}


def _check_proof(conn, proof):
    """Each item must be checkable: a URL, event:<id> that exists, a commit hash, or a path/ref."""
    for item in proof:
        if item.startswith('event:'):
            num = item[6:]
            if not num.isdigit() or not conn.execute('SELECT 1 FROM events WHERE id=?', (int(num),)).fetchone():
                raise ValueError(f'proof {item} does not name an event in this deployment')
        elif not (re.match(r'^https?://\S+$', item) or COMMIT.match(item) or
                  (' ' not in item and ('/' in item or '#' in item or '.' in item))):
            raise ValueError(f'proof "{item}" is not checkable; use a URL, event:<id>, a commit hash, or a file path')


def _lesson(lesson):
    if lesson is None:
        return None
    if not isinstance(lesson, dict) or set(lesson) - {'pattern', 'change', 'metric', 'before', 'after'}:
        raise ValueError('lesson may contain only pattern, change, metric, before and after')
    out = {'pattern': _text(lesson.get('pattern'), 'lesson.pattern', 400),
           'change': _text(lesson.get('change'), 'lesson.change', 400)}
    for key in ('metric', 'before', 'after'):
        value = lesson.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 160:
                raise ValueError(f'lesson.{key} must be a short text or number')
            out[key] = value
    return out


def settle_task(task_id, claim_id, agent, status, outcome, proof=None, lesson=None):
    """Finish a claimed task. done needs checkable proof; blocked and failed need the reason in outcome."""
    if status not in SETTLED:
        raise ValueError('status must be done, blocked or failed')
    agent = _text(agent, 'agent', 100)
    outcome = _text(outcome, 'outcome', 4000)
    proof = _strings(proof, 'proof', 20, 500)
    lesson = _lesson(lesson)
    if status == 'done' and not proof:
        raise ValueError('done needs at least one proof item: a URL, commit, file path or event id')
    with closing(_connect()) as conn:
        _check_proof(conn, proof)
        conn.execute('BEGIN IMMEDIATE')
        try:
            task = _load(conn).get(task_id)
            if task is None:
                raise LookupError('task not found')
            if task['settlement']:
                raise RuntimeError(f"task is already {task['status']}")
            claim = task['claim']
            if not claim or claim['claim_id'] != claim_id or claim['agent'] != agent:
                raise PermissionError('this claim no longer holds the task; claim it again before settling')
            payload = {'summary': f"{task['title']}: {status}. {outcome}"[:2000], 'task_id': task_id,
                       'claim_id': claim_id, 'agent': agent, 'role': task['role'], 'status': status,
                       'outcome': outcome, 'proof': proof, 'settled_at': _now().isoformat()}
            if lesson:
                payload['lesson'] = lesson
            event = _write(conn, 'task.settled', payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {**_load(conn)[task_id], 'settled_event_id': event['event_id']}


def renew_claim(task_id, claim_id, agent, lease_minutes=DEFAULT_LEASE_MINUTES):
    """Extend your own live claim so a long job is not handed to another agent."""
    agent = _text(agent, 'agent', 100)
    if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int) or not 1 <= lease_minutes <= 1440:
        raise ValueError('lease_minutes must be between 1 and 1440')
    with closing(_connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            task = _load(conn).get(task_id)
            if task is None:
                raise LookupError('task not found')
            claim = task['claim']
            if task['status'] != 'claimed' or claim['claim_id'] != claim_id or claim['agent'] != agent:
                raise PermissionError('only the holder of a live claim can renew it; claim the task again')
            until = (_now() + timedelta(minutes=lease_minutes)).isoformat()
            _write(conn, 'task.renewed', {'summary': f"{agent} renewed {task['title']}", 'task_id': task_id,
                                          'claim_id': claim_id, 'agent': agent, 'lease_until': until})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {'task_id': task_id, 'claim_id': claim_id, 'lease_until': until}


def verify_task(task_id, agent, verdict, note):
    """A different agent checks a done task against its done_when and proof: verified or rejected."""
    if verdict not in ('verified', 'rejected'):
        raise ValueError('verdict must be verified or rejected')
    agent = _text(agent, 'agent', 100)
    note = _text(note, 'note', 4000)
    with closing(_connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            task = _load(conn).get(task_id)
            if task is None:
                raise LookupError('task not found')
            if task['verification']:
                raise RuntimeError(f"task was already {task['verification']['verdict']}")
            if task['status'] != 'done':
                raise RuntimeError(f"only a done task can be verified; this one is {task['status']}")
            if task['settlement']['agent'] == agent:
                raise PermissionError('the agent that settled a task cannot verify it')
            event = _write(conn, 'task.verified', {'summary': f"{task['title']}: {verdict} by {agent}. {note}"[:2000],
                                                   'task_id': task_id, 'agent': agent, 'verdict': verdict,
                                                   'note': note, 'verified_at': _now().isoformat()})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {**_load(conn)[task_id], 'verified_event_id': event['event_id']}


def list_tasks(role=None, status=None, limit=50):
    """Current board, newest posted first. status: open, waiting, claimed, done, blocked or failed."""
    if role is not None:
        _role(role)
    if status is not None and status not in STATUSES:
        raise ValueError('status must be one of ' + ', '.join(STATUSES))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError('limit must be between 1 and 500')
    with closing(_connect()) as conn:
        tasks = list(_load(conn).values())
    tasks = [t for t in tasks if (role is None or t['role'] == role) and (status is None or t['status'] == status)]
    tasks.sort(key=lambda t: t['posted_event_id'], reverse=True)
    return tasks[:limit]


def role_brief(role, limit=10):
    """Everything a fresh agent taking this role should read first: queue, active claims, recent results."""
    role = _role(role)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('limit must be between 1 and 50')
    with closing(_connect()) as conn:
        tasks = _load(conn)
    mine = [t for t in tasks.values() if t['role'] == role]
    pick = lambda s: [{'task_id': t['task_id'], 'title': t['title'], 'done_when': t['done_when'],
                       **({'agent': t['claim']['agent'], 'lease_until': t['claim']['lease_until']} if s == 'claimed' else {}),
                       **({'waiting_on': t.get('waiting_on', [])} if s == 'waiting' else {}),
                       **({'stuck_on': t.get('stuck_on', [])} if s == 'stuck' else {}),
                       **({'agent': t['settlement']['agent'], 'proof': t['settlement']['proof']}
                          if s == 'done' and not t['verification'] else {})}
                      for t in sorted(mine, key=lambda t: t['posted_event_id']) if t['status'] == s][:limit]
    counts = {s: sum(1 for t in mine if t['status'] == s) for s in STATUSES}
    counts['done_unverified'] = sum(1 for t in mine if t['status'] == 'done' and not t['verification'])
    return {'role': role, 'counts': counts, 'open': pick('open'), 'claimed': pick('claimed'),
            'waiting': pick('waiting'), 'stuck': pick('stuck'),
            'awaiting_verification': [t for t in pick('done') if 'proof' in t],
            'recent_outcomes': _recent_outcomes(tasks, role, limit),
            'progress_inferred': False}

