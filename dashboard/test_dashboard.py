import json
import tempfile
import sqlite3
import unittest
from pathlib import Path

from dashboard import document, experiment_progress, snapshot


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'state.json').write_text(json.dumps({'status': 'running', 'cycle': 1, 'stage': 'review'}))
        work = self.root / 'cycles/01/work'
        work.mkdir(parents=True)
        (work / 'progress.md').write_text('# Progress\n<script>alert(1)</script>')

    def test_live_snapshot_and_document(self):
        data = snapshot(self.root)
        self.assertEqual(data['state']['stage'], 'review')
        self.assertEqual(data['cycles'][0]['documents'][0]['id'], 'progress')
        self.assertIn('<script>', document(self.root, 'progress', '01')['text'])

    def test_truncated_and_refused_are_finished_not_api_errors(self):
        directory = self.root / 'cycles/01/work/artifacts/probe'
        directory.mkdir(parents=True)
        db = sqlite3.connect(directory / 'ledger.sqlite')
        try:
            with db:
                db.executescript('CREATE TABLE tasks(status TEXT); CREATE TABLE attempts(status TEXT);')
                for table in ('tasks', 'attempts'):
                    db.executemany(f'INSERT INTO {table} VALUES (?)', [('truncated',), ('refused',), ('failed',)])
        finally:
            db.close()
        run = experiment_progress(self.root, 1)[0]
        self.assertEqual((run['done'], run['total'], run['request_errors']), (3, 3, 1))

    def test_v2_screening_and_mixed_cycle_versions(self):
        (self.root / 'campaign.json').write_text('{"pipeline_version":2}')
        (self.root / 'state.json').write_text(json.dumps({'stage': 'idea_review', 'cycle': 2,
            'candidates': [{'cycle': 1, 'kind': 'screened_out', 'review': {'verdict': 'pivot'}}]}))
        second = self.root / 'cycles/02'
        second.mkdir()
        (second / 'cycle.json').write_text('{"pipeline_version":2}')
        data = snapshot(self.root)
        self.assertIn('pilot_review', data['stages'])
        self.assertEqual([c['pipeline_version'] for c in data['cycles']], [1, 2])
        self.assertEqual(data['state']['candidates'][0]['kind'], 'screened_out')

    def test_reject_path_traversal(self):
        for kind, cycle in [('progress', '../../'), ('../state.json', '01'), ('openrouter.env', '01')]:
            with self.assertRaises(ValueError):
                document(self.root, kind, cycle)

    def test_reject_symlink(self):
        path = self.root / 'cycles/01/work/progress.md'
        path.unlink()
        path.symlink_to(self.root / 'state.json')
        with self.assertRaises(ValueError):
            document(self.root, 'progress', '01')

    def test_partial_state_during_update(self):
        (self.root / 'state.json').write_text('{')
        self.assertIsNone(snapshot(self.root)['state']['status'])

    def test_large_history_keeps_status_without_serving_manifests(self):
        state = {'status': 'running', 'cycle': 6, 'stage': 'research',
                 'candidates': [{'cycle': 1, 'review': {'verdict': 'pivot'}, 'manifest': 'x' * 300000}]}
        (self.root / 'state.json').write_text(json.dumps(state))
        data = snapshot(self.root)
        self.assertEqual(data['state']['cycle'], 6)
        self.assertEqual(data['state']['status'], 'running')
        self.assertEqual(data['state']['candidates'][0]['review']['verdict'], 'pivot')
        self.assertNotIn('manifest', data['state']['candidates'][0])

    def test_resume_progress_deduplicates_and_tracks_activity(self):
        artifacts = self.root / 'cycles/01/work/artifacts'
        run = artifacts / 'confirm'
        run.mkdir(parents=True)
        (artifacts / 'spec_confirm.json').write_text(json.dumps({'run_id': 'confirm', 'n': 2,
                                                               'conditions': ['clean'], 'policies': ['H']}))
        row = {'id': 'one', 'condition': 'clean', 'policy': 'H'}
        for name in ('outcomes.partial.jsonl', 'outcomes.jsonl'):
            (run / name).write_text(json.dumps(row) + '\n')
        (run / 'outcomes.resume1.partial.jsonl').write_text(json.dumps(row | {'id': 'two'}) + '\n{"id":')
        (run / 'accounting.jsonl').write_text('{"http_status":200}\n')
        (run / 'accounting.resume1.jsonl').write_text('{"http_status":429}\n')
        (run / 'replay.resume1.jsonl').write_text('{"http_status":200}\n')
        data = experiment_progress(self.root, 1)[0]
        self.assertEqual((data['done'], data['total'], data['requests'], data['request_errors']), (2, 2, 2, 1))
        self.assertGreater(data['updated'], 0)
        with (run / 'accounting.resume1.jsonl').open('a') as stream:
            stream.write('{"http_status":200}\n')
        self.assertEqual(experiment_progress(self.root, 1)[0]['requests'], 3)


if __name__ == '__main__':
    unittest.main()
