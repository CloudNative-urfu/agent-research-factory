"""Durable, bounded experiment requests with explicit uncertain outcomes."""

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from llm_proxy import PROVIDER as DEFAULT_PROVIDER, MAX_REQUEST_SECONDS

TERMINAL = ('completed', 'truncated', 'refused', 'failed')


def positive_budget(value):
    if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] <= 0
            for k in ('max_requests', 'max_output_tokens', 'max_seconds')):
        raise ValueError('Budgets must be positive integers')
    return {k: value[k] for k in ('max_requests', 'max_output_tokens', 'max_seconds')}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def request_body(plan, task):
    body = dict(task['body'])
    prefix = plan.get('request_prefix')
    if prefix is None:
        return body | {'model': plan['model']}
    if prefix is not None:
        body['messages'] = prefix['messages'] + body['messages']
        if 'tools' in prefix:
            body['tools'] = prefix['tools']
    body['model'] = plan['model']
    return json.loads(canonical(body))


def validate(plan):
    if not isinstance(plan, dict):
        raise ValueError('Plan must be an object')
    if any(not isinstance(plan.get(k), str) or not plan[k].strip() for k in ('run_id', 'model')) or not isinstance(plan.get('tasks'), list):
        raise ValueError('run_id, model and tasks are required')
    if plan.get('base_url') != 'http://127.0.0.1:8787/v1':
        raise ValueError('Use the configured local research proxy')
    prefix = plan.get('request_prefix')
    if 'request_prefix' in plan:
        if not isinstance(prefix, dict) or set(prefix) - {'messages', 'tools'}:
            raise ValueError('request_prefix supports only messages and tools')
        messages = prefix.get('messages')
        if not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or m.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool')
                for m in messages):
            raise ValueError('request_prefix needs a nonempty list of chat messages')
        if 'tools' in prefix and (not isinstance(prefix['tools'], list) or
                                 any(not isinstance(t, dict) for t in prefix['tools'])):
            raise ValueError('Shared tools must be a list of objects')
    positive_budget(plan.get('budget'))
    if type(plan.get('request_timeout_seconds', 900)) is not int or not 1 <= plan.get('request_timeout_seconds', 900) <= MAX_REQUEST_SECONDS:
        raise ValueError(f'request_timeout_seconds must be 1..{MAX_REQUEST_SECONDS}')
    if not isinstance(plan.get('provider', DEFAULT_PROVIDER), str) or not plan.get('provider', DEFAULT_PROVIDER).strip():
        raise ValueError('Expected provider must be specified')
    models = plan.get('response_models', [plan['model']])
    if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m for m in models):
        raise ValueError('response_models must be an explicit list of accepted model IDs')
    keys = set()
    for task in plan['tasks']:
        if not isinstance(task, dict) or not isinstance(task.get('body'), dict):
            raise ValueError('Tasks and request bodies must be objects')
        key = (task.get('id'), task.get('replicate'))
        body = task.get('body', {})
        if not isinstance(key[0], str) or not key[0] or type(key[1]) is not int or key[1] < 0 or key in keys:
            raise ValueError('Unique task ID and nonnegative replicate required')
        if task.get('split') not in ('development', 'confirmation') or not task.get('case_id'):
            raise ValueError('Explicit case ID and data split required')
        if type(body.get('max_tokens')) is not int or body['max_tokens'] <= 0 or not body.get('messages'):
            raise ValueError('Messages and finite max_tokens required')
        if not isinstance(body['messages'], list) or any(not isinstance(m, dict) or
                m.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool') for m in body['messages']):
            raise ValueError('Messages must be chat message objects')
        if body.get('stream') or 'model' in body:
            raise ValueError('Model is fixed by the plan; streaming is unsupported')
        if type(body.get('n', 1)) is not int or body.get('n', 1) != 1 or any(k in body for k in
                ('best_of', 'max_completion_tokens', 'max_output_tokens', 'provider', 'models', 'route', 'plugins')):
            raise ValueError('Only one completion and one max_tokens limit are supported; routing is fixed')
        if prefix is not None:
            if not isinstance(body['messages'], list) or any(
                    not isinstance(m, dict) or m.get('role') not in ('user', 'assistant', 'tool')
                    for m in body['messages']):
                raise ValueError('With request_prefix, system/developer messages belong in the fixed prefix')
            if 'tools' in body:
                raise ValueError('With request_prefix, tool schemas belong in the fixed prefix')
        keys.add(key)


@contextlib.contextmanager
def database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT, replicate INTEGER, status TEXT, retry_at REAL DEFAULT 0,
                    PRIMARY KEY(id, replicate));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT, replicate INTEGER, started REAL, cap INTEGER,
                    status TEXT, http INTEGER, response TEXT, headers TEXT);
                CREATE TABLE IF NOT EXISTS resolutions (
                    id TEXT, replicate INTEGER, time REAL, decision TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS attempt_details (
                    attempt INTEGER PRIMARY KEY, kind TEXT, request_sha256 TEXT, reservation TEXT);
                CREATE INDEX IF NOT EXISTS attempts_task ON attempts(id, replicate);
            ''')
            yield db
        finally:
            db.close()


def send_request(url, body, timeout):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        raise ValueError('Unsupported request URL')
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('Request deadline exceeded')
        return value
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=remaining())
    try:
        connection.request('POST', parsed.path + ('?' + parsed.query if parsed.query else ''),
            json.dumps(body).encode(), {'Content-Type': 'application/json',
                                       'X-Research-Timeout-Seconds': str(timeout)})
        sock = connection.sock
        sock.settimeout(remaining())
        with connection.getresponse() as response:
            chunks = []
            while not response.isclosed():
                sock.settimeout(remaining())
                block = response.read1(65536)
                if not block:
                    break
                chunks.append(block)
            raw = b''.join(chunks).decode('utf-8', errors='replace')
            try:
                data = json.loads(raw)
            except ValueError:
                data = {'invalid_response': raw}
            return response.status, data, dict(response.headers)
    finally:
        connection.close()


def classify(status, data, contract):
    if status != 200:
        return 'http_error', status in (429, 502, 503, 504)
    if not isinstance(data, dict):
        return 'invalid_response', False
    if isinstance(data.get('provider'), str) and data['provider'].lower() != contract['response_provider'].lower():
        return 'identity_mismatch', False
    if isinstance(data.get('model'), str) and data['model'] not in contract['response_models']:
        return 'identity_mismatch', False
    if data.get('error'):
        error = data['error']
        code = str(error.get('code', '')) if isinstance(error, dict) else ''
        return 'provider_error', code in ('429', '502', '503', '504')
    choices = data.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return 'invalid_response', False
    choice = choices[0]
    if choice.get('finish_reason') == 'error' or choice.get('error'):
        return 'provider_error', True
    if not isinstance(data.get('provider'), str) or not isinstance(data.get('model'), str):
        return 'identity_unverified', False
    if data['provider'].lower() != contract['response_provider'].lower() or data['model'] not in contract['response_models']:
        return 'identity_mismatch', False
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return 'invalid_response', False
    finish = choice.get('finish_reason')
    if finish == 'length':
        return 'truncated', False
    if finish == 'content_filter' or (isinstance(message.get('refusal'), str) and message['refusal'].strip()):
        return 'refused', False
    if finish == 'stop' and isinstance(message.get('content'), str) and message['content'].strip():
        return 'completed', False
    calls = message.get('tool_calls')
    if finish == 'tool_calls' and isinstance(calls, list) and calls and all(
        isinstance(c, dict) and c.get('type') == 'function' and isinstance(c.get('id'), str)
        and isinstance(c.get('function'), dict) and isinstance(c['function'].get('name'), str)
        and isinstance(c['function'].get('arguments'), str) for c in calls):
        return 'completed', False
    return 'invalid_response', False


class BudgetScope:
    def __init__(self, plan, ledger, now):
        spec = plan.get('budget_scope')
        if os.environ.get('RESEARCH_BUDGET_LEDGER'):
            if spec is not None:
                raise ValueError('Controller budget scope cannot be overridden by a plan')
            spec = {'ledger': os.environ['RESEARCH_BUDGET_LEDGER'],
                    'limits': json.loads(os.environ['RESEARCH_BUDGET_LIMITS'])}
        self.db = None
        self.identity = None
        self.deadline = float('inf')
        if spec is None:
            return
        if not isinstance(spec, dict) or not isinstance(spec.get('ledger'), str) or not spec['ledger']:
            raise ValueError('budget_scope requires a ledger path and limits')
        self.limits = positive_budget(spec.get('limits'))
        path = (Path(ledger).resolve().parent / spec['ledger']).resolve()
        if path == Path(ledger).resolve():
            raise ValueError('Budget scope and run ledger must be different files')
        self.identity = {'ledger': str(path), 'limits': self.limits}
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        try:
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''CREATE TABLE IF NOT EXISTS scope_meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, run TEXT, task TEXT,
                    replicate INTEGER, created REAL, cap INTEGER);
                CREATE TABLE IF NOT EXISTS scope_totals(id INTEGER PRIMARY KEY CHECK(id=1), requests INTEGER, tokens INTEGER);''')
            self.db.execute('BEGIN IMMEDIATE')
            meta = dict(self.db.execute('SELECT key,value FROM scope_meta'))
            if not meta:
                self.db.executemany('INSERT INTO scope_meta VALUES (?,?)',
                    [('limits', canonical(self.limits)), ('started', str(now()))])
                meta = dict(self.db.execute('SELECT key,value FROM scope_meta'))
            if meta['limits'] != canonical(self.limits):
                raise ValueError('Shared budget limits differ from frozen scope')
            self.deadline = float(meta['started']) + self.limits['max_seconds']
            self.db.execute('INSERT OR IGNORE INTO scope_totals SELECT 1,count(*),coalesce(sum(cap),0) FROM reservations')
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()

    def summary(self):
        if self.db is None:
            return None
        count, tokens = self.db.execute('SELECT requests,tokens FROM scope_totals WHERE id=1').fetchone()
        return {'reserved_requests': count, 'reserved_output_tokens': tokens, 'deadline': self.deadline}

    def reserve(self, ledger, task, cap, now):
        if self.db is None:
            return ''
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            count, tokens = self.db.execute('SELECT requests,tokens FROM scope_totals WHERE id=1').fetchone()
            if now() >= self.deadline or count >= self.limits['max_requests'] or tokens + cap > self.limits['max_output_tokens']:
                return None
            identity = uuid.uuid4().hex
            self.db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?)',
                (identity, str(Path(ledger).resolve()), task['id'], task['replicate'], now(), cap))
            self.db.execute('UPDATE scope_totals SET requests=requests+1,tokens=tokens+? WHERE id=1', (cap,))
            return identity


