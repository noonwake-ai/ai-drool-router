"""Central configuration for AI Drool Detector.

Precedence: environment variables > config file > built-in defaults.
The config file path comes from ``DROOL_CONFIG`` and defaults to ``./config.json``.
Everything platform specific lives here, so the engine stays provider agnostic.
"""
import json
import os
from copy import deepcopy
from pathlib import Path

ENV_PREFIX = 'DROOL_'

DEFAULTS = {
    'title': 'AI 流口水检测',
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
            'model': 'gpt-6-astra',
            'effort': 'medium',
            'group_ids': [],
            'group_names': [],
            'exclude_names': ['生图', 'image'],
        },
        'anthropic': {
            'enabled': True,
            'label': 'Claude',
            'model': 'claude-opus-5-5',
            'effort': 'medium',
            'group_ids': [],
            'group_names': [],
            'exclude_names': [],
        },
        'gemini': {
            'enabled': True,
            'label': 'Gemini',
            'model': 'gemini-3.8-flash',
            'effort': 'high',
            'group_ids': [],
            'group_names': [],
            'exclude_names': [],
        },
        'grok': {
            'enabled': True,
            'label': 'Grok',
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
    'identity_salt': 'ai-drool-detector',
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
