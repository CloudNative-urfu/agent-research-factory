"""Durable waits for explicit Claude subscription limits."""

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def is_limit(error):
    text = str(error or '').lower()
    return 'usage_limit_reached' in text or bool(re.search(r"you['’]ve hit your (?:session|weekly|usage) limit", text))


def deadline(error, now):
    match = re.search(r'resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)', str(error), re.I)
    if match:
        hour, minute, period, zone = match.groups()
        try:
            if not 1 <= int(hour) <= 12:
                raise ValueError('Invalid hour')
            local = datetime.fromtimestamp(now, ZoneInfo(zone))
            reset = local.replace(hour=int(hour) % 12 + (12 if period.lower() == 'pm' else 0),
                                  minute=int(minute or 0), second=0, microsecond=0)
            if reset.timestamp() + 120 <= now:
                reset += timedelta(days=1)
            return reset.timestamp() + 120
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return now + 1800


def schedule(root, state, error, save, observed_at=None):
    now = time.time()
    resume_at = deadline(error, now if observed_at is None else observed_at)
    state['rate_limit'] = {'resume_at': resume_at, 'reason': str(error), 'stage': state['stage']}
    state.update(status='waiting_limit', error=None)
    state.setdefault('limit_history', []).append({'detected_at': now, **state['rate_limit']})
    save(root / 'state.json', state)


def wait(root, state, save):
    pending = state.get('rate_limit')
    if not pending:
        return
    while True:
        remaining = pending['resume_at'] - time.time()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 60))
    state.pop('rate_limit')
    state.update(status='running', error=None)
    save(root / 'state.json', state)
