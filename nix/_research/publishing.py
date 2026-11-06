"""Opt-in, unsigned Git publication of completed research revisions."""

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import factory
import deep_case


class PushPending(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo, *args):
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')
    return subprocess.run(['git', '-C', str(repo), *args], env=env, check=True,
                          capture_output=True, text=True, timeout=180).stdout


def export(source, target, git_dir):
    state = factory.load(source / 'state.json')
    if state.get('status') != 'completed' or state.get('steps', {}).get('critique', {}).get('status') != 'done':
        raise factory.Invalid('Publication requires a completed revision and critique')
    case = factory.load(source / 'case/case.json')
    deep_case.validate('critique', source / 'cycles/01/review', case)
    snapshot = source / 'cycles/01/snapshot'
    manifest_path = source / 'cycles/01/manifest.json'
    manifest = factory.load(manifest_path)
    factory.check_snapshot(snapshot, manifest)
    identity = {'campaign': source.name, 'snapshot_sha256': sha(manifest_path),
                'critique_sha256': sha(source / 'cycles/01/review/artifacts/critique.json'),
                'critique_md_sha256': sha(source / 'cycles/01/review/artifacts/critique.md')}
    if target.is_symlink() or target.parent.is_symlink():
        raise factory.Invalid('Publication destination must not be a symlink')
    marker = target / 'export-manifest.json'
    if target.exists():
        if not marker.is_file() or marker.is_symlink():
            raise factory.Invalid('Refusing unrelated existing export')
        record = factory.load(marker)
        if record.get('identity') != identity:
            raise factory.Invalid('Existing export belongs to different evidence')
        factory.check_snapshot(target, record['files'] | {'export-manifest.json': sha(marker)})
        return sha(marker)
    with tempfile.TemporaryDirectory(prefix='research-export-', dir=git_dir) as tmp:
        staging = Path(tmp) / 'export'
        staging.mkdir()
        files, omitted = {}, []
        extensions = {'.py', '.md', '.json', '.jsonl', '.csv', '.toml', '.txt', '.sh', '.nix', '.yaml', '.yml', '.sha256'}
        roots = {'darp_range', 'tests', 'scripts', 'configs', 'corpus', 'case'}

        def copy(path, relative, expected=None):
            if path.is_symlink() or any(factory.protected(part) for part in path.relative_to(source).parts):
                raise factory.Invalid('Secret-shaped file or symlink in publication input')
            if path.stat().st_size > 2_000_000:
                return False
            data = path.read_bytes()
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                return False
            if b'\0' in data:
                return False
            digest = hashlib.sha256(data).hexdigest()
            if expected and digest != expected:
                raise factory.Invalid('Publication source changed')
            dest = staging / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            files[relative] = digest
            return True

        for relative, digest in manifest.items():
            path = Path(relative)
            selected = path.parts[0] in roots or len(path.parts) == 1 or (
                len(path.parts) == 2 and path.parts[0] == 'artifacts' and path.suffix in ('.md', '.json'))
            if not selected or (path.suffix not in extensions and path.name != '.gitignore') or not copy(snapshot / path, 'prototype/' + relative, digest):
                omitted.append(relative)
        for relative in ('FINAL.md', 'state.json', 'cycles/01/verification.json'):
            if not copy(factory.safe_file(source, relative), 'campaign/' + relative):
                raise factory.Invalid('Required campaign report exceeds text export limit')
        for name in ('critique.md', 'critique.json'):
            if not copy(factory.safe_file(source, 'cycles/01/review/artifacts/' + name), 'review/' + name):
                raise factory.Invalid('Required critique exceeds text export limit')
        if not copy(manifest_path, 'source-manifest.json'):
            raise factory.Invalid('Source manifest exceeds text export limit')
        factory.check_snapshot(staging, files, 64 * 1024 * 1024)
        factory.save(staging / 'export-manifest.json', {'identity': identity, 'files': files,
                     'omitted_from_git': omitted,
                     'retention': 'Complete snapshots, raw runs and logs remain in the original campaign.'})
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, target)
    return sha(marker)


def publish(source, cfg, receipt_path):
    source = Path(source).resolve()
    repo = Path(cfg['repository']).resolve()
    branch, remote = cfg.get('branch', 'main'), cfg.get('remote', 'origin')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', source.name) or not re.fullmatch(r'[A-Za-z0-9_-]+', remote):
        raise factory.Invalid('Invalid publication name')
    if Path(git(repo, 'rev-parse', '--show-toplevel').strip()).resolve() != repo:
        raise factory.Invalid('Publication repository must be its Git root')
    git(repo, 'check-ref-format', '--branch', branch)
    if git(repo, 'symbolic-ref', '--short', 'HEAD').strip() != branch:
        raise factory.Blocked('Publication repository is on a different branch')
    if git(repo, 'remote', 'get-url', '--push', remote).strip() != cfg['remote_url']:
        raise factory.Blocked('Publication remote differs from configured destination')
    git_dir = Path(git(repo, 'rev-parse', '--git-dir').strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    relative = 'revisions/' + source.name
    with (git_dir / 'research-publish.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise factory.Blocked('Another publisher owns this repository') from exc
        status = git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
        for entry in status.split('\0'):
            if entry and not entry[3:].startswith(relative + '/'):
                raise factory.Blocked('Unrelated working-tree/index changes; publication paused')
        export_sha = export(source, repo / relative, git_dir)
        marker = relative + '/export-manifest.json'
        commit = git(repo, 'log', '-1', '--format=%H', '--', marker).strip()
        if commit:
            recorded = git(repo, 'show', f'{commit}:{marker}')
            if hashlib.sha256(recorded.encode()).hexdigest() != export_sha:
                raise factory.Invalid('Existing committed export differs; history will not be rewritten')
            if git(repo, 'status', '--porcelain=v1', '-z', '--', relative):
                raise factory.Blocked('Committed export has local changes')
        else:
            git(repo, 'add', '--force', '--', relative)
            git(repo, '-c', 'commit.gpgsign=false', 'commit', '--no-gpg-sign', '--only',
                '-m', f'chore(research): preserve {source.name} and independent review', '--', relative)
            commit = git(repo, 'rev-parse', 'HEAD').strip()
        if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
            raise factory.Invalid('Git returned an invalid commit ID')
        receipt = {'status': 'committed', 'commit': commit, 'export_sha256': export_sha,
                   'campaign': source.name, 'repository': str(repo), 'remote': remote, 'branch': branch}
        factory.save(receipt_path, receipt)
        try:
            git(repo, 'push', remote, f'{commit}:refs/heads/{branch}')
        except (subprocess.SubprocessError, OSError) as exc:
            raise PushPending(f'Commit {commit} is saved locally; push did not complete') from exc
        receipt['status'] = 'published'
        factory.save(receipt_path, receipt)
        return receipt
