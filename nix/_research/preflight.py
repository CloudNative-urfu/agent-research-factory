"""Persistent availability backoff; invalid capabilities still fail closed."""

import time


def transient(error):
    if isinstance(error, str):
        return any(marker in error for marker in ('EAI_AGAIN', 'ECONNRESET', 'ETIMEDOUT'))
    if not isinstance(error, dict):
        return False
    if error.get('finish_reason') == 'length':
        return False
    if error.get('status') in (408, 429, 500, 502, 503, 504):
        return True
    if error.get('finish_reason') == 'error':
        return True
    if error.get('type') in ('TimeoutError', 'ConnectionResetError', 'ConnectionRefusedError', 'gaierror'):
        return True
    return error.get('message') == 'Stream incomplete or final content empty'


def retryable(report):
    checks = report.get('checks', {})
    failed = [key for key, value in checks.items() if not key.endswith('_error') and value is not True]
    def detail(key):
        return checks.get(key.replace('_tool_call', '') + '_error')
    return not report.get('ok') and bool(failed) and all(transient(detail(key)) for key in failed)


def wait(root, state, save):
    retry = state.get('preflight_retry')
    if retry:
        while retry['resume_at'] > time.time():
            time.sleep(min(60, retry['resume_at'] - time.time()))


def schedule(root, state, report, save):
    attempt = state.get('preflight_retry', {}).get('attempt', 0) + 1
    delay = (60, 300, 1800)[min(attempt - 1, 2)]
    retry = {'attempt': attempt, 'resume_at': time.time() + delay, 'checks': report['checks']}
    state.update(status='waiting_preflight', error=None, preflight_retry=retry)
    state['preflight_history'] = (state.get('preflight_history', []) + [retry])[-20:]
    save(root / 'state.json', state)


def recovered(root, state, save):
    if state.pop('preflight_retry', None) is not None or state.get('status') == 'waiting_preflight':
        state.update(status='ready', error=None)
        save(root / 'state.json', state)
