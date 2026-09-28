#!/usr/bin/env python3
"""Fail if a public state projection would leak a credential-shaped value.

Used by CI and safe to run by hand against a live `data/public/state.json`
before exposing a dashboard. It checks the shape of the payload, not a list of
known secrets, so it also catches accidental new fields.
"""
import json
import re
import sys
from pathlib import Path

# Field names that must never appear in the public projection.
FORBIDDEN_KEYS = {
    'api_key', 'apikey', 'access_token', 'refresh_token', 'id_token',
    'authorization', 'password', 'secret', 'client_secret', 'private_key',
    'credentials', 'cookie', 'set-cookie', 'session_token', 'proxy_url',
    'base_url',
}
# Value shapes that look like real credentials or private endpoints.
FORBIDDEN_VALUE_PATTERNS = (
    (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), 'API key'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'), 'GitHub token'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), 'AWS access key'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private key'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.'), 'JWT'),
    (re.compile(r'\bBearer\s+[A-Za-z0-9._-]{16,}'), 'bearer token'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[A-Za-z]{2,}'), 'email address'),
    (re.compile(r'\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'), 'private IPv4 address'),
    (re.compile(r'\b192\.168\.\d{1,3}\.\d{1,3}\b'), 'private IPv4 address'),
    (re.compile(r'\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b'), 'private IPv4 address'),
    (re.compile(r'\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b'), 'carrier-grade NAT address'),
    (re.compile(r'\b169\.254\.\d{1,3}\.\d{1,3}\b'), 'link-local address'),
)
# Any absolute URL is suspect in a public projection except documentation links.
URL_PATTERN = re.compile(r'https?://([^\s/:"\']+)')
ALLOWED_URL_HOSTS = {'github.com', 'raw.githubusercontent.com', 'opensource.org', 'noonwake.ai'}


def walk(node, path='$'):
    """Yield (json-path, key, value) for every node in the payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path + '.' + str(key), key, value
            yield from walk(value, path + '.' + str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, '%s[%d]' % (path, index))


def check(state):
    problems = []
    for path, key, value in walk(state):
        lowered = str(key).lower()
        if lowered in FORBIDDEN_KEYS:
            problems.append('%s: forbidden field %r' % (path, key))
        if isinstance(value, str):
            for pattern, label in FORBIDDEN_VALUE_PATTERNS:
                if pattern.search(value):
                    problems.append('%s: value looks like a %s' % (path, label))
            for host in URL_PATTERN.findall(value):
                host = host.lower().split('@')[-1]
                if host not in ALLOWED_URL_HOSTS and not host.endswith('.noonwake.ai'):
                    problems.append('%s: absolute URL to %r in a public projection' % (path, host))
    if not isinstance(state, dict) or not state.get('accounts'):
        problems.append('$: projection has no accounts, so it proves nothing')
    return problems


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/public/state.json')
    if not target.exists():
        raise SystemExit('no such projection: %s' % target)
    problems = check(json.loads(target.read_text()))
    if problems:
        print('public projection check FAILED for %s' % target)
        for problem in problems:
            print('  - ' + problem)
        return 1
    print('public projection check passed: %s' % target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
