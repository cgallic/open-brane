"""Exercise setup, real HTTP authentication, MCP, and restart persistence."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brane import initialize


class SelfHostingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='brane self host ')
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'starter.json'
        self.invoke('setup')
        self.token_path = self.config.parent / 'http-token'
        self.token = self.token_path.read_text().strip()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.process = None
        self.addCleanup(self.stop)

    def invoke(self, command):
        proc = subprocess.run([sys.executable, str(ROOT / 'brane.py'), command,
                               '--skip-install', '--config', str(self.config)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def start(self):
        self.log = tempfile.TemporaryFile(mode='w+')
        self.addCleanup(self.log.close)
        env = dict(os.environ)
        env.pop('BRAIN_HTTP_TOKEN', None)
        self.process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/http_server.py'),
                                         '--config', str(self.config), '--port', str(self.port)],
                                        stdout=self.log, stderr=self.log, env=env)
        for _ in range(100):
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail(self.log.read())
            try:
                with urlopen(self.url + '/healthz', timeout=.2) as response:
                    self.assertEqual(json.load(response), {'status': 'ok'})
                return
            except (URLError, TimeoutError):
                time.sleep(.05)
        self.fail('HTTP server did not become ready')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def request(self, path, body=None, token=None, headers=None):
        headers = dict(headers or {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if body is not None:
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        return urlopen(Request(self.url + path, data=body, headers=headers), timeout=5)

    def test_setup_preserves_identity_credentials_and_notes_across_restart(self):
        original = self.config.read_bytes()
        if os.name != 'nt':
            self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)
        self.start()
        with self.request('/api/events', {'summary': 'Remember 100% of this'}, self.token) as response:
            self.assertEqual(response.status, 201)
            event = json.load(response)
        self.stop()
        self.invoke('setup')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.token_path.read_text().strip(), self.token)
        self.start()
        with self.request('/api/events?q=100%25', token=self.token) as response:
            rows = json.load(response)
            self.assertEqual(rows[0]['id'], event['event_id'])
        self.assertEqual(json.loads(self.invoke('doctor').stdout)['status'], 'ok')
        mcp = json.loads(self.invoke('mcp-config').stdout)['mcpServers']['open-brane']
        self.assertEqual(mcp['command'], sys.executable)
        self.assertEqual(mcp['args'][-1], str(self.config))

    def test_authentication_input_policy_and_host_validation(self):
        self.start()
        for path in ('/api/events', '/mcp', '/mcp/'):
            for token in (None, 'wrong'):
                with self.subTest(path=path, token=token), self.assertRaises(HTTPError) as error:
                    self.request(path, token=token)
                self.assertEqual(error.exception.code, 401)
        with self.request('/') as response:
            html = response.read().decode()
            self.assertIn('Unlock your brain', html)
            self.assertNotIn(self.token, html)
        for body in ({'summary': ' '}, [], {'summary': 4}, {'summary': 'sk-' + 'X' * 80}):
            with self.subTest(body=body):
                with self.assertRaises(HTTPError) as error:
                    self.request('/api/events', body=body, token=self.token)
                self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/events', body={'summary': 'a' * 300000}, token=self.token)
        self.assertEqual(error.exception.code, 413)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/events', token=self.token, headers={'Host': 'attacker.example'})
        self.assertEqual(error.exception.code, 400)

    def test_real_http_mcp_shares_browser_database(self):
        self.start()

        async def run():
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, trust_env=False) as client:
                async with streamable_http_client(self.url + '/mcp', http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        names = {tool.name for tool in (await session.list_tools()).tools}
                        self.assertIn('record_event', names)
                        result = await session.call_tool('record_event', {'source': 'test', 'type': 'note', 'payload': {'summary': 'HTTP MCP memory'}})
                        self.assertFalse(result.isError, result)
        asyncio.run(run())
        with self.request('/api/events?q=HTTP', token=self.token) as response:
            self.assertEqual(json.load(response)[0]['payload']['summary'], 'HTTP MCP memory')

    def test_server_refuses_missing_or_weak_credentials(self):
        self.token_path.write_text('weak')
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/http_server.py'), '--config', str(self.config)], capture_output=True, text=True, env={**os.environ, 'BRAIN_HTTP_TOKEN': ''})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('at least 32', proc.stderr)

    def test_customer_workflow_http_retry_context_and_restart(self):
        self.start()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/customer-context?contact=alex')
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(HTTPError) as invalid_scope:
            self.request('/api/customer-context?contact=alex&business_id=foreign', token=self.token)
        self.assertEqual(invalid_scope.exception.code, 400)
        with self.request('/api/contacts', {'contact_id': 'alex', 'kind': 'lead', 'contact_policy': 'allowed'}, self.token):
            pass
        body = {'contact': 'alex', 'mailbox': 'demo-line', 'agent': 'voice', 'channel': 'call',
                'direction': 'inbound', 'message_id': 'call-1', 'summary': 'Please call back tomorrow.'}
        with self.request('/api/interactions', body, self.token) as response:
            first = json.load(response)
            self.assertEqual(response.status, 201)
        with self.request('/api/interactions', body, self.token) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)['event_id'], first['event_id'])
        with self.assertRaises(HTTPError) as error:
            self.request('/api/interactions', {**body, 'summary': 'Changed'}, self.token)
        self.assertEqual(error.exception.code, 409)
        self.stop(); self.start()
        with self.request('/api/customer-context?contact=alex', token=self.token) as response:
            result = json.load(response)
        self.assertEqual(result['latest_interaction']['summary'], body['summary'])
        self.assertEqual(result['decision']['whose_turn'], 'awaiting_us')
        self.assertFalse(result['send_authorized'])
        # A browser/HTTP-created interaction is readable through real HTTP MCP.
        async def read_mcp():
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, trust_env=False) as client:
                async with streamable_http_client(self.url + '/mcp', http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        answer = await session.call_tool('customer_context', {'contact': 'alex'})
                        self.assertFalse(answer.isError)
                        data = answer.structuredContent
                        if data is None:
                            data = json.loads(next(block.text for block in answer.content if block.type == 'text'))
                        self.assertEqual(data.get('result', data)['evidence'], result['evidence'])
        asyncio.run(read_mcp())
        # Run the documented standard-library adapter twice against this server.
        command = [sys.executable, str(ROOT / 'examples/push_customer_handoff.py'),
                   '--url', self.url, '--token-file', str(self.token_path)]
        for expected_retry in (False, True):
            example = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(example.returncode, 0, example.stderr)
            data = json.loads(example.stdout)
            self.assertEqual(data['deduped'], expected_retry)
            self.assertEqual(data['customer_context']['decision']['verdict'], 'caution')

    def test_customer_workflow_rejects_unsafe_inputs_without_writes(self):
        self.start()
        invalid = [('/api/contacts', {'contact_id': 'alex', 'business_id': 'other'}),
                   ('/api/contacts', {'contact_id': 'alex', 'db_path': '/tmp/other.db'}),
                   ('/api/contacts', []), ('/api/interactions', {'contact': 'unknown'})]
        for path, body in invalid:
            with self.subTest(path=path, body=body):
                with self.assertRaises(HTTPError) as error:
                    self.request(path, body, self.token)
                self.assertEqual(error.exception.code, 400)
        with self.request('/api/contacts', {'contact_id': 'alex'}, self.token):
            pass
        with self.assertRaises(HTTPError) as error:
            self.request('/api/interactions', {'contact': 'alex', 'mailbox': 'line', 'agent': 'voice',
                'message_id': 'secret', 'summary': 'sk-' + 'X' * 80}, self.token)
        self.assertEqual(error.exception.code, 422)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/interactions', {'summary': 'x' * 270000}, self.token)
        self.assertEqual(error.exception.code, 413)
        with self.request('/api/customer-context?contact=alex', token=self.token) as response:
            self.assertEqual(json.load(response)['timeline'], [])

    def test_scripted_two_client_demo(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'brane.py'), 'demo', '--skip-install'],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['clients'], 2)
        self.assertTrue(result['stale_document_edit_rejected'])
        self.assertEqual(result['messages_sent'], 0)

    def test_owner_workspace_reads_real_saved_projects_and_documents(self):
        self.start()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/workspace')
        self.assertEqual(error.exception.code, 401)
        async def save_and_list():
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            def unpack(result):
                self.assertFalse(result.isError)
                data = result.structuredContent
                if data is None:
                    data = json.loads(next(block.text for block in result.content if block.type == 'text'))
                return data.get('result', data)
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, trust_env=False) as client:
                async with streamable_http_client(self.url + '/mcp', http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        connection = unpack(await session.call_tool('create_connection', {'provider': 'test', 'label': 'Research client'}))
                        await session.call_tool('save_project_checkpoint', {'project_id': 'research-project',
                            'payload': {'goal': 'Review source evidence', 'next_action': 'Read paper two'}, 'connection_id': connection['connection_id']})
                        doc = unpack(await session.call_tool('create_artifact', {'slug': 'research-notes',
                            'content': 'Saved findings <script>never render as HTML</script>', 'connection_id': connection['connection_id']}))
                        overview = unpack(await session.call_tool('workspace_overview', {}))
                        self.assertEqual(overview['projects'][0]['next_action'], 'Read paper two')
                        project = unpack(await session.call_tool('read_saved_project', {'project_id': 'research-project'}))
                        self.assertEqual(project['payload']['goal'], 'Review source evidence')
                        read = unpack(await session.call_tool('read_saved_document', {'artifact_id': doc['artifact_id']}))
                        self.assertEqual(read['content'], doc['content'])
                        return doc
        doc = asyncio.run(save_and_list())
        self.stop(); self.start()
        with self.request('/api/workspace', token=self.token) as response:
            overview = json.load(response)
        self.assertEqual(overview['projects'][0]['project_id'], 'research-project')
        self.assertEqual(overview['documents'][0]['artifact_id'], doc['artifact_id'])
        with self.request('/api/workspace/project?project_id=research-project', token=self.token) as response:
            self.assertEqual(json.load(response)['payload']['next_action'], 'Read paper two')
        with self.request('/api/workspace/document?artifact_id=' + doc['artifact_id'], token=self.token) as response:
            self.assertEqual(json.load(response)['content'], doc['content'])
        with self.assertRaises(HTTPError) as error:
            self.request('/api/workspace/project?project_id=missing', token=self.token)
        self.assertEqual(error.exception.code, 404)
        with self.request('/') as response:
            html = response.read().decode()
            self.assertLess(html.index('Pick up where you left off'), html.index('Customer follow-up tools'))


if __name__ == '__main__':
    unittest.main()
