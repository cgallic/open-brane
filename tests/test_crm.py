"""Live Brain contact/history meanings on OpenBrane canonical events."""
import json
import os
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import crm
from record_event import EventConflict


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "events.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.executescript((SCRIPTS.parent / "SCHEMA.sql").read_text())
        self.environment = patch.dict(os.environ, {"BRAIN_DB": str(self.database), "CRM_BUSINESS_ID": "business-a",
                                                  "CRM_PROVIDER": "event", "CRM_PROVIDER_OPTIONS_JSON": "{}"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def contact(self, contact_id="person", **kwargs):
        return crm.upsert_contact(contact_id, email=contact_id + "@example.invalid", contact_policy="allowed", **kwargs)

    def test_contact_deal_history_persist_after_reopen(self):
        self.contact()
        crm.upsert_deal("deal", "person", "Opportunity", stage="won", value_cents=5000, currency="USD")
        first = crm.log_outreach("person", "sender@example.invalid", "test", message_id="message", channel="sms")
        with closing(sqlite3.connect(self.database)) as conn:
            events = conn.execute("SELECT source,type,payload_json FROM events ORDER BY id").fetchall()
        self.assertEqual([e[1] for e in events], ["crm.contact", "crm.deal", "crm.touch"])
        self.assertTrue(all(e[0] == "crm" and json.loads(e[2])["business_id"] == "business-a" for e in events))
        self.assertEqual(crm.find_contact("person")[0]["whose_turn"], "awaiting_them")
        self.assertEqual(crm.outreach_history("person")[0]["channel"], "sms")
        self.assertFalse(first["sent"])
        self.assertFalse(first["delivery_verified"])
        self.assertFalse(crm.get_pipeline()[0]["revenue_verified"])

    def test_same_contact_id_email_independent_across_businesses(self):
        self.contact(display_name="Business A")
        crm.log_outreach("person", "sender@example.invalid", "test", message_id="same-message")
        os.environ["CRM_BUSINESS_ID"] = "business-b"
        self.assertEqual(crm.find_contact("person"), [])
        self.assertEqual(crm.outreach_history(), [])
        self.contact(display_name="Business B")
        crm.log_outreach("person", "sender@example.invalid", "test", message_id="same-message")
        self.assertEqual(crm.find_contact("person")[0]["display_name"], "Business B")
        self.assertEqual(len(crm.outreach_history()), 1)
        os.environ["CRM_BUSINESS_ID"] = "business-a"
        self.assertEqual(crm.find_contact("person")[0]["display_name"], "Business A")
        self.assertEqual(len(crm.outreach_history()), 1)
        with self.assertRaisesRegex(ValueError, "configured"):
            crm.find_contact("person", business_id="business-b")

    def test_cross_business_relationships_rejected(self):
        self.contact()
        os.environ["CRM_BUSINESS_ID"] = "business-b"
        with self.assertRaisesRegex(ValueError, "contact"):
            crm.upsert_deal("foreign", "person", "Wrong business")
        with self.assertRaisesRegex(ValueError, "contact"):
            crm.log_outreach("person", "sender", "test", message_id="foreign")
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_idempotent_touch_conflict_and_channel_preservation(self):
        self.contact()
        kwargs = dict(contact="person", mailbox="sender", agent="test", message_id="message", channel="linkedin")
        first, retry = crm.log_outreach(**kwargs), crm.log_outreach(**kwargs)
        self.assertEqual(first["event_id"], retry["event_id"])
        self.assertTrue(retry["deduped"])
        self.assertEqual(crm.outreach_history()[0]["channel"], "linkedin")
        with self.assertRaises(EventConflict):
            crm.log_outreach(subject="Changed subject", **kwargs)
        self.assertEqual(len(crm.outreach_history()), 1)

    def test_concurrent_touch_retries(self):
        self.contact()
        def record(_):
            return crm.log_outreach("person", "sender", "test", message_id="concurrent")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(record, range(8)))
        self.assertEqual(len({r["event_id"] for r in results}), 1)
        self.assertEqual(len(crm.outreach_history()), 1)

    def test_newer_inbound_changes_whose_turn_and_clears_reply(self):
        self.contact()
        now = datetime.now(timezone.utc)
        crm.log_outreach("person", "sender", "test", message_id="out", ts=crm._iso(now - timedelta(minutes=2)))
        self.assertEqual(crm.check_outreach("person")["verdict"], "caution")
        crm.log_outreach("person", "sender", "test", message_id="in", direction="inbound", ts=crm._iso(now - timedelta(minutes=1)))
        self.assertEqual(crm.find_contact("person")[0]["whose_turn"], "awaiting_us")
        gate = crm.check_outreach("person")
        self.assertEqual(gate["verdict"], "clear_to_contact")
        self.assertFalse(gate["send_authorized"])

    def test_blocks_take_precedence_over_new_inbound(self):
        cases = [("customer", "allowed", "do_not_contact"), ("internal", "allowed", "do_not_contact"),
                 ("dead", "allowed", "do_not_contact"), ("contact", "do_not_contact", "do_not_contact"),
                 ("contact", "hold", "caution"), ("contact", "unknown", "caution")]
        for index, (kind, policy, expected) in enumerate(cases):
            with self.subTest(kind=kind, policy=policy):
                identifier = "person-" + str(index)
                crm.upsert_contact(identifier, email=identifier + "@example.invalid", kind=kind, contact_policy=policy)
                crm.log_outreach(identifier, "sender", "test", message_id="in", direction="inbound")
                self.assertEqual(crm.check_outreach(identifier)["verdict"], expected)

    def test_stale_unknown_future_and_ambiguous_contacts_fail_closed(self):
        now = datetime.now(timezone.utc)
        self.contact(observed_at=crm._iso(now - timedelta(days=1)), valid_until=crm._iso(now - timedelta(hours=1)))
        self.assertEqual(crm.check_outreach("person")["verdict"], "caution")
        self.assertEqual(crm.check_outreach("missing")["verdict"], "caution")
        with self.assertRaisesRegex(ValueError, "future"):
            self.contact("future", observed_at=crm._iso(now + timedelta(days=1)))
        self.contact("one", display_name="Same Name")
        self.contact("two", display_name="Same Name")
        self.assertEqual(crm.check_outreach("Same Name")["verdict"], "caution")

    def test_explicit_phone_and_business_identity_uniqueness(self):
        with self.assertRaisesRegex(ValueError, "E.164"):
            crm.upsert_contact("bad-phone", phone="4155552671")
        crm.upsert_contact("international", phone="+59177712345")
        self.assertEqual(crm.find_contact("+59177712345")[0]["contact_id"], "international")
        with self.assertRaisesRegex(ValueError, "another contact"):
            crm.upsert_contact("duplicate", phone="+59177712345")

    def test_awaiting_reply_outside_cooldown_blocks(self):
        self.contact()
        crm.log_outreach("person", "sender", "test", message_id="old", ts=crm._iso(datetime.now(timezone.utc) - timedelta(days=10)))
        gate = crm.check_outreach("person")
        self.assertEqual(gate["verdict"], "caution")
        self.assertIn("awaiting_reply", gate["reason"])

    def test_customer_context_has_summary_evidence_and_fail_closed_identity(self):
        self.contact(kind="lead", display_name="Alex")
        touch = crm.log_outreach("person", "phone-line", "voice", message_id="call-1",
                                 channel="call", direction="inbound", summary="Requested a quote.")
        context = crm.customer_context("person")
        self.assertEqual(context["status"], "found")
        self.assertEqual(context["latest_interaction"]["summary"], "Requested a quote.")
        self.assertEqual(context["evidence"]["interaction_event_ids"], [touch["event_id"]])
        self.assertEqual(context["decision"]["whose_turn"], "awaiting_us")
        self.assertTrue(context["contact"]["policy_fresh"])
        self.assertFalse(context["send_authorized"])
        self.assertEqual(crm.customer_context("missing")["status"], "unknown")
        self.contact("second", display_name="Alex")
        ambiguous = crm.customer_context("Alex")
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertIsNone(ambiguous["contact"])
        self.assertEqual(ambiguous["timeline"], [])
        with self.assertRaises(ValueError):
            crm.customer_context("person", business_id="foreign")

    def test_interaction_summary_retries_preserve_identity_and_reject_changes(self):
        self.contact()
        args = dict(contact="person", mailbox="line", agent="voice", message_id="call-1", summary="Original call.")
        first = crm.log_outreach(**args)
        retry = crm.log_outreach(**args)
        self.assertEqual(first["event_id"], retry["event_id"])
        with self.assertRaises(EventConflict):
            crm.log_outreach(**{**args, "summary": "Different call."})
        self.assertEqual(crm.customer_context("person")["latest_interaction"]["summary"], "Original call.")

    def test_context_is_one_local_snapshot_during_concurrent_write(self):
        self.contact(kind="lead")
        original = crm._contacts
        written = False
        def read_and_insert(conn, business):
            nonlocal written
            rows = original(conn, business)
            if not written:
                written = True
                # A separate writer commits after this reader established its snapshot.
                crm.log_outreach("person", "sender", "other-client", message_id="racing-write")
            return rows
        with patch.object(crm, "_contacts", side_effect=read_and_insert):
            context = crm.customer_context("person")
        self.assertIsNone(context["latest_interaction"])
        self.assertEqual(context["decision"]["verdict"], "clear_to_contact")
        self.assertEqual(crm.customer_context("person")["decision"]["verdict"], "caution")


if __name__ == "__main__":
    unittest.main()
