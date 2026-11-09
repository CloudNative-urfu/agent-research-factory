import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import factory
import publishing


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'darp-revision-5'
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        self.snapshot = self.source / 'cycles/01/snapshot'
        factory.save(self.source / 'state.json', {'status': 'completed', 'steps': {'critique': {'status': 'done'}}})
        factory.save(self.source / 'case/case.json', {'requirements': ['FR-1.1']})
        factory.save(self.source / 'campaign.json', {'max_snapshot_bytes': 1000000})
        factory.save(self.source / 'cycles/01/verification.json', {'ok': True})
        (self.source / 'FINAL.md').write_text('Completed')
        (self.snapshot / 'darp_range').mkdir(parents=True)
        (self.snapshot / 'darp_range/main.py').write_text('print("prototype")')
        (self.snapshot / 'runs').mkdir()
        (self.snapshot / 'runs/raw.log').write_text('kept locally')
        (self.snapshot / 'artifacts').mkdir()
        (self.snapshot / 'artifacts/report.md').write_text('Known limitations')
        factory.save(self.source / 'cycles/01/manifest.json', factory.inventory(self.snapshot))
        review = self.source / 'cycles/01/review'
        factory.save(review / 'artifacts/critique.json', {'summary': 'Review', 'built': ['core'], 'weak': [],
                     'fixes': [], 'requirements': {'FR-1.1': {'status': 'simplified'}},
                     'evidence': ['artifacts/probe.md']})
        (review / 'artifacts/critique.md').write_text('Independent review')
        (review / 'artifacts/probe.md').write_text('Checked')
        self.cfg = {'repository': str(self.repo), 'remote': 'origin', 'branch': 'main', 'remote_url': 'ssh://example/repo.git'}
        self.receipt = self.root / 'receipt.json'
        self.calls = []
        self.committed = False
        self.fail_push = False

    def git(self, repo, *args):
        self.calls.append(args)
        if args == ('rev-parse', '--show-toplevel'):
            return str(self.repo)
        if args == ('rev-parse', '--git-dir'):
            return str(self.repo / '.git')
        if args == ('symbolic-ref', '--short', 'HEAD'):
            return 'main'
        if args[:2] == ('remote', 'get-url'):
            return self.cfg['remote_url']
        if args[:1] == ('status',):
            return ''
        if args[:1] == ('log',):
            return 'a' * 40 if self.committed else ''
        if args[:1] == ('show',):
            return (self.repo / 'revisions/darp-revision-5/export-manifest.json').read_text()
        if 'commit' in args:
            self.committed = True
        if args == ('rev-parse', 'HEAD'):
            return 'a' * 40
        if args[:1] == ('push',) and self.fail_push:
            raise subprocess.CalledProcessError(1, ['git', *args], stderr='network failure')
        return ''

    def test_commit_is_unsigned_and_pushes_exact_commit_without_force(self):
        with patch.object(publishing, 'git', side_effect=self.git):
            result = publishing.publish(self.source, self.cfg, self.receipt)
        commit = next(c for c in self.calls if 'commit' in c)
        self.assertIn('commit.gpgsign=false', commit)
        self.assertIn('--no-gpg-sign', commit)
        self.assertIn('--only', commit)
        self.assertIn(('push', 'origin', 'a' * 40 + ':refs/heads/main'), self.calls)
        self.assertEqual(result['status'], 'published')
        self.assertTrue((self.snapshot / 'runs/raw.log').exists())

    def test_failed_push_reuses_commit(self):
        with patch.object(publishing, 'git', side_effect=self.git):
            self.fail_push = True
            with self.assertRaises(publishing.PushPending):
                publishing.publish(self.source, self.cfg, self.receipt)
            self.assertEqual(factory.load(self.receipt)['status'], 'committed')
            self.fail_push = False
            publishing.publish(self.source, self.cfg, self.receipt)
        self.assertEqual(sum('commit' in c for c in self.calls), 1)

    def test_large_source_manifest_preserved_and_retry_reuses_export(self):
        manifest = self.source / 'cycles/01/manifest.json'
        large = self.snapshot / 'darp_range/large.txt'
        large.write_bytes(b'a' * 2_000_001)
        # Retain a valid inventory and make its JSON representation larger than the text limit.
        factory.save(manifest, factory.inventory(self.snapshot))
        original = manifest.read_bytes() + b' ' * 2_000_001
        manifest.write_bytes(original)
        target = self.repo / 'revisions/darp-revision-5'
        digest = publishing.export(self.source, target, self.repo / '.git')
        self.assertEqual((target / 'source-manifest.json').read_bytes(), original)
        record = factory.load(target / 'export-manifest.json')
        self.assertEqual(record['identity']['snapshot_sha256'], publishing.sha(manifest))
        self.assertEqual(record['files']['source-manifest.json'], publishing.sha(manifest))
        self.assertIn('darp_range/large.txt', record['omitted_from_git'])
        self.assertFalse((target / 'prototype/darp_range/large.txt').exists())
        self.assertEqual(publishing.export(self.source, target, self.repo / '.git'), digest)

    def test_export_manifest_counts_towards_total_budget(self):
        reference = self.root / 'reference'
        publishing.export(self.source, reference, self.repo / '.git')
        without_marker = sum(p.stat().st_size for p in reference.rglob('*')
                             if p.is_file() and p.name != 'export-manifest.json')
        target = self.repo / 'revisions/darp-revision-5'
        with patch.object(publishing, 'MAX_EXPORT_BYTES', without_marker, create=True):
            with patch.object(publishing, 'git', side_effect=self.git), self.assertRaises(factory.Invalid):
                publishing.publish(self.source, self.cfg, self.receipt)
        self.assertFalse(target.exists())
        self.assertFalse(any('commit' in c for c in self.calls))

    def test_crash_after_commit_recovers_without_second_commit(self):
        self.committed = True
        with patch.object(publishing, 'git', side_effect=self.git):
            publishing.publish(self.source, self.cfg, self.receipt)
        self.assertFalse(any('commit' in c for c in self.calls))

    def test_unrelated_dirty_files_block_publication(self):
        def dirty(repo, *args):
            if args[:1] == ('status',):
                return ' M notes.txt\0'
            return self.git(repo, *args)
        with patch.object(publishing, 'git', side_effect=dirty), self.assertRaises(factory.Blocked):
            publishing.publish(self.source, self.cfg, self.receipt)
        self.assertFalse(any('commit' in c for c in self.calls))

    def test_existing_export_must_match_not_be_overwritten(self):
        target = self.repo / 'revisions/darp-revision-5'
        target.mkdir(parents=True)
        (target / 'user-work.txt').write_text('keep')
        with patch.object(publishing, 'git', side_effect=self.git), self.assertRaises(factory.Invalid):
            publishing.publish(self.source, self.cfg, self.receipt)
        self.assertEqual((target / 'user-work.txt').read_text(), 'keep')

    def test_disabled_hook_does_not_call_git(self):
        import autopilot
        from test_autopilot import AutopilotTests
        fixture = AutopilotTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        control = fixture.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(fixture.source.parent), 'initial': fixture.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5, 'min_free_bytes': 0,
                     'publication': {'enabled': False}})
        with patch.object(autopilot, 'service_active', return_value=False), \
             patch.object(autopilot, 'assess', return_value={'decision': 'stop', 'rationale': 'No changes'}), \
             patch.object(publishing, 'git') as git:
            autopilot.tick(control)
        git.assert_not_called()

    def test_legacy_manifest_pause_recovers_with_history(self):
        import autopilot
        from test_autopilot import AutopilotTests
        fixture = AutopilotTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        control = fixture.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(fixture.source.parent), 'initial': fixture.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5, 'min_free_bytes': 0,
                     'publication': self.cfg | {'enabled': True}})
        old = {'current': fixture.source.name, 'next_number': 5, 'status': 'paused',
               'reason': 'Source manifest exceeds text export limit'}
        factory.save(control / 'state.json', old)
        with patch.object(autopilot, 'service_active', return_value=False), \
             patch.object(publishing, 'publish', return_value={'status': 'published'}) as publish, \
             patch.object(autopilot, 'assess', return_value={'decision': 'stop', 'rationale': 'No next task'}):
            autopilot.tick(control)
        publish.assert_called_once()
        record = factory.load(control / 'history' / ('manifest-limit-recovery-' + fixture.source.name + '.json'))
        self.assertEqual(record['previous_state'], old)
        self.assertEqual(factory.load(control / 'state.json')['reason'], 'No next task')

    def test_other_pauses_and_disabled_publication_do_not_resume(self):
        import autopilot
        control = self.root / 'control'
        for reason, enabled in (('Operator pause', True), ('Source manifest exceeds text export limit', False)):
            with self.subTest(reason=reason, enabled=enabled):
                factory.save(control / 'config.json', {'campaigns': str(self.source.parent), 'initial': self.source.name,
                             'prefix': 'darp-revision-', 'next_number': 6,
                             'publication': self.cfg | {'enabled': enabled}})
                state = {'current': self.source.name, 'next_number': 6, 'status': 'paused', 'reason': reason}
                factory.save(control / 'state.json', state)
                with patch.object(publishing, 'publish') as publish:
                    autopilot.tick(control)
                publish.assert_not_called()
                self.assertEqual(factory.load(control / 'state.json'), state)

    def test_push_failure_prevents_next_assessment_and_schedules_retry(self):
        import autopilot
        from test_autopilot import AutopilotTests
        fixture = AutopilotTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        control = fixture.root / 'control'
        factory.save(control / 'config.json', {'campaigns': str(fixture.source.parent), 'initial': fixture.source.name,
                     'prefix': 'darp-revision-', 'next_number': 5, 'min_free_bytes': 0,
                     'publication': self.cfg | {'enabled': True}})
        with patch.object(autopilot, 'service_active', return_value=False), \
             patch.object(publishing, 'publish', side_effect=publishing.PushPending('push failed')), \
             patch.object(autopilot, 'assess') as assess:
            autopilot.tick(control)
        assess.assert_not_called()
        state = factory.load(control / 'state.json')
        self.assertEqual(state['status'], 'waiting_publish')
        self.assertEqual(state['current'], fixture.source.name)
        self.assertEqual(state['next_number'], 5)
        self.assertGreater(state['publish_retry_at'], 0)
