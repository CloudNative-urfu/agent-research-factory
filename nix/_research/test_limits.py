import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import deep_case
import factory
import limits


class LimitTests(unittest.TestCase):
    def test_reset_time_timezone_and_midnight(self):
        now = datetime.fromisoformat('2026-11-03T23:40:00+05:00').timestamp()
        error = "usage_limit_reached / HTTP 429 / You've hit your session limit · resets 2:20am (Asia/Yekaterinburg)"
        self.assertEqual(limits.deadline(error, now), datetime.fromisoformat('2026-11-04T02:22:00+05:00').timestamp())
        self.assertEqual(limits.deadline('usage_limit_reached resets 12pm (Asia/Yekaterinburg)', now),
                         datetime.fromisoformat('2026-11-04T12:02:00+05:00').timestamp())

    def test_unknown_time_falls_back_but_other_errors_do_not(self):
        for error in ('usage_limit_reached', 'usage_limit_reached resets 13pm (Invalid/Zone)'):
            self.assertEqual(limits.deadline(error, 1000), 2800)
        for error in ('HTTP 429', 'authentication failed', 'connection timeout'):
            self.assertFalse(limits.is_limit(error))

    def test_wait_survives_restart_without_moving_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'status': 'running', 'stage': 'build:agent'}
            with patch.object(limits.time, 'time', return_value=1000):
                limits.schedule(root, state, 'usage_limit_reached', factory.save)
            with patch.object(limits.time, 'time', return_value=1010), patch.object(limits.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    limits.wait(root, state, factory.save)
            restored = factory.load(root / 'state.json')
            self.assertEqual(restored['rate_limit']['resume_at'], 2800)
            with patch.object(limits.time, 'time', return_value=2801), patch.object(limits.time, 'sleep') as sleep:
                limits.wait(root, restored, factory.save)
            sleep.assert_not_called()
            self.assertEqual(restored['status'], 'running')
            self.assertNotIn('rate_limit', restored)

    def test_deep_case_limit_does_not_burn_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'cycles/01'
            work = base / 'work'
            work.mkdir(parents=True)
            state = {'stage': 'build:agent', 'steps': {}, 'components': []}
            cfg = factory.default_config()
            with patch.object(deep_case, 'handoff', return_value={}):
                deep_case.step(root, base, work, state, cfg, {},
                               lambda *args: {'ok': False, 'error': 'usage_limit_reached'}, None)
            self.assertEqual(state['status'], 'waiting_limit')
            self.assertEqual(state['steps']['build:agent']['attempts'], 0)
            self.assertEqual(state['steps']['build:agent']['infra_failures'], 0)

    def test_preflight_limit_waits_then_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {'stage': 'build:agent', 'status': 'running'}
            reports = iter([
                {'ok': False, 'checks': {'sandbox': True, 'researcher_tool_call': False,
                                        'researcher_error': 'usage_limit_reached'}},
                {'ok': True, 'checks': {}},
            ])
            with patch.object(limits, 'wait') as wait:
                factory.preflight_ready(Path(tmp), state, lambda: next(reports))
            self.assertTrue(wait.called)

    def test_preflight_does_not_retry_unrelated_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {'ok': False, 'checks': {'sandbox': False, 'researcher_tool_call': False,
                                             'researcher_error': 'usage_limit_reached'}}
            with patch.object(limits, 'wait'), self.assertRaises(factory.Blocked):
                factory.preflight_ready(Path(tmp), {'stage': 'build:agent'}, lambda: report)

    def test_preflight_repeated_limit_uses_new_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'status': 'running', 'stage': 'build:agent'}
            now = [1000]
            deadlines = []
            report = {'ok': False, 'checks': {'critic_tool_call': False, 'critic_error': 'usage_limit_reached'}}
            reports = iter([report, report, {'ok': True}])
            def advance(seconds):
                now[0] += seconds
                deadlines.append(factory.load(root / 'state.json')['rate_limit']['resume_at'])
            with patch.object(limits.time, 'time', side_effect=lambda: now[0]), patch.object(limits.time, 'sleep', side_effect=advance):
                factory.preflight_ready(root, state, lambda: next(reports))
            self.assertEqual(set(deadlines), {2800, 4600})
            self.assertEqual(state['status'], 'running')

    def test_standard_pipeline_retries_without_burning_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'campaign'
            brief = Path(tmp) / 'brief.md'
            brief.write_text('Test')
            factory.initialize(root, brief, factory.default_config())
            responses = iter([{'ok': False, 'error': 'usage_limit_reached'},
                              {'ok': False, 'error': 'usage_limit_reached'}])
            def invoke(*args):
                try:
                    return next(responses)
                except StopIteration:
                    raise factory.Blocked('end of test')
            now = [1000]
            with patch.object(limits.time, 'time', side_effect=lambda: now[0]), \
                 patch.object(limits.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
                with self.assertRaisesRegex(factory.Blocked, 'end of test'):
                    factory.run(root, invoke, None)
            state = factory.load(root / 'state.json')
            self.assertEqual(len(state['limit_history']), 2)
            self.assertEqual(state['attempts']['1:research:0'], 1)
            self.assertEqual(state['stage'], 'research')
