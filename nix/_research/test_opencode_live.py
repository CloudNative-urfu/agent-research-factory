"""Contract test against the installed CLI with a synthetic HTTP model."""

import http.server
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import factory


class OpenCodeContract(unittest.TestCase):
    def test_real_cli_and_tool_execution(self):
        requests = []
        mode = {'pipeline': False}

        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                tools = body.get('tools', [])
                shell = next((t['function'] for t in tools
                              if 'command' in t.get('function', {}).get('parameters', {}).get('properties', {})), None)
                done = any(m.get('role') == 'tool' for m in body['messages'])
                delta = {'role': 'assistant', 'content': 'PROBE_OK'}
                finish = 'stop'
                if shell and not done:
                    command = 'printf PROBE_OK > probe.txt'
                    if mode['pipeline']:
                        text = json.dumps(body['messages'], ensure_ascii=False)
                        if 'окончательная независимая' in text:
                            stage = 'confirm'
                        elif 'Ты независимый критик' in text:
                            stage = 'review'
                        else:
                            stage = next(s for s, marker in {
                                'research': 'Создай artifacts/hypotheses.md',
                                'plan': 'Создай artifacts/protocol.json',
                                'build': 'Создай artifacts/submission.json',
                                'experiment': 'Создай artifacts/results.json',
                                'report': 'Создай artifacts/report.md',
                            }.items() if marker in text)
                        command = shlex.join(['python3', str(Path(__file__).parent.resolve() / 'test_worker_fixture.py'), stage])
                    delta = {'role': 'assistant', 'tool_calls': [{
                        'index': 0, 'id': 'call_probe', 'type': 'function',
                        'function': {'name': shell['name'], 'arguments': json.dumps({
                            'command': command,
                            'description': 'Write probe marker',
                        })},
                    }]}
                    finish = 'tool_calls'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for d, f in [(delta, None), ({}, finish)]:
                    chunk = {'id': 'chatcmpl-probe', 'object': 'chat.completion.chunk',
                             'created': 1, 'model': 'probe',
                             'choices': [{'index': 0, 'delta': d, 'finish_reason': f}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

        with tempfile.TemporaryDirectory(prefix='contract-', dir='/tmp/opencode') as tmp:
            root = Path(tmp)
            model = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Model)
            thread = threading.Thread(target=model.serve_forever, daemon=True)
            thread.start()
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'SSL_CERT_FILE', 'NIX_SSL_CERT_FILE')}
            for kind in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
                env[f'XDG_{kind}_HOME'] = str(root / kind.lower())
            env['OPENCODE_DISABLE_PROJECT_CONFIG'] = '1'
            env['PROBE_API_KEY'] = 'synthetic-not-a-secret'
            env['OPENCODE_PASSWORD'] = 'synthetic-test-password'
            cfg = root / 'config/opencode/opencode.json'
            cfg.parent.mkdir(parents=True)
            config = json.loads(Path(os.environ['RESEARCH_TEST_OPENCODE_CONFIG']).read_text()) if os.environ.get('RESEARCH_TEST_OPENCODE_CONFIG') else {}
            config.update({
                'default_agent': 'researcher',
                'permissions': [{'action': '*', 'resource': '*', 'effect': 'allow'}],
                'agents': {'researcher': {'mode': 'primary'}, 'critic': {'mode': 'all'}},
                'plugins': [str(Path(__file__).parent.resolve() / 'plugin')],
                'providers': {'probe': {
                    'env': ['PROBE_API_KEY'],
                    'package': '@opencode/ai/providers/openai-compatible',
                    'settings': {'baseURL': f'http://127.0.0.1:{model.server_port}/v1'},
                    'models': {'probe': {'capabilities': {'tools': True},
                                         'limit': {'context': 32000, 'output': 1000}}},
                }},
            })
            cfg.write_text(json.dumps(config))
            (root / 'handoff.json').write_text('{"previous": [{"summary": "HANDOFF_SENTINEL"}]}')
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.close()
            server_log = (root / 'server.log').open('w')
            server = subprocess.Popen(['opencode', 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                                      env=env, cwd=root, stdout=server_log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            url = f'http://127.0.0.1:{port}'
            try:
                last_error = None
                for _ in range(100):
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=1):
                            break
                    except OSError as exc:
                        last_error = str(exc)
                        time.sleep(0.1)
                else:
                    self.fail('Private server did not become ready: ' + str(last_error))
                result = subprocess.run([
                    shutil.which('opencode'), 'run', '--server', url, '--agent', 'researcher',
                    '--model', 'probe/probe', '--format', 'json', 'Write probe.txt using the shell.',
                ], env=env, cwd=root, capture_output=True, text=True, timeout=90, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(requests, result.stdout + result.stderr)
                self.assertTrue(any('HANDOFF_SENTINEL' in json.dumps(r['messages']) for r in requests))
                self.assertTrue((root / 'probe.txt').exists(),
                                json.dumps([r.get('tools') for r in requests]) + result.stdout + result.stderr)
                self.assertEqual((root / 'probe.txt').read_text(), 'PROBE_OK')
                listing = subprocess.run(['opencode', 'api', '--server', url, 'get', '/api/agent'],
                                         env=env, cwd=root, capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(listing.returncode, 0, listing.stdout)
                agents = json.loads(listing.stdout)['data']
                self.assertTrue({'researcher', 'critic'} <= {a['id'] for a in agents}, agents)
                if config.get('mcp', {}).get('servers'):
                    listing = subprocess.run(['opencode', 'api', '--server', url, 'get', '/api/mcp'],
                                             env=env, cwd=root, capture_output=True, text=True, timeout=30, check=False)
                    self.assertEqual(listing.returncode, 0, listing.stdout)
                    actual = json.loads(listing.stdout)
                    self.assertTrue(all(name in listing.stdout for name in config['mcp']['servers']), actual)
                if shutil.which('bwrap'):
                    mode['pipeline'] = True
                    brief = root / 'brief.md'
                    brief.write_text('Synthetic infrastructure test, not scientific evidence.')
                    campaign = root / 'campaign'
                    config = factory.default_config()
                    config['cycles'] = 2
                    factory.initialize(campaign, brief, config)
                    env['RESEARCH_WRITABLE_PATHS'] = ':'.join(env[f'XDG_{k}_HOME'] for k in ('CONFIG', 'DATA', 'STATE', 'CACHE'))

                    def invoke(role, stage, work, prompt, log):
                        argv = ['opencode', 'run', '--standalone', '--agent', role,
                                '--model', 'probe/probe', '--format', 'json', prompt]
                        result = factory.process(factory.sandbox(argv, work, campaign), work, log, 60, 16777216)
                        return result | factory.parse_output('opencode', log.read_text(), result['exit_code'])

                    with patch.dict(os.environ, env, clear=True):
                        runtime = factory.Runtime(campaign, config)
                        factory.run(campaign, invoke, runtime.verify)
                    state = factory.load(campaign / 'state.json')
                    self.assertEqual(state['status'], 'completed')
                    self.assertEqual(len(state['candidates']), 2)
                    self.assertTrue((campaign / 'confirmations/01/artifacts/review.json').exists())
            finally:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=10)
                server_log.close()
                model.shutdown()
                model.server_close()


if __name__ == '__main__':
    unittest.main()
