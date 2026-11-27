#!/usr/bin/env python3
"""Three scripted MCP clients hand work along by role. No LLM, no network, temporary data.

A lead posts a research task and a build task that depends on it. The research
client claims, settles with proof, and the build client then receives that
result in its briefing. The script also shows that the build task cannot be
claimed early and that "done" without proof is refused.
"""
import asyncio
from contextlib import AsyncExitStack
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
        raise RuntimeError(next((b.text for b in result.content if b.type == 'text'), 'tool error'))
    if result.structuredContent is not None:
        return result.structuredContent.get('result', result.structuredContent)
    return json.loads(next(block.text for block in result.content if block.type == 'text'))


async def demo():
    with tempfile.TemporaryDirectory(prefix='brane role demo ') as folder:
        config = Path(folder) / 'starter.json'
        config.write_text(json.dumps({'version': 1, 'business_id': 'demo-only', 'owner_id': 'demo-only',
                                      'database': 'events.db', 'storage': {'root': '.'},
                                      'vectors': {'enabled': False}, 'crm': {'provider': 'event'}}))
        subprocess.run([sys.executable, str(ROOT / 'scripts/starter.py'), 'init', '--config', str(config)],
                       capture_output=True, text=True, check=True)
        async with AsyncExitStack() as stack:
            quiet = stack.enter_context(open(os.devnull, 'w'))  # server request logs
            sessions = []
            for _ in range(3):
                server = StdioServerParameters(command=sys.executable,
                    args=[str(ROOT / 'scripts/starter.py'), 'run', '--config', str(config)], env=dict(os.environ))
                reader, writer = await stack.enter_async_context(stdio_client(server, errlog=quiet))
                session = await stack.enter_async_context(ClientSession(reader, writer))
                await session.initialize()
                sessions.append(session)
            lead, researcher, builder = sessions

            async def call(session, tool, **arguments):
                return output(await session.call_tool(tool, arguments))

            research = await call(lead, 'post_task', role='research', posted_by='lead',
                                  title='Collect the five most common setup questions',
                                  brief='Read the issue tracker and list the five questions new users ask most.',
                                  done_when='A note listing five questions, each with one issue link.')
            build = await call(lead, 'post_task', role='build', posted_by='lead',
                               title='Add answers to the setup guide',
                               brief='Answer each question from the research task in docs/SETUP.md.',
                               done_when='A commit that adds the five answers.',
                               depends_on=[research['task_id']])
            print(f"lead posted {research['task_id']} for research and {build['task_id']} for build")

            early = await call(builder, 'claim_task', role='build', agent='builder')
            assert early['task'] is None and early['waiting_on_dependencies'] == 1
            print('builder asked for work: nothing ready, 1 task waiting on research')

            got = await call(researcher, 'claim_task', role='research', agent='researcher')
            print(f"researcher claimed {got['task']['task_id']} until {got['lease_until'][:16]}Z")
            try:
                await call(researcher, 'settle_task', task_id=got['task']['task_id'], claim_id=got['claim_id'],
                           agent='researcher', status='done', outcome='Listed them')
                raise AssertionError('done without proof was accepted')
            except RuntimeError:
                print('settling as done without proof: refused')
            note = await call(researcher, 'record_event', source='manual', type='note',
                              payload={'summary': 'Top setup questions: Windows path, token location, Docker port, '
                                                  'MCP config, backups'})
            await call(researcher, 'settle_task', task_id=got['task']['task_id'], claim_id=got['claim_id'],
                       agent='researcher', status='done', outcome='Five questions saved as a note.',
                       proof=[f"event:{note['event_id']}"])
            print(f"researcher settled done with proof event:{note['event_id']}")

            handed = await call(builder, 'claim_task', role='build', agent='builder')
            upstream = handed['briefing']['upstream_results'][0]
            assert upstream['proof'] == [f"event:{note['event_id']}"]
            print(f"builder claimed {handed['task']['task_id']} and received: \"{upstream['outcome']}\" "
                  f"(proof {upstream['proof'][0]})")

            board = await call(lead, 'list_tasks')
            print('board: ' + ', '.join(f"{t['role']}={t['status']}" for t in board))
    print('Demo data was temporary and has been deleted. Nothing was sent.')


if __name__ == '__main__':
    asyncio.run(demo())
