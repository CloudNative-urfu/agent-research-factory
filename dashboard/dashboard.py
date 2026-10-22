"""Read-only local dashboard for research campaign artifacts."""

import argparse
import json
import re
import sqlite3
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STAGES = ['research', 'plan', 'build', 'experiment', 'report', 'verify', 'review', 'confirm']
STAGES_V2 = ['research', 'idea_review', 'pilot_plan', 'pilot', 'pilot_review', *STAGES[1:]]
DOCUMENTS = {
    'progress': 'cycles/{cycle}/work/progress.md',
    'hypotheses': 'cycles/{cycle}/work/artifacts/hypotheses.md',
    'protocol': 'cycles/{cycle}/work/artifacts/protocol.json',
    'results': 'cycles/{cycle}/work/artifacts/results.json',
    'report': 'cycles/{cycle}/work/artifacts/report.md',
    'review': 'cycles/{cycle}/review/artifacts/review.json',
    'verification': 'cycles/{cycle}/verification.json',
    'final': 'FINAL.md',
    'selection': 'selection.json',
    'candidates': 'cycles/{cycle}/work/artifacts/candidates.json',
    'idea_review': 'cycles/{cycle}/work/artifacts/idea_review.json',
    'pilot_plan': 'cycles/{cycle}/work/artifacts/pilot_plan.json',
    'pilot_results': 'cycles/{cycle}/work/artifacts/pilot_results.json',
    'pilot_review': 'cycles/{cycle}/work/artifacts/pilot_review.json',
    'materials': 'materials.json',
}
LIMIT = 256 * 1024


def safe_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path outside campaign')
    if any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise ValueError('Symlinks are not served')
    return path


def read_json(root, relative, limit=LIMIT):
    try:
        path = safe_path(root, relative)
        if path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=128)
def log_summary(path, size, modified):
    outcomes, requests, errors = set(), 0, 0
    with Path(path).open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if all(key in row for key in ('id', 'condition', 'policy')):
                outcomes.add(tuple(str(row[key]) for key in ('id', 'condition', 'policy')))
            if Path(path).name.startswith('accounting'):
                requests += 1
                errors += bool(row.get('error') or (row.get('http_status') is not None and row['http_status'] != 200))
    return frozenset(outcomes), requests, errors


