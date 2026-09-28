"""The public projection checker must catch real leaks and not cry wolf.

The negative fixtures here are the regression lock for
``scripts/check_public_projection.py``: if someone loosens a rule, these fail.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_checker():
    path = ROOT / 'scripts' / 'check_public_projection.py'
    spec = importlib.util.spec_from_file_location('check_public_projection', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['check_public_projection'] = module
    spec.loader.exec_module(module)
    return module


CHECKER = load_checker()


def baseline():
    """A minimal projection shaped like the real one."""
    return {
        'title': 'AI 流口水检测',
        'accounts': [
            {'id': 'abcdef123456', 'name': 'relay-alpha', 'type': 'apikey',
             'platform': 'openai', 'model': 'gpt-6-astra', 'effort': 'medium',
             'paused': False, 'priority': {'score': 84.2, 'actual_priority': 1680},
             'history': {'candy': [], 'drawing': []}},
        ],
        'costs': {'windows': {'24h': {'amount_usd': 1.28}}},
    }


class LeakDetectionTests(unittest.TestCase):
    def assertCaught(self, mutate, label):
        payload = baseline()
        mutate(payload)
        problems = CHECKER.check(payload)
        self.assertTrue(problems, '%s should have been flagged' % label)

    def test_a_clean_projection_passes(self):
        self.assertEqual(CHECKER.check(baseline()), [])

    def test_credential_shaped_values_are_caught(self):
        cases = {
            'api key': 'sk-abcdefghijklmnopqrstuvwxyz012345',
            'github token': 'ghp_' + 'a' * 30,
            'aws key': 'AKIAIOSFODNN7EXAMPLE',
            'jwt': 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature',
            'bearer': 'Bearer abcdefghijklmnopqrstuvwxyz012345',
            'private key': '-----BEGIN RSA PRIVATE KEY-----',
            'email': 'someone@example.com',
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                self.assertCaught(lambda p, v=value: p['accounts'][0].update({'note': v}), label)

    def test_private_and_special_ip_ranges_are_caught(self):
        for address in ('10.12.0.4', '127.0.0.1', '192.168.1.9', '172.16.5.5',
                        '172.31.255.1', '100.64.2.1', '169.254.2.1'):
            with self.subTest(address=address):
                self.assertCaught(
                    lambda p, a=address: p['accounts'][0].update({'note': 'http://%s/v1' % a}),
                    address)

    def test_unknown_hosts_are_caught_but_documentation_links_are_not(self):
        self.assertCaught(
            lambda p: p['accounts'][0].update({'note': 'https://relay.internal.example/v1'}),
            'internal host')
        self.assertCaught(
            lambda p: p['accounts'][0].update({'note': 'https://api.moonshot.cn/v1'}),
            'upstream host')
        for allowed in ('https://github.com/noonwake-ai/drool-detector',
                        'https://noonwake.ai/', 'https://codex-watch.noonwake.ai/'):
            with self.subTest(allowed=allowed):
                payload = baseline()
                payload['note'] = allowed
                self.assertEqual(CHECKER.check(payload), [])

    def test_credential_bearing_field_names_are_rejected_outright(self):
        for key in ('api_key', 'access_token', 'refresh_token', 'authorization',
                    'password', 'client_secret', 'private_key', 'credentials',
                    'cookie', 'proxy_url', 'base_url'):
            with self.subTest(key=key):
                self.assertCaught(
                    lambda p, k=key: p['accounts'][0].update({k: 'anything'}), key)

    def test_a_projection_without_accounts_proves_nothing(self):
        payload = baseline()
        payload['accounts'] = []
        self.assertTrue(CHECKER.check(payload))

    def test_nested_values_are_walked(self):
        self.assertCaught(
            lambda p: p['costs']['windows']['24h'].update({'note': 'sk-abcdefghijklmnopqrstuvwxyz'}),
            'nested key')


class CommandLineTests(unittest.TestCase):
    def test_the_cli_exits_non_zero_on_a_leak_and_zero_on_a_clean_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / 'good.json'
            bad = Path(tmp) / 'bad.json'
            good.write_text(json.dumps(baseline()))
            leaky = baseline()
            leaky['accounts'][0]['note'] = 'sk-abcdefghijklmnopqrstuvwxyz012345'
            bad.write_text(json.dumps(leaky))
            argv = sys.argv
            try:
                sys.argv = ['check', str(good)]
                self.assertEqual(CHECKER.main(), 0)
                sys.argv = ['check', str(bad)]
                self.assertEqual(CHECKER.main(), 1)
            finally:
                sys.argv = argv


if __name__ == '__main__':
    unittest.main()
