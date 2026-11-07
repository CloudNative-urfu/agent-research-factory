import tempfile
import unittest
from pathlib import Path

import factory
import spec_workflow


class SpecWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.change = self.work / 'artifacts/openspec/changes/isolate-workers'
        (self.change / 'specs/isolation').mkdir(parents=True)
        (self.change / 'proposal.md').write_text('## Why\nIsolation\n## What Changes\nSeparate workers\n## Impact\nRuntime\n')
        (self.change / 'design.md').write_text('Separate processes and private mounts.')
        (self.change / 'tasks.md').write_text('- [ ] isolate Prove isolation with synthetic canaries\n')
        (self.change / 'specs/isolation/spec.md').write_text(
            '## ADDED Requirements\n\n### Requirement: Private workers\n'
            'Workers SHALL not read opponent files.\n\n#### Scenario: Denied read\n'
            '- **WHEN** attacker requests a defender file\n- **THEN** access is denied\n')
        factory.save(self.work / 'artifacts/openspec-plan.json', {'change': 'isolate-workers'})
        self.plan = {'tasks': [{'id': 'isolate'}]}

    def test_valid_spec_and_partial_evidence(self):
        contract = spec_workflow.validate_proposal(self.work, self.plan)
        self.install(contract)
        spec_workflow.validate_delivery('report', self.work, contract)

    def install(self, contract):
        import shutil
        shutil.copytree(self.work / 'artifacts/openspec', self.work / 'openspec')
        (self.work / 'artifacts/probe.md').write_text('RED: opponent mount visible; implementation pending')
        (self.work / 'artifacts/coevolution-progress.md').write_text('A: pending; see probe.md')
        factory.save(self.work / 'artifacts/openspec-results.json', {
            'change': contract['change'], 'tasks': [{'id': 'isolate', 'status': 'partial',
            'summary': 'OS boundary not yet implemented', 'evidence': ['artifacts/probe.md']}]})

    def test_unknown_task_cannot_be_approved(self):
        (self.change / 'tasks.md').write_text('- [ ] unrelated Wrong scope\n')
        with self.assertRaises(factory.Invalid):
            spec_workflow.validate_proposal(self.work, self.plan)

    def test_openspec_rejects_missing_scenarios(self):
        (self.change / 'specs/isolation/spec.md').write_text('## ADDED Requirements\n### Requirement: Isolation\nWorkers SHALL be private.\n')
        with self.assertRaises(factory.Invalid):
            spec_workflow.validate_proposal(self.work, self.plan)

    def test_partial_cannot_have_completed_checkbox(self):
        contract = spec_workflow.validate_proposal(self.work, self.plan)
        self.install(contract)
        task = self.work / 'openspec/changes/isolate-workers/tasks.md'
        task.write_text(task.read_text().replace('[ ]', '[x]'))
        with self.assertRaises(factory.Invalid):
            spec_workflow.validate_delivery('report', self.work, contract)

    def test_rewritten_approved_scope_is_rejected(self):
        contract = spec_workflow.validate_proposal(self.work, self.plan)
        self.install(contract)
        (self.work / 'openspec/changes/isolate-workers/design.md').write_text('Shared process is enough')
        with self.assertRaises(factory.Invalid):
            spec_workflow.validate_delivery('report', self.work, contract)

    def test_implemented_requires_evidence(self):
        contract = spec_workflow.validate_proposal(self.work, self.plan)
        self.install(contract)
        result = factory.load(self.work / 'artifacts/openspec-results.json')
        result['tasks'][0]['evidence'] = ['artifacts/missing.md']
        factory.save(self.work / 'artifacts/openspec-results.json', result)
        with self.assertRaises(factory.Invalid):
            spec_workflow.validate_delivery('report', self.work, contract)

    def test_critic_cannot_omit_tasks(self):
        contract = spec_workflow.validate_proposal(self.work, self.plan)
        self.install(contract)
        (self.work / 'candidate').mkdir()
        (self.work / 'openspec').rename(self.work / 'candidate/openspec')
        factory.save(self.work / 'artifacts/openspec-review.json', {'change': contract['change'], 'tasks': []})
        with self.assertRaises(factory.Invalid):
            spec_workflow.validate_delivery('critique', self.work, contract)

    def test_critic_can_disagree_with_author(self):
        contract = spec_workflow.validate_proposal(self.work, self.plan)
        self.install(contract)
        (self.work / 'candidate').mkdir()
        (self.work / 'openspec').rename(self.work / 'candidate/openspec')
        factory.save(self.work / 'artifacts/openspec-review.json', {
            'change': contract['change'], 'tasks': [{'id': 'isolate', 'status': 'blocked',
            'summary': 'Independent OS probe failed', 'evidence': ['artifacts/probe.md']}]})
        spec_workflow.validate_delivery('critique', self.work, contract)
