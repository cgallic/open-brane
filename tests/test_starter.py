import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_config import load_config


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="open brane test ")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config" / "starter.json"
        self.path.parent.mkdir()
        self.config = json.loads((ROOT / "config/starter.example.json").read_text())
        self.config["business_id"] = "test-business"
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def invoke(self, command):
        return subprocess.run([sys.executable, str(ROOT / "scripts/starter.py"), command, "--config", str(self.path)], text=True, capture_output=True)

    def test_validate_is_read_only_and_database_relative_to_config(self):
        proc = self.invoke("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        db = Path(self.temp.name) / "data" / "events.db"
        self.assertEqual(Path(json.loads(proc.stdout)["database"]), db)
        self.assertFalse(db.parent.exists())

    def test_initialize_is_repeatable_and_preserves_events(self):
        proc = self.invoke("init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, env = load_config(self.path)
        write = subprocess.run([sys.executable, str(ROOT / "scripts/record_event.py"), "--source", "test", "--type", "note", "--payload-stdin"], input=json.dumps({"summary": "preserved event"}), text=True, capture_output=True, env={**os.environ, **env})
        self.assertEqual(write.returncode, 0, write.stderr)
        self.assertEqual(self.invoke("init").returncode, 0)
        with closing(sqlite3.connect(env["BRAIN_DB"])) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"event_governance", "recall_traces", "event_edges"}.issubset(tables))
        doctor = self.invoke("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)["vector_health"]["status"], "disabled")

    def test_invalid_configuration_fails_without_writing_database(self):
        for replacement in ({"vectors": {"dimensions": 0}}, {"business_id": ""}, {"policy_mode": "off"}, {"vectors": {"enabled": "false"}}, {"vectors": {"ollama_url": "http://secret:credential@localhost"}}, {"version": True}, {"crm": {"provider": "supabase", "options": {"api_key": "unsafe-literal"}}}):
            with self.subTest(replacement=replacement):
                self.path.write_text(json.dumps({**self.config, **replacement}))
                proc = self.invoke("init")
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse((Path(self.temp.name) / "data/events.db").exists())

    def test_embedding_dimensions_reject_existing_incompatible_collection(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import embed_events
        client = Mock()
        client.get_collections.return_value = SimpleNamespace(collections=[SimpleNamespace(name=embed_events.COLLECTION)])
        client.get_collection.return_value = SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=384))))
        with self.assertRaisesRegex(ValueError, "dimensions differ"):
            embed_events.ensure_collection(client)
        client.create_collection.assert_not_called()

    def test_embedding_disabled_is_explicit_failure(self):
        self.assertEqual(self.invoke("init").returncode, 0)
        proc = self.invoke("embed")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("disabled", proc.stderr)

    def test_custom_provider_configuration_initializes_actual_factory(self):
        from unittest.mock import patch
        import crm_providers
        module = Path(self.temp.name) / "starter_test_provider.py"
        module.write_text("class Provider:\n    def __init__(self, options): self.options = options\n    def contact_facts(self, business_id, *, email=None, phone=None):\n        return {'business_id': business_id, 'email': email, 'provider_label': self.options['label']}\ndef build(options): return Provider(options)\n")
        self.config["crm"] = {"provider": "starter_test_provider:build", "options": {"label": "replacement"}}
        self.path.write_text(json.dumps(self.config))
        _, env = load_config(self.path)
        with patch.dict(os.environ, env), patch.object(sys, "path", [self.temp.name, *sys.path]):
            provider = crm_providers.load_provider()
            facts = provider.contact_facts("test-business", email="person@example.test")
        sys.modules.pop("starter_test_provider", None)
        self.assertEqual(facts["provider_label"], "replacement")
        self.assertEqual(facts["business_id"], "test-business")


if __name__ == "__main__":
    unittest.main()
