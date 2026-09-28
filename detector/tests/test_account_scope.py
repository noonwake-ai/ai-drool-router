# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Scope rules: deployer-configured groups narrow the set, name keywords always win."""
import unittest
from unittest.mock import Mock, patch

from detector import config
from detector.upstream import ProbeError, Sub2API, in_evaluation_scope

OPENAI = {'group_ids': [4], 'group_names': [], 'exclude_names': ['生图', 'image']}


class AccountScopeTests(unittest.TestCase):
    def row(self, group_ids, name='fixture', platform='openai', **extra):
        return {'id': 1, 'name': name, 'platform': platform, 'group_ids': group_ids,
                'status': 'active', 'schedulable': True, **extra}

    def test_without_group_configuration_every_enabled_platform_account_is_in_scope(self):
        spec = {'group_ids': [], 'group_names': [], 'exclude_names': []}
        with patch.dict(config.CONFIG['platforms']['openai'], spec):
            for groups in ([], [4], [10], None, ['4']):
                with self.subTest(groups=groups):
                    self.assertTrue(in_evaluation_scope(self.row(groups)))
            self.assertFalse(in_evaluation_scope(self.row([4], platform='unknown')))

    def test_excluded_name_keyword_always_wins_over_group_membership(self):
        with patch.dict(config.CONFIG['platforms']['openai'], OPENAI):
            for name in ('fixture 生图', 'IMAGE-only relay', 'image-gen relay'):
                with self.subTest(name=name):
                    self.assertFalse(in_evaluation_scope(self.row([4], name)))
            # A name that only *contains* a keyword as a substring is still excluded,
            # and a clean name stays in scope.
            self.assertFalse(in_evaluation_scope(self.row([4], 'myimage relay')))
            self.assertTrue(in_evaluation_scope(self.row([4], 'plain relay')))

    def test_group_ids_scope_is_authoritative_and_ignores_non_integer_ids(self):
        spec = {'group_ids': [4], 'group_names': [], 'exclude_names': []}
        with patch.dict(config.CONFIG['platforms']['openai'], spec):
            self.assertTrue(in_evaluation_scope(self.row([4])))
            self.assertTrue(in_evaluation_scope(self.row([4, 10])))
            for groups in ([], [10], [18], ['4'], [True], None):
                with self.subTest(groups=groups):
                    self.assertFalse(in_evaluation_scope(self.row(groups)))

    def test_name_scope_needs_the_gateway_to_publish_names(self):
        spec = {'group_ids': [], 'group_names': ['openai-main'], 'exclude_names': []}
        with patch.dict(config.CONFIG['platforms']['openai'], spec):
            self.assertTrue(in_evaluation_scope(self.row([4], group_names=['OpenAI-Main'])))
            self.assertTrue(in_evaluation_scope(self.row([4], groups=[{'name': 'openai-main'}])))
            self.assertFalse(in_evaluation_scope(self.row([4], group_names=['image-only'])))

    def test_name_scope_without_published_names_is_unknown_not_silently_excluded(self):
        # A numeric id list plus a name rule must not be guessed: including or
        # excluding the account silently could probe something unintended.
        spec = {'group_ids': [], 'group_names': ['openai-main'], 'exclude_names': []}
        with patch.dict(config.CONFIG['platforms']['openai'], spec):
            from detector.upstream import scope_state
            self.assertEqual(scope_state(self.row([4])), 'unknown')

    def test_name_and_id_scope_both_apply_when_both_are_configured(self):
        spec = {'group_ids': [4], 'group_names': ['openai-main'], 'exclude_names': []}
        with patch.dict(config.CONFIG['platforms']['openai'], spec):
            self.assertTrue(in_evaluation_scope(self.row([4])))
            self.assertTrue(in_evaluation_scope(self.row([7], group_names=['openai-main'])))
            self.assertFalse(in_evaluation_scope(self.row([7], group_names=['other'])))

    def test_discovery_filters_by_scope_and_keeps_other_platforms(self):
        rows = [{**self.row(g, n), 'id': i} for i, (g, n) in enumerate(
            (([4], 'OpenAI'), ([4, 10], 'dual group'), ([10], 'outside'), ([18], 'shadow')), 1)]
        rows.append({'id': 5, 'name': 'Claude', 'platform': 'anthropic', 'group_ids': [1]})
        api = Sub2API('https://example.test', 'offline')
        api.get = Mock(return_value={'items': rows, 'total': len(rows)})
        with patch.dict(config.CONFIG['platforms']['openai'], OPENAI):
            self.assertEqual([r['id'] for r in api.accounts()], [1, 2, 5])

    def test_missing_group_metadata_aborts_whole_sync(self):
        api = Sub2API('https://example.test', 'offline')
        api.get = Mock(return_value={'items': [self.row(None)], 'total': 1})
        with patch.dict(config.CONFIG['platforms']['openai'], OPENAI):
            with self.assertRaises(ProbeError) as exc:
                api.accounts()
        self.assertEqual(exc.exception.code, 'ACCOUNT_SCOPE_UNKNOWN')

    def test_moved_account_cannot_export_credentials_or_change_routing(self):
        api = Sub2API('https://example.test', 'offline')
        api.get = Mock(return_value=self.row([10]))
        with patch.dict(config.CONFIG['platforms']['openai'], OPENAI):
            with self.assertRaises(ProbeError):
                api.account(1)
            api.get.assert_called_once_with('/accounts/1')
            api.account_state = Mock(return_value=self.row([10]))
            with patch('detector.upstream.requests.Session') as session:
                with self.assertRaises(ProbeError):
                    api.set_callable(1, False)
            session.assert_not_called()


if __name__ == '__main__':
    unittest.main()
