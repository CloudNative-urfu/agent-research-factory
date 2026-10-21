"""Small MCP transport checks, without stored credentials."""

import json
import os
import queue
import subprocess
import tempfile
import threading
import urllib.request


class Stdio:
    def __init__(self, command):
        os.makedirs('/tmp/opencode', exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='mcp-probe-', dir='/tmp/opencode')
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, start_new_session=True,
                                      cwd=self.temporary.name)
        self.inbox = queue.Queue()

        def reader():
            for line in self.child.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    pass
        threading.Thread(target=reader, daemon=True).start()
        self.serial = 0

    def call(self, method, params):
        self.serial += 1
        self.child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.serial, 'method': method, 'params': params}) + '\n')
        self.child.stdin.flush()
        while True:
            result = self.inbox.get(timeout=60)
            if result.get('id') == self.serial:
                if 'error' in result:
                    raise RuntimeError('MCP protocol error')
                return result['result']

    def close(self):
        try:
            os.killpg(self.child.pid, 15)
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.child.pid, 9)
            self.child.wait()
        finally:
            self.child.stdin.close()
            self.child.stdout.close()
            self.temporary.cleanup()


def probe(command, name, arguments):
    client = Stdio(command)
    try:
        client.call('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                  'clientInfo': {'name': 'research-preflight', 'version': '1'}})
        client.child.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        client.child.stdin.flush()
        listing = client.call('tools/list', {})
        if name not in [tool['name'] for tool in listing['tools']]:
            return False
        result = client.call('tools/call', {'name': name, 'arguments': arguments})
        return not result.get('isError', False) and bool(result.get('content'))
    finally:
        client.close()


def context7_probe():
    def call(method, params):
        request = urllib.request.Request('https://mcp.context7.com/mcp',
            data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
        if text.lstrip().startswith('{'):
            return json.loads(text)
        for line in text.splitlines():
            if line.startswith('data: '):
                value = json.loads(line[6:])
                if 'result' in value or 'error' in value:
                    return value
        raise RuntimeError('No MCP response')
    result = call('tools/call', {'name': 'resolve-library-id', 'arguments': {
        'libraryName': 'Python', 'query': 'Python documentation standard library'}})
    return 'result' in result and not result['result'].get('isError', False)


def checks():
    result = {}
    for name, check in {
        'mcp_nixos': lambda: probe(['mcp-nixos'], 'nix', {'action': 'info', 'query': 'python3'}),
        'mcp_playwright': lambda: probe(['playwright-mcp', '--headless', '--isolated'],
                                      'browser_navigate', {'url': 'https://example.com'}),
        'mcp_context7': context7_probe,
    }.items():
        try:
            result[name] = check()
        except (OSError, ValueError, RuntimeError, queue.Empty, subprocess.SubprocessError) as exc:
            result[name] = False
            result[name + '_error'] = type(exc).__name__
    return result


if __name__ == '__main__':
    print(json.dumps(checks(), indent=2))
