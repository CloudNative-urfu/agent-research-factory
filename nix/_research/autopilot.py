"""Persistent improvement planning, independent review and revision handoff."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import factory
import deep_case
import spec_workflow
import preflight

SLUG = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]*')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_fields(value, keys):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip() for k in keys):
        raise factory.Invalid('Missing nonempty fields: ' + ', '.join(keys))


def validate_plan(work):
    factory.safe_file(work, 'artifacts/brainstorm.md')
    plan = factory.object_json(factory.safe_file(work, 'artifacts/plan.json'))
    text_fields(plan, ('problem', 'beneficiary', 'current_value', 'alternatives_reason'))
    factory.evidence(work, plan.get('evidence'))
    if type(plan.get('alternatives_necessary')) is not bool:
        raise factory.Invalid('alternatives_necessary must be boolean')
    deep_case.strings(plan.get('non_goals'), 'non_goals')
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 5:
        raise factory.Invalid('Plan needs 1–5 improvements to the current solution')
    ids = set()
    for task in tasks:
        text_fields(task, ('id', 'change', 'value', 'acceptance', 'effort'))
        if not SLUG.fullmatch(task['id']) or task['id'] in ids:
            raise factory.Invalid('Task IDs must be unique slugs')
        ids.add(task['id'])
        factory.evidence(work, task.get('evidence'))
    if (work / 'candidate/evaluation-method.md').exists():
        protocol = factory.object_json(factory.safe_file(work, 'artifacts/evaluation-protocol.json'))
        text_fields(protocol, ('process', 'trust_boundaries', 'baseline', 'attack_admission',
                               'independence', 'measurement'))
        if protocol.get('phase') not in ('foundation', 'evaluation'):
            raise factory.Invalid('Unknown evaluation phase')
        factory.evidence(work, protocol.get('evidence'))
        required = protocol.get('task_ids')
        deep_case.strings(required, 'task_ids')
        if not required or len(required) != len(set(required)) or set(required) - ids:
            raise factory.Invalid('Evaluation protocol needs known unique task IDs')
    if (work / 'candidate/development.json').exists():
        spec_workflow.validate_proposal(work, plan)
        factory.safe_file(work, 'artifacts/coevolution-progress.md')
    return plan


def validate_review(work, plan):
    factory.safe_file(work, 'artifacts/review.md')
    factory.safe_file(work, 'artifacts/independent-notes.md')
    result = factory.object_json(factory.safe_file(work, 'artifacts/review.json'))
    text_fields(result, ('decision', 'rationale', 'business_value'))
    factory.evidence(work, result.get('evidence'))
    decision = result['decision']
    if decision not in ('proceed', 'revise', 'pivot', 'stop', 'blocked'):
        raise factory.Invalid('Unknown relevance decision')
    ids = result.get('approved_task_ids')
    deep_case.strings(ids, 'approved_task_ids')
    deep_case.strings(result.get('required_changes'), 'required_changes')
    if len(ids) != len(set(ids)) or set(ids) - {t['id'] for t in plan['tasks']}:
        raise factory.Invalid('Review approved unknown/duplicate tasks')
    if decision == 'proceed' and (not ids or result['required_changes']):
        raise factory.Invalid('Proceed needs approved tasks without mandatory revisions')
    if decision != 'proceed' and ids:
        raise factory.Invalid('Non-proceed cannot approve tasks')
    if decision == 'revise' and not result['required_changes']:
        raise factory.Invalid('Revise needs actionable feedback')
    if decision == 'pivot':
        text_fields(result, ('pivot_evidence',))
    if (work / 'candidate/evaluation-method.md').exists():
        realism = factory.object_json(factory.safe_file(work, 'artifacts/realism-review.json'))
        text_fields(realism, ('rationale',))
        factory.evidence(work, realism.get('evidence'))
        if type(realism.get('acceptable')) is not bool:
            raise factory.Invalid('Realism acceptance must be boolean')
        protocol = factory.object_json(factory.safe_file(work, 'candidate/proposal/evaluation-protocol.json'))
        if decision == 'proceed' and (not realism['acceptable'] or set(protocol['task_ids']) - set(ids)):
            raise factory.Invalid('Proceed requires realism approval and all protocol tasks')
    if (work / 'candidate/development.json').exists() and decision == 'proceed':
        if set(ids) != {t['id'] for t in plan['tasks']}:
            raise factory.Invalid('OpenSpec proceed must approve the entire specified scope')
    return result


def source_info(source):
    state = factory.load(source / 'state.json')
    if state.get('status') != 'completed' or state.get('steps', {}).get('critique', {}).get('status') != 'done':
        raise factory.Invalid('A completed independent critique is required')
    cfg = factory.load(source / 'campaign.json')
    deep_case.check_config(cfg)
    case = factory.load(source / 'case/case.json')
    deep_case.validate('critique', source / 'cycles/01/review', case, cfg.get('openspec_contract'))
    return {'source': str(source.resolve()),
            'manifest_sha256': digest(source / 'cycles/01/manifest.json'),
            'critique_sha256': digest(source / 'cycles/01/review/artifacts/critique.json')}


def prepare_inputs(source, root, development=None):
    identity = source_info(source)
    marker = root / 'source.json'
    if marker.exists():
        if factory.load(marker) != identity:
            raise factory.Invalid('Assessment source changed')
        factory.check_snapshot(root / 'inputs', factory.load(root / 'inputs-manifest.json'))
        return
    initializing = root / 'initializing.json'
    if root.exists():
        if not initializing.exists() or factory.load(initializing) != identity:
            raise factory.Invalid('Refusing unrelated assessment directory')
    else:
        root.mkdir(parents=True)
        factory.save(initializing, identity)
    cfg = factory.load(source / 'campaign.json')
    snapshot = source / 'cycles/01/snapshot'
    manifest = factory.load(source / 'cycles/01/manifest.json')
    factory.check_snapshot(snapshot, manifest, cfg['max_snapshot_bytes'])
    inputs = root / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    for relative in manifest:
        path = Path(relative)
        if path.parts[0] in ('darp_range', 'tests', 'scripts', 'configs', 'corpus', 'case', 'openspec') or (len(path.parts) == 1 and path.suffix in ('.md', '.py', '.toml', '.txt')):
            dest = inputs / 'prototype' / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / relative, dest)
    for relative, name in (
        ('cycles/01/review/artifacts/critique.md', 'critique.md'),
        ('cycles/01/review/artifacts/critique.json', 'critique.json'),
        ('cycles/01/snapshot/artifacts/case-update.md', 'case-update.md'),
        ('cycles/01/snapshot/artifacts/report.md', 'report.md'),
        ('brief.md', 'previous-brief.md'),
    ):
        shutil.copy2(factory.safe_file(source, relative), inputs / name)
    factory.save(inputs / 'provenance.json', identity)
    shutil.copy2(Path(__file__).with_name('EVALUATION-METHOD.md'), inputs / 'evaluation-method.md')
    if development:
        if development != {'workflow': 'openspec', 'track': 'adversarial-coevolution'}:
            raise factory.Invalid('Unsupported development workflow/track')
        factory.save(inputs / 'development.json', development)
        for name, dest in (('COEVOLUTION.md', 'coevolution.md'), ('OPENSPEC-WORKFLOW.md', 'openspec-workflow.md')):
            shutil.copy2(Path(__file__).with_name(name), inputs / dest)
        for origin, dest in (
            ('cycles/01/snapshot/artifacts/coevolution-progress.md', 'previous-coevolution-progress.md'),
            ('cycles/01/snapshot/artifacts/openspec-results.json', 'previous-openspec-results.json'),
            ('cycles/01/review/artifacts/openspec-review.json', 'previous-openspec-review.json'),
        ):
            if (source / origin).exists():
                shutil.copy2(factory.safe_file(source, origin), inputs / dest)
    factory.save(root / 'inputs-manifest.json', factory.inventory(inputs, cfg['max_snapshot_bytes']))
    factory.save(marker, identity)


def advance_review(root, state, decision):
    if decision['decision'] == 'revise' and state['round'] < 2:
        state.update(stage='brainstorm', round=state['round'] + 1, attempt=0)
        return None
    if decision['decision'] == 'revise':
        decision = decision | {'decision': 'blocked', 'rationale': 'Revise limit reached: ' + decision['rationale']}
    result = decision | {'round': state['round']}
    factory.save(root / 'result.json', result)
    state.update(status='completed')
    factory.save(root / 'state.json', state)
    return result


def assess(source, root, invoke=None, development=None):
    prepare_inputs(source, root, development)
    state_path = root / 'state.json'
    state = factory.load(state_path) if state_path.exists() else {
        'status': 'ready', 'stage': 'brainstorm', 'round': 0, 'attempt': 0}
    if state.get('status') == 'completed':
        return factory.load(root / 'result.json')
    if state.get('status') == 'blocked':
        raise factory.Blocked(state.get('error', 'Assessment blocked'))
    cfg = factory.load(source / 'campaign.json') | {'pipeline_version': 1}
    cfg['timeout_seconds'] = min(cfg['timeout_seconds'], 3600)
    invoke = invoke or factory.Runtime(root, cfg).invoke
    while True:
        factory.limits.wait(root, state, factory.save)
        stage, round_no = state['stage'], state['round']
        round_dir = root / f'round-{round_no}'
        published = round_dir / ('proposal' if stage == 'brainstorm' else 'review')
        if published.exists():
            previous = round_dir / f"{stage}-{state['attempt']}"
            factory.check_snapshot(published, factory.inventory(previous / 'artifacts'))
            if stage == 'brainstorm':
                validate_plan(previous)
                state.update(stage='relevance_review', attempt=0)
            else:
                decision = validate_review(previous, factory.load(round_dir / 'proposal/plan.json'))
                result = advance_review(root, state, decision)
                if result:
                    return result
            state.pop('last_failure', None)
            factory.save(state_path, state)
            continue
        if state['attempt'] >= 3:
            state.update(status='blocked', error='Three invalid or failed assessment attempts; inspect logs')
            factory.save(state_path, state)
            raise factory.Blocked(state['error'])
        state['attempt'] += 1
        state.update(status='running', error=None)
        factory.save(state_path, state)
        work = round_dir / f"{stage}-{state['attempt']}"
        work.mkdir(parents=True, exist_ok=True)
        candidate = work / 'candidate'
        factory.snapshot(root / 'inputs', candidate, cfg['max_snapshot_bytes'])
        if stage == 'relevance_review':
            factory.snapshot(round_dir / 'proposal', candidate / 'proposal')
        elif round_no:
            factory.snapshot(root / f'round-{round_no - 1}' / 'review', candidate / 'feedback')
        frozen = factory.inventory(candidate, cfg['max_snapshot_bytes'])
        (work / 'artifacts').mkdir(exist_ok=True)
        factory.save(work / 'workflow-contract.json', {'mode': 'improvement_gate', 'stage': stage,
                     'focus': 'Improve current solution; alternatives only as last resort'})
        prompt_name = 'IMPROVEMENT-BRAINSTORM.md' if stage == 'brainstorm' else 'IMPROVEMENT-REVIEW.md'
        prompt = Path(__file__).with_name(prompt_name).read_text()
        if (candidate / 'evaluation-method.md').exists():
            prompt += '\n\n' + (candidate / 'evaluation-method.md').read_text()
        if (candidate / 'development.json').exists():
            prompt += '\n\n' + (candidate / 'openspec-workflow.md').read_text()
        if state.get('last_failure'):
            prompt += '\nИсправь ошибку предыдущей сдачи: ' + state['last_failure']
        role = 'researcher' if stage == 'brainstorm' else 'critic'
        log = root / 'logs' / f'{round_no}-{stage}-{state["attempt"]}-{time.time_ns()}.jsonl'
        factory.save(log.with_suffix('.request.json'), {'role': role, 'stage': stage, 'prompt': prompt})
        result = invoke(role, stage, work, prompt, log)
        factory.save(log.with_suffix('.receipt.json'), result)
        if not result.get('ok') and cfg[role]['backend'] == 'claude' and factory.limits.is_limit(result.get('error')):
            state['attempt'] -= 1
            factory.limits.schedule(root, state, result['error'], factory.save)
            continue
        try:
            if not result.get('ok'):
                raise factory.Invalid(result.get('error') or 'Worker failed')
            factory.check_snapshot(candidate, frozen, cfg['max_snapshot_bytes'])
            if stage == 'brainstorm':
                validate_plan(work)
                factory.snapshot(work / 'artifacts', round_dir / 'proposal')
                state.update(stage='relevance_review', attempt=0)
            else:
                plan = factory.load(round_dir / 'proposal/plan.json')
                decision = validate_review(work, plan)
                factory.snapshot(work / 'artifacts', round_dir / 'review')
                result = advance_review(root, state, decision)
                if result:
                    return result
            state.pop('last_failure', None)
        except (factory.Invalid, ValueError) as exc:
            state.update(status='retrying', last_failure=str(exc))
        factory.save(state_path, state)


def prepare_revision(source, target, assessment):
    identity = source_info(source) | {'assessment': str(assessment.resolve()),
                                    'decision_sha256': digest(assessment / 'result.json')}
    decision = factory.load(assessment / 'result.json')
    if decision['decision'] != 'proceed':
        raise factory.Invalid('Only proceed may launch a revision')
    if target.exists():
        marker = target / 'autopilot.json'
        if not marker.exists() or factory.load(marker) != identity:
            raise factory.Invalid('Refusing unrelated existing revision')
        factory.load(target / 'state.json')
        return
    staging = target.with_name('.' + target.name + '.preparing')
    marker = staging / 'autopilot.json'
    if staging.exists():
        if not marker.exists() or factory.load(marker) != identity:
            raise factory.Invalid('Refusing unrelated staging directory')
    else:
        staging.mkdir()
        factory.save(marker, identity)
    cfg = factory.load(source / 'campaign.json')
    manifest = factory.load(source / 'cycles/01/manifest.json')
    snapshot = source / 'cycles/01/snapshot'
    factory.check_snapshot(snapshot, manifest, cfg['max_snapshot_bytes'])
    work = staging / 'cycles/01/work'
    # The untouched baseline is kept separately; work is created only once per staging attempt.
    baseline = staging / 'baseline'
    factory.snapshot(snapshot, baseline, cfg['max_snapshot_bytes'])
    if not (staging / 'work-copy.json').exists():
        factory.snapshot(baseline, work, cfg['max_snapshot_bytes'])
        factory.save(staging / 'work-copy.json', {'complete': True})
    case_manifest = factory.snapshot(source / 'case', staging / 'case')
    factory.check_snapshot(work / 'case', case_manifest)
    input_name = 'improvement_' + target.name.replace('-', '_')
    inputs = work / input_name
    inputs.mkdir(exist_ok=True)
    round_dir = assessment / f"round-{decision['round']}"
    for directory in ('proposal', 'review'):
        factory.snapshot(round_dir / directory, inputs / directory)
    for filename in ('critique.md', 'case-update.md', 'report.md', 'previous-brief.md'):
        shutil.copy2(assessment / 'inputs' / filename, inputs / filename)
    factory.save(inputs / 'source.json', identity)
    plan = factory.load(round_dir / 'proposal/plan.json')
    tasks = [task for task in plan['tasks'] if task['id'] in decision['approved_task_ids']]
    cfg.pop('openspec_contract', None)
    if (assessment / 'inputs/development.json').exists():
        proposal_work = round_dir / 'openspec-check'
        proposal_work.mkdir(exist_ok=True)
        factory.snapshot(round_dir / 'proposal', proposal_work / 'artifacts')
        contract = spec_workflow.validate_proposal(proposal_work, plan)
        if set(contract['tasks']) != set(decision['approved_task_ids']):
            raise factory.Invalid('Approved tasks differ from OpenSpec scope')
        relative = 'openspec/changes/' + contract['change']
        if (baseline / relative).exists():
            raise factory.Invalid('Refusing existing OpenSpec change name')
        factory.snapshot(round_dir / 'proposal' / relative, work / relative)
        cfg['openspec_contract'] = contract
        factory.save(staging / 'openspec-contract.json', contract)
    brief = (
        f'# Доработка {target.name}: одобренные улучшения текущего решения\n\n'
        f'Прочитай целиком {input_name}/: brainstorm/plan в proposal, независимую review, '
        'последнюю critique и case-update. Прочитай документы case/ и проверь затронутый код.\n'
        'Продолжай существующий прототип. Не меняй направление и не создавай новую платформу.\n'
        'Одобренные задачи следующего прохода:\n\n' + json.dumps(tasks, ensure_ascii=False, indent=2) + '\n\n'
        'Сначала artifacts/improvement-plan.md: задачи, критерии, RED-регрессии, ресурсы. '
        'Исправляй первопричины; не завершай работу одним отчётом или очередной лексической заплаткой. '
        'Проведи GREEN, отрицательные контроли, мутации и полный verify из чистой копии. '
        'Сохрани работающие контракты и все прежние результаты; новые прогоны и locks отдельно. '
        'Принятые задачи не разрешают ослаблять остальные требования. Неизвестное отмечай честно.\n'
        'Обнови artifacts/case-update.md, report.md, status.json, submission.json. '
        'В artifacts/improvement-results.md сопоставь каждый одобренный ID с кодом, тестом, '
        'результатом и остатками. Изменения вне плана допустимы только как необходимые зависимости '
        'или исправления обнаруженной ошибки; обоснуй их. Исторические входы не изменять.\n'
        'Бюджет живых экспериментов на весь проход автор+критик: 180 запросов/3600 секунд, '
        'с резервом критику 60 запросов/900 секунд; один запуск максимум 60 запросов, '
        '150000 токенов и 900 секунд. Все попытки учитывать в artifacts/improvement-budget.json. '
        'Офлайн verify не обращается к моделям. Не обходи ограничения.\n'
        'Критик: прочитай одобренный план, прежнюю критику и оба case-update целиком. '
        'Независимо проверь каждый ID и реальные изменения, воспроизведи контрпримеры и verify. '
        'Оцени достигнутую пользу, а не только количество тестов. Перечисли остатки и новые проблемы. '
        'После critique проход заканчивается; следующий план создаёт отдельный автопилот.\n'
    )
    method = assessment / 'inputs/evaluation-method.md'
    if method.exists():
        shutil.copy2(method, inputs / 'evaluation-method.md')
        brief += '\n\n' + method.read_text()
    if cfg.get('openspec_contract'):
        for filename in ('coevolution.md', 'openspec-workflow.md'):
            shutil.copy2(assessment / 'inputs' / filename, inputs / filename)
            brief += '\n\n' + (inputs / filename).read_text()
        brief += '\nТекущий утверждённый change: ' + cfg['openspec_contract']['change'] + '\n'
    for path in (staging / 'brief.md', work / 'brief.md'):
        path.write_text(brief)
    factory.save(staging / 'campaign.json', cfg)
    components = deep_case.validate_decomposition(work, factory.load(staging / 'case/case.json'))
    state = {'mode': 'deep_case', 'version': 1, 'status': 'ready', 'cycle': 1, 'stage': 'integrate',
             'winner': None, 'components': components, 'steps': {}}
    for name in ['decompose', *('build:' + c['id'] for c in components)]:
        state['steps'][name] = {'status': 'inherited', 'attempts': 0, 'infra_failures': 0,
                                'failures': [], 'source_campaign': source.name}
    factory.save(staging / 'state.json', state)
    factory.save(staging / 'revision.json', identity)
    factory.save(staging / 'baseline-manifest.json', manifest)
    (work / 'progress.md').write_text(f'# {target.name}\n\nСледующий шаг: выполнить одобренный improvement-plan.\n')
    deep_case.prepare(staging, staging / 'cycles/01', work, cfg)
    factory.save(work / 'handoff.json', {'mode': 'deep_case', 'step': 'integrate', 'input': input_name})
    deep_case.journal(staging, event='automatic_revision_created', **identity)
    factory.check_snapshot(snapshot, manifest, cfg['max_snapshot_bytes'])
    os.rename(staging, target)


def service_active(name):
    result = subprocess.run(['systemctl', '--user', 'show', f'research-campaign@{name}.service',
                             '--property=ActiveState', '--value'], check=True, capture_output=True, text=True)
    return result.stdout.strip() in ('active', 'activating', 'deactivating', 'reloading')


def start_service(name):
    subprocess.run(['systemctl', '--user', 'start', f'research-campaign@{name}.service'], check=True)


def transient_preflight_block(source):
    state = factory.load(source / 'state.json')
    doctor = source / 'doctor.json'
    return (state.get('status') == 'blocked'
            and state.get('error') == 'Live preflight failed; see doctor.json'
            and doctor.exists() and preflight.retryable(factory.load(doctor)))


def tick(root, invoke=None):
    cfg = factory.load(root / 'config.json')
    campaigns = Path(cfg['campaigns']).resolve()
    if not SLUG.fullmatch(cfg['initial']) or not SLUG.fullmatch(cfg['prefix']) or type(cfg['next_number']) is not int or cfg['next_number'] < 1:
        raise factory.Invalid('Invalid campaign naming configuration')
    state_path = root / 'state.json'
    state = factory.load(state_path) if state_path.exists() else {
        'current': cfg['initial'], 'next_number': cfg['next_number'], 'status': 'watching'}
    if not cfg.get('enabled', True):
        return
    if not SLUG.fullmatch(state['current']):
        raise factory.Invalid('Invalid current campaign name')
    if state['status'] == 'paused':
        legacy_manifest = (state.get('reason') == 'Source manifest exceeds text export limit'
                           and cfg.get('publication', {}).get('enabled', False))
        legacy_preflight = (state.get('reason') == 'Current campaign blocked; inspect its state.json'
                            and transient_preflight_block(campaigns / state['current']))
        if not legacy_manifest and not legacy_preflight:
            return
        prefix = 'manifest-limit-recovery-' if legacy_manifest else 'preflight-recovery-'
        recovery = root / 'history' / (prefix + state['current'] + '.json')
        if not recovery.exists():
            factory.save(recovery, {'time': time.time(), 'previous_state': state.copy()})
        state.update(status='watching')
        state.pop('reason', None)
        factory.save(state_path, state)
    if state.get('publish_retry_at', 0) > time.time() and cfg.get('publication', {}).get('enabled', False):
        return
    factory.save(state_path, state)
    if service_active(state['current']):
        return
    source = campaigns / state['current']
    current = factory.load(source / 'state.json')
    if (current['status'] in ('ready', 'running', 'waiting_limit', 'waiting_preflight', 'retrying')
            or transient_preflight_block(source)):
        start_service(source.name)
        return
    if current['status'] != 'completed':
        state.update(status='paused', reason='Current campaign blocked; inspect its state.json')
        factory.save(state_path, state)
        return
    with factory.lock(source):
        if cfg.get('publication', {}).get('enabled', False):
            import publishing
            state.update(status='publishing')
            factory.save(state_path, state)
            try:
                receipt = publishing.publish(source, cfg['publication'], root / 'publications' / (source.name + '.json'))
            except publishing.PushPending as exc:
                state.update(status='waiting_publish', publish_retry_at=time.time() + 1800, reason=str(exc))
                factory.save(state_path, state)
                return
            state['publication'] = receipt
            state.pop('publish_retry_at', None)
            state.pop('reason', None)
            factory.save(state_path, state)
        size = sum(p.stat().st_size for p in (source / 'cycles/01/snapshot').rglob('*') if p.is_file())
        if shutil.disk_usage(campaigns).free < size * 3 + cfg.get('min_free_bytes', 1073741824):
            raise factory.Blocked('Not enough free space to preserve inputs and create another revision')
        target = campaigns / f"{cfg['prefix']}{state['next_number']}"
        assessment = root / 'assessments' / target.name
        state.update(status='assessing', assessment=str(assessment))
        factory.save(state_path, state)
        result = assess(source, assessment, invoke, development=cfg.get('development'))
        if result['decision'] != 'proceed':
            state.update(status='paused', decision=result['decision'], reason=result['rationale'])
            factory.save(state_path, state)
            return
        prepare_revision(source, target, assessment)
        factory.save(root / 'history' / (target.name + '.json'),
                     {'source': source.name, 'target': target.name, 'assessment': str(assessment), 'time': time.time()})
        state.update(current=target.name, next_number=state['next_number'] + 1, status='watching')
        factory.save(state_path, state)
    start_service(target.name)


def run(root):
    root = Path(root).resolve()
    with factory.lock(root):
        while True:
            try:
                tick(root)
            except (factory.Blocked, factory.Invalid, OSError, ValueError, subprocess.SubprocessError) as exc:
                state = factory.load(root / 'state.json') if (root / 'state.json').exists() else {}
                state.update(status='paused', reason=str(exc))
                factory.save(root / 'state.json', state)
                print(f'Autopilot paused: {exc}', flush=True)
            time.sleep(60)
