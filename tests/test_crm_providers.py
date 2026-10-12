"""Provider replacement against HTTP serialization without live credentials."""
import os
import sqlite3
from contextlib import closing
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import crm
import crm_providers
from crm_providers import CRMProviderError, SupabaseFacts


class CRMProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.executescript((SCRIPTS.parent / "SCHEMA.sql").read_text())
        self.environment = patch.dict(os.environ, {"BRAIN_DB": str(self.database), "CRM_BUSINESS_ID": "business-a",
                                                  "CRM_PROVIDER": "event", "CRM_PROVIDER_OPTIONS_JSON": "{}"})
        self.environment.start()
        crm.upsert_contact("person", email="person@example.invalid", contact_policy="allowed")

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_supabase_request_contains_scope_identity_and_configured_credentials(self):
        with patch.dict(os.environ, {"DEST_CRM_URL": "https://crm.example.invalid", "DEST_CRM_KEY": "synthetic-test-only"}):
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(200, json=[{"business_id": "business-a", "email": "person@example.invalid", "lifecycle_status": "lead"}])
            provider = SupabaseFacts({"endpoint_env": "DEST_CRM_URL", "credential_env": "DEST_CRM_KEY"}, transport=httpx.MockTransport(respond))
            try:
                facts = provider.contact_facts("business-a", email="person@example.invalid")
            finally:
                provider.close()
        self.assertEqual(facts["business_id"], "business-a")
        self.assertEqual(requests[0].url.path, "/rest/v1/crm_contacts")
        self.assertEqual(requests[0].url.params["business_id"], "eq.business-a")
        self.assertEqual(requests[0].url.params["email"], "eq.person@example.invalid")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer synthetic-test-only")

    def test_supabase_wrong_scope_invalid_or_ambiguous_result_rejected(self):
        cases = [[{"business_id": "business-b"}], {"error": "invalid"},
                 [{"business_id": "business-a"}, {"business_id": "business-a"}]]
        with patch.dict(os.environ, {"SUPABASE_URL": "https://crm.example.invalid", "SUPABASE_SERVICE_KEY": "synthetic-test-only"}):
            for payload in cases:
                with self.subTest(payload=payload):
                    provider = SupabaseFacts(transport=httpx.MockTransport(lambda _, result=payload: httpx.Response(200, json=result)))
                    try:
                        with self.assertRaises(CRMProviderError):
                            provider.contact_facts("business-a", email="person@example.invalid")
                    finally:
                        provider.close()

    def test_missing_credentials_and_unknown_options_fail_closed(self):
        with patch.dict(os.environ, {"CRM_PROVIDER": "supabase", "SUPABASE_URL": "https://crm.example.invalid"}):
            os.environ.pop("SUPABASE_SERVICE_KEY", None)
            with self.assertRaisesRegex(CRMProviderError, "credential"):
                crm_providers.load_provider()
        with self.assertRaisesRegex(CRMProviderError, "unsupported"):
            SupabaseFacts({"service_key": "should-never-be-literal"})

    def test_provider_swap_through_actual_config_loader(self):
        import runtime_config
        module = types.ModuleType("synthetic_crm_plugin")
        class Destination:
            def contact_facts(self, business_id, *, email=None, phone=None):
                return {"business_id": business_id, "email": email, "lifecycle_status": "customer",
                        "updated_at": crm._iso(datetime.now(timezone.utc))}
        module.factory = lambda options: Destination()
        import json
        config = {"version": 1, "business_id": "business-a", "database": str(self.database),
                  "crm": {"provider": "synthetic_crm_plugin:factory", "options": {}}}
        config_file = Path(self.temp.name) / "swap.json"
        config_file.write_text(json.dumps(config))
        self.assertEqual(crm.check_outreach("person")["verdict"], "clear_to_contact")
        with patch.dict(sys.modules, {"synthetic_crm_plugin": module}):
            _, environment = runtime_config.load_config(config_file)
            with patch.dict(os.environ, environment):
                result = crm.check_outreach("person")
        self.assertEqual(result["verdict"], "do_not_contact")
        self.assertFalse(result["send_authorized"])

    def test_external_unavailable_missing_mismatched_held_or_stale_facts_cannot_clear(self):
        scenarios = ["unavailable", "no-match", "wrong-business", "wrong-identity", "stale",
                     "missing-timestamp", "future-timestamp", "held", "unknown", "missing-status", "arbitrary-status"]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                class Destination:
                    def contact_facts(self, business_id, *, email=None, phone=None):
                        if scenario == "unavailable":
                            raise RuntimeError("private-token-should-never-leak")
                        if scenario == "no-match":
                            return None
                        result = {"business_id": "wrong" if scenario == "wrong-business" else business_id,
                                  "email": "wrong@example.invalid" if scenario == "wrong-identity" else email,
                                  "lifecycle_status": "lead",
                                  "updated_at": crm._iso(datetime.now(timezone.utc) - timedelta(days=2) if scenario == "stale" else datetime.now(timezone.utc))}
                        if scenario == "missing-timestamp":
                            result.pop("updated_at")
                        if scenario == "future-timestamp":
                            result["updated_at"] = crm._iso(datetime.now(timezone.utc) + timedelta(days=2))
                        if scenario in {"held", "unknown", "arbitrary-status"}:
                            result["lifecycle_status"] = {"held": "hold", "unknown": "unknown", "arbitrary-status": "invented"}[scenario]
                        if scenario == "missing-status":
                            result.pop("lifecycle_status")
                        return result
                with patch.object(crm, "load_provider", lambda: Destination()):
                    result = crm.check_outreach("person")
                self.assertEqual(result["verdict"], "caution")
                self.assertNotIn("private-token", str(result))

    def test_provider_http_failure_safe(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://crm.example.invalid", "SUPABASE_SERVICE_KEY": "synthetic-test-only"}):
            provider = SupabaseFacts(transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"key": "do-not-expose"})))
            try:
                with self.assertRaisesRegex(CRMProviderError, "lookup failed") as caught:
                    provider.contact_facts("business-a", email="person@example.invalid")
                self.assertNotIn("do-not-expose", str(caught.exception))
            finally:
                provider.close()


if __name__ == "__main__":
    unittest.main()
