"""Regressions for defects found by running the tools the way a self-hoster would."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import policy_check

# Synthetic values with the right shape; none is a real credential.
FAKE_SECRETS = {
    "OpenAI project key": "sk-proj-" + "Ab1" * 16,
    "GitHub OAuth token": "gho_" + "a1B2" * 9,
    "GitHub server token": "ghs_" + "a1B2" * 9,
    "private key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
    "RSA private key": "-----BEGIN RSA PRIVATE KEY-----",
    "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.c2lnbmF0dXJlLXZhbHVl",
    "Stripe webhook secret": "whsec_" + "Zx9" * 10,
    "GitLab token": "glpat-" + "Zx9_" * 5,
    "Google API key": "AIza" + "Sy0" * 11 + "ab",
    "npm token": "npm_" + "a1B2" * 9,
    "Hugging Face token": "hf_" + "aB3" * 11,
    "database URL": "postgres://app:correct-horse-battery@db.example.test:5432/app",
}


class PolicyTests(unittest.TestCase):
    def check(self, summary, mode="strict"):
        return policy_check.check(source="manual", etype="note",
                                  payload_raw=json.dumps({"summary": summary}), mode=mode)

    def test_common_credential_formats_are_rejected(self):
        for label, value in FAKE_SECRETS.items():
            with self.subTest(label), self.assertRaises(policy_check.PolicyError):
                self.check("deploy note " + value)

    def test_ordinary_notes_are_accepted(self):
        for note in ("Call Alex at https://example.test/path?id=42 about the invoice",
                     "Email sam@example.test: JWT auth is planned, see sk- prefix docs",
                     "Postgres runs at postgres://db.example.test:5432/app (no password in the URL)",
                     "Decided: begin private key rotation next sprint"):
            with self.subTest(note):
                self.assertIn("summary", json.loads(self.check(note)))

    def test_sanitize_redacts_every_secret_in_one_string(self):
        aws, github = "AKIA" + "ABCDEFGH12345678", "ghp_" + "a1B2" * 9
        cleaned, warnings = self.check(f"keys {aws} and {github} leaked", mode="sanitize")
        # Each redaction used to start again from the original text, restoring the earlier secret.
        self.assertNotIn(aws, cleaned)
        self.assertNotIn(github, cleaned)
        self.assertEqual(len(warnings), 2)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / "events.db"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((ROOT / "SCHEMA.sql").read_text(encoding="utf-8"))
        self.env = dict(os.environ, BRAIN_DB=str(self.db), BRAIN_AUTH_DB=str(self.base / "auth.db"),
                        BRAIN_ARTIFACT_ROOT=str(self.base / "artifacts"), BRAIN_OWNER_ID="owner",
                        BRAIN_ROOT=str(ROOT), BRAIN_POLICY_MODE="strict")

    def count(self, where="1=1"):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT count(*) FROM events WHERE " + where).fetchone()[0]

    def script(self, name, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / name), *args],
                              capture_output=True, text=True, env=self.env)

    def python(self, code, **env):
        proc = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r)\n" % str(ROOT / "scripts")
                               + textwrap.dedent(code)], capture_output=True, text=True, env=dict(self.env, **env))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    @unittest.skipUnless(shutil.which("git"), "git is needed for the adapter check")
    def test_git_adapter_does_not_duplicate_commits_on_a_second_run(self):
        repo = self.base / "repo"
        repo.mkdir()
        git = lambda *args: subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "-c", "commit.gpgsign=false", *args],
            cwd=repo, check=True, capture_output=True)
        git("init", "-q")
        for number in (1, 2):
            (repo / "file.txt").write_text(str(number))
            git("add", "file.txt")
            git("commit", "-q", "-m", f"change {number}")
        config = self.base / "git-repos.json"
        config.write_text(json.dumps({"repos": [{"name": "demo", "path": str(repo)}]}))
        for _ in range(2):  # the documented schedule re-runs this every few minutes
            proc = self.script("ingest_git.py", "--config", str(config))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["errors"], 0)
            self.assertEqual(self.count("source='git'"), 2)

    def test_claude_adapter_does_not_duplicate_sessions_on_a_second_run(self):
        sessions = self.base / "sessions" / "project"
        sessions.mkdir(parents=True)
        (sessions / "one.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                                                        "message": {"role": "user", "content": "hello"}}) + "\n")
        for _ in range(2):
            proc = self.script("ingest_claude.py", "--root", str(self.base / "sessions"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.count("source='claude'"), 1)

    def test_health_probe_is_actually_recorded(self):
        proc = self.script("health_check.py", "--record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT payload_json FROM events WHERE source='health' AND type='probe'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(json.loads(rows[0][0])["summary"].startswith("health probe:"))

    def test_saving_a_session_without_lists_keeps_saved_decisions_and_sources(self):
        result = self.python("""
            import json, workspace, customer_runtime as customer
            from record_event import append_event
            event = append_event(source="manual", event_type="note", payload={"summary": "source note"})["event_id"]
            cid = customer.create_connection("test", "Test", read_only=False)["connection_id"]
            workspace.save_session("p", "first", "do A", 0, cid, decisions=["use sqlite"],
                                   blockers=["waiting on key"], event_ids=[event])
            # An agent that only reports progress must not erase what was decided.
            workspace.save_session("p", "second", "do B", 1, cid)
            kept = workspace.read_project("p")["payload"]
            workspace.save_session("p", "third", "do C", 2, cid, decisions=[], event_ids=[])
            cleared = workspace.read_project("p")["payload"]
            print(json.dumps({"kept": kept, "cleared": cleared, "event": event}))
        """)
        self.assertEqual(result["kept"]["summary"], "second")
        self.assertEqual(result["kept"]["decisions"], ["use sqlite"])
        self.assertEqual(result["kept"]["blockers"], ["waiting on key"])
        self.assertEqual(result["kept"]["session_sources"]["event_ids"], [result["event"]])
        self.assertEqual(result["cleared"]["decisions"], [])
        self.assertEqual(result["cleared"]["blockers"], ["waiting on key"])
        self.assertEqual(result["cleared"]["session_sources"]["event_ids"], [])

    def test_a_lock_left_by_a_killed_process_does_not_block_a_document_forever(self):
        result = self.python("""
            import json, os, time, artifacts, customer_runtime as customer
            cid = customer.create_connection("test", "Test", read_only=False)["connection_id"]
            made = artifacts.create("plan", "v1", cid)
            lock = artifacts.ROOT / "plan" / ".lock"
            lock.write_text("")
            old = time.time() - artifacts.STALE_LOCK_SECONDS - 5
            os.utime(lock, (old, old))
            started = time.time()
            edited = artifacts.edit(made["artifact_id"], "v2", 1, cid)
            print(json.dumps({"revision": edited["revision"], "seconds": time.time() - started}))
        """)
        self.assertEqual(result["revision"], 2)
        self.assertLess(result["seconds"], 5)

    def test_documents_stay_readable_after_the_data_directory_moves(self):
        made = self.python("""
            import json, artifacts, customer_runtime as customer
            cid = customer.create_connection("test", "Test", read_only=False)["connection_id"]
            made = artifacts.create("brief", "first", cid)
            artifacts.edit(made["artifact_id"], "second", 1, cid)
            print(json.dumps({"id": made["artifact_id"], "cid": cid}))
        """)
        moved = self.base / "restored"
        moved.mkdir()
        shutil.move(str(self.base / "artifacts"), str(moved / "artifacts"))
        shutil.move(str(self.base / "auth.db"), str(moved / "auth.db"))
        result = self.python(f"""
            import json, artifacts, workspace
            aid, cid = {made["id"]!r}, {made["cid"]!r}
            restored = artifacts.restore(aid, 1, 2, cid)
            print(json.dumps({{"read": artifacts.read(aid, cid)["content"],
                              "first": workspace.read_document(aid, 1)["content"],
                              "second": workspace.read_document(aid, 2)["content"],
                              "restored": restored["revision"]}}))
        """, BRAIN_AUTH_DB=str(moved / "auth.db"), BRAIN_ARTIFACT_ROOT=str(moved / "artifacts"))
        self.assertEqual(result, {"read": "first", "first": "first", "second": "second", "restored": 3})

    def test_embed_on_a_core_install_explains_itself(self):
        config = self.base / "starter.json"
        config.write_text(json.dumps({"version": 1, "business_id": "t", "owner_id": "t", "database": "events.db",
                                      "storage": {"root": "."}, "vectors": {"enabled": False},
                                      "crm": {"provider": "event", "options": {}}}))
        self.assertEqual(self.script("starter.py", "init", "--config", str(config)).returncode, 0)
        proc = self.script("starter.py", "embed", "--config", str(config))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("disabled", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_search_and_reindex_commands(self):
        config = self.base / "starter.json"
        config.write_text(json.dumps({"version": 1, "business_id": "t", "owner_id": "t", "database": "events.db",
                                      "storage": {"root": "."}, "vectors": {"enabled": False},
                                      "crm": {"provider": "event", "options": {}}}))
        self.assertEqual(self.script("starter.py", "init", "--config", str(config)).returncode, 0)
        env = {k: v for k, v in self.env.items() if not k.startswith("BRAIN_")}
        run = lambda *args: subprocess.run([sys.executable, str(ROOT / "brane.py"), *args, "--config", str(config),
                                            "--skip-install"], capture_output=True, text=True, env=env)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO events (ts, source, type, payload_json, ingested_at) VALUES "
                         "('2026-01-01T00:00:00+00:00', 'manual', 'note', ?, 'x')",
                         (json.dumps({"summary": "Pricing decision: keep the ten dollar plan"}),))
            conn.commit()
        found = run("search", "plan", "pricing")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("Pricing decision", found.stdout)
        self.assertIn("[all_terms]", found.stdout)
        self.assertEqual(json.loads(run("reindex").stdout), {"status": "ok", "indexed": 1})
        self.assertEqual(json.loads(run("doctor").stdout)["text_search_indexed"], 1)


if __name__ == "__main__":
    unittest.main()
