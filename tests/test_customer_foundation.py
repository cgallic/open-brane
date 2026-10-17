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

    def test_read_only_connection_cannot_write(self):
        folder, cfg = self.instance("readonly")
        rt, art, _, _ = self.load_modules(folder, cfg)
        ro = rt.create_connection("claude", "Read only", ["brain:read"], True)
        with self.assertRaises(PermissionError): rt.save_checkpoint("p", {}, ro["connection_id"])
        with self.assertRaises(PermissionError): art.create("x", "content", ro["connection_id"])

if __name__ == "__main__": unittest.main()
