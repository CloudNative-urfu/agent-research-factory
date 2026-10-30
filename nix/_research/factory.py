"""Durable research campaigns. Standard library only, Linux execution sandbox."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import llm_proxy
import mcp_probe
import workflow_v2

STAGES = ('research', 'plan', 'build', 'experiment', 'report', 'verify', 'review')
GATES = ('reproducible', 'hypothesis_supported', 'useful', 'relevant', 'valid_evaluation')
EXCLUDED = {'.git', '.venv', '.venv_verify', 'venv', 'venvs', 'tmp', 'node_modules',
            '__pycache__', '.cache', '.runtime', '.home', 'prior-evidence'}


class Invalid(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


def load(path):
    return json.loads(Path(path).read_text())


def object_json(path):
    value = load(path)
    if not isinstance(value, dict):
        raise Invalid(f'Expected JSON object: {Path(path).name}')
    return value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def default_config():
    return {'cycles': 5, 'attempts_per_stage': 2, 'timeout_seconds': 14400,
            'verify_timeout_seconds': 3600, 'max_log_bytes': 67108864,
            'max_snapshot_bytes': 2147483648,
            'researcher': {'backend': 'opencode', 'model': ''},
            'critic': {'backend': 'claude', 'model': 'opus'},
            'experiment': {'base_url': f'http://127.0.0.1:{llm_proxy.PORT}/v1',
                           'model': llm_proxy.MODEL, 'context': 262144}}


def check_config(cfg):
    if cfg.get('mode', 'research') not in ('research', 'deep_case'):
        raise Invalid('mode must be research or deep_case')
    if type(cfg.get('pipeline_version', 1)) is not int or cfg.get('pipeline_version', 1) not in (1, 2):
        raise Invalid('pipeline_version must be 1 or 2')
    for key, default in (('gate_revisions', 1), ('pilot_max_requests', 100)):
        if type(cfg.get(key, default)) is not int or cfg.get(key, default) < (0 if key == 'gate_revisions' else 1):
            raise Invalid(f'Invalid {key}')
    for key in ('cycles', 'attempts_per_stage', 'timeout_seconds', 'verify_timeout_seconds',
                'max_log_bytes', 'max_snapshot_bytes'):
        if type(cfg.get(key)) is not int or cfg[key] < 1:
            raise Invalid(f'{key}: expected positive integer')
    for role in ('researcher', 'critic'):
        if cfg.get(role, {}).get('backend') not in ('opencode', 'claude'):
            raise Invalid(f'{role}: backend must be opencode or claude')
        if not isinstance(cfg[role].get('model'), str):
            raise Invalid(f'{role}: model must be a string')
    experiment = cfg.get('experiment', {})
    if not isinstance(experiment, dict) or any(not isinstance(experiment.get(k, ''), str) for k in ('base_url', 'model')):
        raise Invalid('experiment needs base_url and model strings')


def initialize(root, brief, config):
    if config.get('mode') == 'deep_case':
        import deep_case
        return deep_case.initialize(root, brief, config)
    check_config(config)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if list(root.iterdir()):
        raise Invalid('Campaign directory must be empty')
    shutil.copyfile(brief, root / 'brief.md')
    save(root / 'campaign.json', config)
    save(root / 'state.json', {'version': config.get('pipeline_version', 1), 'status': 'ready', 'cycle': 1,
                             'stage': 'research', 'attempts': {}, 'candidates': [], 'confirm_index': 0})


@contextlib.contextmanager
def lock(root):
    with (root / 'controller.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked('Another controller owns this campaign') from exc
        yield


def safe_file(root, relative):
    if not isinstance(relative, str):
        raise Invalid('Artifact path must be a string')
    if Path(relative).is_absolute():
        raise Invalid('Artifact path must be relative')
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise Invalid('Artifact escapes workspace')
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise Invalid(f'Missing/empty artifact: {relative}')
    return path


def protected(name):
    return (name in ('.env', 'secrets', '.secrets')
            or name.endswith(('-secrets', '.pem', '.key', '.p12', '.pfx'))
            or name.startswith(('.env.', 'id_rsa', 'id_ed25519', 'token')))


def inventory(root, limit=2147483648):
    result, total = {}, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
        for name in dirs + sorted(files):
            path = Path(directory) / name
            if protected(name):
                raise Invalid(f'Secret-shaped file cannot enter artifact archive: {name}')
            if path.is_symlink():
                raise Invalid(f'Symlink cannot enter artifact archive: {path.relative_to(root)}')
            if path.is_dir():
                continue
            if not path.is_file():
                # Sockets, FIFOs and device nodes carry no storable bytes; they are runtime
                # objects, never evidence. Symlinks above stay fatal because they escape root.
                continue
            total += path.stat().st_size
            if total > limit:
                raise Invalid('Snapshot size limit exceeded; keep model weights and caches outside artifacts')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1048576), b''):
                    digest.update(block)
            result[str(path.relative_to(root))] = digest.hexdigest()
    return result


def snapshot(source, target, limit=2147483648):
    expected = inventory(source, limit)
    if target.exists():
        check_snapshot(target, expected, limit)
        return expected
    temporary = target.with_name(target.name + '.partial')
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    for relative in expected:
        dest = temporary / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, dest)
    check_snapshot(temporary, expected, limit)
    os.replace(temporary, target)
    return expected


def check_snapshot(root, expected, limit=2147483648):
    if inventory(root, limit) != expected:
        raise Invalid('Snapshot integrity mismatch')


def check_approvals(base, work):
    approved = load(base / 'approvals.json') if (base / 'approvals.json').exists() else {}
    for relative, expected in approved.items():
        path = safe_file(work, relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise Invalid(f'Approved artifact changed: {relative}; restore the reviewer-approved copy')
    return approved


def approve_artifacts(base, work, reviewer, stage):
    approved = check_approvals(base, work)
    names = ('candidates.json',) if stage == 'idea_review' else ('pilot_results.json',)
    paths = {'artifacts/' + name for name in names}
    if stage == 'pilot_review':
        pilot = load(reviewer / 'candidate/artifacts/pilot_results.json')
        paths.update(pilot['evidence'])
        for field in ('scenario_checks', 'guardrail_trials', 'lesson_checks'):
            for row in pilot[field]:
                paths.update(row['evidence'])
    for relative in sorted(paths):
        original = safe_file(reviewer / 'candidate', relative)
        current = safe_file(work, relative)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        if hashlib.sha256(current.read_bytes()).hexdigest() != digest:
            raise Invalid(f'Reviewed artifact changed: {relative}')
        approved[relative] = digest
    relative = f'artifacts/{stage}.json'
    approved[relative] = hashlib.sha256(safe_file(work, relative).read_bytes()).hexdigest()
    save(base / 'approvals.json', approved)
    save(work / 'approved-inputs.json', approved)


def evidence(root, values):
    if not isinstance(values, list) or not values:
        raise Invalid('Evidence must be a nonempty list of relative files')
    for value in values:
        safe_file(root, value)


def validate_review(work, path):
    data = object_json(path)
    if data.get('verdict') not in ('accept', 'revise', 'pivot', 'blocked'):
        raise Invalid('Invalid reviewer verdict')
    if type(data.get('score')) is not int or not 0 <= data['score'] <= 4:
        raise Invalid('Reviewer score must be 0..4')
    if not isinstance(data.get('gates'), dict) or any(type(data['gates'].get(k)) is not bool for k in GATES):
        raise Invalid('Reviewer gates must be explicit booleans')
    if not isinstance(data.get('issues'), list) or not isinstance(data.get('next'), list):
        raise Invalid('Reviewer issues and next must be lists')
    if not isinstance(data.get('summary'), str) or not data['summary'].strip():
        raise Invalid('Reviewer summary is required')
    if type(data.get('publication_ready')) is not bool:
        raise Invalid('Publication readiness must be separate boolean')
    evidence(work, data.get('evidence'))
    for issue in data['issues']:
        if not isinstance(issue, dict) or not all(issue.get(k) for k in ('severity', 'evidence', 'fix')):
            raise Invalid('Each issue needs severity, evidence and fix')
    if data['verdict'] == 'accept' and (
        not all(data['gates'].values()) or any(i['severity'] == 'blocking' for i in data['issues'])
    ):
        raise Invalid('Acceptance contradicts gates or blocking issues')
    return data


def validate_stage(stage, work):
    if stage == 'research':
        safe_file(work, 'artifacts/hypotheses.md')
        sources = load(safe_file(work, 'artifacts/sources.json'))
        if not isinstance(sources, list) or not sources:
            raise Invalid('Sources list is empty')
        if any(not isinstance(s, dict) or not all(s.get(k) for k in ('url', 'claim', 'status')) for s in sources):
            raise Invalid('Source needs url, claim and verification status')
    elif stage == 'plan':
        protocol = object_json(safe_file(work, 'artifacts/protocol.json'))
        if not all(protocol.get(k) for k in ('hypothesis', 'baselines', 'metrics', 'falsification', 'split_policy', 'budget')):
            raise Invalid('Incomplete experiment protocol')
    elif stage == 'build':
        submission = object_json(safe_file(work, 'artifacts/submission.json'))
        command = submission.get('verify_command')
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise Invalid('verify_command must be an argv list')
    elif stage == 'experiment':
        results = object_json(safe_file(work, 'artifacts/results.json'))
        if not isinstance(results.get('measurements'), list) or not results['measurements']:
            raise Invalid('No measurements')
        evidence(work, results.get('evidence'))
    elif stage == 'report':
        safe_file(work, 'artifacts/report.md')
        memory = object_json(safe_file(work, 'artifacts/memory.json'))
        if any(not isinstance(memory.get(k), list) for k in ('facts', 'failures', 'unknowns', 'next')):
            raise Invalid('Incomplete memory card')


def parse_output(backend, text, code):
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            pass
    if backend == 'claude':
        end = next((e for e in reversed(events) if e.get('type') == 'result'), {})
        ok = code == 0 and end.get('subtype') == 'success' and not end.get('is_error')
        if ok:
            error = None
        elif not end:
            error = 'missing_result'
        elif end.get('is_error'):
            parts = [str(end.get('api_error') or ''),
                     f"HTTP {end['api_error_status']}" if end.get('api_error_status') else '',
                     str(end.get('result') or '')]
            error = ' / '.join(part for part in parts if part)[:300]
        elif code:
            error = f'nonzero_exit_{code}'
        else:
            error = str(end.get('subtype') or 'missing_result')
        return {'ok': ok, 'session_id': end.get('session_id'),
                'cost_usd': end.get('total_cost_usd'), 'usage': end.get('modelUsage', end.get('usage')),
                'error': error}
    errors = [e for e in events if e.get('type') == 'error']
    final = next((e for e in reversed(events) if e.get('type') in ('text', 'step_finish')), {})
    ok = code == 0 and not errors and (final.get('type') == 'text' or
          final.get('part', {}).get('reason') == 'stop')
    return {'ok': ok, 'session_id': final.get('sessionID'),
            'cost_usd': sum(e.get('part', {}).get('cost', 0) for e in events if e.get('type') == 'step_finish'),
            'error': None if ok else 'missing_completion_or_provider_error'}


def sandbox(argv, work, campaign):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise Blocked('bubblewrap is required; run the Nix-provided research-factory')
    (work / '.home').mkdir(exist_ok=True)
    (work / '.cache').mkdir(exist_ok=True)
    mounts = []
    for name in os.environ.get('RESEARCH_WRITABLE_PATHS', '').split(':'):
        if name:
            path = Path(name).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            if path == Path('/') or path == Path.home() or campaign.is_relative_to(path) or path.is_relative_to(campaign):
                raise Invalid('Writable runtime path must not contain campaign or entire home')
            mounts += ['--bind', str(path), str(path)]
    readonly = []
    for name in ('brief.md', 'handoff.json', 'case', 'candidate', 'prior-evidence', 'protocol-frozen.json', 'pilot-frozen.json', 'workflow-contract.json', 'approved-inputs.json'):
        path = work / name
        if path.exists():
            readonly += ['--ro-bind', str(path), str(path)]
    if (work / 'protocol-frozen.json').exists():
        path = work / 'artifacts/protocol.json'
        readonly += ['--ro-bind', str(path), str(path)]
    if (work / 'pilot-frozen.json').exists():
        path = work / 'artifacts/pilot_plan.json'
        readonly += ['--ro-bind', str(path), str(path)]
    if (work / 'approved-inputs.json').exists():
        for relative in load(work / 'approved-inputs.json'):
            path = safe_file(work, relative)
            readonly += ['--ro-bind', str(path), str(path)]
    return [bwrap, '--die-with-parent', '--new-session', '--unshare-pid',
            '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
            '--tmpfs', '/tmp', '--tmpfs', str(campaign),
            *mounts, '--bind', str(work), str(work), *readonly, '--chdir', str(work),
            '--setenv', 'TMPDIR', '/tmp', '--setenv', 'TMP', '/tmp',
            '--setenv', 'HOME', str(work / '.home'),
            '--setenv', 'XDG_CACHE_HOME', str(work / '.cache'),
            '--', *argv]


def process(argv, work, log, timeout, maximum):
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    secrets = [v for k, v in os.environ.items() if len(v) >= 6 and any(x in k for x in ('TOKEN', 'API_KEY', 'PASSWORD'))]
    overflow = threading.Event()
    with log.open('w') as out:
        child = subprocess.Popen(argv, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True)

        def collect():
            size = 0
            for line in child.stdout:
                for secret in secrets:
                    line = line.replace(secret, '[REDACTED]')
                size += len(line.encode())
                if size > maximum:
                    overflow.set()
                    os.killpg(child.pid, signal.SIGTERM)
                    break
                out.write(line)
                out.flush()

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            code = child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise Blocked('Interrupted or timed out; checkpoint retained')
        finally:
            reader.join(timeout=5)
            if reader.is_alive():
                os.killpg(child.pid, signal.SIGKILL)
                reader.join(timeout=5)
            child.stdout.close()
    return {'exit_code': code, 'seconds': round(time.monotonic() - started, 3),
            'ok': code == 0 and not overflow.is_set()}


class Runtime:
    def __init__(self, root, cfg):
        self.root, self.cfg = root, cfg

    def invoke(self, role, stage, work, prompt, log):
        if self.cfg.get('pipeline_version', 1) == 2 and stage in ('report', 'review', 'confirm'):
            prompt += ('\nОграничение короткого цикла имеет приоритет: новые экспериментальные API-вызовы '
                       'на этой стадии запрещены. Проверяй сохранённые независимые измерения и локальные '
                       'тесты без API. Если подтверждающих данных недостаточно, не принимай гипотезу; '
                       'зафиксируй неполноту вместо запуска дополнительных экспериментов.')
        spec = self.cfg[role]
        if spec['backend'] == 'opencode':
            argv = ['research-opencode', 'run', '--agent', role, '--format', 'json']
            if not spec['model']:
                raise Blocked('Specify researcher/critic model ID in campaign.json')
            argv += ['--model', spec['model'], prompt]
        else:
            argv = ['research-claude', '-p', '--output-format', 'json', '--model', spec['model']]
            if role == 'critic':
                argv += ['--agent', 'critic']
            argv += ['--', prompt]
        if self.cfg.get('pipeline_version', 1) == 2:
            if stage in ('pilot', 'experiment'):
                frozen = load(work / ('pilot-frozen.json' if stage == 'pilot' else 'protocol-frozen.json'))
                limits = frozen['budget' if stage == 'pilot' else 'request_budget']
                argv = ['env', f'RESEARCH_BUDGET_LEDGER={work / "artifacts" / ("budget-" + stage + ".sqlite")}',
                        f'RESEARCH_BUDGET_LIMITS={json.dumps(limits)}', *argv]
            argv = ['env', f'RESEARCH_STAGE_DEADLINE={time.time() + self.cfg["timeout_seconds"] - 5}', *argv]
        result = process(sandbox(argv, work, self.root), work, log,
                         self.cfg['timeout_seconds'], self.cfg['max_log_bytes'])
        parsed = parse_output(spec['backend'], log.read_text(), result['exit_code'])
        parsed['ok'] = parsed['ok'] and result['ok']
        return result | parsed

    def verify(self, work, argv, log):
        return process(sandbox(argv, work, self.root), work, log,
                       self.cfg['verify_timeout_seconds'], self.cfg['max_log_bytes'])


STAGE_TASKS = {
    'research': 'Проверь первоисточники, выдели ближайшие работы и несколько гипотез; выбери наиболее перспективную. Создай artifacts/hypotheses.md и artifacts/sources.json (список объектов url, claim, status). Не выдавай отсутствие найденной работы за доказанную новизну.',
    'plan': 'Создай artifacts/protocol.json с полями hypothesis, baselines, metrics, falsification, split_policy, budget. Зафиксируй единицу анализа, правильные исходы, контрольные условия, критерии полезного преимущества и подтверждающий эксперимент. После этой стадии протокол заморожен.',
    'build': 'Реализуй MVP с нуля, установи и закрепи зависимости. Создай artifacts/submission.json с verify_command: массив аргументов команды, которая из чистой копии устанавливает зависимости и выполняет содержательные проверки. Кеши и виртуальные окружения не архивируются.',
    'experiment': 'Выполни эксперименты по замороженному протоколу. Создай artifacts/results.json с непустыми measurements и evidence (относительные пути к исходным данным). Сохраняй все попытки, затраты и отрицательные результаты. Для повторов используй уникальные ID, не смешивай повтор после сбоя с независимым наблюдением.',
    'report': 'Создай artifacts/report.md и artifacts/memory.json: списки facts, failures, unknowns, next. В фактах укажи условия и ссылки на измерения. Отдели полезность, новизну и готовность к статье. Предложи лучшее направление при провале гипотезы. Не копируй исходники в память.',
}


def prompt_for(stage, version=1):
    common = ('Работай только в текущем каталоге. Прочитай brief.md и handoff.json. '
              'Не запрашивай подтверждений. Не меняй brief.md, handoff.json и замороженный протокол. '
              'Сохраняй progress.md после каждого блока. Артефакты стадии обязательны. ')
    if version == 2 and stage in ('research', 'idea_review', 'pilot_plan', 'pilot', 'pilot_review'):
        return common + 'Прочитай workflow-contract.json (пути относительно artifacts/). ' + workflow_v2.TASKS[stage]
    if version == 2 and stage in workflow_v2.TASKS:
        return prompt_for(stage, 1) + '\nПравила v2: ' + workflow_v2.TASKS[stage] + ' Прочитай workflow-contract.json.'
    if stage in STAGE_TASKS:
        return common + STAGE_TASKS[stage]
    return common + (
        'Ты независимый критик. MVP находится в candidate/. verification.json содержит результат '
        'внешней команды проверки. Сначала оцени протокол и сырые данные, затем отчёт автора. '
        'Сам повтори ключевые проверки, сохрани новые логи. Не исправляй candidate/. '
        + ('Это окончательная независимая проверка выбранного кандидата. Создай новые случаи, '
           'не использовавшиеся для разработки, проверь перенос вывода и повтори измерения. '
           'Отличай независимую оценку от синтетического oracle той же модели. ' if stage == 'confirm' else '')
        + 'Создай artifacts/review.json: verdict accept/revise/pivot/blocked, score 0..4 '
        '(0 нет результата, 1 слабый, 2 перспективный, 3 полезный подтверждённый, 4 устойчивый сильный), '
        'gates — объект с булевыми ' + ', '.join(GATES) + '. '
        'issues — список объектов severity, evidence, fix; severity blocking для критичных проблем. '
        'evidence — непустой список путей к твоим логам относительно текущего каталога; '
        'summary — обоснование, next — рекомендации следующему циклу, publication_ready — bool. '
        'accept разрешён только при всех gates=true и отсутствии blocking. '
        'Отсутствие доказательств не заменяй уверенностью. Даже для отказа сохрани диагностический лог.'
    )


def handoff(root, state):
    return {'cycle': state['cycle'], 'previous': [
        {'cycle': c['cycle'], 'memory': c['memory'], 'review': c['review'],
         'details': f"prior-evidence/{c['cycle']:02d}"}
        for c in state['candidates']],
        'extensions': state.get('extensions', []),
        'scenario_history': [
            {'cycle': c['cycle'], 'scope': c.get('scenario_scope', 'legacy_unspecified'),
             'profiles': c.get('scenario_profiles', {}), 'summary': c['review']['summary'],
             'details': f"prior-evidence/{c['cycle']:02d}"}
            for c in state['candidates']],
        'lessons': [{'id': f"c{c['cycle']}-issue{n}", 'cycle': c['cycle'],
                     'problem': issue.get('evidence'), 'required_check': issue.get('fix'),
                     'scope': 'Assess applicability to this new hypothesis; justify exclusions with evidence'}
                    for c in state['candidates'] for n, issue in enumerate(c['review'].get('issues', []))
                    if issue.get('severity') == 'blocking'],
        'model': {'name': os.environ.get('RESEARCH_LLM_MODEL', 'not configured'),
                  'api': 'OpenAI-compatible chat completions',
                  'base_url': os.environ.get('RESEARCH_LLM_BASE_URL'),
                  'routing': f'For the supplied loopback proxy: {llm_proxy.PROVIDER} only, no fallback. Record actual response model/provider, usage and errors; local quantization is a separate experimental condition. Recheck reasoning, tools and caching on this route; previous-provider findings do not establish compatibility.',
                  'declared_context': load(root / 'campaign.json').get('experiment', {}).get('context'),
                  'environment': ['RESEARCH_LLM_BASE_URL', 'RESEARCH_LLM_MODEL', 'RESEARCH_LLM_API_KEY']}}


def prepare_memory(root, work, state):
    for candidate in state['candidates']:
        source = root / 'cycles' / f"{candidate['cycle']:02d}" / 'snapshot'
        target = work / 'prior-evidence' / f"{candidate['cycle']:02d}"
        if target.exists():
            continue
        temporary = target.with_name(target.name + '.partial')
        if temporary.exists():
            shutil.rmtree(temporary)
        if candidate.get('kind') == 'screened_out':
            paths = [p for p in candidate['manifest'] if p.startswith('artifacts/')
                     and Path(p).suffix in ('.json', '.jsonl', '.csv', '.tsv', '.txt', '.md')]
        else:
            paths = ['artifacts/' + name for name in ('protocol.json', 'sources.json', 'hypotheses.md',
                                                      'results.json', 'report.md', 'memory.json')]
            paths += [p for p in load(source / 'artifacts/results.json')['evidence']
                      if Path(p).suffix in ('.json', '.jsonl', '.csv', '.tsv', '.txt', '.md')]
            paths += [p for p in ('artifacts/candidates.json', 'artifacts/idea_review.json',
                                 'artifacts/pilot_plan.json', 'artifacts/pilot_results.json', 'artifacts/pilot_review.json')
                      if (source / p).is_file()]
        for relative in set(paths):
            original = safe_file(source, relative)
            dest = temporary / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, dest)
        if candidate.get('screening_workspace'):
            reviewer = root / candidate['screening_workspace']
            for relative in candidate['screening']['evidence']:
                original = safe_file(reviewer, relative)
                dest = temporary / 'screening-evidence' / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, dest)
        save(temporary / 'review.json', candidate['review'])
        os.replace(temporary, target)


def extend(root, cycles, swap_agents=False, pipeline_version=None):
    """Reopen a finished campaign, keeping old cycles and archiving its final decision."""
    root = Path(root).resolve()
    with lock(root):
        state, cfg = load(root / 'state.json'), load(root / 'campaign.json')
        check_config(cfg)
        if state['status'] not in ('completed', 'exhausted'):
            raise Invalid('Only a finished campaign can be extended')
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= cfg['cycles']:
            raise Invalid('New total cycles must exceed the previous budget')
        last = max((c['cycle'] for c in state['candidates']), default=0)
        if last != cfg['cycles'] or (root / 'cycles' / f'{last + 1:02d}').exists():
            raise Invalid('Completed cycle history does not match budget, or next cycle already exists')
        updated_cfg = cfg | {'cycles': cycles}
        if pipeline_version is not None:
            if pipeline_version < cfg.get('pipeline_version', 1):
                raise Invalid('Downgrade requires explicit restoration of an archived campaign')
            updated_cfg['pipeline_version'] = pipeline_version
        if swap_agents:
            updated_cfg.update(researcher=cfg['critic'], critic=cfg['researcher'])
        check_config(updated_cfg)
        archive = root / 'history' / f'after-{last:02d}-{time.time_ns()}'
        archive.mkdir(parents=True)
        files = ('state.json', 'campaign.json', 'FINAL.md', 'selection.json', 'materials.json', 'doctor.json')
        for name in files:
            if (root / name).is_file():
                shutil.copy2(root / name, archive / name)
        change = {'time': time.time(), 'after_cycle': last, 'total_cycles': cycles,
                  'archive': str(archive.relative_to(root)),
                  'researcher': updated_cfg['researcher'], 'critic': updated_cfg['critic']}
        save(archive / 'extension.json', change)
        updated_state = state | {'version': updated_cfg.get('pipeline_version', 1),
                                 'status': 'ready', 'cycle': last + 1, 'stage': 'research',
                                 'winner': None, 'error': None, 'last_failure': None, 'confirm_index': 0,
                                 'extensions': [*state.get('extensions', []), change]}
        moved_confirmation = False
        try:
            if (root / 'confirmations').exists():
                os.replace(root / 'confirmations', archive / 'confirmations')
                moved_confirmation = True
            save(root / 'campaign.json', updated_cfg)
            for name in ('FINAL.md', 'selection.json', 'materials.json', 'doctor.json'):
                (root / name).unlink(missing_ok=True)
            save(root / 'state.json', updated_state)
        except OSError:
            for name in files:
                if (archive / name).exists():
                    shutil.copy2(archive / name, root / name)
            if moved_confirmation:
                os.replace(archive / 'confirmations', root / 'confirmations')
            raise
        return archive


def run(root, invoke, verify, retry=False, preflight=None):
    root = Path(root).resolve()
    if load(root / 'campaign.json').get('mode') == 'deep_case':
        import deep_case
        return deep_case.run(root, invoke, verify, retry, preflight)
    with lock(root):
        state, cfg = load(root / 'state.json'), load(root / 'campaign.json')
        check_config(cfg)
        version = cfg.get('pipeline_version', 1)
        if state.get('version', 1) != version:
            raise Invalid('Workflow changes require explicit extend --pipeline-version')
        stages = workflow_v2.STAGES if version == 2 else STAGES
        if state['status'] in ('completed', 'exhausted'):
            return
        if preflight is not None and not preflight()['ok']:
            state.update(status='blocked', error='Live preflight failed; see doctor.json')
            save(root / 'state.json', state)
            raise Blocked(state['error'])
        if retry:
            prefix = f'{state["cycle"]}:{state["stage"]}:{state["confirm_index"]}'
            previous = {k: v for k, v in state['attempts'].items() if k == prefix or k.startswith(prefix + ':')}
            state.setdefault('attempt_resets', []).append({'time': time.time(), 'stage': state['stage'], 'previous': previous})
            for key in previous:
                state['attempts'][key] = 0
            save(root / 'state.json', state)
        while True:
            cycle, stage = state['cycle'], state['stage']
            base = root / 'cycles' / f'{cycle:02d}'
            base.mkdir(parents=True, exist_ok=True)
            work = base / 'work'
            meta = base / 'cycle.json'
            if meta.exists() and load(meta)['pipeline_version'] != version and stage != 'confirm':
                raise Invalid('Cannot switch workflow inside a cycle')
            if not meta.exists():
                save(meta, {'pipeline_version': version, 'controller_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                            'researcher': cfg['researcher'], 'critic': cfg['critic'], 'created': time.time()})
            rounds = state.setdefault('gate_rounds', {}) if version == 2 else {}
            round_no = rounds.get(f'{cycle}:{stage}', 0)
            if version == 2 and stage in workflow_v2.EARLY:
                input_work = work
                work = base / 'early' / f'{stage}-{round_no:02d}'
                work.mkdir(parents=True, exist_ok=True)
                if (work / 'input_manifest.json').exists():
                    manifest = load(work / 'input_manifest.json')
                    check_snapshot(work / 'candidate', manifest, cfg['max_snapshot_bytes'])
                else:
                    manifest = snapshot(input_work, work / 'candidate', cfg['max_snapshot_bytes'])
                    save(work / 'input_manifest.json', manifest)
            elif stage == 'confirm':
                ranked = sorted((c for c in state['candidates'] if c['eligible']), key=lambda c: (-c['review']['score'], c['cycle']))
                if state['confirm_index'] >= len(ranked):
                    finish(root, state, None)
                    return
                candidate = ranked[state['confirm_index']]
                source = root / 'cycles' / f"{candidate['cycle']:02d}"
                work = root / 'confirmations' / f"{candidate['cycle']:02d}"
                work.mkdir(parents=True, exist_ok=True)
                check_snapshot(source / 'snapshot', candidate['manifest'], cfg['max_snapshot_bytes'])
                if not (work / 'candidate').exists():
                    shutil.copytree(source / 'snapshot', work / 'candidate')
                shutil.copyfile(source / 'verification.json', work / 'verification.json')
                base = work
            elif stage == 'review':
                work = base / 'review'
                work.mkdir(exist_ok=True)
                manifest = load(base / 'manifest.json')
                check_snapshot(base / 'snapshot', manifest, cfg['max_snapshot_bytes'])
                if not (work / 'candidate').exists():
                    shutil.copytree(base / 'snapshot', work / 'candidate')
                shutil.copyfile(base / 'verification.json', work / 'verification.json')
            else:
                work.mkdir(exist_ok=True)
            if not (work / 'brief.md').exists():
                shutil.copyfile(root / 'brief.md', work / 'brief.md')
            if not (work / 'handoff.json').exists():
                prepare_memory(root, work, state)
                save(work / 'handoff.json', handoff(root, state))
            if version == 2 and not (work / 'workflow-contract.json').exists():
                save(work / 'workflow-contract.json', workflow_v2.CONTRACT)
            revision = sum(v for k, v in rounds.items() if k.startswith(f'{cycle}:'))
            key = f'{cycle}:{stage}:{state["confirm_index"]}' + (f':r{revision}' if version == 2 else '')
            attempt = state['attempts'].get(key, 0) + 1
            if attempt > cfg['attempts_per_stage']:
                state.update(status='blocked', error='Attempt budget exhausted; resume with --retry after resolving cause')
                save(root / 'state.json', state)
                raise Blocked(state['error'])
            state['attempts'][key] = attempt
            state.update(status='running', error=None)
            save(root / 'state.json', state)
            log = base / 'logs' / f'{stage}-{attempt}-{time.time_ns()}.jsonl'
            task = prompt_for(stage, version) if stage != 'verify' else 'external verification'
            if state.get('last_failure'):
                task += '\nПредыдущая попытка: ' + state['last_failure']
            save(log.with_suffix('.request.json'), {'stage': stage, 'attempt': attempt, 'prompt': task,
                                                   'pipeline_version': version,
                                                   'executor': cfg['critic' if stage in ('review', 'confirm', *workflow_v2.EARLY) else 'researcher']})
            try:
                if version == 2 and stage not in (*workflow_v2.EARLY, 'review', 'confirm'):
                    approved = check_approvals(base, work)
                    save(work / 'approved-inputs.json', approved)
                if stage == 'verify':
                    manifest = snapshot(work, base / 'snapshot', cfg['max_snapshot_bytes'])
                    save(base / 'manifest.json', manifest)
                    verify_work = base / f'verification-{attempt}-{time.time_ns()}'
                    shutil.copytree(base / 'snapshot', verify_work)
                    result = verify(verify_work, load(work / 'artifacts/submission.json')['verify_command'], log)
                    save(base / 'verification.json', result)
                    # Failed verification is evidence for the critic, not an infrastructure retry.
                else:
                    result = invoke('critic' if stage in ('review', 'confirm', *workflow_v2.EARLY) else 'researcher', stage, work, task, log)
                    if not result.get('ok'):
                        raise Invalid(result.get('error') or 'Worker failed')
                    if version == 2 and stage in workflow_v2.EARLY:
                        review = workflow_v2.screening(stage, work)
                        check_snapshot(work / 'candidate', manifest, cfg['max_snapshot_bytes'])
                    elif stage in ('review', 'confirm'):
                        review = validate_review(work, safe_file(work, 'artifacts/review.json'))
                        if version == 2:
                            workflow_v2.assessment(work, review)
                    else:
                        if version == 2:
                            check_approvals(base, work)
                        validate_stage(stage, work)
                        if version == 2:
                            workflow_v2.validate(stage, work, cfg)
                        if stage == 'pilot_plan':
                            save(work / 'pilot-frozen.json', load(work / 'artifacts/pilot_plan.json'))
                        elif stage == 'pilot' and load(work / 'pilot-frozen.json') != load(work / 'artifacts/pilot_plan.json'):
                            raise Invalid('Frozen pilot plan changed')
                        if stage == 'plan':
                            save(base / 'protocol.json', load(work / 'artifacts/protocol.json'))
                            save(work / 'protocol-frozen.json', load(work / 'artifacts/protocol.json'))
                        elif stage in ('build', 'experiment', 'report'):
                            if load(base / 'protocol.json') != load(work / 'artifacts/protocol.json'):
                                raise Invalid('Frozen protocol changed; restore it and report deviations separately')
                save(log.with_suffix('.receipt.json'), result)
                state.pop('last_failure', None)
            except (Invalid, ValueError, OSError) as exc:
                state.update(status='retrying', last_failure=str(exc))
                save(log.with_suffix('.receipt.json'), {'ok': False, 'error': str(exc)})
                save(root / 'state.json', state)
                continue
            except (Blocked, KeyboardInterrupt) as exc:
                state.update(status='blocked', error=str(exc))
                save(root / 'state.json', state)
                raise Blocked(str(exc)) from exc
            if version == 2 and stage in workflow_v2.EARLY:
                save(base / 'work/artifacts' / f'{stage}.json', review)
                if review['verdict'] == 'blocked':
                    state.update(status='blocked', error=review['summary'])
                    save(root / 'state.json', state)
                    raise Blocked(review['summary'])
                if review['verdict'] == 'revise' and round_no < cfg.get('gate_revisions', 1):
                    rounds[f'{cycle}:{stage}'] = round_no + 1
                    state['stage'] = 'research' if stage == 'idea_review' else 'pilot'
                elif review['verdict'] in ('reject', 'revise'):
                    close_screened_cycle(root, base, state, cfg, stage, review, work)
                else:
                    approve_artifacts(base, base / 'work', work, stage)
                    state['stage'] = stages[stages.index(stage) + 1]
            elif stage == 'confirm':
                check_snapshot(source / 'snapshot', candidate['manifest'], cfg['max_snapshot_bytes'])
                if review['verdict'] == 'accept':
                    finish(root, state, candidate, review)
                    return
                state['confirm_index'] += 1
            elif stage == 'review':
                check_snapshot(base / 'snapshot', manifest, cfg['max_snapshot_bytes'])
                state['candidates'].append({'cycle': cycle, 'review': review,
                    'memory': load(base / 'snapshot/artifacts/memory.json'), 'manifest': manifest,
                    'eligible': review['verdict'] == 'accept' and load(base / 'verification.json')['ok']})
                if version == 2:
                    protocol = load(base / 'snapshot/artifacts/protocol.json')
                    state['candidates'][-1].update(scenario_scope='evaluated',
                        scenario_profiles={protocol['selected_id']: protocol['scenario_profile']})
                if cycle < cfg['cycles']:
                    state.update(cycle=cycle + 1, stage='research')
                else:
                    state['stage'] = 'confirm'
            else:
                state['stage'] = stages[stages.index(stage) + 1]
            save(root / 'state.json', state)


def close_screened_cycle(root, base, state, cfg, stage, screening, reviewer):
    work = base / 'work'
    memory = {'facts': [{'claim': 'Early screening only; no efficacy confirmation', 'stage': stage}],
              'failures': [screening['summary']], 'unknowns': ['Main hypothesis not evaluated'], 'next': screening['next']}
    save(base / 'screened_memory.json', memory)
    digest = snapshot(work, base / 'snapshot', cfg['max_snapshot_bytes'])
    save(base / 'manifest.json', digest)
    review = {'verdict': 'pivot', 'score': 0, 'summary': screening['summary'], 'next': screening['next'],
              'issues': screening['issues'], 'gates': {k: False for k in GATES}, 'publication_ready': False,
              'screening_stage': stage}
    state['candidates'].append({'cycle': state['cycle'], 'kind': 'screened_out', 'eligible': False,
                                'review': review, 'screening': screening,
                                'screening_workspace': str(reviewer.relative_to(root)),
                                'memory': memory, 'manifest': digest})
    selected = (load(work / 'pilot-frozen.json')['selected_ids'] if stage == 'pilot_review'
                else workflow_v2.candidate_ids(work))
    state['candidates'][-1].update(scenario_scope='piloted' if stage == 'pilot_review' else 'proposed',
                                   scenario_profiles=workflow_v2.scenario_profiles(work, selected))
    if state['cycle'] < cfg['cycles']:
        state.update(cycle=state['cycle'] + 1, stage='research')
    else:
        state['stage'] = 'confirm'


def finish(root, state, candidate, confirmation=None):
    state.update(status='completed' if candidate else 'exhausted', winner=candidate['cycle'] if candidate else None)
    save(root / 'selection.json', {'winner': state['winner'], 'confirmation': confirmation,
                                  'candidates': [{k: v for k, v in c.items() if k != 'manifest'} for c in state['candidates']]})
    save(root / 'materials.json', {'winner': state['winner'], 'cycles': [
        {'cycle': c['cycle'], 'kind': c.get('kind', 'full'), 'verdict': c['review']['verdict'],
         'assessment': c['review'].get('assessment'), 'summary': c['review']['summary'],
         'next': c['review']['next'], 'snapshot': f"cycles/{c['cycle']:02d}/snapshot"}
        for c in state['candidates']], 'note': 'Unsupported and negative findings are materials, not accepted winners.'})
    text = ('# Итог кампании\n\n' + (f"Выбран цикл {candidate['cycle']:02d}; артефакты: cycles/{candidate['cycle']:02d}/snapshot.\n"
            if candidate else 'Бюджет исчерпан: ни один кандидат не прошёл независимую приёмку.\n')
            + '\nВсе результаты и рекомендации: selection.json.\n'
            + 'Оценка критика не заменяет человеческую экспертизу новизны; автоматической публикации нет.\n')
    (root / 'FINAL.md').write_text(text)
    save(root / 'state.json', state)


def doctor(root, cfg, live=False):
    checks = {}
    for executable in ('research-opencode', 'research-claude', 'bwrap', 'python3', 'git', 'curl', 'uv', 'bun'):
        checks[executable] = bool(shutil.which(executable))
        work = root / 'doctor'
    work.mkdir(parents=True, exist_ok=True)
    if checks['bwrap']:
        test = subprocess.run(sandbox(['sh', '-c', 'test ! -e ../state.json && printf OK > sandbox.txt'], work, root),
                              capture_output=True, timeout=20, check=False)
        checks['sandbox'] = test.returncode == 0 and (work / 'sandbox.txt').read_text() == 'OK'
    if live:
        checks.update(mcp_probe.checks())
        runtime = Runtime(root, cfg | {'timeout_seconds': min(cfg['timeout_seconds'], 180),
                                       'verify_timeout_seconds': 120})
        search_log = work / 'websearch.json'
        try:
            result = runtime.verify(work, ['research-opencode', 'api', 'post', '/api/websearch',
                '--data', json.dumps({'query': 'AgentDojo prompt injection benchmark', 'providerID': 'tinyfish'})], search_log)
            checks['websearch'] = result['ok'] and bool(load(search_log).get('data', {}).get('results'))
        except (Blocked, Invalid, OSError, ValueError, subprocess.SubprocessError) as exc:
            checks['websearch'] = False
            checks['websearch_error'] = type(exc).__name__
        url, model = os.environ.get('RESEARCH_LLM_BASE_URL'), os.environ.get('RESEARCH_LLM_MODEL')
        if not url or not model:
            checks['local_llm'] = False
        elif url == f'http://127.0.0.1:{llm_proxy.PORT}/v1' and model == llm_proxy.MODEL:
            probe = llm_proxy.check_api(url)
            checks.update({'experiment_' + k: v for k, v in probe['checks'].items()})
            checks.update({'experiment_' + k + '_error': v for k, v in probe['errors'].items()})
        else:
            body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}], 'max_tokens': 32}).encode()
            headers = {'Content-Type': 'application/json'}
            if os.environ.get('RESEARCH_LLM_API_KEY'):
                headers['Authorization'] = 'Bearer ' + os.environ['RESEARCH_LLM_API_KEY']
            try:
                request = urllib.request.Request(url.rstrip('/') + '/chat/completions', data=body, headers=headers)
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = json.load(response)
                checks['local_llm'] = isinstance(data, dict) and bool(data.get('choices'))
            except (OSError, ValueError):
                checks['local_llm'] = False
        for role in ('researcher', 'critic'):
            role_work = work / role
            role_work.mkdir(exist_ok=True)
            marker = role_work / 'probe.txt'
            marker.unlink(missing_ok=True)
            try:
                result = runtime.invoke(role, 'doctor', role_work,
                                        'Вызови shell и запиши ровно PROBE_OK в probe.txt. Затем заверши.', work / f'{role}.jsonl')
                checks[role + '_tool_call'] = result['ok'] and marker.exists() and marker.read_text().strip() == 'PROBE_OK'
            except (Blocked, Invalid, OSError, ValueError, subprocess.SubprocessError) as exc:
                checks[role + '_tool_call'] = False
                checks[role + '_error'] = type(exc).__name__
    report = {'time': time.time(), 'live': live, 'checks': checks,
              'ok': all(v is True for k, v in checks.items() if not k.endswith('_error'))}
    save(root / 'doctor.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Автономная исследовательская фабрика')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init')
    p.add_argument('directory', type=Path)
    p.add_argument('--brief', required=True, type=Path)
    p.add_argument('--config', type=Path)
    p = sub.add_parser('extend')
    p.add_argument('directory', type=Path)
    p.add_argument('--cycles', type=int, required=True, help='New total cycle budget')
    p.add_argument('--swap-agents', action='store_true')
    p.add_argument('--pipeline-version', type=int, choices=(1, 2))
    for name in ('run', 'status', 'doctor'):
        p = sub.add_parser(name)
        p.add_argument('directory', type=Path)
        if name == 'run':
            p.add_argument('--retry', action='store_true')
        if name == 'doctor':
            p.add_argument('--live', action='store_true')
    args = parser.parse_args()
    root = args.directory.resolve()
    try:
        if args.command == 'init':
            initialize(root, args.brief, load(args.config) if args.config else default_config())
        elif args.command == 'extend':
            archive = extend(root, args.cycles, args.swap_agents, args.pipeline_version)
            print(json.dumps({'archive': str(archive), 'status': 'ready'}, ensure_ascii=False))
        elif args.command == 'status':
            state = load(root / 'state.json')
            print(json.dumps({k: state.get(k) for k in ('status', 'cycle', 'stage', 'winner', 'error', 'last_failure')}, ensure_ascii=False, indent=2))
        else:
            cfg = load(root / 'campaign.json')
            check_config(cfg)
            for key, field in (('RESEARCH_LLM_BASE_URL', 'base_url'), ('RESEARCH_LLM_MODEL', 'model')):
                if cfg.get('experiment', {}).get(field):
                    os.environ.setdefault(key, cfg['experiment'][field])
            if args.command == 'doctor':
                with lock(root):
                    report = doctor(root, cfg, args.live)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report['ok'] else 2
            runtime = Runtime(root, cfg)
            run(root, runtime.invoke, runtime.verify, args.retry,
                preflight=lambda: doctor(root, cfg, live=True))
    except (Blocked, Invalid, OSError, ValueError) as exc:
        print(f'BLOCKED: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.modules['factory'] = sys.modules[__name__]  # deep_case imports the controller by name; keep one Blocked/Invalid
    sys.exit(main())
