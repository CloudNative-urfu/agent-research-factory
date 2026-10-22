"""Actual Claude Code CLI against a synthetic Anthropic-compatible server."""

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import factory

CLAUDE = os.environ.get('RESEARCH_TEST_CLAUDE') or shutil.which('claude')


@unittest.skipUnless(CLAUDE, 'requires Claude Code')
class ClaudeContract(unittest.TestCase):
    def test_real_tool_execution_and_result(self):
        requests = []

        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                requests.append((self.path, body))
                if 'messages' not in body or self.path.endswith('count_tokens'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"input_tokens":10}')
                    return
                done = any(isinstance(m.get('content'), list) and
                           any(c.get('type') == 'tool_result' for c in m['content']) for m in body['messages'])
                tool = next((t for t in body.get('tools', []) if t.get('name') == 'Bash'), None)
                use_tool = tool and not done
                block = ({'type': 'tool_use', 'id': 'toolu_probe', 'name': 'Bash', 'input': {}}
                         if use_tool else {'type': 'text', 'text': ''})
                delta = ({'type': 'input_json_delta', 'partial_json': json.dumps({
                    'command': 'printf PROBE_OK > probe.txt', 'description': 'Write marker'})}
                    if use_tool else {'type': 'text_delta', 'text': 'PROBE_OK'})
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                events = [
                    ('message_start', {'message': {'id': 'msg_probe', 'type': 'message', 'role': 'assistant',
                        'model': body.get('model'), 'content': [], 'stop_reason': None, 'stop_sequence': None,
                        'usage': {'input_tokens': 10, 'output_tokens': 0}}}),
                    ('content_block_start', {'index': 0, 'content_block': block}),
                    ('content_block_delta', {'index': 0, 'delta': delta}),
                    ('content_block_stop', {'index': 0}),
                    ('message_delta', {'delta': {'stop_reason': 'tool_use' if use_tool else 'end_turn',
                                               'stop_sequence': None}, 'usage': {'output_tokens': 10}}),
                    ('message_stop', {}),
                ]
                for kind, value in events:
                    self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **value}) + '\n\n').encode())

        with tempfile.TemporaryDirectory(prefix='claude-contract-', dir='/tmp/opencode') as directory:
            root = Path(directory)
            model = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Model)
            threading.Thread(target=model.serve_forever, daemon=True).start()
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'SSL_CERT_FILE', 'NIX_SSL_CERT_FILE')}
            env.update(CLAUDE_CONFIG_DIR=str(root / 'profile'), ANTHROPIC_API_KEY='synthetic-not-a-secret',
                       ANTHROPIC_BASE_URL=f'http://127.0.0.1:{model.server_port}',
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1')
            try:
                result = subprocess.run([CLAUDE, '-p', '--output-format', 'json', '--model', 'opus',
                    '--permission-mode', 'bypassPermissions', '--setting-sources', 'user',
                    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', 'Bash',
                    '--', 'Write probe.txt with PROBE_OK using Bash.'],
                    cwd=root, env=env, capture_output=True, text=True, timeout=90, check=False)
                parsed = factory.parse_output('claude', result.stdout, result.returncode)
                self.assertTrue(parsed['ok'], result.stdout + result.stderr)
                self.assertTrue((root / 'probe.txt').exists(), result.stdout + result.stderr)
                self.assertEqual((root / 'probe.txt').read_text(), 'PROBE_OK')
                self.assertTrue(requests)
            finally:
                model.shutdown()
                model.server_close()


if __name__ == '__main__':
    unittest.main()
