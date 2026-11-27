"""Role-based task board: posting, claiming, leases, dependencies, proof and briefings."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import tasks
import search_index


class TaskBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'events.db'
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript((ROOT / 'SCHEMA.sql').read_text())
        env = patch.dict(os.environ, {'BRAIN_DB': str(self.db), 'BRAIN_AUTH_DB': str(self.root / 'auth.db'),
                                      'BRAIN_ARTIFACT_ROOT': str(self.root / 'artifacts'), 'BRAIN_OWNER_ID': 'one'})
        env.start(); self.addCleanup(env.stop)

    def post(self, role='research', title='Find three competitors', **kw):
        return tasks.post_task(role, title, kw.pop('brief', 'List them with links'),
                               kw.pop('done_when', 'A note with three links'), **kw)

    def test_claim_settle_round_trip_is_append_only(self):
        posted = self.post(posted_by='lead')
        self.assertEqual(posted['status'], 'open')
        got = tasks.claim_task('research', 'agent-a')
        self.assertEqual(got['task']['task_id'], posted['task_id'])
        self.assertEqual(got['task']['status'], 'claimed')
        done = tasks.settle_task(posted['task_id'], got['claim_id'], 'agent-a', 'done',
                                 'Found three', ['https://example.com/a'])
        self.assertEqual(done['status'], 'done')
        with closing(sqlite3.connect(self.db)) as c:
            types = [r[0] for r in c.execute("SELECT type FROM events WHERE source='tasks' ORDER BY id")]
        self.assertEqual(types, ['task.posted', 'task.claimed', 'task.settled'])
        self.assertIsNone(tasks.claim_task('research', 'agent-b')['task'])

    def test_roles_are_separate_queues(self):
        self.post(role='build', title='Fix the form')
        self.assertIsNone(tasks.claim_task('research', 'agent-a')['task'])
        self.assertEqual(tasks.claim_task('build', 'agent-b')['task']['title'], 'Fix the form')

    def test_done_requires_proof_and_reasons_are_required(self):
        t = self.post()
        claim = tasks.claim_task('research', 'agent-a')
        with self.assertRaises(ValueError):
            tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-a', 'done', 'Did it')
        with self.assertRaises(ValueError):
            tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-a', 'blocked', ' ')
        blocked = tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-a', 'blocked', 'Needs a login to the ads account')
        self.assertEqual(blocked['status'], 'blocked')
        with self.assertRaises(RuntimeError):
            tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-a', 'done', 'x', ['y'])

    def test_expired_lease_lets_another_agent_take_over_and_old_claim_cannot_settle(self):
        t = self.post()
        first = tasks.claim_task('research', 'agent-a', lease_minutes=5)
        self.assertIsNone(tasks.claim_task('research', 'agent-b')['task'])
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch.object(tasks, '_now', return_value=later):
            second = tasks.claim_task('research', 'agent-b')
            self.assertEqual(second['task']['task_id'], t['task_id'])
            self.assertEqual(second['task']['attempts'], 2)
            with self.assertRaises(PermissionError):
                tasks.settle_task(t['task_id'], first['claim_id'], 'agent-a', 'done', 'late', ['x'])
            tasks.settle_task(t['task_id'], second['claim_id'], 'agent-b', 'done', 'ok', ['x'])

    def test_wrong_agent_cannot_settle_someone_elses_claim(self):
        t = self.post()
        claim = tasks.claim_task('research', 'agent-a')
        with self.assertRaises(PermissionError):
            tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-b', 'done', 'ok', ['x'])

    def test_dependencies_wait_and_pass_upstream_results_into_the_briefing(self):
        research = self.post()
        build = self.post(role='build', title='Build the comparison page', depends_on=[research['task_id']])
        self.assertEqual(build['status'], 'waiting')
        none = tasks.claim_task('build', 'builder')
        self.assertIsNone(none['task'])
        self.assertEqual(none['waiting_on_dependencies'], 1)
        r = tasks.claim_task('research', 'researcher')
        tasks.settle_task(research['task_id'], r['claim_id'], 'researcher', 'done', 'Three found', ['event:12'])
        b = tasks.claim_task('build', 'builder')
        self.assertEqual(b['task']['task_id'], build['task_id'])
        upstream = b['briefing']['upstream_results']
        self.assertEqual(upstream[0]['outcome'], 'Three found')
        self.assertEqual(upstream[0]['proof'], ['event:12'])
        with self.assertRaises(ValueError):
            self.post(depends_on=['t_000000000000'])

    def test_blocked_dependency_keeps_dependent_waiting(self):
        research = self.post()
        self.post(role='build', title='Build it', depends_on=[research['task_id']])
        r = tasks.claim_task('research', 'researcher')
        tasks.settle_task(research['task_id'], r['claim_id'], 'researcher', 'blocked', 'Site is down')
        self.assertIsNone(tasks.claim_task('build', 'builder')['task'])

    def test_briefing_carries_role_history_context_events_and_missing_project_warning(self):
        from record_event import append_event
        note = append_event(source='manual', event_type='note', payload={'summary': 'Customers ask for CSV export'})
        first = self.post(title='Earlier research')
        c = tasks.claim_task('research', 'a')
        tasks.settle_task(first['task_id'], c['claim_id'], 'a', 'done', 'Earlier result', ['x'])
        self.post(title='Next research', context_event_ids=[note['event_id']], project_id='alpha')
        got = tasks.claim_task('research', 'b')
        brief = got['briefing']
        self.assertEqual(brief['recent_outcomes_for_role'][0]['outcome'], 'Earlier result')
        self.assertEqual(brief['context_events'][0]['summary'], 'Customers ask for CSV export')
        self.assertIsNone(brief['project'])
        self.assertTrue(brief['warnings'])
        with self.assertRaises(ValueError):
            self.post(context_event_ids=[99999])

    def test_request_id_makes_post_idempotent(self):
        a = self.post(request_id='lead-run-7')
        b = self.post(request_id='lead-run-7')
        self.assertEqual(a['task_id'], b['task_id'])
        self.assertTrue(b['deduped'])
        self.assertEqual(len(tasks.list_tasks()), 1)

    def test_concurrent_claims_never_hand_out_the_same_task(self):
        self.post(title='only one')
        results, errors = [], []
        def grab(name):
            try:
                results.append(tasks.claim_task('research', name)['task'])
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
        threads = [threading.Thread(target=grab, args=(f'agent-{i}',)) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in results if r is not None), 1)

    def test_role_brief_and_list_filters(self):
        a = self.post(title='one')
        self.post(title='two')
        tasks.claim_task('research', 'agent-a', task_id=a['task_id'])
        brief = tasks.role_brief('research')
        self.assertEqual(brief['counts']['open'], 1)
        self.assertEqual(brief['counts']['claimed'], 1)
        self.assertEqual(brief['claimed'][0]['agent'], 'agent-a')
        self.assertEqual([t['title'] for t in tasks.list_tasks(status='open')], ['two'])
        for bad in ('Research', 'has space', ''):
            with self.assertRaises(ValueError):
                tasks.role_brief(bad)

    def test_tasks_are_findable_with_text_search(self):
        self.post(title='Audit the pricing page')
        with closing(sqlite3.connect(self.db)) as c:
            rows = search_index.search(c, 'pricing audit')
        self.assertTrue(any(r['source'] == 'tasks' for r in rows))

    def test_secrets_in_a_brief_are_rejected_by_the_write_policy(self):
        from policy_check import PolicyError
        with self.assertRaises(PolicyError):
            self.post(brief='use key sk-proj-' + 'a' * 48)


if __name__ == '__main__':
    unittest.main()
