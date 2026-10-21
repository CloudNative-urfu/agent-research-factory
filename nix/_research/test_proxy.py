import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from llm_proxy import MODEL, check_api, make_server


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.auth = self.headers.get('Authorization')
        status = self.server.status
        self.send_response(status)
        self.send_header('Content-Type', 'text/event-stream' if self.server.body.get('stream') else 'application/json')
        self.send_header('Retry-After', '7')
        self.end_headers()
        if getattr(self.server, 'probe_mode', False):
            body = self.server.body
            if body.get('stream'):
                payload = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'
            elif body.get('tools'):
                payload = json.dumps({'choices': [{'message': {'tool_calls': [{'function': {'name': 'probe', 'arguments': '{"ok":true}'}}]}}]}).encode()
            else:
                payload = b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'
            self.wfile.write(payload)
            return
        self.wfile.write(self.server.payload)
        self.wfile.flush()
        if getattr(self.server, 'release', None):
            self.server.release.wait(5)
            self.wfile.write(b'data: [DONE]\n\n')


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.up = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.up.status = 200
        self.up.payload = b'{"choices":[],"usage":{"cost":0.01}}'
        self.proxy = make_server(0, 'fixture-key', upstream=f'http://127.0.0.1:{self.up.server_port}')
        for server in (self.up, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def request(self, body, path='/v1/chat/completions', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('POST', path, json.dumps(body), {'Content-Type': 'application/json', **(headers or {})})
        response = conn.getresponse()
        return response.status, response.read(), response.getheader('Retry-After')

    def test_routing_and_tools(self):
        tools = [{'type': 'function', 'function': {'name': 'probe', 'parameters': {'type': 'object'}}}]
        status, body, _ = self.request({'model': MODEL, 'messages': [], 'tools': tools, 'tool_choice': 'required'})
        self.assertEqual(status, 200)
        self.assertEqual(body, self.up.payload)
        self.assertEqual(self.up.auth, 'Bearer fixture-key')
        self.assertEqual(self.up.body['tools'], tools)
        self.assertEqual(self.up.body['provider'], {'only': ['darkbloom/fp4'], 'allow_fallbacks': False, 'require_parameters': True})

    def test_stream_and_midstream_error_preserved(self):
        self.up.payload = b'data: {"choices":[]}\n\ndata: {"error":{"message":"failed"}}\n\ndata: [DONE]\n\n'
        self.assertEqual(self.request({'model': MODEL, 'messages': [], 'stream': True})[1], self.up.payload)

    def test_upstream_timeout_matches_client_request(self):
        with patch.object(self.proxy.opener, 'open', wraps=self.proxy.opener.open) as call:
            self.assertEqual(self.request({'messages': []}, headers={'X-Research-Timeout-Seconds': '600'})[0], 200)
            self.assertEqual(call.call_args.kwargs['timeout'], 600)
        for value in ('nan', 'inf', '-1', '3601'):
            self.assertEqual(self.request({'messages': []}, headers={'X-Research-Timeout-Seconds': value})[0], 400)

    def test_errors_and_retry_after(self):
        for status in (401, 402, 429, 502):
            self.up.status = status
            self.up.payload = b'{"error":{"message":"upstream failure"}}'
            self.assertEqual(self.request({'model': MODEL, 'messages': []}), (status, self.up.payload, '7'))

    def test_reject_overrides_and_unknown_routes(self):
        for override in ({'model': 'other'}, {'provider': {}}, {'models': ['other']}):
            self.assertEqual(self.request({'messages': [], **override})[0], 400)
        self.assertEqual(self.request({}, '/other')[0], 404)

    def test_preflight_rejects_empty_success(self):
        report = check_api(f'http://127.0.0.1:{self.proxy.server_port}/v1')
        self.assertFalse(report['ok'])
        self.assertFalse(any(report['checks'].values()))

    def test_preflight_handles_null_content_and_finish_error(self):
        for message in ({'content': None}, {'content': 'partial'}):
            self.up.payload = json.dumps({'choices': [{'finish_reason': 'error', 'message': message}]}).encode()
            report = check_api(f'http://127.0.0.1:{self.proxy.server_port}/v1')
            self.assertFalse(report['checks']['generation'])
        self.up.payload = b'{"choices":[{"finish_reason":"length","message":{"content":null}}]}'
        self.assertFalse(check_api(f'http://127.0.0.1:{self.proxy.server_port}/v1')['ok'])

    def test_preflight_success(self):
        self.up.probe_mode = True
        report = check_api(f'http://127.0.0.1:{self.proxy.server_port}/v1')
        self.assertTrue(report['ok'], report)
        self.assertNotIn('reasoning', self.up.body)
        self.assertGreaterEqual(self.up.body['max_tokens'], 1024)

    def test_preflight_reports_http_errors(self):
        self.up.status = 400
        self.up.payload = b'{"error":{"message":"Provider returned error","metadata":{"provider_name":"Together","raw":"{\\"error\\":{\\"message\\":\\"Input validation error\\"}}"}}}'
        report = check_api(f'http://127.0.0.1:{self.proxy.server_port}/v1')
        self.assertFalse(report['ok'])
        self.assertEqual(report['errors']['generation']['status'], 400)
        self.assertEqual(report['errors']['generation']['provider_message'], 'Input validation error')

    def test_stream_arrives_before_completion(self):
        self.up.payload = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
        self.up.release = threading.Event()
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=2)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps({'messages': [], 'stream': True}))
            response = conn.getresponse()
            self.assertEqual(response.readline(), self.up.payload.splitlines(keepends=True)[0])
            self.up.release.set()
            self.assertIn(b'[DONE]', response.read())
        finally:
            self.up.release.set()
            conn.close()


if __name__ == '__main__':
    unittest.main()
