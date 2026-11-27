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
            tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-a', 'done', 'x', ['https://example.com/y'])

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
                tasks.settle_task(t['task_id'], first['claim_id'], 'agent-a', 'done', 'late', ['https://example.com/x'])
            tasks.settle_task(t['task_id'], second['claim_id'], 'agent-b', 'done', 'ok', ['https://example.com/x'])

    def test_wrong_agent_cannot_settle_someone_elses_claim(self):
        t = self.post()
        claim = tasks.claim_task('research', 'agent-a')
        with self.assertRaises(PermissionError):
            tasks.settle_task(t['task_id'], claim['claim_id'], 'agent-b', 'done', 'ok', ['https://example.com/x'])

    def test_dependencies_wait_and_pass_upstream_results_into_the_briefing(self):
        research = self.post()
        build = self.post(role='build', title='Build the comparison page', depends_on=[research['task_id']])
        self.assertEqual(build['status'], 'waiting')
        none = tasks.claim_task('build', 'builder')
        self.assertIsNone(none['task'])
        self.assertEqual(none['waiting_on_dependencies'], 1)
        r = tasks.claim_task('research', 'researcher')
        tasks.settle_task(research['task_id'], r['claim_id'], 'researcher', 'done', 'Three found', ['event:1'])
        b = tasks.claim_task('build', 'builder')
        self.assertEqual(b['task']['task_id'], build['task_id'])
        upstream = b['briefing']['upstream_results']
        self.assertEqual(upstream[0]['outcome'], 'Three found')
        self.assertEqual(upstream[0]['proof'], ['event:1'])
        with self.assertRaises(ValueError):
            self.post(depends_on=['t_000000000000'])

    def test_blocked_dependency_shows_stuck_until_a_follow_up_is_done(self):
        research = self.post()
        build = self.post(role='build', title='Build it', depends_on=[research['task_id']])
        r = tasks.claim_task('research', 'researcher')
        tasks.settle_task(research['task_id'], r['claim_id'], 'researcher', 'blocked', 'Site is down')
        self.assertIsNone(tasks.claim_task('build', 'builder')['task'])
        self.assertEqual(tasks.list_tasks(role='build')[0]['stuck_on'], [research['task_id']])
        self.assertEqual(tasks.role_brief('build')['counts']['stuck'], 1)
        retry = self.post(title='Find three competitors from the archive', followup_to=research['task_id'])
        with self.assertRaises(ValueError):
            self.post(followup_to=research['task_id'])
        self.assertEqual(tasks.list_tasks(role='build')[0]['status'], 'waiting')
        r2 = tasks.claim_task('research', 'researcher')
        self.assertEqual(r2['briefing']['replaces']['outcome'], 'Site is down')
        tasks.settle_task(retry['task_id'], r2['claim_id'], 'researcher', 'done', 'Used the archive', ['https://archive.org/x'])
        b = tasks.claim_task('build', 'builder')
        self.assertEqual(b['task']['task_id'], build['task_id'])
        self.assertEqual(b['briefing']['upstream_results'][0]['outcome'], 'Used the archive')

    def test_follow_up_only_replaces_dead_end_tasks(self):
        t = self.post()
        with self.assertRaises(ValueError):
            self.post(followup_to=t['task_id'])

    def test_proof_must_be_checkable(self):
        t = self.post()
        c = tasks.claim_task('research', 'a')
        for bad in (['x'], ['did it'], ['event:99999'], ['event:abc']):
            with self.assertRaises(ValueError):
                tasks.settle_task(t['task_id'], c['claim_id'], 'a', 'done', 'ok', bad)
        for good in ('https://example.com/a', 'abc1234', 'docs/README.md', 'cgallic/open-brane#4', 'event:1'):
            tasks._check_proof(sqlite3.connect(self.db), [good])

    def test_verification_by_a_second_agent_gates_dependents_that_require_it(self):
        research = self.post()
        build = self.post(role='build', title='Build it', depends_on=[research['task_id']], require_verified=True)
        r = tasks.claim_task('research', 'researcher')
        tasks.settle_task(research['task_id'], r['claim_id'], 'researcher', 'done', 'ok', ['https://example.com/n'])
        self.assertEqual(tasks.list_tasks(role='build')[0]['status'], 'waiting')
        self.assertEqual(tasks.role_brief('research')['counts']['done_unverified'], 1)
        with self.assertRaises(PermissionError):
            tasks.verify_task(research['task_id'], 'researcher', 'verified', 'mine')
        v = tasks.verify_task(research['task_id'], 'reviewer', 'verified', 'Links load and match')
        self.assertEqual(v['verification']['verdict'], 'verified')
        with self.assertRaises(RuntimeError):
            tasks.verify_task(research['task_id'], 'reviewer2', 'rejected', 'again')
        b = tasks.claim_task('build', 'builder')
        self.assertEqual(b['task']['task_id'], build['task_id'])
        self.assertEqual(b['briefing']['upstream_results'][0]['verification']['agent'], 'reviewer')

    def test_rejected_task_makes_dependents_stuck_and_can_be_followed_up(self):
        research = self.post()
        self.post(role='build', title='Build it', depends_on=[research['task_id']])
        r = tasks.claim_task('research', 'researcher')
        tasks.settle_task(research['task_id'], r['claim_id'], 'researcher', 'done', 'ok', ['https://example.com/n'])
        tasks.verify_task(research['task_id'], 'reviewer', 'rejected', 'Two links are dead')
        self.assertEqual(tasks.list_tasks(role='research')[0]['status'], 'rejected')
        self.assertEqual(tasks.list_tasks(role='build')[0]['status'], 'stuck')
        retry = self.post(title='Redo competitor list', followup_to=research['task_id'])
        got = tasks.claim_task('research', 'researcher')
        self.assertEqual(got['task']['task_id'], retry['task_id'])
        self.assertEqual(got['briefing']['replaces']['rejection_note'], 'Two links are dead')

    def test_renew_extends_only_your_live_claim(self):
        t = self.post()
        c = tasks.claim_task('research', 'a', lease_minutes=5)
        with self.assertRaises(PermissionError):
            tasks.renew_claim(t['task_id'], c['claim_id'], 'b')
        tasks.renew_claim(t['task_id'], c['claim_id'], 'a', lease_minutes=60)
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch.object(tasks, '_now', return_value=later):
            self.assertIsNone(tasks.claim_task('research', 'b')['task'])

    def test_lessons_and_prior_work_reach_the_next_agent(self):
        from record_event import append_event
        note = append_event(source='manual', event_type='note', payload={'summary': 'Competitors list: Acme, Beta, Gamma'})
        first = self.post(title='List competitors')
        c = tasks.claim_task('research', 'a')
        tasks.settle_task(first['task_id'], c['claim_id'], 'a', 'done', 'See note', [f"event:{note['event_id']}"],
                          lesson={'pattern': 'Check the pricing page first', 'change': 'Saved 3 searches', 'metric': 'searches', 'before': 5, 'after': 2})
        with self.assertRaises(ValueError):
            tasks.settle_task('t_000000000000', 'x', 'a', 'done', 'x', ['https://e.com'], lesson={'pattern': 'p'})
        self.post(title='Update the competitors list', never=['Email any competitor'])
        got = tasks.claim_task('research', 'b')
        brief = got['briefing']
        self.assertEqual(brief['never'], ['Email any competitor'])
        self.assertEqual(brief['recent_outcomes_for_role'][0]['lesson']['after'], 2)
        known = [k['event_id'] for k in brief['already_known']]
        self.assertIn(note['event_id'], known)
        self.assertNotIn(got['task']['posted_event_id'], known)

    def test_briefing_carries_role_history_context_events_and_missing_project_warning(self):
        from record_event import append_event
        note = append_event(source='manual', event_type='note', payload={'summary': 'Customers ask for CSV export'})
        first = self.post(title='Earlier research')
        c = tasks.claim_task('research', 'a')
        tasks.settle_task(first['task_id'], c['claim_id'], 'a', 'done', 'Earlier result', ['https://example.com/x'])
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
