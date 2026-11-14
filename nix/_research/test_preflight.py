import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import factory
import preflight


class PreflightTests(unittest.TestCase):
    def test_only_transient_failures_can_retry(self):
        for error, retry in (({'status': 502}, True), ({'status': 401}, False),
                             ({'message': 'Stream incomplete or final content empty'}, True),
                             ({'message': 'Response failed validation'}, False)):
            report = {'ok': False, 'checks': {'experiment_stream': False, 'experiment_stream_error': error}}
            self.assertEqual(preflight.retryable(report), retry)
        self.assertFalse(preflight.retryable({'ok': False, 'checks': {'sandbox': False}}))
        self.assertFalse(preflight.retryable({'ok': False, 'checks': {
            'experiment_json': False, 'experiment_json_error': {'status': 502},
            'experiment_tools': False, 'experiment_tools_error': {'status': 401}}}))

    def test_backoff_caps_at_thirty_minutes_and_waits_before_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'stage': 'report'}
            failure = {'ok': False, 'checks': {'experiment_json': False, 'experiment_json_error': {'status': 502}}}
            with patch.object(preflight.time, 'time', return_value=1000):
                for delay in (60, 300, 1800, 1800):
                    preflight.schedule(root, state, failure, factory.save)
                    self.assertEqual(state['preflight_retry']['resume_at'], 1000 + delay)
                invoke = Mock()
                with patch.object(preflight.time, 'sleep', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        factory.preflight_ready(root, state, invoke)
                invoke.assert_not_called()

    def test_wait_is_saved_before_sleep_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'status': 'ready', 'stage': 'report'}
            failure = {'ok': False, 'checks': {'experiment_json': False, 'experiment_json_error': {'status': 502}}}
            with patch.object(preflight.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    factory.preflight_ready(root, state, lambda: failure)
            saved = factory.load(root / 'state.json')
            self.assertEqual(saved['status'], 'waiting_preflight')
            self.assertGreater(saved['preflight_retry']['resume_at'], 0)
            invoke = Mock(return_value={'ok': True})
            with patch.object(preflight.time, 'time', return_value=saved['preflight_retry']['resume_at'] + 1):
                factory.preflight_ready(root, saved, invoke)
            invoke.assert_called_once()
            self.assertNotIn('preflight_retry', saved)
            self.assertEqual(saved['stage'], 'report')

    def test_invalid_capability_blocks_without_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {'status': 'ready', 'stage': 'report'}
            with patch.object(preflight.time, 'sleep') as sleep:
                with self.assertRaises(factory.Blocked):
                    factory.preflight_ready(Path(tmp), state, lambda: {
                        'ok': False, 'checks': {'experiment_tools': False,
                        'experiment_tools_error': {'message': 'Expected exactly one probe tool call'}}})
                sleep.assert_not_called()

    def test_autopilot_recovers_only_transient_legacy_block(self):
        import autopilot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'campaigns/dvarp-revision-11'
            control = root / 'control'
            factory.save(control / 'config.json', {'campaigns': str(source.parent),
                         'initial': source.name, 'prefix': 'dvarp-revision-', 'next_number': 12})
            old = {'current': source.name, 'next_number': 12, 'status': 'paused',
                   'reason': 'Current campaign blocked; inspect its state.json'}
            factory.save(source / 'state.json', {'status': 'blocked', 'stage': 'report',
                         'error': 'Live preflight failed; see doctor.json'})
            for status, restart in ((401, False), (502, True)):
                factory.save(control / 'state.json', old)
                factory.save(source / 'doctor.json', {'ok': False, 'checks': {
                    'experiment_json': False, 'experiment_json_error': {'status': status}}})
                with patch.object(autopilot, 'service_active', return_value=False), \
                     patch.object(autopilot, 'start_service') as start:
                    autopilot.tick(control)
                self.assertEqual(start.called, restart)
            record = factory.load(control / 'history/preflight-recovery-dvarp-revision-11.json')
            self.assertEqual(record['previous_state'], old)
