import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import factory
import autopilot


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'campaigns/darp-revision-4'
        self.base = self.source / 'cycles/01'
        self.work = self.base / 'snapshot'
        self.work.mkdir(parents=True)
        cfg = factory.default_config() | {'mode': 'deep_case', 'cycles': 1,
              'case': {'repository': 'fixture', 'ref': 'research', 'documents': ['brief.md']}}
        factory.save(self.source / 'campaign.json', cfg)
        factory.save(self.source / 'state.json', {'status': 'completed', 'stage': 'done',
                     'steps': {'critique': {'status': 'done'}}})
        case = {'requirements': ['FR-1.1'], 'key_requirements': ['FR-1.1'], 'commit': 'fixture',
                'repository': 'fixture', 'ref': 'research', 'documents': {}}
        factory.save(self.source / 'case/case.json', case)
        factory.save(self.work / 'case/case.json', case)
        (self.source / 'brief.md').write_text('Original brief')
        (self.work / 'brief.md').write_text('Original brief')
        (self.work / 'main.py').write_text('print("prototype")')
        (self.work / 'progress.md').write_text('Original progress')
        factory.save(self.work / 'artifacts/decomposition.json', {'components': [
            {'id': 'core', 'title': 'Core', 'requirements': ['FR-1.1'], 'depends_on': [], 'check_command': ['true']}],
            'assumptions': [{'topic': 'test', 'decision': 'fixture', 'rationale': 'unit test'}]})
        (self.work / 'artifacts/case-update.md').write_text('Improve the current solution')
        (self.work / 'artifacts/report.md').write_text('Original report')
        review = self.base / 'review'
        factory.save(review / 'artifacts/critique.json', {'summary': 'Needs improvement', 'built': ['core'],
                     'weak': ['confirm'], 'fixes': [], 'requirements': {'FR-1.1': {'status': 'simplified'}},
                     'evidence': ['artifacts/probe.md']})
        (review / 'artifacts/critique.md').write_text('Confirm accepts a wrong account')
        (review / 'artifacts/probe.md').write_text('Reproduced')
        factory.save(self.base / 'verification.json', {'ok': True, 'checks': []})
        factory.save(self.base / 'manifest.json', factory.inventory(self.work))
        self.plan = {'problem': 'Wrong confirmation', 'beneficiary': 'Process owner',
                     'current_value': 'Reusable workflow', 'evidence': ['candidate/critique.md'],
                     'tasks': [{'id': 'confirm', 'change': 'Validate complete proposal',
                                'value': 'Correct utility measurement', 'acceptance': 'Wrong account rejected',
                                'effort': 'One revision', 'evidence': ['candidate/critique.md']}],
                     'non_goals': ['New platform'], 'alternatives_necessary': False,
                     'alternatives_reason': 'Current solution can be improved'}
        self.review = {'decision': 'proceed', 'rationale': 'Concrete fix', 'business_value': 'Reliable utility',
                       'evidence': ['candidate/critique.md'], 'approved_task_ids': ['confirm'],
                       'required_changes': [], 'pivot_evidence': ''}

    def assessor(self, role, stage, work, prompt, log):
        if (work / 'candidate/evaluation-method.md').exists():
            if role == 'researcher':
                factory.save(work / 'artifacts/evaluation-protocol.json', {
                    'phase': 'foundation', 'process': 'Refund workflow',
                    'trust_boundaries': 'Customer text versus executor state',
                    'baseline': 'Server checks; confirmation is incomplete',
                    'attack_admission': 'Real access, invariant, independent DB oracle, legitimate pair',
                    'independence': 'Critic creates held-out after freeze',
                    'measurement': 'Equal paired budgets; utility and cost; zero attacks is valid',
                    'task_ids': ['confirm'], 'evidence': ['candidate/critique.md']})
            else:
                factory.save(work / 'artifacts/realism-review.json', {
                    'acceptable': True, 'rationale': 'Fix foundation before effectiveness claims',
                    'evidence': ['candidate/critique.md']})
        if role == 'researcher':
            factory.save(work / 'artifacts/plan.json', self.plan)
            (work / 'artifacts/brainstorm.md').write_text('Compare improvements, not replacement projects')
        else:
            factory.save(work / 'artifacts/review.json', self.review)
            (work / 'artifacts/review.md').write_text('Independent assessment')
            (work / 'artifacts/independent-notes.md').write_text('Current solution has value; confirm is defective')
        return {'ok': True}

    def test_gate_runs_roles_in_order_and_resumes_without_repeating(self):
        calls = []
        def invoke(*args):
            calls.append(args[:2])
            return self.assessor(*args)
        assessment = self.root / 'assessment'
        result = autopilot.assess(self.source, assessment, invoke)
        self.assertEqual(result['decision'], 'proceed')
        self.assertEqual([c[0] for c in calls], ['researcher', 'critic'])
        autopilot.assess(self.source, assessment, invoke)
        self.assertEqual(len(calls), 2)

    def test_missing_critique_prevents_gate(self):
        (self.base / 'review/artifacts/critique.md').unlink()
        with self.assertRaises(factory.Invalid):
            autopilot.assess(self.source, self.root / 'assessment', self.assessor)

    def test_missing_protocol_blocks_new_gate(self):
        def invoke(*args):
            result = self.assessor(*args)
            (args[2] / 'artifacts/evaluation-protocol.json').unlink(missing_ok=True)
            return result
        with self.assertRaises(factory.Blocked):
            autopilot.assess(self.source, self.root / 'assessment', invoke)

    def test_realism_rejection_cannot_proceed(self):
        def invoke(*args):
            result = self.assessor(*args)
            if args[0] == 'critic':
                factory.save(args[2] / 'artifacts/realism-review.json', {
                    'acceptable': False, 'rationale': 'Artificially weakened executor',
                    'evidence': ['candidate/critique.md']})
            return result
        with self.assertRaises(factory.Blocked):
            autopilot.assess(self.source, self.root / 'assessment', invoke)

    def test_method_is_frozen_and_reaches_successor(self):
        assessment = self.root / 'assessment'
        autopilot.assess(self.source, assessment, self.assessor)
        method = (assessment / 'inputs/evaluation-method.md').read_text()
        dest = self.source.parent / 'darp-revision-5'
        autopilot.prepare_revision(self.source, dest, assessment)
        self.assertIn(method, (dest / 'brief.md').read_text())
        self.assertIn('evaluation-protocol.json', (dest / 'brief.md').read_text())

    def test_interrupted_phase_transition_reuses_saved_proposal(self):
        assessment = self.root / 'assessment'
        save = factory.save
        def interrupted(path, value):
            if path == assessment / 'state.json' and value.get('stage') == 'relevance_review':
                raise OSError('crash after proposal publication')
            save(path, value)
        with patch.object(factory, 'save', side_effect=interrupted):
            with self.assertRaises(OSError):
                autopilot.assess(self.source, assessment, self.assessor)
        calls = []
        def invoke(*args):
            calls.append(args[0])
            return self.assessor(*args)
        autopilot.assess(self.source, assessment, invoke)
        self.assertEqual(calls, ['critic'])

    def test_insufficient_disk_space_prevents_new_work(self):
        control = self.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(self.source.parent), 'initial': self.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5})
        with patch.object(autopilot, 'service_active', return_value=False), \
             patch.object(autopilot.shutil, 'disk_usage', return_value=Mock(free=0)):
            with self.assertRaisesRegex(factory.Blocked, 'free space'):
                autopilot.tick(control, self.assessor)
        self.assertFalse((control / 'assessments').exists())

    def test_review_cannot_approve_unknown_tasks(self):
        self.review['approved_task_ids'] = ['invented']
        with self.assertRaises(factory.Blocked):
            autopilot.assess(self.source, self.root / 'assessment', self.assessor)

    def test_revise_is_bounded(self):
        self.review['decision'] = 'revise'
        self.review['approved_task_ids'] = []
        self.review['required_changes'] = ['Make acceptance measurable']
        result = autopilot.assess(self.source, self.root / 'assessment', self.assessor)
        self.assertEqual(result['decision'], 'blocked')

    def test_stop_never_creates_successor(self):
        self.review['decision'] = 'stop'
        self.review['approved_task_ids'] = []
        control = self.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(self.source.parent), 'initial': self.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5, 'min_free_bytes': 0})
        with patch.object(autopilot, 'service_active', return_value=False), patch.object(autopilot, 'start_service') as start:
            autopilot.tick(control, self.assessor)
        self.assertFalse((self.source.parent / 'darp-revision-5').exists())
        start.assert_not_called()
        self.assertEqual(factory.load(control / 'state.json')['status'], 'paused')

    def test_successor_is_verified_and_idempotent(self):
        assessment = self.root / 'assessment'
        autopilot.assess(self.source, assessment, self.assessor)
        original = factory.inventory(self.work)
        dest = self.source.parent / 'darp-revision-5'
        autopilot.prepare_revision(self.source, dest, assessment)
        self.assertEqual(factory.inventory(self.work), original)
        self.assertEqual((dest / 'cycles/01/work/main.py').read_text(), 'print("prototype")')
        self.assertIn('confirm', (dest / 'brief.md').read_text())
        self.assertEqual(factory.load(dest / 'state.json')['stage'], 'integrate')
        autopilot.prepare_revision(self.source, dest, assessment)
        self.assertEqual(factory.load(dest / 'state.json')['stage'], 'integrate')

    def test_refuses_unrelated_existing_destination(self):
        assessment = self.root / 'assessment'
        autopilot.assess(self.source, assessment, self.assessor)
        dest = self.source.parent / 'darp-revision-5'
        dest.mkdir()
        (dest / 'user-work.txt').write_text('keep me')
        with self.assertRaises(factory.Invalid):
            autopilot.prepare_revision(self.source, dest, assessment)
        self.assertEqual((dest / 'user-work.txt').read_text(), 'keep me')

    def test_partial_work_copy_resumes_atomically(self):
        assessment = self.root / 'assessment'
        autopilot.assess(self.source, assessment, self.assessor)
        dest = self.source.parent / 'darp-revision-5'
        original = factory.snapshot
        def interrupted(source, target, *args):
            if target.name == 'work':
                raise OSError('interrupted before work publication')
            return original(source, target, *args)
        with patch.object(factory, 'snapshot', side_effect=interrupted):
            with self.assertRaises(OSError):
                autopilot.prepare_revision(self.source, dest, assessment)
        autopilot.prepare_revision(self.source, dest, assessment)
        self.assertTrue((dest / 'cycles/01/work/main.py').is_file())

    def test_tampered_source_does_not_launch(self):
        (self.work / 'main.py').write_text('modified after approval')
        with self.assertRaises(factory.Invalid):
            autopilot.assess(self.source, self.root / 'assessment', self.assessor)

    def test_quota_does_not_consume_attempt_and_state_survives(self):
        assessment = self.root / 'assessment'
        with patch.object(factory.limits, 'wait', side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                autopilot.assess(self.source, assessment,
                                 lambda *args: {'ok': False, 'error': 'usage_limit_reached'})
        state = factory.load(assessment / 'state.json')
        self.assertEqual(state['status'], 'waiting_limit')
        self.assertEqual(state['attempt'], 0)
        with patch.object(factory.limits, 'wait'):
            result = autopilot.assess(self.source, assessment, self.assessor)
        self.assertEqual(result['decision'], 'proceed')

    def test_active_campaign_is_not_interrupted(self):
        control = self.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(self.source.parent), 'initial': self.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5, 'min_free_bytes': 0})
        with patch.object(autopilot, 'service_active', return_value=True), patch.object(autopilot, 'start_service') as start:
            autopilot.tick(control, self.assessor)
        start.assert_not_called()
        self.assertFalse((control / 'assessments').exists())

    def test_crash_after_publication_does_not_duplicate_revision(self):
        control = self.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(self.source.parent), 'initial': self.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5, 'min_free_bytes': 0})
        with patch.object(autopilot, 'service_active', return_value=False), \
             patch.object(autopilot, 'start_service', side_effect=OSError('simulated systemd failure')):
            with self.assertRaises(OSError):
                autopilot.tick(control, self.assessor)
        with patch.object(autopilot, 'service_active', return_value=False), patch.object(autopilot, 'start_service') as start:
            autopilot.tick(control, self.assessor)
        start.assert_called_once_with('darp-revision-5')
        self.assertFalse((self.source.parent / 'darp-revision-6').exists())