def experiment_progress(root, cycle):
    base = f'cycles/{cycle:02d}/work'
    artifacts = safe_path(root, base + '/artifacts')
    if not artifacts.is_dir():
        return []
    runs = []
    for ledger in sorted(artifacts.glob('**/ledger.sqlite')):
        try:
            ledger = safe_path(root, str(ledger.relative_to(root)))
            db = sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=0.1)
            try:
                counts = dict(db.execute('SELECT status,count(*) FROM tasks GROUP BY status').fetchall())
                calls, errors = db.execute("SELECT count(*),coalesce(sum(status IN ('failed','unknown','pending')),0) FROM attempts").fetchone()
            finally:
                db.close()
            runs.append({'run': str(ledger.parent.relative_to(artifacts)),
                         'done': sum(counts.get(key, 0) for key in ('completed', 'truncated', 'refused', 'failed')),
                         'total': sum(counts.values()), 'requests': calls, 'request_errors': errors,
                         'unknown': counts.get('unknown', 0) + counts.get('in_flight', 0),
                         'updated': ledger.stat().st_mtime})
        except (OSError, ValueError, sqlite3.Error):
            continue
    for spec_path in sorted(artifacts.glob('spec_*.json')):
        spec = read_json(root, str(spec_path.relative_to(root)))
        run = spec.get('run_id', '')
        if not isinstance(run, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', run):
            continue
        conditions, policies = spec.get('conditions', []), spec.get('policies', [])
        if not isinstance(conditions, list) or not isinstance(policies, list):
            continue
        ids = spec.get('ids')
        count = len(ids) if isinstance(ids, list) else None
        n = spec.get('n')
        if isinstance(n, int) and n > 0:
            count = min(count, n) if count is not None else n
        if count is None:
            split = read_json(root, base + '/data/derived/split.json')
            subset = split.get(spec.get('subset', ''))
            if isinstance(subset, list):
                count = len(subset)
        total = count * len(conditions) * len(policies) if count is not None else None
        directory = safe_path(root, base + '/artifacts/' + run)
        keys, calls, errors, updated = set(), 0, 0, 0
        if directory.is_dir():
            for path in directory.iterdir():
                if not re.fullmatch(r'(outcomes|accounting)(\.[a-zA-Z0-9_-]+)*\.jsonl', path.name):
                    continue
                try:
                    path = safe_path(root, str(path.relative_to(root)))
                    stat = path.stat()
                    found, requests, failed = log_summary(str(path), stat.st_size, stat.st_mtime_ns)
                    keys.update(key for key in found if key[1] in conditions and key[2] in policies)
                    calls += requests
                    errors += failed
                    updated = max(updated, stat.st_mtime)
                except (OSError, ValueError):
                    continue
        runs.append({'run': run, 'done': len(keys), 'total': total,
                     'requests': calls, 'request_errors': errors, 'updated': updated})
    return sorted(runs, key=lambda item: item['updated'], reverse=True)


def snapshot(root):
    stored_state = read_json(root, 'state.json', limit=64 * 1024 * 1024)
    state = {key: stored_state.get(key) for key in ('status', 'cycle', 'stage', 'winner', 'error', 'last_failure')}
    state['candidates'] = [{'cycle': c.get('cycle'), 'kind': c.get('kind', 'full'), 'review': {'verdict': c.get('review', {}).get('verdict')}}
                           for c in stored_state.get('candidates', [])]
    cfg = read_json(root, 'campaign.json')
    cycles, activity = [], []
    directory = safe_path(root, 'cycles')
    if directory.exists():
        for cycle in sorted(directory.iterdir()):
            if not re.fullmatch(r'\d{2,4}', cycle.name) or cycle.is_symlink() or not cycle.is_dir():
                continue
            documents = []
            for key, template in DOCUMENTS.items():
                try:
                    path = safe_path(root, template.format(cycle=cycle.name))
                    if path.is_file():
                        documents.append({'id': key, 'updated': path.stat().st_mtime})
                except (OSError, ValueError):
                    continue
            metadata = read_json(root, f'cycles/{cycle.name}/cycle.json')
            cycles.append({'id': cycle.name, 'documents': documents, 'pipeline_version': metadata.get('pipeline_version', 1)})
            logs = safe_path(root, f'cycles/{cycle.name}/logs')
            if logs.exists():
                for path in logs.iterdir():
                    if path.is_symlink() or not path.is_file() or not re.fullmatch(r'[a-z_]+-\d+-\d+\.(jsonl|receipt\.json)', path.name):
                        continue
                    stat = path.stat()
                    item = {'cycle': cycle.name, 'name': path.name, 'updated': stat.st_mtime, 'bytes': stat.st_size}
                    if path.name.endswith('.receipt.json'):
                        receipt = read_json(root, str(path.relative_to(root)))
                        item.update({k: receipt.get(k) for k in ('ok', 'error', 'cost_usd')})
                    activity.append(item)
    activity.sort(key=lambda item: item['updated'], reverse=True)
    experiments = experiment_progress(root, state['cycle']) if isinstance(state['cycle'], int) else []
    return {'time': time.time(), 'campaign': root.name, 'state': state, 'experiments': experiments,
            'models': {role: cfg.get(role, {}).get('model') for role in ('researcher', 'critic', 'experiment')},
            'total_cycles': cfg.get('cycles'), 'pipeline_version': cfg.get('pipeline_version', 1),
            'stages': STAGES_V2 if cfg.get('pipeline_version', 1) == 2 else STAGES, 'cycles': cycles,
            'activity': activity[:30], 'doctor': read_json(root, 'doctor.json')}


def document(root, kind, cycle):
    if kind not in DOCUMENTS or not re.fullmatch(r'\d{2,4}', cycle):
        raise ValueError('Unknown document')
    path = safe_path(root, DOCUMENTS[kind].format(cycle=cycle))
    with path.open('rb') as stream:
        size = path.stat().st_size
        if kind == 'progress' and size > LIMIT:
            stream.seek(size - LIMIT)
        text = stream.read(LIMIT).decode('utf-8', errors='replace')
    if path.suffix == '.json' and size <= LIMIT:
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return {'text': text, 'path': str(path.relative_to(root)), 'truncated': size > LIMIT,
            'updated': path.stat().st_mtime}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        try:
            if parsed.path == '/':
                body = Path(__file__).with_name('dashboard.html').read_bytes()
                content_type = 'text/html; charset=utf-8'
            elif parsed.path == '/api/status':
                body = json.dumps(snapshot(self.server.root), ensure_ascii=False).encode()
                content_type = 'application/json; charset=utf-8'
            elif parsed.path == '/api/document':
                args = parse_qs(parsed.query)
                body = json.dumps(document(self.server.root, args.get('kind', [''])[0], args.get('cycle', [''])[0]), ensure_ascii=False).encode()
                content_type = 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
        except (OSError, ValueError):
            self.send_error(404, 'Artifact not available')
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    parser.add_argument('--port', type=int, default=8788)
    args = parser.parse_args()
    root = args.campaign.expanduser().resolve()
    if not (root / 'state.json').is_file():
        parser.error('Campaign state.json not found')
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.root = root
        print(f'Dashboard: http://127.0.0.1:{args.port} ({root.name})', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
