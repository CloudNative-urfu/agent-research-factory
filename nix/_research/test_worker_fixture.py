"""Synthetic artifacts for the transport integration test, never a research result."""

import json
import sys
from pathlib import Path


def write(name, value):
    path = Path('artifacts') / name
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)


def main(stage):
    Path('artifacts').mkdir(exist_ok=True)
    if stage == 'research':
        assert not Path('implementation.py').exists(), 'New cycle did not start clean'
        write('hypotheses.md', 'Synthetic transport fixture')
        write('sources.json', [{'url': 'https://example.com', 'claim': 'fixture', 'status': 'synthetic'}])
    elif stage == 'plan':
        write('protocol.json', {k: 'synthetic fixture' for k in
              ('hypothesis', 'baselines', 'metrics', 'falsification', 'split_policy', 'budget')})
    elif stage == 'build':
        Path('implementation.py').write_text('assert 2 + 2 == 4\nprint("fixture verification passed")\n')
        write('submission.json', {'verify_command': ['python3', 'implementation.py']})
    elif stage == 'experiment':
        write('raw.json', [1, 2, 3])
        write('results.json', {'measurements': [1], 'evidence': ['artifacts/raw.json']})
    elif stage == 'report':
        write('report.md', 'SYNTHETIC INFRASTRUCTURE TEST, NOT RESEARCH')
        write('memory.json', {k: ['synthetic fixture'] for k in ('facts', 'failures', 'unknowns', 'next')})
    elif stage in ('review', 'confirm'):
        assert Path('candidate/implementation.py').is_file()
        write('proof.txt', 'SYNTHETIC FIXTURE')
        write('review.json', {'verdict': 'accept', 'score': 3, 'issues': [],
            'gates': {k: True for k in ('reproducible', 'hypothesis_supported', 'useful', 'relevant', 'valid_evaluation')},
            'evidence': ['artifacts/proof.txt'], 'summary': 'Synthetic fixture', 'next': [], 'publication_ready': False})
    else:
        raise ValueError(stage)


if __name__ == '__main__':
    main(sys.argv[1])
