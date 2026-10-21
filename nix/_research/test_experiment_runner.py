import tempfile
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import experiment_runner as runner


def response(content='OK', finish='stop'):
    return {'model': 'qwen/qwen3.5-9b', 'provider': 'Darkbloom',
            'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': content}}]}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / 'ledger.sqlite'
        self.plan = {'run_id': 'fixture', 'base_url': 'http://127.0.0.1:8787/v1',
                     'model': 'qwen/qwen3.5-9b',
                     'budget': {'max_requests': 8, 'max_output_tokens': 100, 'max_seconds': 60},
                     'tasks': [{'id': 'case-a', 'replicate': i, 'case_id': 'a', 'split': 'development',
                                'body': {'messages': [{'role': 'user', 'content': 'OK'}], 'max_tokens': 10}}
                               for i in range(2)]}
        self.calls = []

    def send(self, url, body):
        self.calls.append(body)
        return 200, response() | {'usage': {'completion_tokens': 2}}, {}

    def test_resume_does_not_repeat_completed_replicates(self):
        first = runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(first['status'], 'completed')
        second = runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(second['counts']['completed'], 2)
        self.assertEqual(len(self.calls), 2)

    def test_crash_becomes_unknown_not_retry(self):
        def crash(*args):
            self.calls.append('sent')
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            runner.run(self.plan, self.ledger, send=crash)
        report = runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)
        runner.resolve(self.ledger, 'case-a', 0, 'failed', 'operator records interrupted sample')
        report = runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(report['counts']['failed'], 1)
        self.assertEqual(report['counts']['completed'], 1)

    def test_plan_changes_and_duplicate_ids_rejected(self):
        runner.run(self.plan, self.ledger, send=self.send)
        self.plan['tasks'][0]['body']['max_tokens'] = 11
        with self.assertRaises(ValueError):
            runner.run(self.plan, self.ledger, send=self.send)
        self.plan['tasks'][1]['replicate'] = 0
        with self.assertRaises(ValueError):
            runner.validate(self.plan)

    def test_rate_limit_wait_is_durable_and_bounded(self):
        def limited(*args):
            return 429, {'error': {'message': 'quota'}}, {'Retry-After': '30'}

        first = runner.run(self.plan, self.ledger, send=limited, now=lambda: 1000)
        self.assertEqual(first['status'], 'waiting')
        self.assertEqual(first['next_retry_at'], 1030)
        second = runner.run(self.plan, self.ledger, send=self.send, now=lambda: 1001)
        self.assertEqual(second['status'], 'waiting')
        self.assertFalse(self.calls)
        self.assertEqual(runner.run(self.plan, self.ledger, send=self.send, now=lambda: 1031)['status'], 'completed')

    def test_budget_includes_attempts_with_unknown_usage(self):
        self.plan['budget']['max_output_tokens'] = 10
        def error(*args):
            return 401, {'error': {'message': 'auth'}}, {}

        report = runner.run(self.plan, self.ledger, send=error)
        self.assertEqual(report['status'], 'budget_exhausted')
        self.assertEqual(report['requests'], 1)
        self.assertEqual(report['reserved_output_tokens'], 10)

    def test_json_error_in_http_200_not_success(self):
        report = runner.run(self.plan, self.ledger, send=lambda *args: (200, {'error': {'message': 'upstream'}}, {}))
        self.assertEqual(report['counts']['failed'], 2)

    def test_prefix_is_sent_before_each_task_and_frozen_on_resume(self):
        self.plan['request_prefix'] = {'messages': [{'role': 'system', 'content': 'Fixed trusted rules'}],
            'tools': [{'type': 'function', 'function': {'name': 'probe', 'parameters': {'type': 'object'}}}]}
        self.plan['tasks'][1]['body']['messages'] = [{'role': 'user', 'content': 'Different task'}]
        result = runner.run(self.plan, self.ledger, send=self.send)
        with runner.database(self.ledger) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='prefix_sha256'").fetchone()[0],
                             result['prefix_sha256'])
        self.assertEqual(len(self.calls), 2)
        for body in self.calls:
            self.assertEqual(body['messages'][0], self.plan['request_prefix']['messages'][0])
            self.assertEqual(body['tools'], self.plan['request_prefix']['tools'])
        self.assertNotEqual(self.calls[0]['messages'][1], self.calls[1]['messages'][1])
        self.assertEqual(len(self.plan['tasks'][0]['body']['messages']), 1)
        runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(len(self.calls), 2)
        self.plan['request_prefix']['messages'][0]['content'] = 'Changed rules'
        with self.assertRaises(ValueError):
            runner.run(self.plan, self.ledger, send=self.send)

    def test_task_cannot_override_shared_tools_or_add_dynamic_system_message(self):
        self.plan['request_prefix'] = {'messages': [{'role': 'system', 'content': 'Stable'}], 'tools': []}
        self.plan['tasks'][0]['body']['tools'] = []
        with self.assertRaises(ValueError):
            runner.run(self.plan, self.ledger, send=self.send)
        del self.plan['tasks'][0]['body']['tools']
        self.plan['tasks'][0]['body']['messages'] = [{'role': 'system', 'content': 'Dynamic'}]
        with self.assertRaises(ValueError):
            runner.run(self.plan, self.ledger, send=self.send)
        self.assertFalse(self.calls)

    def test_prefix_survives_rate_limit_and_resume(self):
        self.plan['request_prefix'] = {'messages': [{'role': 'system', 'content': 'Fixed rules'}]}
        def limited(url, body):
            self.calls.append(body)
            return 429, {'error': {'message': 'rate limit'}}, {'Retry-After': '30'}
        first = runner.run(self.plan, self.ledger, send=limited, now=lambda: 1000)
        second = runner.run(self.plan, self.ledger, send=self.send, now=lambda: 1031)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(first['prefix_sha256'], second['prefix_sha256'])
        self.assertEqual(self.calls[0], self.calls[1])
        self.assertEqual(len(self.calls), 3)

    def test_deadline_survives_restart(self):
        runner.run(self.plan, self.ledger, send=lambda *a: (429, {}, {}), now=lambda: 1000)
        report = runner.run(self.plan, self.ledger, send=self.send, now=lambda: 1061)
        self.assertEqual(report['status'], 'budget_exhausted')
        self.assertFalse(self.calls)

    def test_concurrent_owner_is_rejected(self):
        with runner.database(self.ledger), self.assertRaises(BlockingIOError):
            runner.run(self.plan, self.ledger, send=self.send)
        self.assertFalse(self.calls)

    def test_http_transport_preserves_raw_response(self):
        requests = []
        self.plan['request_prefix'] = {'messages': [{'role': 'system', 'content': 'Fixed prefix'}]}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps(response('fixture')).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            send = lambda url, body: runner.send_request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions', body, 2)
            result = runner.run(self.plan, self.ledger, send=send)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(r['messages'] == [
                {'role': 'system', 'content': 'Fixed prefix'}, {'role': 'user', 'content': 'OK'}]
                for r in requests))
            with runner.database(self.ledger) as db:
                self.assertIn('fixture', db.execute('SELECT response FROM attempts').fetchone()[0])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_finish_error_is_retried_but_never_completed(self):
        first = runner.run(self.plan, self.ledger, send=lambda *a: (200, response(None, 'error'), {}), now=lambda: 1000)
        self.assertEqual(first['status'], 'waiting')
        self.assertNotIn('completed', first['counts'])
        with runner.database(self.ledger) as db:
            self.assertEqual(db.execute('SELECT kind FROM attempt_details').fetchone()[0], 'provider_error')
        second = runner.run(self.plan, self.ledger, send=self.send, now=lambda: 1031)
        self.assertEqual(second['counts']['completed'], 2)
        self.assertEqual(second['requests'], 3)

    def test_length_and_refusal_are_observations_not_transport_failures(self):
        values = iter([response(None, 'length'), response(None, 'content_filter')])
        report = runner.run(self.plan, self.ledger, send=lambda *a: (200, next(values), {}))
        self.assertEqual(report['counts'], {'truncated': 1, 'refused': 1})
        runner.run(self.plan, self.ledger, send=self.send)
        self.assertFalse(self.calls)

    def test_malformed_or_unverified_response_is_not_completed(self):
        cases = [ {'choices': [None]}, response() | {'model': 'different'},
                  response() | {'provider': 'Together'}, {'choices': response()['choices']} ]
        for i, value in enumerate(cases):
            with self.subTest(value=value):
                result = runner.run(self.plan, self.ledger.with_name(f'case-{i}.sqlite'), send=lambda *a: (200, value, {}))
                self.assertNotIn('completed', result['counts'])

    def test_route_change_on_resume_is_rejected_before_dispatch(self):
        runner.run(self.plan, self.ledger, send=self.send)
        with patch.object(runner, 'DEFAULT_PROVIDER', 'together'), self.assertRaises(ValueError):
            runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(len(self.calls), 2)

    def test_variant_route_and_response_provider_are_separate(self):
        result = runner.run(self.plan, self.ledger,
            send=lambda *a: (200, response(), {'X-Research-Provider': 'darkbloom/fp4'}))
        self.assertEqual(result['counts']['completed'], 2)
        with runner.database(self.ledger) as db:
            contract = json.loads(db.execute("SELECT value FROM meta WHERE key='execution_contract'").fetchone()[0])
        self.assertEqual(contract['provider'], 'darkbloom/fp4')
        self.assertEqual(contract['response_provider'], 'Darkbloom')
        other = self.ledger.with_name('wrong-variant.sqlite')
        result = runner.run(self.plan, other,
            send=lambda *a: (200, response(), {'X-Research-Provider': 'darkbloom/fp8'}))
        self.assertEqual(result['status'], 'blocked')

    def test_request_timeout_is_configurable_and_clamped_to_budget(self):
        self.plan['request_timeout_seconds'] = 600
        self.plan['budget']['max_seconds'] = 1800
        with patch.object(runner, 'send_request', return_value=(200, response(), {})) as send:
            runner.run(self.plan, self.ledger)
        self.assertGreater(send.call_args.args[2], 120)
        self.assertLessEqual(send.call_args.args[2], 600)

    def test_ambiguous_output_limits_are_rejected(self):
        for key, value in [('n', 2), ('n', True), ('best_of', 3), ('max_completion_tokens', 1000), ('max_output_tokens', 1000)]:
            with self.subTest(key=key):
                body = self.plan['tasks'][0]['body']
                body[key] = value
                with self.assertRaises(ValueError):
                    runner.validate(self.plan)
                del body[key]

    def test_multiple_ledgers_share_budget_and_scope_cannot_be_increased(self):
        scope = Path(self.temp.name) / 'scope.sqlite'
        limits = {'max_requests': 3, 'max_output_tokens': 30, 'max_seconds': 60}
        with patch.dict('os.environ', {'RESEARCH_BUDGET_LEDGER': str(scope), 'RESEARCH_BUDGET_LIMITS': json.dumps(limits)}):
            runner.run(self.plan, self.ledger, send=self.send)
            other = self.ledger.with_name('other.sqlite')
            report = runner.run(self.plan, other, send=self.send)
            self.assertEqual(report['status'], 'budget_exhausted')
            self.assertEqual(len(self.calls), 3)
            with patch.dict('os.environ', {'RESEARCH_BUDGET_LIMITS': json.dumps(limits | {'max_requests': 4})}), self.assertRaises(ValueError):
                runner.run(self.plan, other, send=self.send)

    def test_shared_budget_reservation_is_atomic_between_runners(self):
        self.plan['budget_scope'] = {'ledger': str(Path(self.temp.name) / 'scope.sqlite'),
            'limits': {'max_requests': 1, 'max_output_tokens': 10, 'max_seconds': 60}}
        barrier = threading.Barrier(2)
        def execute(number):
            barrier.wait(timeout=5)
            return runner.run(self.plan, self.ledger.with_name(f'parallel-{number}.sqlite'), send=self.send)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(execute, range(2)))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r['status'] == 'budget_exhausted' for r in reports))

    def test_budget_scope_cannot_change_on_resume(self):
        limits = {'max_requests': 3, 'max_output_tokens': 30, 'max_seconds': 60}
        with patch.dict('os.environ', {'RESEARCH_BUDGET_LEDGER': str(Path(self.temp.name) / 'scope.sqlite'),
                                      'RESEARCH_BUDGET_LIMITS': json.dumps(limits)}):
            runner.run(self.plan, self.ledger, send=self.send)
            with patch.dict('os.environ', {'RESEARCH_BUDGET_LEDGER': str(Path(self.temp.name) / 'other-scope.sqlite')}), self.assertRaises(ValueError):
                runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(len(self.calls), 2)

    def test_error_retry_limit_is_per_task_and_all_attempts_remain(self):
        self.plan['budget']['max_seconds'] = 600
        for t in (1000, 1031, 1062, 1093, 1124):
            report = runner.run(self.plan, self.ledger,
                send=lambda *a: (200, response(None, 'error'), {}), now=lambda: t)
        self.assertEqual(report['status'], 'completed_with_failures')
        self.assertEqual(report['counts'], {'failed': 2})
        self.assertEqual(report['requests'], 6)

    def test_legacy_ledger_requires_audit_without_repeating_requests(self):
        runner.run(self.plan, self.ledger, send=self.send)
        with runner.database(self.ledger) as db, db:
            db.execute("DELETE FROM meta WHERE key='execution_contract'")
        with self.assertRaises(ValueError):
            runner.run(self.plan, self.ledger, send=self.send)
        self.assertEqual(len(self.calls), 2)

    def test_tool_call_response_is_a_valid_completion(self):
        value = response(None, 'tool_calls')
        value['choices'][0]['message']['tool_calls'] = [{'id': 'call1', 'type': 'function',
            'function': {'name': 'probe', 'arguments': '{"ok":true}'}}]
        report = runner.run(self.plan, self.ledger, send=lambda *a: (200, value, {}))
        self.assertEqual(report['counts'], {'completed': 2})

    def test_transport_deadline_covers_slow_body_not_only_each_read(self):
        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                runner.send_request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions', {}, 0.15)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
