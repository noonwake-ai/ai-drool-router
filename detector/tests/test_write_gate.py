"""config.json is the only thing that can authorise a Sub2API mutation.

These tests exist because a stock deployment must be able to score suppliers
without ever writing to the gateway. A CLI flag or a systemd unit must not be
able to turn a write on behind the deployer's back.
"""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from detector import config, pricing_tick
from detector.monitor import Store, collect
from detector.supplier_prices import price_signature

SCOPE = {'group_ids': [], 'group_names': [], 'exclude_names': []}


class WritePermissionTests(unittest.TestCase):
    def test_write_enabled_defaults_to_false_for_both_kinds(self):
        self.assertFalse(config.write_enabled('priority'))
        self.assertFalse(config.write_enabled('callable'))

    def test_write_enabled_accepts_only_real_booleans_or_numeric_strings(self):
        for raw, expected in ((True, True), (False, False), (1, True), (0, False),
                              ('true', True), ('on', True), ('1', True),
                              ('false', False), ('', False), (None, False)):
            with self.subTest(raw=raw):
                with patch.dict(config.CONFIG['routing'], {'write_priority': raw}):
                    self.assertEqual(config.write_enabled('priority'), expected)

    def test_unknown_write_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            config.write_enabled('everything')


class WorkerWriteGateTests(unittest.TestCase):
    """`collect` must not construct an enabled router unless config agrees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def capture(self, cli_quality, cli_priority, config_priority, config_callable):
        seen = {}

        class CapturingChain:
            def __init__(self, quality, priority):
                seen['quality_enabled'] = quality.enabled
                seen['priority_enabled'] = priority.enabled

            def reconcile(self):
                pass

        api = Mock()
        api.accounts.return_value = []
        routing_patch = {'write_priority': config_priority, 'write_callable': config_callable}
        with patch.dict(config.CONFIG['routing'], routing_patch), \
                patch('detector.monitor.Sub2API', return_value=api), \
                patch('detector.monitor.RoutingChain', CapturingChain), \
                patch('detector.monitor.read_admin_key', return_value='x' * 32), \
                patch('detector.monitor.current_slot', return_value=1_800_000_000), \
                patch('detector.monitor.next_slot', return_value=1_800_003_000):
            collect(self.tmp.name, '', 'https://gateway.test', source='timer',
                    quality_routing=cli_quality, priority_routing=cli_priority)
        return seen

    def test_cli_flags_cannot_enable_writes_when_config_says_no(self):
        seen = self.capture(cli_quality=True, cli_priority=True,
                            config_priority=False, config_callable=False)
        self.assertFalse(seen['quality_enabled'])
        self.assertFalse(seen['priority_enabled'])

    def test_config_alone_does_not_enable_writes_without_the_cli_request(self):
        seen = self.capture(cli_quality=False, cli_priority=False,
                            config_priority=True, config_callable=True)
        self.assertFalse(seen['quality_enabled'])
        self.assertFalse(seen['priority_enabled'])

    def test_both_must_agree_before_a_write_path_is_live(self):
        seen = self.capture(cli_quality=True, cli_priority=True,
                            config_priority=True, config_callable=True)
        self.assertTrue(seen['quality_enabled'])
        self.assertTrue(seen['priority_enabled'])

    def test_the_two_switches_are_independent(self):
        seen = self.capture(cli_quality=True, cli_priority=True,
                            config_priority=True, config_callable=False)
        self.assertFalse(seen['quality_enabled'])
        self.assertTrue(seen['priority_enabled'])


class PricingTickWriteGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_disabled_tick_never_constructs_a_gateway_client(self):
        with patch.dict(config.CONFIG['routing'], {'write_priority': False}), \
                patch('detector.pricing_tick.Sub2API') as factory, \
                patch('detector.pricing_tick.PriorityRouter') as router:
            result = pricing_tick.tick(self.tmp.name, '', 'https://gateway.test')
        self.assertEqual(result['status'], 'write_disabled_by_config')
        self.assertEqual(result['model_requests'], 0)
        factory.assert_not_called()
        router.assert_not_called()

    def test_disabled_tick_records_the_signature_and_does_not_retry_forever(self):
        with patch.dict(config.CONFIG['routing'], {'write_priority': False}):
            first = pricing_tick.tick(self.tmp.name, '', 'https://gateway.test')
            second = pricing_tick.tick(self.tmp.name, '', 'https://gateway.test')
        self.assertEqual(first['status'], 'write_disabled_by_config')
        self.assertEqual(second['status'], 'unchanged')

    def test_enabled_tick_reaches_the_router(self):
        api = Mock()
        with patch.dict(config.CONFIG['routing'], {'write_priority': True}), \
                patch('detector.pricing_tick.Sub2API', return_value=api) as factory, \
                patch('detector.pricing_tick.PriorityRouter') as router:
            router.return_value.reconcile.return_value = 'applied'
            result = pricing_tick.tick(self.tmp.name, '', 'https://gateway.test')
        self.assertEqual(result['status'], 'applied')
        factory.assert_called_once()
        self.assertTrue(router.call_args.args[2])


class PublicStateAdvertisesTheGateTests(unittest.TestCase):
    def test_published_state_reports_which_writes_are_armed(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with patch.dict(config.CONFIG['routing'], {'write_priority': False, 'write_callable': True}):
                store.publish_state()
            state = json.loads((store.public / 'state.json').read_text())
        self.assertEqual(state['routing_writes'], {'priority': False, 'callable': True})


if __name__ == '__main__':
    unittest.main()
