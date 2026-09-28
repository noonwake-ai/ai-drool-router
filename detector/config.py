# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Central configuration for Drool Detector & Router.

Precedence: environment variables > config file > built-in defaults.
The config file path comes from ``DROOL_CONFIG`` and defaults to ``./config.json``.
Everything platform specific lives here, so the engine stays provider agnostic.
"""
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

ENV_PREFIX = 'DROOL_'

# Wire protocols this project can speak. Anything else a supplier offers is
# almost always reachable through `openai_chat`, which is the default for a
# platform that does not declare one — that is what makes "any model" real
# rather than a marketing claim.
PROTOCOLS = ('openai_responses', 'openai_chat', 'anthropic_messages',
             'gemini_generate', 'xai_responses')
DEFAULT_PROTOCOL = 'openai_chat'
# Platforms whose native protocol differs from the generic default. A deployer
# can override any of these with an explicit `protocol` in config.json.
IMPLIED_PROTOCOLS = {
    'openai': 'openai_responses',
    'anthropic': 'anthropic_messages',
    'claude': 'anthropic_messages',
    'gemini': 'gemini_generate',
    'grok': 'xai_responses',
}


def protocol_for(platform):
    """Resolve the wire protocol for a platform.

    Explicit config wins; otherwise a known platform keeps its native protocol
    and everything else falls back to OpenAI-compatible chat completions.
    """
    spec = platform_spec(platform)
    declared = str(spec.get('protocol') or '').strip()
    if declared:
        if declared not in PROTOCOLS:
            raise SystemExit('平台 %s 的 protocol 无效：%s（可选：%s）' %
                             (platform, declared, ', '.join(PROTOCOLS)))
        return declared
    return IMPLIED_PROTOCOLS.get(platform, DEFAULT_PROTOCOL)

DEFAULTS = {
    'title': '流口水降智检测与智能调度',
    'subtitle': '你的 AI 现在还在流口水吗？',
    'base_url': '',
    'data_dir': './data',
    'web': {'host': '127.0.0.1', 'port': 4191, 'public_controls': False},
    'schedule': {
        'timezone': 'Asia/Shanghai',
        'regular_minutes': 45,
        'quiet_start': '04:00',
        'quiet_end': '08:00',
        'quiet_minutes': 90,
        'history_hours': 24,
    },
    'platforms': {
        'openai': {
            'enabled': True,
            'label': 'GPT',
            'protocol': 'openai_responses',
            'model': 'gpt-6-astra',
            'effort': 'medium',
            'group_ids': [],
            'group_names': [],
            'exclude_names': ['生图', 'image'],
        },
        'anthropic': {
            'enabled': True,
            'label': 'Claude',
            'protocol': 'anthropic_messages',
            'model': 'claude-opus-5-5',
            'effort': 'medium',
            'group_ids': [],
            'group_names': [],
            'exclude_names': [],
        },
        'gemini': {
            'enabled': True,
            'label': 'Gemini',
            'protocol': 'gemini_generate',
            'model': 'gemini-3.8-flash',
            'effort': 'high',
            'group_ids': [],
            'group_names': [],
            'exclude_names': [],
        },
        'grok': {
            'enabled': True,
            'label': 'Grok',
            'protocol': 'xai_responses',
            'model': 'grok-4.7',
            'effort': 'high',
            'group_ids': [],
            'group_names': [],
            'exclude_names': [],
        },
    },
    'budgets': {
        'default_seconds': 900,
        'idle_seconds': 120,
        'gemini_high_idle_seconds': 300,
        'drawing_platform_seconds': {'grok': 1500},
        'max_attempts': 3,
    },
    'upstream': {
        # Path to a PEM bundle used to verify upstream TLS. Empty = the normal
        # system/certifi trust store. Set this when the gateway sits behind a
        # TLS-inspecting proxy, or when probing an internal endpoint with a
        # private CA.
        'ca_bundle': '',
    },
    'routing': {
        'weights': {'intelligence': 0.36, 'cost': 0.36, 'stability': 0.18, 'speed': 0.10},
        'rounds': 3,
        'stability_rounds': 3,
        'priority_min': 100,
        'priority_max': 100100,
        'write_priority': False,
        'write_callable': False,
        'stability_endpoint': '',
    },
    'retention': {'artifacts_hours': 24, 'cost_days': 30},
    'privacy': {
        # 'full'    - publish the gateway account name verbatim
        # 'alias'   - publish a stable pseudonym such as "supplier-1a2b3c"
        # 'masked'  - keep the first 2 and last 2 characters, mask the middle
        'account_names': 'full',
    },
    'identity_salt': 'drool-detector',
}


def _deep_merge(base, override):
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _env_overrides():
    overrides = {}
    mapping = {
        ENV_PREFIX + 'TITLE': ('title', str),
        ENV_PREFIX + 'BASE_URL': ('base_url', str),
        ENV_PREFIX + 'DATA_DIR': ('data_dir', str),
        ENV_PREFIX + 'WEB_HOST': ('web.host', str),
        ENV_PREFIX + 'WEB_PORT': ('web.port', int),
        ENV_PREFIX + 'PUBLIC_CONTROLS': ('web.public_controls', 'bool'),
        ENV_PREFIX + 'ROUTING_WRITE_PRIORITY': ('routing.write_priority', 'bool'),
        ENV_PREFIX + 'ROUTING_WRITE_CALLABLE': ('routing.write_callable', 'bool'),
        ENV_PREFIX + 'STABILITY_ENDPOINT': ('routing.stability_endpoint', str),
        ENV_PREFIX + 'CA_BUNDLE': ('upstream.ca_bundle', str),
    }
    for name, (path, kind) in mapping.items():
        raw = os.environ.get(name)
        if raw is None or raw == '':
            continue
        if kind == 'bool':
            value = raw.strip().lower() in ('1', 'true', 'yes', 'on')
        else:
            value = kind(raw)
        node = overrides
        parts = path.split('.')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return overrides


def _load_file(path):
    if not path:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit('配置文件无法解析：%s（%s）' % (target, exc))
    if not isinstance(data, dict):
        raise SystemExit('配置文件顶层必须是 JSON 对象：%s' % target)
    return data


def load(path=None):
    """Return the merged configuration dictionary."""
    chosen = path or os.environ.get(ENV_PREFIX + 'CONFIG') or './config.json'
    config = _deep_merge(DEFAULTS, _load_file(chosen))
    config = _deep_merge(config, _env_overrides())
    config['config_path'] = str(chosen)
    return config


CONFIG = load()


def get(path, default=None):
    node = CONFIG
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def enabled_platforms():
    platforms = CONFIG.get('platforms') or {}
    return {name: spec for name, spec in platforms.items()
            if isinstance(spec, dict) and spec.get('enabled') and spec.get('model')}


def platform_spec(name):
    return enabled_platforms().get(name) or {}


def data_dir():
    return Path(os.path.expanduser(CONFIG.get('data_dir') or './data'))


def evaluation_scope_ids(platform):
    spec = platform_spec(platform)
    return [int(value) for value in (spec.get('group_ids') or []) if str(value).lstrip('-').isdigit()]


def write_enabled(kind):
    """Whether a Sub2API mutation is authorised by config.

    This is the single source of truth for write permission. Neither a CLI flag
    nor a systemd unit may turn a write on that config has left off, so a stock
    deployment can never mutate the gateway by accident.

    ``kind`` is ``priority`` or ``callable``.
    """
    if kind not in ('priority', 'callable'):
        raise ValueError('unknown write kind: %r' % kind)
    return bool(config_bool('routing.write_' + kind))


def max_attempts():
    """Total attempts allowed per probe, including the first request.

    Clamped to 1..3 so a typo cannot start an unbounded paid retry loop.
    """
    if 'max_attempts' not in (CONFIG.get('budgets') or {}):
        return 3
    try:
        value = int(CONFIG['budgets']['max_attempts'])
    except (TypeError, ValueError):
        return 3
    return max(1, min(3, value))


def account_name_mode():
    """How much of a gateway account name may appear in the public board."""
    mode = str(CONFIG.get('privacy', {}).get('account_names') or 'full').strip().lower()
    return mode if mode in ('full', 'alias', 'masked') else 'full'


def public_account_name(account_id, name):
    """Apply the configured account-name privacy mode.

    Account names often identify the real upstream supplier, so a public
    deployment may not want them verbatim. Aliasing stays stable across rounds
    so history still lines up.
    """
    cleaned = str(name or '')
    mode = account_name_mode()
    if mode == 'alias':
        digest = hashlib.sha256((config_salt() + '-name-' + str(account_id)).encode()).hexdigest()[:6]
        return 'supplier-' + digest
    if mode == 'masked':
        if len(cleaned) <= 4:
            return '*' * len(cleaned)
        return cleaned[:2] + '*' * max(1, len(cleaned) - 4) + cleaned[-2:]
    return cleaned


def config_salt():
    return str(CONFIG.get('identity_salt') or 'drool-detector')


def config_bool(path, default=False):
    """Read a boolean config value, accepting only real booleans or 0/1 strings."""
    value = get(path, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(value, int):
        return bool(value)
    return bool(default)
