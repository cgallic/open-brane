"""Owner isolation, recorded activity, and read-only saved-work access."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import workspace
from record_event import append_event


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'events.db'
        self.auth = self.root / 'auth.db'
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript((ROOT / 'SCHEMA.sql').read_text())
        env = patch.dict(os.environ, {'BRAIN_DB': str(self.db), 'BRAIN_AUTH_DB': str(self.auth),
            'BRAIN_ARTIFACT_ROOT': str(self.root / 'artifacts'), 'BRAIN_OWNER_ID': 'one'})
        env.start(); self.addCleanup(env.stop)

    def seed(self):
        with closing(sqlite3.connect(self.auth)) as c:
            c.executescript((ROOT / 'schemas/customer.sql').read_text())
            for owner in ('one', 'other'):
                c.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?)', (owner + '-project', owner,
                    json.dumps({'goal': owner + ' research', 'next_action': 'Read the source'}), 'fixture', '2026-01-01T00:00:00+00:00'))
            c.commit()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the browser script check')
    def test_browser_conflict_preserves_draft_and_lock_clears_it(self):
        result = subprocess.run(['node', str(ROOT / 'tests/test_workspace_ui.cjs')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('conflict preserves draft', result.stdout)

    def test_missing_optional_store_is_not_created_and_since_uses_recording_time(self):
        append_event(source='research', event_type='note', payload={'summary': 'Imported older research'}, ts='2020-01-01T00:00:00Z')
        result = workspace.overview(since=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        self.assertEqual(len(result['recent_events']), 1)
        self.assertEqual(result['recent_events'][0]['ts'], '2020-01-01T00:00:00Z')
        self.assertFalse(result['workspace_available'])
        self.assertFalse(self.auth.exists())
        self.assertFalse(result['progress_inferred'])
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                workspace.overview(limit=limit)
        with self.assertRaises(ValueError):
            workspace.overview(since='2026-01-01')

    def test_projects_are_owner_filtered_and_reads_do_not_create_connections(self):
        self.seed()
        result = workspace.overview()
        self.assertEqual([p['project_id'] for p in result['projects']], ['one-project'])
        self.assertEqual(result['projects'][0]['next_action'], 'Read the source')
        self.assertEqual(workspace.read_project('one-project')['payload']['goal'], 'one research')
        with self.assertRaises(FileNotFoundError):
            workspace.read_project('other-project')
        with closing(sqlite3.connect(self.auth)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM connections').fetchone()[0], 0)
            c.execute('UPDATE checkpoints SET payload_json=? WHERE project_id=?', (json.dumps({'goal': 'Only a goal'}), 'one-project'))
            c.commit()
        self.assertIsNone(workspace.overview()['projects'][0]['next_action'])

    def test_document_read_rejects_foreign_owner_path_escape_and_corruption(self):
        self.seed()
        root = self.root / 'artifacts'; root.mkdir()
        path = root / 'revision.bin'; path.write_text('Saved research')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.auth)) as c:
            c.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)', ('doc','one','research','markdown',1,'2026-01-01','2026-01-01'))
            c.execute('INSERT INTO artifact_revisions VALUES (?,?,?,?,?,?,?,?,?)', ('doc',1,None,digest,len(path.read_bytes()),str(path),'{}','2026-01-01','fixture'))
            c.commit()
        self.assertEqual(workspace.read_document('doc')['content'], 'Saved research')
        path.write_text('Modified bytes')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            workspace.read_document('doc')
        with closing(sqlite3.connect(self.auth)) as c:
            c.execute('UPDATE artifact_revisions SET path=?', (str(self.root / 'outside.txt'),)); c.commit()
        with self.assertRaisesRegex(ValueError, 'outside'):
            workspace.read_document('doc')
        with patch.dict(os.environ, {'BRAIN_OWNER_ID': 'other'}):
            with self.assertRaises(FileNotFoundError):
                workspace.read_document('doc')


if __name__ == '__main__':
    unittest.main()
