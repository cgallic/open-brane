import importlib, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

class CustomerFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.modules = {}
        self.original_env = {key: os.environ.get(key) for key in ("BRAIN_OWNER_ID", "BRAIN_AUTH_DB", "BRAIN_ARTIFACT_ROOT", "BRAIN_DB", "BRAIN_ROOT")}

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        if "record_event" in sys.modules:
            record_event = sys.modules["record_event"]
            record_event.DB = Path(os.environ.get("BRAIN_DB", ROOT / "events.db"))

    def instance(self, name):
        folder = self.base / name; folder.mkdir()
        cfg = {"version": 1, "business_id": name, "owner_id": name + "-owner", "public_origin": "https://" + name + ".example.test", "database": "events.db", "storage": {"root": "data", "auth_db": "data/auth.db", "artifact_root": "data/artifacts", "vector_namespace": "vector-" + name}, "vectors": {"enabled": False}, "crm": {"provider": "event", "options": {}}}
        path = folder / "starter.json"; path.write_text(json.dumps(cfg), encoding="utf-8")
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/starter.py"), "init", "--config", str(path)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return folder, cfg

    def load_modules(self, folder, cfg):
        os.environ.update({"BRAIN_OWNER_ID": cfg["owner_id"], "BRAIN_AUTH_DB": str(folder / "data/auth.db"), "BRAIN_ARTIFACT_ROOT": str(folder / "data/artifacts"), "BRAIN_DB": str(folder / "events.db"), "BRAIN_ROOT": str(ROOT)})
        import customer_runtime, artifacts, record_event, mcp_server
        for module in (customer_runtime, artifacts, mcp_server):
            self.modules[module.__name__] = importlib.reload(module)
        return self.modules["customer_runtime"], self.modules["artifacts"], record_event, self.modules["mcp_server"]

    def test_two_instances_are_isolated_and_lexical_search_needs_no_gpu(self):
        one, c1 = self.instance("one"); two, c2 = self.instance("two")
        rt, art, record, mcp = self.load_modules(one, c1)
        record.append_event(source="test", event_type="note", payload={"summary": "one private lexical phrase"})
        self.assertEqual(len(mcp.search_events("private lexical phrase")), 1)
        self.load_modules(two, c2)
        self.assertEqual(mcp.search_events("private lexical phrase"), [])
        self.assertNotEqual(c1["storage"]["vector_namespace"], c2["storage"]["vector_namespace"])

    def test_connections_checkpoint_and_artifact_cas_history_restore_export(self):
        folder, cfg = self.instance("customer")
        rt, art, record, mcp = self.load_modules(folder, cfg)
        a = rt.create_connection("claude", "Claude"); b = rt.create_connection("chatgpt", "ChatGPT")
        self.assertNotEqual(a["connection_id"], b["connection_id"])
        rt.save_checkpoint("project-1", {"goal": "continue", "decision": "ship"}, a["connection_id"])
        self.assertEqual(rt.resume_checkpoint("project-1", b["connection_id"])["payload"]["goal"], "continue")
        body = "é" * 11000
        made = art.create("brief-2026", body, a["connection_id"], provenance={"source": "test"})
        self.assertGreaterEqual(made["byte_length"], 20000)
        edited = art.edit(made["artifact_id"], body + "\nrevision", 1, b["connection_id"])
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            art.edit(made["artifact_id"], "stale", 1, a["connection_id"])
        self.assertEqual(len(art.history(made["artifact_id"], a["connection_id"])), 2)
        restored = art.restore(made["artifact_id"], 1, edited["revision"], a["connection_id"])
        self.assertEqual(restored["content"], body)
        exported = art.export(made["artifact_id"], b["connection_id"])
        self.assertEqual(exported["export_bytes"], body.encode())
        rt.revoke_connection(a["connection_id"])
        with self.assertRaises(PermissionError): art.read(made["artifact_id"], a["connection_id"])
        with self.assertRaises(ValueError): art.create("../escape", "x", b["connection_id"])

    def test_session_handoff_pins_sources_and_restores_history(self):
        import sqlite3
        from contextlib import closing
        import workspace
        folder, cfg = self.instance("sessions")
        rt, art, record, mcp = self.load_modules(folder, cfg)
        a = rt.create_connection("client-a", "First session")["connection_id"]
        b = rt.create_connection("client-b", "Next session")["connection_id"]
        event = record.append_event(source="test", event_type="note", payload={"summary": "Decision evidence"})
        doc = art.create("research", "Original source", a)
        rt.save_checkpoint("research", {"goal": "Keep original goal", "custom": 42}, a)
        saved = mcp.save_work_session("research", "Read the paper", "Check the appendix", 1, a,
                                     ["Use source data"], ["Need appendix"], [event["event_id"]], [doc["artifact_id"]])
        self.assertEqual(saved["revision"], 2)
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            mcp.save_work_session("research", "Stale edit", "Wrong next step", 1, b)
        art.edit(doc["artifact_id"], "Updated source", 1, b)
        packet = mcp.resume_work_session("research")
        self.assertEqual(packet["payload"]["custom"], 42)
        self.assertEqual(packet["payload"]["next_action"], "Check the appendix")
        self.assertEqual(packet["events"][0]["summary"], "Decision evidence")
        self.assertEqual(packet["documents"][0]["revision"], 1)
        self.assertIn("has changed", packet["warnings"][0])
        self.assertEqual(mcp.read_saved_document(doc["artifact_id"], 1)["content"], "Original source")
        restored = mcp.restore_project_checkpoint("research", 1, 2, b)
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(mcp.resume_project_checkpoint("research", b)["payload"], {"goal": "Keep original goal", "custom": 42})
        self.assertEqual([r["revision"] for r in rt.checkpoint_history("research", b)], [3, 2, 1])
        with self.assertRaises(ValueError):
            workspace.save_session("missing", "summary", "next", 0, a, event_ids=[999999])
        ro = rt.create_connection("reader", "Read only", read_only=True)["connection_id"]
        with self.assertRaises(PermissionError):
            workspace.save_session("research", "summary", "next", 3, ro)
        rt.revoke_connection(a)
        with self.assertRaises(PermissionError):
            workspace.save_session("research", "summary", "next", 3, a)
        with closing(sqlite3.connect(rt.AUTH_DB)) as conn:
            conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?)", ("foreign", "another-owner", '{}', b, "2020-01-01"))
            conn.commit()
        with self.assertRaises(PermissionError):
            rt.save_checkpoint("foreign", {"overwrite": True}, b)
        with self.assertRaises(FileNotFoundError):
            rt.checkpoint_history("foreign", b)
        with self.assertRaises(FileNotFoundError):
            rt.restore_checkpoint("foreign", 1, 1, b)
        # A missing source never replaces the current checkpoint.
        with self.assertRaises(ValueError):
            workspace.save_session("research", "invalid update", "next", 3, b, event_ids=[999999])
        self.assertEqual(workspace.read_project("research")["revision"], 3)
        # Existing deployments retain their last checkpoint as revision 1 on first edit.
        with closing(sqlite3.connect(rt.AUTH_DB)) as conn:
            conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?)", ("legacy", rt.OWNER, '{"old":true}', b, "2020-01-01"))
            conn.commit()
        self.assertEqual(workspace.read_project("legacy")["revision"], 1)
        rt.save_checkpoint("legacy", {"new": True}, b, expected_revision=1)
        self.assertEqual(rt.checkpoint_history("legacy", b)[1]["payload"], {"old": True})

    def test_concurrent_checkpoint_saves_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        folder, cfg = self.instance("race")
        rt, _, _, _ = self.load_modules(folder, cfg)
        cid = rt.create_connection("test", "Concurrent sessions")["connection_id"]
        rt.save_checkpoint("race", {"summary": "start"}, cid, 0)
        def save(value):
            try:
                return rt.save_checkpoint("race", {"summary": value}, cid, 1)["revision"]
            except RuntimeError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["one", "two"]))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(len(rt.checkpoint_history("race", cid)), 2)

    def test_read_only_connection_cannot_write(self):
        folder, cfg = self.instance("readonly")
        rt, art, _, _ = self.load_modules(folder, cfg)
        ro = rt.create_connection("claude", "Read only", ["brain:read"], True)
        with self.assertRaises(PermissionError): rt.save_checkpoint("p", {}, ro["connection_id"])
        with self.assertRaises(PermissionError): art.create("x", "content", ro["connection_id"])

if __name__ == "__main__": unittest.main()
