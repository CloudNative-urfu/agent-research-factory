"""OpenSpec approval scope and evidence contracts for opted-in revisions."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import factory


TASK = re.compile(r'^- \[([ xX])\] ([a-zA-Z0-9][a-zA-Z0-9_-]*)\s+\S.*$', re.MULTILINE)
CHANGE = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')


def tasks(text):
    rows = TASK.findall(text)
    ids = [key for _, key in rows]
    if not ids or len(set(ids)) != len(ids) or len(rows) != len(re.findall(r'^\s*- \[', text, re.MULTILINE)):
        raise factory.Invalid('OpenSpec tasks require unique plan IDs: - [ ] task-id Description')
    return {key: mark.lower() == 'x' for mark, key in rows}


def scope(directory):
    manifest = factory.inventory(directory)
    if not manifest:
        raise factory.Invalid('Empty OpenSpec change')
    if 'tasks.md' in manifest:
        text = factory.safe_file(directory, 'tasks.md').read_text()
        normalized = re.sub(r'^- \[[xX]\]', '- [ ]', text, flags=re.MULTILINE)
        manifest['tasks.md'] = hashlib.sha256(normalized.encode()).hexdigest()
    return manifest


def validate_proposal(work, plan):
    record = factory.object_json(factory.safe_file(work, 'artifacts/openspec-plan.json'))
    change = record.get('change')
    if not isinstance(change, str) or not CHANGE.fullmatch(change):
        raise factory.Invalid('Invalid OpenSpec change name')
    base = work / 'artifacts'
    relative = f'openspec/changes/{change}'
    for name in ('proposal.md', 'design.md', 'tasks.md'):
        factory.safe_file(base, f'{relative}/{name}')
    directory = base / relative
    declared = tasks((directory / 'tasks.md').read_text())
    if set(declared) != {t['id'] for t in plan['tasks']} or any(declared.values()):
        raise factory.Invalid('OpenSpec tasks must match the plan and start unchecked')
    if not list((directory / 'specs').glob('*/spec.md')):
        raise factory.Invalid('OpenSpec requires delta specs')
    manifest = scope(directory)
    env = dict(os.environ, OPENSPEC_TELEMETRY='0', CI='1')
    result = subprocess.run(['openspec', 'validate', change, '--strict', '--no-interactive'],
                            cwd=base, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise factory.Invalid('OpenSpec validation failed: ' + (result.stdout + result.stderr)[-4000:])
    return {'change': change, 'tasks': list(declared), 'scope': manifest}


def validate_delivery(step, work, contract):
    if not contract or step not in ('report', 'critique'):
        return
    candidate = work / 'candidate' if step == 'critique' else work
    factory.safe_file(work, 'artifacts/coevolution-progress.md')
    change = contract['change']
    directory = candidate / 'openspec/changes' / change
    if scope(directory) != contract['scope']:
        raise factory.Invalid('Approved OpenSpec scope was changed')
    checked = tasks(factory.safe_file(directory, 'tasks.md').read_text())
    filename = 'openspec-review.json' if step == 'critique' else 'openspec-results.json'
    result = factory.object_json(factory.safe_file(work, 'artifacts/' + filename))
    rows = result.get('tasks')
    if result.get('change') != change or not isinstance(rows, list):
        raise factory.Invalid('OpenSpec results need change and tasks')
    ids = []
    for row in rows:
        if not isinstance(row, dict) or row.get('status') not in ('implemented', 'partial', 'blocked'):
            raise factory.Invalid('OpenSpec task status must be implemented, partial or blocked')
        key = row.get('id')
        if key not in contract['tasks'] or key in ids:
            raise factory.Invalid('Unknown or duplicate OpenSpec result task')
        ids.append(key)
        if not isinstance(row.get('summary'), str) or not row['summary'].strip():
            raise factory.Invalid('OpenSpec task needs a result summary')
        factory.evidence(work, row.get('evidence'))
        if step == 'report' and checked[key] != (row['status'] == 'implemented'):
            raise factory.Invalid('OpenSpec checkbox contradicts task result')
    if set(ids) != set(contract['tasks']):
        raise factory.Invalid('OpenSpec results must cover every approved task')
