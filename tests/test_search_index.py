import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import search_index


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "events.db"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / "SCHEMA.sql").read_text(encoding="utf-8"))
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)

    def add(self, payload, source="manual", etype="note", actor=None, ts="2026-01-01T00:00:00+00:00"):
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        cur = self.conn.execute(
            "INSERT INTO events (ts, source, type, actor, payload_json, ingested_at) VALUES (?,?,?,?,?,?)",
            (ts, source, etype, actor, raw, ts))
        self.conn.commit()
        return cur.lastrowid

    def ids(self, query, **kwargs):
        return [row["id"] for row in search_index.search(self.conn, query, **kwargs)]

    def test_words_match_in_any_order_with_stemming_and_accents(self):
        pricing = self.add({"summary": "Decision: the backup plan stays at ten dollars a month"})
        self.add({"summary": "Lunch order for Friday"})
        cafe = self.add({"summary": "Meeting notes", "detail": "line one\nnext: send the café proposal"})
        # The old substring search needed the exact phrase; these all returned nothing.
        self.assertEqual(self.ids("backup decisions"), [pricing])
        self.assertEqual(self.ids("month plan"), [pricing])
        self.assertEqual(self.ids("cafe proposal"), [cafe])
        # A JSON-escaped newline must not glue "n" onto the next word.
        self.assertEqual(self.ids("next"), [cafe])
        self.assertEqual(search_index.search(self.conn, "backup decisions")[0]["match"], "all_terms")

    def test_summary_matches_rank_above_body_matches(self):
        body = self.add({"summary": "Weekly notes", "detail": "we talked about the invoice briefly"})
        summary = self.add({"summary": "Invoice sent to the client", "detail": "nothing else"})
        self.assertEqual(self.ids("invoice"), [summary, body])

    def test_falls_back_to_any_word_then_to_literal_substring(self):
        event = self.add({"summary": "Kai Backup launch checklist 100% done"})
        rows = search_index.search(self.conn, "what did we decide about the backup launch")
        self.assertEqual([(r["id"], r["match"]) for r in rows], [(event, "any_term")])
        rows = search_index.search(self.conn, "ackup")  # a partial word is not an indexed term
        self.assertEqual([(r["id"], r["match"]) for r in rows], [(event, "literal")])
        self.assertEqual(self.ids("0% d"), [event])
        self.assertEqual(self.ids("%"), [event])
        self.assertEqual(self.ids("zebra crossing"), [])

    def test_query_syntax_cannot_break_or_widen_a_search(self):
        event = self.add({"summary": "alpha beta"})
        self.add({"summary": "gamma"})
        for query in ('alpha" OR "gamma', "alpha NOT beta", "alpha*", "summary:alpha", "alpha AND (", "NEAR(alpha beta)"):
            self.assertIn(event, self.ids(query), query)
        self.assertEqual(self.ids('"alpha beta"'), [event])
        self.assertEqual(self.ids('"beta alpha"'), [])

    def test_filters_paging_and_metadata_terms(self):
        first = self.add({"summary": "deploy fixed"}, source="git", etype="commit", actor="abc123", ts="2026-01-01T00:00:00+00:00")
        second = self.add({"summary": "deploy broken"}, source="manual", ts="2026-02-01T00:00:00+00:00")
        self.assertEqual(sorted(self.ids("deploy")), [first, second])
        self.assertEqual(self.ids("deploy", source="git"), [first])
        self.assertEqual(self.ids("deploy", etype="note"), [second])
        self.assertEqual(self.ids("deploy", since="2026-01-15T00:00:00+00:00"), [second])
        self.assertEqual(self.ids("deploy", until="2026-01-15T00:00:00+00:00"), [first])
        self.assertEqual(self.ids("git commit deploy"), [first])
        page_one = self.ids("deploy", limit=1)
        page_two = self.ids("deploy", limit=1, offset=1)
        self.assertEqual(sorted(page_one + page_two), [first, second])

    def test_index_catches_up_with_any_writer_and_rebuilds(self):
        self.add({"summary": "first walrus"})
        self.assertEqual(len(self.ids("walrus")), 1)
        # A second connection writes directly, as an adapter or the sqlite3 shell would.
        with closing(sqlite3.connect(self.db)) as other:
            other.execute("INSERT INTO events (ts, source, type, payload_json, ingested_at) VALUES "
                          "('2026-03-01T00:00:00+00:00', 'adapter', 'doc', 'second walrus, not JSON', 'x')")
            other.commit()
        self.assertEqual(len(self.ids("walrus")), 2)
        self.assertEqual(search_index.status(self.conn), {"events": 2, "indexed": 2, "index_exists": True})
        self.conn.execute(f"DROP TABLE {search_index.TABLE}")
        self.conn.commit()
        self.assertFalse(search_index.status(self.conn)["index_exists"])
        self.assertEqual(search_index.rebuild(self.conn), 2)
        self.assertEqual(len(self.ids("walrus")), 2)
        # The events table itself is untouched by indexing.
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_concurrent_first_searches_index_each_event_once(self):
        for number in range(300):
            self.add({"summary": f"otter sighting {number}"})
        errors = []

        def worker():
            try:
                with closing(sqlite3.connect(self.db, timeout=30)) as conn:
                    search_index.search(conn, "otter", limit=5)
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(search_index.status(self.conn)["indexed"], 300)
        self.assertEqual(len(self.ids("otter", limit=1000)), 300)

    def test_read_only_database_still_answers_with_literal_search(self):
        event = self.add({"summary": "heron by the river"})
        with closing(sqlite3.connect(f"{self.db.as_uri()}?mode=ro", uri=True)) as readonly:
            rows = search_index.search(readonly, "heron")
        self.assertEqual([(r["id"], r["match"]) for r in rows], [(event, "literal")])

    def test_snippet_shows_the_matching_part_of_a_long_payload(self):
        self.add({"summary": "Call summary", "transcript": "filler " * 200 + "the pelican budget was approved " + "filler " * 200})
        row = search_index.search(self.conn, "pelican budget")[0]
        self.assertIn("pelican budget was approved", row["snippet"])
        self.assertLessEqual(len(row["snippet"]), search_index.SNIPPET_CHARS + 2)
        self.assertEqual(row["payload"]["summary"], "Call summary")

    def test_command_line(self):
        self.add({"summary": "tangerine harvest"})
        env = dict(os.environ, BRAIN_DB=str(self.db))
        run = lambda *args: subprocess.run([sys.executable, str(ROOT / "scripts/search_index.py"), *args],
                                           capture_output=True, text=True, env=env)
        found = run("search", "harvest tangerine")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(json.loads(found.stdout)["payload"]["summary"], "tangerine harvest")
        self.assertEqual(json.loads(run("rebuild").stdout), {"status": "ok", "indexed": 1})
        self.assertEqual(json.loads(run("status").stdout)["indexed"], 1)
        missing = subprocess.run([sys.executable, str(ROOT / "scripts/search_index.py"), "status"],
                                 capture_output=True, text=True, env=dict(env, BRAIN_DB=str(self.db) + ".absent"))
        self.assertEqual(missing.returncode, 1)


if __name__ == "__main__":
    unittest.main()
