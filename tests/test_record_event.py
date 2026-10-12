"""Canonical writer legacy append behavior and atomic durable retry identity."""
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from record_event import EventConflict, append_event
from policy_check import PolicyError


class CanonicalWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.executescript((SCRIPTS.parent / "SCHEMA.sql").read_text())

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_events_append_without_dedupe_table(self):
        first = append_event(source="test", event_type="note", payload={"summary": "Test"}, db_path=self.database)
        second = append_event(source="test", event_type="note", payload={"summary": "Test"}, db_path=self.database)
        self.assertNotEqual(first["event_id"], second["event_id"])
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='event_dedup'").fetchone())

    def test_dedupe_durable_and_conflicting_contents_rejected(self):
        kwargs = dict(source="test", event_type="note", dedupe_key="identity", db_path=self.database)
        first = append_event(payload={"summary": "Synthetic", "value": 1}, **kwargs)
        retry = append_event(payload={"value": 1, "summary": "Synthetic"}, **kwargs)
        self.assertEqual(retry["event_id"], first["event_id"])
        self.assertTrue(retry["deduped"])
        with self.assertRaises(EventConflict):
            append_event(payload={"summary": "Changed", "value": 1}, **kwargs)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_dedup").fetchone()[0], 1)

    def test_imported_writer_enforces_policy(self):
        with self.assertRaises(PolicyError):
            append_event(source="test", event_type="note", payload={"note": "no summary"}, db_path=self.database)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_caller_transaction_rolls_back_event_and_dedupe(self):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            append_event(source="test", event_type="note", payload={"summary": "Rollback"},
                         dedupe_key="rollback", connection=conn)
            conn.rollback()
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='event_dedup'").fetchone())

    def test_concurrent_retries_insert_one_event(self):
        def write(_):
            return append_event(source="test", event_type="note", payload={"summary": "Concurrent"},
                                dedupe_key="concurrent", db_path=self.database)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(8)))
        self.assertEqual(len({r["event_id"] for r in results}), 1)
        self.assertEqual(sum(not r["deduped"] for r in results), 1)


if __name__ == "__main__":
    unittest.main()
