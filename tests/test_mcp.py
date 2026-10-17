import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def output(result):
    if result.isError:
        raise AssertionError(str(result.content))
    if result.structuredContent is not None:
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(next(block.text for block in result.content if block.type == "text"))


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_tools_with_vectors_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            config = json.loads((ROOT / "config/starter.example.json").read_text())
            config.update(business_id="test-business", database="events.db")
            path = Path(folder) / "starter.json"
            path.write_text(json.dumps(config))
            init = subprocess.run([sys.executable, str(ROOT / "scripts/starter.py"), "init", "--config", str(path)], capture_output=True, text=True)
            self.assertEqual(init.returncode, 0, init.stderr)
            server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts/starter.py"), "run", "--config", str(path)], env=dict(os.environ))
            async with stdio_client(server) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertTrue({"record_event", "query_events", "semantic_search", "report_usage", "propose_edge", "health_check", "search_events", "check_outreach", "outreach_history", "find_contact", "log_outreach", "upsert_contact", "upsert_deal", "get_pipeline"}.issubset(names))
                    event = output(await session.call_tool("record_event", {"source": "test", "type": "note", "payload": {"summary": "portable event 100%"}}))
                    self.assertEqual(event["status"], "ok")
                    query = output(await session.call_tool("query_events", {"source": "test"}))
                    self.assertEqual(query[0]["payload"]["summary"], "portable event 100%")
                    literal = output(await session.call_tool("search_events", {"query": "100%", "source": "test"}))
                    self.assertEqual(len(literal), 1)
                    missing = output(await session.call_tool("semantic_search", {"query": "portable"}))
                    self.assertEqual(missing["status"], "backend_unavailable")
                    health = output(await session.call_tool("health_check", {}))
                    self.assertTrue(health["all_healthy"])
                    self.assertFalse(health["semantic_search_available"])
                    bad_order = await session.call_tool("query_events", {"order": "desc; drop table events"})
                    self.assertTrue(bad_order.isError)
                    # A real contact snapshot and real deal tools run locally; no outbound provider action.
                    contact = output(await session.call_tool("upsert_contact", {"contact_id": "person-1", "email": "person@example.test", "display_name": "Test Contact"}))
                    self.assertEqual(contact["contact_id"], "person-1")
                    found = output(await session.call_tool("find_contact", {"query": "person@example.test"}))
                    self.assertEqual(found[0]["contact_id"], "person-1")
                    deal = output(await session.call_tool("upsert_deal", {"deal_id": "deal-1", "contact_id": "person-1", "title": "Starter deal", "next_action": "Review request"}))
                    self.assertEqual(deal["deal_id"], "deal-1")
                    pipeline = output(await session.call_tool("get_pipeline", {}))
                    self.assertEqual(pipeline[0]["deal_id"], "deal-1")
                    gate = output(await session.call_tool("check_outreach", {"contact": "person@example.test"}))
                    self.assertEqual(gate["verdict"], "caution")
                    self.assertEqual(gate["exit"], 1)
                    touch = output(await session.call_tool("log_outreach", {"contact": "person@example.test", "mailbox": "test@example.test", "agent": "test-agent", "message_id": "test-receipt"}))
                    self.assertFalse(touch["sent"])
                    history = output(await session.call_tool("outreach_history", {"contact": "person@example.test"}))
                    self.assertEqual(history[0]["message_id"], "test-receipt")

    async def test_customer_mcp_runtime_isolated_connections_and_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = []
            for name in ("mcp-one", "mcp-two"):
                folder = Path(root) / name; folder.mkdir()
                cfg = {"version": 1, "business_id": name, "owner_id": name + "-owner", "public_origin": "https://" + name + ".example.test", "database": "events.db", "storage": {"root": "data", "auth_db": "data/auth.db", "artifact_root": "data/artifacts", "vector_namespace": "vector-" + name}, "vectors": {"enabled": False}, "crm": {"provider": "event", "options": {}}}
                path = folder / "starter.json"; path.write_text(json.dumps(cfg), encoding="utf-8")
                init = subprocess.run([sys.executable, str(ROOT / "scripts/starter.py"), "init", "--config", str(path)], capture_output=True, text=True)
                self.assertEqual(init.returncode, 0, init.stderr)
                server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts/starter.py"), "run", "--config", str(path)], env=dict(os.environ))
                sessions.append(stdio_client(server))
            async with sessions[0] as (r1, w1), sessions[1] as (r2, w2):
                async with ClientSession(r1, w1) as one, ClientSession(r2, w2) as two:
                    await one.initialize(); await two.initialize()
                    c1 = output(await one.call_tool("create_connection", {"provider": "claude", "label": "A"}))
                    c2 = output(await two.call_tool("create_connection", {"provider": "chatgpt", "label": "B"}))
                    self.assertNotEqual(c1["connection_id"], c2["connection_id"])
                    await one.call_tool("save_project_checkpoint", {"project_id": "p", "payload": {"goal": "cross-account"}, "connection_id": c1["connection_id"]})
                    self.assertEqual(output(await one.call_tool("resume_project_checkpoint", {"project_id": "p", "connection_id": c1["connection_id"]}))["payload"]["goal"], "cross-account")
                    body = "é" * 11000
                    created = output(await one.call_tool("create_artifact", {"slug": "mcp-brief", "content": body, "connection_id": c1["connection_id"]}))
                    read = output(await one.call_tool("read_artifact", {"artifact_id": created["artifact_id"], "connection_id": c1["connection_id"]}))
                    self.assertEqual(read["byte_length"], len(body.encode()))
                    foreign = await two.call_tool("read_artifact", {"artifact_id": created["artifact_id"], "connection_id": c2["connection_id"]})
                    self.assertTrue(foreign.isError)


if __name__ == "__main__":
    unittest.main()
