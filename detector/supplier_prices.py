# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Exclusive fixed/daily supplier rates, with Beijing time and optimistic writes."""
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

from .controls import ControlConflict

TIMEZONE = 'Asia/Shanghai'
MAX_PERIODS = 24


def valid_multiplier(value):
    return value is None or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1000)


def minute(value, end=False):
    if end and value == '24:00':
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError('INVALID_PERIOD_TIME')
    hours, minutes = map(int, value.split(':'))
    return hours*60+minutes


def normalize_config(config):
    if not isinstance(config, dict):
        raise ValueError('INVALID_PRICE')
    if config.get('mode') == 'fixed':
        if set(config) != {'mode', 'multiplier'} or not valid_multiplier(config['multiplier']):
            raise ValueError('INVALID_PRICE')
        return dict(config)
    if (config.get('mode') != 'scheduled' or set(config) != {'mode', 'timezone', 'periods'}
            or config['timezone'] != TIMEZONE or not isinstance(config['periods'], list)
            or not 1 <= len(config['periods']) <= MAX_PERIODS):
        raise ValueError('INVALID_PRICE_SCHEDULE')
    boundary = 0
    periods = []
    for row in config['periods']:
        if not isinstance(row, dict) or set(row) != {'start', 'end', 'multiplier'}:
            raise ValueError('INVALID_PRICE_PERIOD')
        start, end = minute(row['start']), minute(row['end'], end=True)
        if start != boundary or end <= start or row['multiplier'] is None or not valid_multiplier(row['multiplier']):
            raise ValueError('INVALID_PRICE_COVERAGE')
        periods.append(dict(row))
        boundary = end
    if boundary != 1440:
        raise ValueError('INCOMPLETE_PRICE_COVERAGE')
    return {'mode': 'scheduled', 'timezone': TIMEZONE, 'periods': periods}


def stored_config(value):
    mode = value.get('mode', 'fixed')
    return normalize_config({'mode': mode, 'multiplier': value.get('multiplier')} if mode == 'fixed' else
        {'mode': mode, 'timezone': value.get('timezone'), 'periods': value.get('periods')})


def revision(value):
    if value is None:
        return 'none'
    canonical = {**stored_config(value), 'updated_at': value.get('updated_at', 0)}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def project_price(value=None, now=None):
    now = time.time() if now is None else now
    config = stored_config(value or {})
    result = {**config, 'updated_at': (value or {}).get('updated_at', 0),
              'revision': revision(value), 'evaluated_at': now, 'next_change_at': None}
    if config['mode'] == 'scheduled':
        local = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
        minutes = local.hour*60+local.minute
        for index, row in enumerate(config['periods']):
            end = minute(row['end'], end=True)
            if minute(row['start']) <= minutes < end:
                result.update(multiplier=row['multiplier'], active_period=index,
                    next_change_at=(local.replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(minutes=end)).timestamp())
                break
    return result


def price_signature(prices):
    # Configuration or effective-value changes matter; wall-clock reads do not.
    values = {key: [value['revision'], value['multiplier']] for key, value in prices.items()}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Prices:
    def __init__(self, root):
        self.root = Path(root)

    def _load(self):
        try:
            data = json.loads((self.root/'prices.json').read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or len(data) > 5000:
            raise ValueError('INVALID_PRICES')
        for ident, value in data.items():
            if not re.fullmatch(r'[a-f0-9]{12}', ident) or not isinstance(value, dict):
                raise ValueError('INVALID_PRICES')
            stored_config(value)
            stamp = value.get('updated_at', 0)
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
                raise ValueError('INVALID_PRICE_TIMESTAMP')
        return data

    def read(self, now=None):
        now = time.time() if now is None else now
        return {key: project_price(value, now) for key, value in self._load().items()}

    def set(self, ident, multiplier, expected):
        if not valid_multiplier(expected):
            raise ValueError('INVALID_PRICE')
        return self._set(ident, {'mode': 'fixed', 'multiplier': multiplier}, expected, legacy=True)

    def set_config(self, ident, config, expected_revision):
        if not isinstance(expected_revision, str) or not re.fullmatch(r'none|[a-f0-9]{64}', expected_revision):
            raise ValueError('INVALID_PRICE_REVISION')
        return self._set(ident, config, expected_revision)

    def _set(self, ident, config, expected, legacy=False):
        if not re.fullmatch(r'[a-f0-9]{12}', ident):
            raise ValueError('INVALID_PRICE')
        config = normalize_config(config)
        self.root.mkdir(mode=0o750, exist_ok=True)
        with (self.root/'prices.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._load()
            current = data.get(ident)
            current_config = stored_config(current or {})
            matches = (current_config['mode'] == 'fixed' and current_config['multiplier'] == expected) if legacy else revision(current) == expected
            if not matches:
                raise ControlConflict('PRICE_CHANGED')
            data[ident] = {**config, 'updated_at': max(time.time(), (current or {}).get('updated_at', 0)+0.000001)}
            temp = self.root/('.'+uuid.uuid4().hex+'.tmp')
            try:
                with os.fdopen(os.open(temp, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o640), 'w') as stream:
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.root/'prices.json')
            finally:
                temp.unlink(missing_ok=True)
            return project_price(data[ident])
