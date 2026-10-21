"""Loopback-only, fixed-model OpenRouter gateway; credentials stay in this process."""

import argparse
import json
import math
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = 'qwen/qwen3.5-9b'
PROVIDER = 'darkbloom/fp4'
PORT = 8787
MAX_BODY = 16 * 1024 * 1024
MAX_REQUEST_SECONDS = 3600


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'status': 'ok', 'model': MODEL, 'provider': PROVIDER,
                             'upstream_verified': False})
        elif self.path == '/v1/models':
            self.reply(200, {'object': 'list', 'data': [{'id': MODEL, 'object': 'model', 'owned_by': 'qwen'}]})
        else:
            self.reply(404, {'error': {'message': 'Unknown route'}})

    def do_POST(self):
        self.connection.settimeout(30)
        if self.path != '/v1/chat/completions':
            self.reply(404, {'error': {'message': 'Unknown route'}})
            return
        try:
            request_timeout = float(self.headers.get('X-Research-Timeout-Seconds', '900'))
            if not math.isfinite(request_timeout) or not 0 < request_timeout <= MAX_REQUEST_SECONDS:
                raise ValueError('Invalid request timeout')
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 0 < length <= MAX_BODY:
                raise ValueError('Content-Length must be between 1 and 16777216')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
                raise TypeError('messages must be an array')
            if body.get('model', MODEL) != MODEL or any(k in body for k in ('provider', 'models', 'route', 'plugins')):
                raise ValueError('Model and routing are fixed by the research proxy')
        except (ValueError, TypeError, TimeoutError) as exc:
            self.reply(400, {'error': {'message': str(exc)}})
            return
        body['model'] = MODEL
        body['provider'] = {'only': [PROVIDER], 'allow_fallbacks': False, 'require_parameters': True}
        request = urllib.request.Request(
            self.server.upstream + '/chat/completions', data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.server.api_key,
                     'Content-Type': 'application/json', 'Accept': 'text/event-stream' if body.get('stream') else 'application/json'})
        try:
            response = self.server.opener.open(request, timeout=request_timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError):
            self.reply(502, {'error': {'message': 'OpenRouter connection failed'}})
            return
        with response:
            self.send_response(response.status)
            for name in ('Content-Type', 'Retry-After', 'X-Request-ID'):
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            self.send_header('Connection', 'close')
            self.send_header('X-Research-Provider', PROVIDER)
            self.end_headers()
            self.close_connection = True
            try:
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                # An interrupted SSE stream deliberately has no synthetic [DONE].
                pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_server(port, api_key, upstream='https://openrouter.ai/api/v1'):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.api_key = api_key
    server.upstream = upstream
    server.opener = urllib.request.build_opener(NoRedirect)
    return server


def error_details(data, status=None):
    error = data.get('error', {})
    if not isinstance(error, dict):
        return {'status': status, 'message': 'Invalid upstream error'}
    metadata = error.get('metadata') or {}
    details = {'status': status, 'message': str(error.get('message', 'Unknown API error'))[:500]}
    if isinstance(metadata, dict):
        details['provider'] = metadata.get('provider_name')
        try:
            raw = json.loads(metadata.get('raw', '{}'))
            details['provider_message'] = str(raw['error']['message'])[:500]
        except (ValueError, TypeError, KeyError):
            pass
    return details


def check_api(base):
    checks = {}
    errors = {}
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    probes = {
        'generation': {},
        'json': {'response_format': {'type': 'json_schema', 'json_schema': {'name': 'probe', 'strict': True, 'schema': schema}}},
        'tools': {'tools': [{'type': 'function', 'function': {'name': 'probe', 'description': 'Confirm success', 'parameters': schema}}],
                  'tool_choice': 'auto'},
        'stream': {'stream': True},
    }
    for name, options in probes.items():
        body = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Reply with {"ok":true}. If a tool is provided, call it with ok=true.'}],
                'max_tokens': 2048, **options}
        try:
            request = urllib.request.Request(base + '/chat/completions', data=json.dumps(body).encode(),
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=180) as response:
                if name == 'stream':
                    done, content, error = False, '', False
                    for line in response:
                        if not line.startswith(b'data:'):
                            continue
                        payload = line[5:].strip()
                        if payload == b'[DONE]':
                            done = True
                            break
                        chunk = json.loads(payload)
                        error |= bool(chunk.get('error'))
                        if chunk.get('error'):
                            errors[name] = error_details(chunk, response.status)
                        for choice in chunk.get('choices', []):
                            content += choice.get('delta', {}).get('content') or ''
                    checks[name] = done and bool(content.strip()) and not error
                    if not checks[name] and name not in errors:
                        errors[name] = {'message': 'Stream incomplete or final content empty'}
                    continue
                data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError('Expected response object')
            if data.get('error'):
                checks[name] = False
                errors[name] = error_details(data)
                continue
            message = data['choices'][0]['message']
            if not isinstance(message, dict) or data['choices'][0].get('finish_reason') == 'error':
                raise ValueError('Invalid or failed generation')
            if name == 'json':
                checks[name] = json.loads(message['content']) == {'ok': True}
            elif name == 'tools':
                call = message['tool_calls'][0]['function']
                checks[name] = call['name'] == 'probe' and json.loads(call['arguments']) == {'ok': True}
            else:
                checks[name] = isinstance(message.get('content'), str) and bool(message['content'].strip())
            if not checks[name]:
                errors[name] = {'message': 'Response failed validation', 'finish_reason': data['choices'][0].get('finish_reason')}
        except urllib.error.HTTPError as exc:
            checks[name] = False
            with exc:
                try:
                    errors[name] = error_details(json.load(exc), exc.code)
                except (OSError, ValueError, TypeError, AttributeError):
                    errors[name] = {'status': exc.code, 'message': 'Non-JSON HTTP error'}
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            checks[name] = False
            errors[name] = {'message': 'Connection or response validation failed', 'type': type(exc).__name__}
    return {'ok': all(checks.values()), 'checks': checks, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--check', action='store_true', help='Probe generation, JSON, tool calling and SSE through the running proxy (billable)')
    args = parser.parse_args()
    if args.check:
        report = check_api(f'http://127.0.0.1:{args.port}/v1')
        print(json.dumps(report, indent=2))
        return 0 if report['ok'] else 2
    key = os.environ.pop('OPENROUTER_API_KEY', '').strip()
    if not key or key == 'REPLACE_WITH_YOUR_OPENROUTER_KEY':
        parser.exit(2, 'Set OPENROUTER_API_KEY in the proxy service environment.\n')
    with make_server(args.port, key) as server:
        print(f'Research proxy: http://127.0.0.1:{args.port}/v1; {MODEL}; {PROVIDER}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
