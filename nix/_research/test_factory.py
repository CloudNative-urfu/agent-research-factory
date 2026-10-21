import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import factory


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'campaign'
        self.brief = Path(self.tmp.name) / 'brief.md'
        self.brief.write_text('Research brief')
        self.config = factory.default_config()
        self.config['cycles'] = 2
        factory.initialize(self.root, self.brief, self.config)
        self.calls = []

    def worker(self, role, stage, work, prompt, log):
        self.calls.append((role, stage, str(work)))
        a = work / 'artifacts'
        a.mkdir(exist_ok=True)
        if stage == 'research':
            self.assertFalse((work / 'implementation.py').exists())
            (a / 'hypotheses.md').write_text('Hypothesis')
            factory.save(a / 'sources.json', [{'url': 'https://example.org', 'claim': 'Example', 'status': 'verified'}])
        elif stage == 'plan':
            factory.save(a / 'protocol.json', {'hypothesis': 'H', 'baselines': ['simple'],
                'metrics': ['utility'], 'falsification': 'No improvement', 'split_policy': 'fresh', 'budget': 'small'})
        elif stage == 'build':
            (work / 'implementation.py').write_text('print("verified")')
            factory.save(a / 'submission.json', {'verify_command': ['python3', 'implementation.py']})
        elif stage == 'experiment':
            (a / 'raw.json').write_text('[1,2,3]')
            factory.save(a / 'results.json', {'measurements': [{'utility': 1}], 'evidence': ['artifacts/raw.json']})
        elif stage == 'report':
            (a / 'report.md').write_text('Report')
            factory.save(a / 'memory.json', {'facts': ['Measured improvement'],
                'failures': [], 'unknowns': ['Transfer'], 'next': ['Confirm']})
        elif stage in ('review', 'confirm'):
            self.assertTrue((work / 'candidate/implementation.py').exists())
            (a / 'reproduction.txt').write_text('Reproduced in test fixture')
            factory.save(a / 'review.json', {'verdict': 'accept', 'score': 3,
                'gates': {k: True for k in factory.GATES}, 'issues': [],
                'evidence': ['artifacts/reproduction.txt'], 'summary': 'Useful',
                'next': ['Improve coverage'], 'publication_ready': False})
        return {'ok': True, 'session_id': 'fixture', 'seconds': 0}

    def verify(self, work, argv, log):
        return {'ok': True, 'exit_code': 0, 'seconds': 0}

    def test_two_cycles_and_confirmation(self):
        factory.run(self.root, self.worker, self.verify)
        state = factory.load(self.root / 'state.json')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(state['candidates']), 2)
        self.assertEqual(sum(s == 'confirm' for _, s, _ in self.calls), 1)
        self.assertTrue((self.root / 'FINAL.md').exists())
        count = len(self.calls)
        factory.run(self.root, self.worker, self.verify)
        self.assertEqual(len(self.calls), count)

    def test_failure_resumes_same_stage(self):
        def broken(*args):
            if args[1] == 'build':
                return {'ok': False, 'error': 'quota', 'seconds': 0}
            return self.worker(*args)
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, broken, self.verify)
        state = factory.load(self.root / 'state.json')
        self.assertEqual(state['stage'], 'build')
        self.assertEqual(state['cycle'], 1)
        factory.run(self.root, self.worker, self.verify, retry=True)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'completed')

    def test_extend_exhausted_campaign_keeps_evidence_and_swaps_roles(self):
        factory.run(self.root, self.worker, lambda *args: {'ok': False, 'exit_code': 1})
        before = factory.load(self.root / 'state.json')
        old_config = factory.load(self.root / 'campaign.json')
        snapshot = (self.root / 'cycles/01/snapshot/implementation.py').read_bytes()
        archive = factory.extend(self.root, 4, swap_agents=True)
        state = factory.load(self.root / 'state.json')
        config = factory.load(self.root / 'campaign.json')
        self.assertEqual(state['cycle'], 3)
        self.assertEqual(state['stage'], 'research')
        self.assertEqual(state['candidates'], before['candidates'])
        self.assertEqual(state['attempts'], before['attempts'])
        self.assertEqual(config['researcher'], old_config['critic'])
        self.assertEqual(config['critic'], old_config['researcher'])
        self.assertEqual(config['cycles'], 4)
        self.assertEqual(factory.load(archive / 'state.json'), before)
        self.assertTrue((archive / 'FINAL.md').exists())
        self.assertFalse((self.root / 'FINAL.md').exists())
        self.assertEqual((self.root / 'cycles/01/snapshot/implementation.py').read_bytes(), snapshot)
        self.calls.clear()
        factory.run(self.root, self.worker, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'completed')
        self.assertEqual(sum(stage == 'research' for _, stage, _ in self.calls), 2)
        self.assertTrue((self.root / 'cycles/03/work/prior-evidence/01/review.json').exists())

    def test_extend_rejects_running_or_non_increasing_budget(self):
        with self.assertRaises(factory.Invalid):
            factory.extend(self.root, 4)
        factory.run(self.root, self.worker, self.verify)
        with self.assertRaises(factory.Invalid):
            factory.extend(self.root, 2)

    def test_extend_archives_previous_confirmation(self):
        factory.run(self.root, self.worker, self.verify)
        archive = factory.extend(self.root, 3)
        self.assertTrue((archive / 'confirmations/01/artifacts/review.json').exists())
        self.assertFalse((self.root / 'confirmations').exists())

    def test_extend_restores_state_when_commit_fails(self):
        factory.run(self.root, self.worker, self.verify)
        old_state = factory.load(self.root / 'state.json')
        old_config = factory.load(self.root / 'campaign.json')
        original_save = factory.save

        def fail_commit(path, value):
            if path == self.root / 'state.json':
                raise OSError('simulated write failure')
            original_save(path, value)

        with patch.object(factory, 'save', side_effect=fail_commit), self.assertRaises(OSError):
            factory.extend(self.root, 3, swap_agents=True)
        self.assertEqual(factory.load(self.root / 'state.json'), old_state)
        self.assertEqual(factory.load(self.root / 'campaign.json'), old_config)
        self.assertTrue((self.root / 'FINAL.md').exists())
        self.assertTrue((self.root / 'confirmations/01/artifacts/review.json').exists())

    def test_reviewer_cannot_accept_missing_evidence(self):
        work = self.root / 'invalid'
        work.mkdir()
        factory.save(work / 'review.json', {'verdict': 'accept', 'score': 4,
            'gates': {k: True for k in factory.GATES}, 'issues': [],
            'evidence': ['missing.txt'], 'summary': 'Trust me', 'next': [],
            'publication_ready': True})
        with self.assertRaises(factory.Invalid):
            factory.validate_review(work, work / 'review.json')

    def test_snapshot_detects_tampering(self):
        work = self.root / 'work'
        work.mkdir()
        (work / 'data.txt').write_text('original')
        target = self.root / 'snapshot'
        digest = factory.snapshot(work, target)
        (target / 'data.txt').write_text('changed')
        with self.assertRaises(factory.Invalid):
            factory.check_snapshot(target, digest)

    def test_external_symlinks_rejected(self):
        work = self.root / 'work'
        work.mkdir()
        (work / 'outside').symlink_to(self.brief)
        with self.assertRaises(factory.Invalid):
            factory.snapshot(work, self.root / 'snapshot')

    def test_runtime_sockets_and_venv_links_leave_the_snapshot(self):
        work = self.root / 'work'
        (work / 'pilot_work/tmp/t1/run').mkdir(parents=True)
        holder = socket.socket(socket.AF_UNIX)
        self.addCleanup(holder.close)
        holder.bind(str(work / 'pilot_work/tmp/t1/run/worker.sock'))
        (work / 'pilot_work/venvs/agentgit/bin').mkdir(parents=True)
        (work / 'pilot_work/venvs/agentgit/bin/python3').symlink_to('/bin/sh')
        (work / 'artifacts').mkdir()
        (work / 'artifacts/results.json').write_text('{"ok": true}')
        manifest = factory.snapshot(work, self.root / 'snapshot')
        self.assertEqual(set(manifest), {'artifacts/results.json'})
        self.assertFalse((self.root / 'snapshot/pilot_work').exists())

    def test_socket_outside_runtime_dirs_never_enters_the_snapshot(self):
        work = self.root / 'work'
        work.mkdir()
        holder = socket.socket(socket.AF_UNIX)
        self.addCleanup(holder.close)
        holder.bind(str(work / 'worker.sock'))
        (work / 'report.md').write_text('evidence')
        manifest = factory.snapshot(work, self.root / 'snapshot')
        self.assertEqual(set(manifest), {'report.md'})
        self.assertFalse((self.root / 'snapshot/worker.sock').exists())

    def test_verification_environment_is_not_archived(self):
        work = self.root / 'work'
        (work / '.venv_verify').mkdir(parents=True)
        (work / '.venv_verify/lib64').symlink_to('lib')
        (work / 'project.py').write_text('print("project")')
        manifest = factory.snapshot(work, self.root / 'snapshot')
        self.assertEqual(set(manifest), {'project.py'})
        self.assertFalse((self.root / 'snapshot/.venv_verify').exists())

    def test_zero_exit_without_terminal_event_is_failure(self):
        self.assertFalse(factory.parse_output('opencode', '{"type":"step_start"}\n', 0)['ok'])
        self.assertFalse(factory.parse_output('claude', '{"type":"result","is_error":true}', 0)['ok'])

    def test_claude_usage_limit_is_named_instead_of_success(self):
        import json
        event = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True,
                            'api_error': 'usage_limit_reached', 'api_error_status': 429,
                            'result': "You've hit your session limit · resets 3:50pm"})
        parsed = factory.parse_output('claude', event, 0)
        self.assertFalse(parsed['ok'])
        self.assertIn('usage_limit_reached', parsed['error'])
        self.assertIn('session limit', parsed['error'])
        self.assertNotEqual(parsed['error'], 'success')
        self.assertEqual(factory.parse_output('claude', '{"type":"result","subtype":"success"}\n', 3)['error'],
                         'nonzero_exit_3')

    def test_accept_requires_all_gates(self):
        work = self.root / 'review'
        work.mkdir()
        (work / 'evidence.txt').write_text('test')
        data = {'verdict': 'accept', 'score': 3, 'gates': {k: True for k in factory.GATES},
                'issues': [], 'evidence': ['evidence.txt'], 'summary': 'OK', 'next': [],
                'publication_ready': False}
        data['gates']['valid_evaluation'] = False
        factory.save(work / 'review.json', data)
        with self.assertRaises(factory.Invalid):
            factory.validate_review(work, work / 'review.json')

    def test_failed_external_verification_prevents_selection(self):
        factory.run(self.root, self.worker, lambda *args: {'ok': False, 'exit_code': 1})
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'exhausted')
        self.assertFalse(any(s == 'confirm' for _, s, _ in self.calls))

    def test_frozen_protocol_cannot_be_changed(self):
        def mutate(role, stage, work, *rest):
            result = self.worker(role, stage, work, *rest)
            if stage == 'build':
                factory.save(work / 'artifacts/protocol.json', {'hypothesis': 'post hoc'})
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, mutate, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'build')

    def test_preflight_blocks_before_any_worker(self):
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, self.worker, self.verify, preflight=lambda: {'ok': False})
        self.assertFalse(self.calls)

    def test_single_controller_lock(self):
        with factory.lock(self.root), self.assertRaises(factory.Blocked):
            factory.run(self.root, self.worker, self.verify)

    @unittest.skipUnless(shutil.which('bwrap'), 'requires Linux bubblewrap')
    def test_campaign_with_real_verification_processes(self):
        runtime = factory.Runtime(self.root, self.config)
        factory.run(self.root, self.worker, runtime.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'completed')
        self.assertTrue(factory.load(self.root / 'cycles/01/verification.json')['ok'])

    @unittest.skipUnless(shutil.which('bwrap'), 'requires Linux bubblewrap')
    def test_sandbox_hides_controller_and_protects_candidate(self):
        work = self.root / 'work'
        work.mkdir()
        (work / 'candidate').mkdir()
        (work / 'candidate/frozen.txt').write_text('immutable')
        code = ('from pathlib import Path; '
                'assert not Path("../state.json").exists(); '
                'Path("writable.txt").write_text("yes"); '
                'Path("candidate/frozen.txt").write_text("tampered")')
        result = subprocess.run(factory.sandbox(['python3', '-c', code], work, self.root),
                                capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((work / 'writable.txt').exists())
        self.assertEqual((work / 'candidate/frozen.txt').read_text(), 'immutable')

    def test_process_timeout_and_redaction(self):
        log = self.root / 'timeout.log'
        with self.assertRaises(factory.Blocked):
            factory.process(['python3', '-c', 'import time; time.sleep(30)'], self.root, log, 0.1, 10000)
        with patch.dict('os.environ', {'RESEARCH_LLM_API_KEY': 'synthetic-redaction-value'}):
            result = factory.process(['python3', '-c', 'print("synthetic-redaction-value")'],
                                     self.root, log, 10, 10000)
        self.assertTrue(result['ok'])
        self.assertNotIn('synthetic-redaction-value', log.read_text())
        self.assertIn('[REDACTED]', log.read_text())

    def test_malformed_review_is_validation_failure(self):
        work = self.root / 'malformed'
        work.mkdir()
        factory.save(work / 'review.json', [])
        with self.assertRaises(factory.Invalid):
            factory.validate_review(work, work / 'review.json')


if __name__ == '__main__':
    unittest.main()
