#!/usr/bin/env python3
"""Two scripted MCP clients share an isolated customer record. No LLM or sends."""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def output(result):
    if result.isError:
        raise AssertionError(str(result.content))
    if result.structuredContent is not None:
        return result.structuredContent.get('result', result.structuredContent)
    return json.loads(next(block.text for block in result.content if block.type == 'text'))


async def demo():
    with tempfile.TemporaryDirectory(prefix='brane customer demo ') as folder:
        config = Path(folder) / 'starter.json'
        config.write_text(json.dumps({'version': 1, 'business_id': 'demo-only', 'owner_id': 'demo-only',
                                      'database': 'events.db', 'storage': {'root': '.'},
                                      'vectors': {'enabled': False}, 'crm': {'provider': 'event'}}))
        subprocess.run([sys.executable, str(ROOT / 'scripts/starter.py'), 'init', '--config', str(config)],
                       capture_output=True, text=True, check=True)
        async with AsyncExitStack() as stack:
            sessions = []
            for _ in range(2):
                server = StdioServerParameters(command=sys.executable,
                    args=[str(ROOT / 'scripts/starter.py'), 'run', '--config', str(config)], env=dict(os.environ))
                reader, writer = await stack.enter_async_context(stdio_client(server))
                session = await stack.enter_async_context(ClientSession(reader, writer))
                await session.initialize()
                sessions.append(session)
            voice, followup = sessions

            async def call(session, tool, **arguments):
                return output(await session.call_tool(tool, arguments))

            await call(voice, 'upsert_contact', contact_id='alex', email='alex@example.invalid',
                       display_name='Alex Demo', kind='lead', contact_policy='allowed')
            interaction = dict(contact='alex', mailbox='demo-phone', agent='voice-client',
                               channel='call', direction='inbound', message_id='demo-call-001',
                               summary='Alex requested a quote and a callback tomorrow.')
            first = await call(voice, 'log_outreach', **interaction)
            retried = await call(voice, 'log_outreach', **interaction)
            assert first['event_id'] == retried['event_id'] and retried['deduped']
            a = await call(voice, 'customer_context', contact='alex')
            b = await call(followup, 'customer_context', contact='alex')
            assert a['evidence'] == b['evidence']
            assert b['latest_interaction']['summary'] == interaction['summary']
            assert b['decision']['whose_turn'] == 'awaiting_us'

            await call(followup, 'log_outreach', contact='alex', mailbox='demo-email', agent='followup-client',
                       message_id='demo-email-001', summary='Quote follow-up reported by the email workflow.')
            pause = await call(voice, 'customer_context', contact='alex')
            assert pause['decision']['verdict'] == 'caution'
            assert 'recently_contacted' in pause['decision']['reason']
            await call(voice, 'upsert_contact', contact_id='alex', email='alex@example.invalid',
                       kind='customer', contact_policy='allowed')
            excluded = await call(followup, 'customer_context', contact='alex')
            assert excluded['decision']['verdict'] == 'do_not_contact'

            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            await call(voice, 'upsert_contact', contact_id='stale-lead', kind='lead', contact_policy='allowed',
                       observed_at=yesterday.isoformat(), valid_until=(yesterday + timedelta(hours=2)).isoformat())
            stale = await call(followup, 'customer_context', contact='stale-lead')
            assert not stale['contact']['policy_fresh'] and stale['decision']['verdict'] == 'caution'

            c1 = await call(voice, 'create_connection', provider='demo', label='Voice client')
            c2 = await call(followup, 'create_connection', provider='demo', label='Follow-up client')
            document = await call(voice, 'create_artifact', slug='alex-handoff', content='Quote requested.', connection_id=c1['connection_id'])
            old = await call(followup, 'read_artifact', artifact_id=document['artifact_id'], connection_id=c2['connection_id'])
            await call(voice, 'edit_artifact', artifact_id=document['artifact_id'], expected_revision=1,
                       content='Quote prepared. Customer status updated.', connection_id=c1['connection_id'])
            conflict = await followup.call_tool('edit_artifact', {'artifact_id': document['artifact_id'],
                'expected_revision': old['revision'], 'content': 'Stale overwrite.', 'connection_id': c2['connection_id']})
            assert conflict.isError
            current = await call(followup, 'read_artifact', artifact_id=document['artifact_id'], connection_id=c2['connection_id'])
            history = await call(followup, 'artifact_history', artifact_id=document['artifact_id'], connection_id=c2['connection_id'])
            assert current['revision'] == 2 and len(history) == 2
            return {'status': 'passed', 'clients': 2, 'isolated_temporary_data': True,
                    'shared_interaction_event_id': first['event_id'], 'retry_deduplicated': True,
                    'inbound_reply_turn': b['decision']['whose_turn'],
                    'recent_outbound': pause['decision']['verdict'],
                    'existing_customer': excluded['decision']['verdict'],
                    'stale_policy': stale['decision']['verdict'],
                    'stale_document_edit_rejected': True, 'document_revisions_retained': len(history),
                    'messages_sent': 0, 'provider_delivery_verified': False}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(demo()), indent=2))