def run(plan, ledger, send=None, now=time.time):
    validate(plan)
    encoded = json.dumps(plan, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    prefix_hash = (hashlib.sha256(canonical({'model': plan['model'], **plan['request_prefix']}).encode()).hexdigest()
                   if 'request_prefix' in plan else None)
    contract = {'version': 2, 'model': plan['model'], 'base_url': plan['base_url'],
                'provider': plan.get('provider', DEFAULT_PROVIDER),
                'response_provider': {'darkbloom/fp4': 'Darkbloom'}.get(plan.get('provider', DEFAULT_PROVIDER), plan.get('provider', DEFAULT_PROVIDER)),
                'response_models': plan.get('response_models', [plan['model']]),
                'request_timeout_seconds': plan.get('request_timeout_seconds', 900)}
    with database(ledger) as db, contextlib.closing(BudgetScope(plan, ledger, now)) as scope:
        contract['budget_scope'] = scope.identity
        meta = dict(db.execute('SELECT key,value FROM meta').fetchall())
        if meta and meta.get('digest') != digest:
            raise ValueError('Plan differs from frozen ledger plan')
        if meta and meta.get('execution_contract') != canonical(contract):
            raise ValueError('Execution contract changed or legacy ledger needs an explicit audit; no requests sent')
        if not meta:
            with db:
                db.executemany('INSERT INTO meta VALUES (?,?)', [('digest', digest), ('plan', encoded), ('started', str(now()))])
                db.execute('INSERT INTO meta VALUES (?,?)', ('execution_contract', canonical(contract)))
                if prefix_hash is not None:
                    db.execute('INSERT INTO meta VALUES (?,?)', ('prefix_sha256', prefix_hash))
                db.executemany('INSERT INTO tasks(id,replicate,status) VALUES (?,?,?)',
                               [(t['id'], t['replicate'], 'pending') for t in plan['tasks']])
            meta = dict(db.execute('SELECT key,value FROM meta').fetchall())
        with db:
            db.execute("UPDATE tasks SET status='unknown' WHERE status='in_flight'")
            db.execute("UPDATE attempts SET status='unknown' WHERE status='in_flight'")
            db.execute("UPDATE attempt_details SET kind='unknown' WHERE kind='in_flight'")

        requests, reserved = db.execute('SELECT count(*),coalesce(sum(cap),0) FROM attempts').fetchone()

        def report(status, **extra):
            counts = dict(db.execute('SELECT status,count(*) FROM tasks GROUP BY status').fetchall())
            return dict(status=status, counts=counts, requests=requests, reserved_output_tokens=reserved,
                        prefix_sha256=prefix_hash, shared_budget=scope.summary(), **extra)

        if db.execute("SELECT 1 FROM tasks WHERE status='unknown'").fetchone():
            return report('unknown')
        if db.execute("SELECT 1 FROM attempt_details WHERE kind IN ('identity_unverified','identity_mismatch')").fetchone():
            return report('blocked')
        deadline = min(float(meta['started']) + plan['budget']['max_seconds'], scope.deadline)
        if os.environ.get('RESEARCH_STAGE_DEADLINE'):
            deadline = min(deadline, float(os.environ['RESEARCH_STAGE_DEADLINE']))
        for task in plan['tasks']:
            key = (task['id'], task['replicate'])
            row = db.execute('SELECT * FROM tasks WHERE id=? AND replicate=?', key).fetchone()
            if row['status'] in TERMINAL:
                continue
            if row['retry_at'] > now():
                return report('waiting', next_retry_at=row['retry_at'])
            cap = task['body']['max_tokens']
            if now() >= deadline or requests >= plan['budget']['max_requests'] or reserved + cap > plan['budget']['max_output_tokens']:
                return report('budget_exhausted')
            body = request_body(plan, task)
            reservation = scope.reserve(ledger, task, cap, now)
            if reservation is None:
                return report('budget_exhausted')
            with db:
                db.execute("UPDATE tasks SET status='in_flight' WHERE id=? AND replicate=?", key)
                attempt = db.execute("INSERT INTO attempts(id,replicate,started,cap,status) VALUES (?,?,?,?,'in_flight')", (*key, now(), cap)).lastrowid
                db.execute('INSERT INTO attempt_details VALUES (?,?,?,?)',
                           (attempt, 'in_flight', hashlib.sha256(canonical(body).encode()).hexdigest(), reservation))
            requests += 1
            reserved += cap
            try:
                url = plan['base_url'] + '/chat/completions'
                status, data, headers = send(url, body) if send else send_request(url, body,
                    min(contract['request_timeout_seconds'], max(0.001, deadline - now())))
            except Exception as exc:
                with db:
                    db.execute("UPDATE tasks SET status='unknown' WHERE id=? AND replicate=?", key)
                    db.execute("UPDATE attempts SET status='unknown',response=? WHERE rowid=?", (json.dumps({'error_type': type(exc).__name__}), attempt))
                    db.execute("UPDATE attempt_details SET kind='unknown' WHERE attempt=?", (attempt,))
                return report('unknown')
            count = db.execute('SELECT count(*) FROM attempts WHERE id=? AND replicate=?', key).fetchone()[0]
            kind, transient = classify(status, data, contract)
            configured = next((str(v) for k, v in headers.items() if k.lower() == 'x-research-provider'), None)
            if configured is not None and configured.lower() != contract['provider'].lower():
                kind, transient = 'identity_mismatch', False
            retry = transient and count < 3
            outcome = kind if kind in TERMINAL else ('pending' if retry else 'failed')
            try:
                delay = min(300, max(1, float(next((v for k, v in headers.items() if k.lower() == 'retry-after'), 30))))
            except (TypeError, ValueError):
                delay = 30
            retry_at = now() + delay if retry else 0
            with db:
                db.execute('UPDATE tasks SET status=?,retry_at=? WHERE id=? AND replicate=?', (outcome, retry_at, *key))
                db.execute('UPDATE attempts SET status=?,http=?,response=?,headers=? WHERE rowid=?',
                           (outcome, status, json.dumps(data), json.dumps(headers), attempt))
                db.execute('UPDATE attempt_details SET kind=? WHERE attempt=?', (kind, attempt))
            if kind in ('identity_unverified', 'identity_mismatch'):
                return report('blocked')
            if retry:
                return report('waiting', next_retry_at=retry_at)
        return report('completed_with_failures' if db.execute("SELECT 1 FROM tasks WHERE status='failed'").fetchone() else 'completed')


def resolve(ledger, task_id, replicate, decision, reason):
    if decision != 'failed' or not reason.strip():
        raise ValueError('Unknown requests may only be closed as failed with a reason')
    with database(ledger) as db, db:
        row = db.execute('SELECT status FROM tasks WHERE id=? AND replicate=?', (task_id, replicate)).fetchone()
        if not row or row['status'] != 'unknown':
            raise ValueError('Task is not unknown')
        db.execute("UPDATE tasks SET status='failed' WHERE id=? AND replicate=?", (task_id, replicate))
        db.execute('INSERT INTO resolutions VALUES (?,?,?,?,?)', (task_id, replicate, time.time(), decision, reason))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run_parser = sub.add_parser('run')
    run_parser.add_argument('plan', type=Path)
    run_parser.add_argument('ledger', type=Path)
    close = sub.add_parser('resolve')
    close.add_argument('ledger', type=Path)
    close.add_argument('task_id')
    close.add_argument('replicate', type=int)
    close.add_argument('--reason', required=True)
    args = parser.parse_args()
    if args.command == 'resolve':
        resolve(args.ledger, args.task_id, args.replicate, 'failed', args.reason)
    else:
        result = run(json.loads(args.plan.read_text()), args.ledger)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
