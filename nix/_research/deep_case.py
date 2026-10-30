"""Deep case mode: build a working zero-version from external case documents, repairing instead of screening."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import factory
from factory import Blocked, Invalid, load, save

REQUIREMENT = re.compile(r'\b(?:FR|NFR)-\d+(?:\.\d+)+\b')
SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
STATUSES = ('implemented', 'simplified', 'missing', 'broken', 'out_of_scope')
TAIL = 4000

CONTRACT = {
    'mode': 'deep_case: постановка из case/ принята, гейтов допуска нет, сбой стадии ведёт к ремонту',
    'decomposition.json': {
        'components': '1–20 objects {id: slug [a-z0-9_-], title, requirements: [FR/NFR IDs from case/], depends_on: [component ids], check_command: argv of an offline check run from a clean copy of the workspace}; depends_on has no cycles',
        'out_of_scope': 'list of {id, reason}: requirements not built in the zero-version; key_requirements are not allowed here',
        'assumptions': 'nonempty list of {topic, decision, rationale}: decisions where case/ gives no details',
    },
    'components/<id>.md': 'per component: what is implemented, simplifications, how to check',
    'simplifications.json': 'list of {component, what, why, upgrade}; every stub or hard-coded decision is listed',
    'submission.json': {'verify_command': 'argv: from a clean copy installs dependencies and runs the offline end-to-end check of key_requirements',
                        'run_command': 'optional argv: live run through RESEARCH_LLM_* with hard request limits'},
    'status.json': {'requirements': 'object: every ID from handoff.requirements -> {status: ' + '/'.join(STATUSES) + ', evidence: relative files (nonempty for implemented/simplified), note}',
                    'simplifications': 'list, same objects as simplifications.json'},
    'critique.json': {'summary': 'what is built and in what state', 'built': 'list of strings', 'weak': 'list of strings',
                      'fixes': 'list of {evidence, fix}, most important first',
                      'requirements': 'object: every ID from handoff.requirements -> {status: ' + '/'.join(STATUSES) + ', note}',
                      'evidence': 'nonempty list of your own check logs relative to the review workspace'},
}

COMMON = ('Работай только в текущем каталоге: это общий рабочий каталог полигона, его достраивают '
          'последовательные стадии. Прочитай brief.md, handoff.json, workflow-contract.json и целиком все '
          'документы в case/ — это ТЗ кейса. Постановка принята, её жизнеспособность не обсуждается и '
          'вердикт о ней не выносится. Не запрашивай подтверждений. Не меняй brief.md, handoff.json, '
          'workflow-contract.json и case/. Сохраняй progress.md после каждого блока. Ремонт вместо отказа: '
          'если деталь не получается, упрости её и запиши упрощение в artifacts/simplifications.json, но '
          'оставь исполняемый код. Заглушка без такой записи недопустима. Проверки должны работать офлайн, '
          'без сети и модельных API; живая модель — только в run_command. ')

TASKS = {
    'decompose': 'Разбей кейс на компоненты zero-версии полигона и создай artifacts/decomposition.json по контракту. '
                 'Каждое требование из handoff.requirements отнеси к компоненту или в out_of_scope с причиной; '
                 'handoff.key_requirements — только в компоненты. В assumptions зафиксируй решения там, где доки '
                 'не задают деталей; числовые допущения задавай диапазонами, если доки этого требуют. Создай каркас '
                 'проекта: структуру каталогов, README с запуском, закреплённые зависимости. Код компонентов пока не пиши.',
    'build': 'Реализуй компонент handoff.component поверх уже построенного: его requirements и контракты из case/. '
             'Не ломай готовые компоненты — их check_command должны продолжать проходить. check_command компонента '
             'должен проверять содержательное поведение, а не всегда завершаться успехом. Опиши результат в '
             'artifacts/components/<id>.md.',
    'integrate': 'Собери компоненты в единый исполняемый полигон и создай artifacts/submission.json по контракту. '
                 'verify_command из чистой копии ставит зависимости и офлайн прогоняет сквозной сценарий, '
                 'покрывающий handoff.key_requirements; результаты прогона записываются в файлы. Если компонент '
                 'в handoff.components упал, доделай минимально необходимое здесь же.',
    'fix': 'Сквозная проверка контроллера не прошла: handoff.verification перечисляет упавшие проверки компонентов '
           'и verify_command с хвостами журналов. Исправь причины, начиная с самой ранней по зависимостям. '
           'Не ослабляй проверки ради зелёного статуса; осознанное сужение запиши в simplifications.json.',
    'report': 'Опиши для людей фактическое состояние полигона. artifacts/report.md: что построено, как запустить '
              '(verify_command, run_command), итог последней проверки контроллера (handoff.verification), упрощения '
              'и принятые допущения, известные поломки, следующие шаги. artifacts/status.json по контракту: статус '
              'каждого требования по коду и проверкам. Заглушку не выдавай за реализацию. Секретоподобные файлы и '
              'символические ссылки убери из рабочего каталога: они не попадут в снимок.',
    'critique': 'Ты независимый критик режима deep_case. Полигон — в candidate/ (только чтение), verification.json — '
                'последняя сквозная проверка контроллера, journal.jsonl — журнал попыток, отказов и ремонтов. '
                'Вердикта нет: это описательный отчёт для людей, а не gate. Скопируй candidate/ во временный каталог '
                'tmp/ и сам запусти проверки, логи сохрани в artifacts/. Сверь каждое требование из '
                'handoff.requirements с кодом и запуском, а не с отчётом автора. Отметь, где фабрика ремонтировала '
                'одно и то же по кругу. Создай artifacts/critique.json по контракту и artifacts/critique.md. '
                'Не исправляй candidate/.',
}

ACTIONS = {
    'repair': 'Ремонт: предыдущая попытка этого шага не прошла, причины и хвосты журналов — в handoff.failures. '
              'Найди корень проблемы и исправь его; без необходимости не начинай заново.',
    'replan': 'Последняя попытка шага: перепланируй. Сузь задачу шага до минимальной работающей версии, явная '
              'заглушка допустима. При необходимости исправь check_command своего компонента в '
              'artifacts/decomposition.json и запиши причину в artifacts/simplifications.json.',
}


def check_config(cfg):
    factory.check_config(cfg)
    if cfg.get('mode') != 'deep_case' or cfg.get('pipeline_version', 1) != 1:
        raise Invalid('deep_case needs mode=deep_case and pipeline_version 1')
    if type(cfg.get('fix_rounds', 3)) is not int or cfg.get('fix_rounds', 3) < 1:
        raise Invalid('fix_rounds: expected positive integer')
    case = cfg.get('case')
    if not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k] for k in ('repository', 'ref')):
        raise Invalid('case needs repository and ref strings')
    documents = case.get('documents')
    if not isinstance(documents, list) or not documents or any(
            not isinstance(d, str) or not d or Path(d).is_absolute() or '..' in Path(d).parts for d in documents):
        raise Invalid('case.documents: nonempty list of relative paths')
    if not isinstance(case.get('key_requirements', []), list) or any(not isinstance(r, str) for r in case.get('key_requirements', [])):
        raise Invalid('case.key_requirements must be a list of IDs')


def fetch_case(case, target):
    """Copy the case documents from a pinned checkout; the resolved commit is provenance."""
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / 'case'
        subprocess.run(['git', 'clone', '--quiet', '--depth', '1', '--branch', case['ref'], case['repository'], str(checkout)],
                       check=True, capture_output=True, timeout=300)
        commit = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True).stdout.strip()
        documents = {}
        for relative in case['documents']:
            source = factory.safe_file(checkout, relative)
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            documents[relative] = hashlib.sha256(dest.read_bytes()).hexdigest()
    text = '\n'.join((target / relative).read_text() for relative in case['documents'])
    ids = sorted(set(REQUIREMENT.findall(text)), key=lambda r: (r.split('-')[0], [int(n) for n in r.split('-')[1].split('.')]))
    unknown = set(case.get('key_requirements', [])) - set(ids)
    if unknown:
        raise Invalid('key_requirements absent from case documents: ' + ', '.join(sorted(unknown)))
    return {'repository': case['repository'], 'ref': case['ref'], 'commit': commit, 'documents': documents,
            'requirements': ids, 'key_requirements': case.get('key_requirements', []), 'fetched': time.time()}


def initialize(root, brief, config):
    check_config(config)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if list(root.iterdir()):
        raise Invalid('Campaign directory must be empty')
    try:
        info = fetch_case(config['case'], root / 'case')
        save(root / 'case' / 'case.json', info)
    except (Invalid, OSError, subprocess.SubprocessError):
        shutil.rmtree(root / 'case', ignore_errors=True)
        raise
    shutil.copyfile(brief, root / 'brief.md')
    save(root / 'campaign.json', config)
    save(root / 'state.json', {'mode': 'deep_case', 'version': 1, 'status': 'ready', 'cycle': 1, 'stage': 'decompose',
                               'steps': {}, 'components': [], 'winner': None})


def journal(root, **entry):
    with (root / 'journal.jsonl').open('a') as stream:
        stream.write(json.dumps({'time': time.time(), **entry}, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def sequence(state):
    return ['decompose', *(f"build:{c['id']}" for c in state['components']), 'integrate', 'fix', 'report', 'critique']


def argv(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise Invalid(f'{name} must be an argv list')
    return value


def strings(value, name):
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise Invalid(f'{name} must be a list of strings')
    return value


def ordered(components):
    order, done = [], set()
    pending = list(components)
    while pending:
        ready = [c for c in pending if set(c['depends_on']) <= done]
        if not ready:
            raise Invalid('Component dependencies contain a cycle')
        for c in ready:
            order.append(c)
            done.add(c['id'])
            pending.remove(c)
    return order


def validate_decomposition(work, case):
    data = factory.object_json(factory.safe_file(work, 'artifacts/decomposition.json'))
    components = data.get('components')
    if not isinstance(components, list) or not 1 <= len(components) <= 20:
        raise Invalid('decomposition needs 1–20 components')
    seen = set()
    for c in components:
        if not isinstance(c, dict) or not isinstance(c.get('id'), str) or not SLUG.fullmatch(c['id']) or c['id'] in seen:
            raise Invalid('Component ids must be unique slugs')
        if not isinstance(c.get('title'), str) or not c['title'].strip():
            raise Invalid(f"Component {c['id']} needs a title")
        strings(c.get('requirements'), f"{c['id']}.requirements")
        strings(c.get('depends_on'), f"{c['id']}.depends_on")
        argv(c.get('check_command'), f"{c['id']}.check_command")
        seen.add(c['id'])
    if any(set(c['depends_on']) - seen for c in components):
        raise Invalid('depends_on references an unknown component')
    out = data.get('out_of_scope', [])
    if not isinstance(out, list) or any(not isinstance(o, dict) or not o.get('id') or not o.get('reason') for o in out):
        raise Invalid('out_of_scope must be a list of {id, reason}')
    assumptions = data.get('assumptions')
    if not isinstance(assumptions, list) or not assumptions or any(
            not isinstance(a, dict) or not all(a.get(k) for k in ('topic', 'decision', 'rationale')) for a in assumptions):
        raise Invalid('assumptions must be a nonempty list of {topic, decision, rationale}')
    built = {r for c in components for r in c['requirements']}
    skipped = {o['id'] for o in out}
    missing = set(case['requirements']) - built - skipped
    if missing:
        raise Invalid('Requirements without component or out_of_scope: ' + ', '.join(sorted(missing)))
    unbuilt = set(case['key_requirements']) - built
    if unbuilt:
        raise Invalid('Key requirements must be built by components: ' + ', '.join(sorted(unbuilt)))
    return [{k: c[k] for k in ('id', 'title', 'requirements', 'depends_on', 'check_command')} for c in ordered(components)]


def validate_requirements(rows, case, name):
    if not isinstance(rows, dict) or set(case['requirements']) - set(rows):
        raise Invalid(f'{name} must cover every requirement ID')
    for key, row in rows.items():
        if not isinstance(row, dict) or row.get('status') not in STATUSES:
            raise Invalid(f'{name}.{key}: status must be one of ' + ', '.join(STATUSES))


def validate(step, work, case):
    if step == 'integrate':
        submission = factory.object_json(factory.safe_file(work, 'artifacts/submission.json'))
        argv(submission.get('verify_command'), 'verify_command')
        if 'run_command' in submission:
            argv(submission['run_command'], 'run_command')
    elif step == 'report':
        factory.safe_file(work, 'artifacts/report.md')
        status = factory.object_json(factory.safe_file(work, 'artifacts/status.json'))
        validate_requirements(status.get('requirements'), case, 'status.requirements')
        for row in status['requirements'].values():
            if row['status'] in ('implemented', 'simplified'):
                factory.evidence(work, row.get('evidence'))
        if not isinstance(status.get('simplifications'), list):
            raise Invalid('status.simplifications must be a list')
        factory.inventory(work)
    elif step == 'critique':
        factory.safe_file(work, 'artifacts/critique.md')
        critique = factory.object_json(factory.safe_file(work, 'artifacts/critique.json'))
        if not isinstance(critique.get('summary'), str) or not critique['summary'].strip():
            raise Invalid('critique.summary is required')
        for key in ('built', 'weak'):
            strings(critique.get(key), f'critique.{key}')
        fixes = critique.get('fixes')
        if not isinstance(fixes, list) or any(not isinstance(f, dict) or not f.get('evidence') or not f.get('fix') for f in fixes):
            raise Invalid('critique.fixes must be a list of {evidence, fix}')
        validate_requirements(critique.get('requirements'), case, 'critique.requirements')
        factory.evidence(work, critique.get('evidence'))


def tail(path):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - TAIL))
            return stream.read().decode(errors='replace')
    except OSError:
        return ''


def command_of(work, component):
    """The live decomposition wins: a replan may narrow its own check; the argv used is journaled."""
    try:
        for c in factory.object_json(work / 'artifacts/decomposition.json').get('components', []):
            if isinstance(c, dict) and c.get('id') == component['id']:
                return argv(c.get('check_command'), 'check_command')
    except (Invalid, OSError, ValueError):
        pass
    return component['check_command']


def check(base, work, name, command, verify, cfg):
    """Run one check in a fresh copy so it cannot rely on state left in the shared workspace."""
    slug = name.replace(':', '-')
    copy = base / 'checks' / f'{slug}-{time.time_ns()}'
    log = base / 'logs' / f'check-{slug}-{time.time_ns()}.log'
    started = time.monotonic()
    try:
        factory.snapshot(work, copy, cfg['max_snapshot_bytes'])
        result = verify(copy, command, log)
    except Blocked:
        if time.monotonic() - started < cfg['verify_timeout_seconds'] - 1:
            raise
        result = {'ok': False, 'exit_code': None, 'error': 'timeout'}
    except (Invalid, OSError, ValueError) as exc:
        result = {'ok': False, 'exit_code': None, 'error': str(exc)}
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    return {'name': name, 'argv': command, 'ok': bool(result.get('ok')), 'exit_code': result.get('exit_code'),
            'error': result.get('error'), 'log': str(log.relative_to(base)), 'tail': '' if result.get('ok') else tail(log)}


def suite(base, work, state, verify, cfg):
    checks = [check(base, work, f"build:{c['id']}", command_of(work, c), verify, cfg) for c in state['components']]
    try:
        command = argv(factory.object_json(work / 'artifacts/submission.json').get('verify_command'), 'verify_command')
        checks.append(check(base, work, 'verify_command', command, verify, cfg))
    except (Invalid, OSError, ValueError) as exc:
        checks.append({'name': 'verify_command', 'ok': False, 'error': f'artifacts/submission.json: {exc}'})
    result = {'time': time.time(), 'ok': all(c['ok'] for c in checks), 'checks': checks}
    save(base / 'verification.json', result)
    return result


def handoff(state, cfg, case, step, record, action):
    component = next((c for c in state['components'] if f"build:{c['id']}" == step), None)
    verification = state.get('verification')
    if verification:
        verification = {**verification, 'checks': [c for c in verification['checks'] if not c['ok']]}
    return {'mode': 'deep_case', 'step': step, 'attempt': record['attempts'], 'budget': budget(step, cfg),
            'action': action, 'component': component,
            'components': [{'id': c['id'], 'title': c['title'],
                            'status': state['steps'].get(f"build:{c['id']}", {}).get('status', 'pending')}
                           for c in state['components']],
            'requirements': case['requirements'], 'key_requirements': case['key_requirements'],
            'case': {k: case[k] for k in ('repository', 'ref', 'commit', 'documents')},
            'failures': record.get('failures', [])[-3:], 'verification': verification,
            'model': {'name': os.environ.get('RESEARCH_LLM_MODEL', 'not configured'),
                      'api': 'OpenAI-compatible chat completions',
                      'base_url': os.environ.get('RESEARCH_LLM_BASE_URL'),
                      'declared_context': cfg.get('experiment', {}).get('context'),
                      'environment': ['RESEARCH_LLM_BASE_URL', 'RESEARCH_LLM_MODEL', 'RESEARCH_LLM_API_KEY']}}


def budget(step, cfg):
    return cfg.get('fix_rounds', 3) if step == 'fix' else cfg['attempts_per_stage']


def prompt(step, action):
    task = TASKS[step.split(':')[0]]
    return COMMON + task + ('\n' + ACTIONS[action] if action in ACTIONS else '')


def prepare(root, base, work, cfg):
    work.mkdir(parents=True, exist_ok=True)
    if not (base / 'cycle.json').exists():
        save(base / 'cycle.json', {'mode': 'deep_case', 'pipeline_version': 1,
                                   'controller_sha256': hashlib.sha256(Path(factory.__file__).read_bytes()).hexdigest(),
                                   'deep_case_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                   'researcher': cfg['researcher'], 'critic': cfg['critic'], 'created': time.time()})
    if not (work / 'brief.md').exists():
        shutil.copyfile(root / 'brief.md', work / 'brief.md')
    if not (work / 'case').exists():
        shutil.copytree(root / 'case', work / 'case')
    save(work / 'workflow-contract.json', CONTRACT)


def prepare_review(root, base, cfg):
    review = base / 'review'
    if not (base / 'manifest.json').exists():
        save(base / 'manifest.json', factory.snapshot(base / 'work', base / 'snapshot', cfg['max_snapshot_bytes']))
    factory.check_snapshot(base / 'snapshot', load(base / 'manifest.json'), cfg['max_snapshot_bytes'])
    review.mkdir(exist_ok=True)
    if not (review / 'candidate').exists():
        shutil.copytree(base / 'snapshot', review / 'candidate')
    for name in ('brief.md', 'workflow-contract.json'):
        shutil.copyfile(base / 'work' / name, review / name)
    if not (review / 'case').exists():
        shutil.copytree(root / 'case', review / 'case')
    shutil.copyfile(base / 'verification.json', review / 'verification.json')
    shutil.copyfile(root / 'journal.jsonl', review / 'journal.jsonl')
    return review


def run(root, invoke, verify, retry=False, preflight=None):
    root = Path(root).resolve()
    with factory.lock(root):
        state, cfg = load(root / 'state.json'), load(root / 'campaign.json')
        check_config(cfg)
        case = load(root / 'case' / 'case.json')
        if state['status'] == 'completed':
            return
        if preflight is not None and not preflight()['ok']:
            state.update(status='blocked', error='Live preflight failed; see doctor.json')
            save(root / 'state.json', state)
            raise Blocked(state['error'])
        base = root / 'cycles' / '01'
        work = base / 'work'
        prepare(root, base, work, cfg)
        if retry and state['stage'] in state['steps']:
            record = state['steps'][state['stage']]
            journal(root, step=state['stage'], event='retry', previous={k: record.get(k) for k in ('attempts', 'infra_failures')})
            record.update(attempts=0, infra_failures=0)
            save(root / 'state.json', state)
        try:
            while state['stage'] != 'done':
                step(root, base, work, state, cfg, case, invoke, verify)
        except (Blocked, KeyboardInterrupt) as exc:
            state.update(status='blocked', error=str(exc) or 'Interrupted; checkpoint retained')
            journal(root, step=state['stage'], event='blocked', detail=state['error'])
            save(root / 'state.json', state)
            raise Blocked(state['error']) from exc
        finish(root, base, state)


def step(root, base, work, state, cfg, case, invoke, verify):
    """One attempt of the current step; failures are journaled and repaired, never fatal to the campaign."""
    name = state['stage']
    record = state['steps'].setdefault(name, {'status': 'pending', 'attempts': 0, 'infra_failures': 0, 'failures': []})
    if name == 'fix':
        state['verification'] = suite(base, work, state, verify, cfg)
        for c in state['verification']['checks']:
            build = state['steps'].get(c['name'])
            if c['ok'] and build and build['status'] == 'failed':
                build['status'] = 'repaired'
        journal(root, step=name, event='suite', ok=state['verification']['ok'],
                failed=[c['name'] for c in state['verification']['checks'] if not c['ok']])
        if state['verification']['ok']:
            record['status'] = 'done'
            return advance(root, state, name)
    if name == 'critique' and not (base / 'review' / 'candidate').exists():
        try:
            prepare_review(root, base, cfg)
        except (Invalid, OSError, ValueError) as exc:
            record.update(status='skipped', failures=[{'kind': 'snapshot', 'detail': str(exc)}])
            journal(root, step=name, event='skipped', detail=str(exc))
            return advance(root, state, name)
    if record['attempts'] >= budget(name, cfg):
        record['status'] = 'failed'
        journal(root, step=name, event='gave_up', attempts=record['attempts'])
        return advance(root, state, name)
    record['attempts'] += 1
    attempt = record['attempts']
    if name == 'fix':
        action = 'repair'
    elif attempt == 1:
        action = 'build'
    else:
        action = 'replan' if attempt == budget(name, cfg) and attempt > 2 else 'repair'
    role = 'critic' if name == 'critique' else 'researcher'
    place = base / 'review' if name == 'critique' else work
    if name == 'critique':
        shutil.copyfile(root / 'journal.jsonl', place / 'journal.jsonl')
    save(place / 'handoff.json', handoff(state, cfg, case, name, record, action))
    state.update(status='running', error=None)
    save(root / 'state.json', state)
    log = base / 'logs' / f"{name.replace(':', '-')}-{attempt}-{time.time_ns()}.jsonl"
    task = prompt(name, action)
    save(log.with_suffix('.request.json'), {'step': name, 'attempt': attempt, 'action': action,
                                            'prompt': task, 'executor': cfg[role]})
    journal(root, step=name, event='started', attempt=attempt, action=action, role=role, log=str(log.relative_to(root)))
    started = time.monotonic()
    try:
        result = invoke(role, name, place, task, log)
    except Blocked:
        if time.monotonic() - started < cfg['timeout_seconds'] - 1:
            record['attempts'] -= 1
            raise
        result = {'ok': False, 'error': 'timeout', 'timeout': True}
    save(log.with_suffix('.receipt.json'), result)
    if not result.get('ok') and not result.get('timeout'):
        # A provider/runtime failure says nothing about the polygon: it must not burn the repair budget.
        record['attempts'] -= 1
        record['infra_failures'] += 1
        journal(root, step=name, event='worker_failed', attempt=attempt, detail=result.get('error'))
        save(root / 'state.json', state)
        if record['infra_failures'] >= cfg['attempts_per_stage']:
            raise Blocked(f"{name}: worker failed {record['infra_failures']} times: {result.get('error')}")
        return
    failure = {'kind': 'timeout', 'detail': 'stage timeout; narrow the step'} if result.get('timeout') else None
    if not failure:
        try:
            if name == 'decompose':
                state['components'] = validate_decomposition(work, case)
            elif name != 'fix':
                validate(name, place, case)
        except (Invalid, ValueError, OSError) as exc:
            failure = {'kind': 'invalid', 'detail': str(exc)}
    if not failure and (name.startswith('build:') or name == 'integrate'):
        component = next((c for c in state['components'] if f"build:{c['id']}" == name), None)
        command = command_of(work, component) if component else load(work / 'artifacts/submission.json')['verify_command']
        result = check(base, work, name, command, verify, cfg)
        if not result['ok']:
            failure = {'kind': 'check_failed', 'detail': result.get('error') or f"exit {result['exit_code']}",
                       'argv': command, 'log': result['log'], 'tail': result['tail']}
    if failure:
        record['failures'] = [*record['failures'], {'attempt': attempt, **failure}][-5:]
        journal(root, step=name, event=failure['kind'], attempt=attempt, **{k: v for k, v in failure.items() if k != 'kind'})
        state['last_failure'] = f"{name}: {failure['detail']}"
        save(root / 'state.json', state)
        return
    if name == 'fix':
        journal(root, step=name, event='repaired', attempt=attempt)
        return save(root / 'state.json', state)
    record['status'] = 'done'
    state.pop('last_failure', None)
    journal(root, step=name, event='done', attempt=attempt)
    advance(root, state, name)


def advance(root, state, name):
    order = sequence(state)
    position = order.index(name)
    state['stage'] = order[position + 1] if position + 1 < len(order) else 'done'
    save(root / 'state.json', state)


def finish(root, base, state):
    rows = '\n'.join(f"| {name} | {state['steps'].get(name, {}).get('status', 'pending')} | "
                     f"{state['steps'].get(name, {}).get('attempts', 0)} |" for name in sequence(state))
    verification = state.get('verification', {})
    report = base / 'work/artifacts/report.md'
    critique = base / 'review/artifacts/critique.md'
    text = ('# Итог кампании deep_case\n\n'
            'Полигон zero-версии: ' + ('cycles/01/snapshot' if (base / 'snapshot').exists() else 'cycles/01/work (снимок не создан)') + '.\n'
            'Последняя сквозная проверка контроллера (cycles/01/verification.json): '
            + ('пройдена' if verification.get('ok') else 'не пройдена') + '.\n'
            + ('Отчёт о состоянии: cycles/01/work/artifacts/report.md, status.json.\n' if report.exists()
               else 'Отчёт автора не создан: состояние — по journal.jsonl и verification.json.\n')
            + ('Описательная критика: cycles/01/review/artifacts/critique.md.\n' if critique.exists()
               else 'Независимая критика не создана: см. journal.jsonl.\n')
            + 'Журнал попыток, отказов и ремонтов: journal.jsonl.\n\n'
            '| Шаг | Статус | Попытки |\n|---|---|---|\n' + rows + '\n\n'
            'Режим deep_case не выносит вердикт жизнеспособности: результат требует просмотра людьми.\n')
    (root / 'FINAL.md').write_text(text)
    state.update(status='completed', stage='done', error=None)
    journal(root, step='done', event='completed', verification=verification.get('ok'))
    save(root / 'state.json', state)
