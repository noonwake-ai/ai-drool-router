# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Observations cannot change verdicts, history provenance, or routing."""
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from detector.drawing_signals import analyze, from_attempts, VERSION
from detector.monitor import Store, execute_run, hour
from detector.prompts import DRAWING_PROFILE, INSTRUCTIONS, LEGACY_DRAWING_INSTRUCTIONS

HTML = '<!doctype html><html><body><svg><desc>踩踏</desc><!-- 动画循环 --></svg><script>let loop=1</script></body></html>'


class SignalTests(unittest.TestCase):
    def test_prompt_does_not_seed_terms(self):
        for term in ('循环', '踩踏', '内嵌', '内联', '沿途风景', '背景移动'):
            self.assertNotIn(term, INSTRUCTIONS['drawing'])

    def test_code_is_not_a_preface(self):
        data = analyze('```html\n' + HTML + '\n```', DRAWING_PROFILE)
        self.assertEqual(data['state'], 'no_preface')
        self.assertFalse(data['preface']['risk'])
        self.assertEqual(data['descriptions']['risk'], ['循环'])
        self.assertEqual(data['source']['positive'], ['踩踏'])

    def test_only_first_paragraph_is_classified(self):
        data = analyze('我会让两只鸟踩踏，背景移动。\n\n接着安排循环。\n```html\n'+HTML, DRAWING_PROFILE)
        self.assertEqual(data['state'], 'positive_terms')
        self.assertFalse(data['preface']['risk'])
        self.assertNotIn('循环', data['preface']['excerpt'])

    def test_mixed_and_risk_are_observations(self):
        data = analyze('用内联SVG呈现踩踏循环。\n'+HTML, DRAWING_PROFILE)
        self.assertEqual(data['state'], 'mixed')
        self.assertEqual(data['preface']['risk'], ['内联 SVG', '循环'])
        self.assertNotIn('verdict', data)
        self.assertEqual(analyze('内嵌 svg 动画。\n'+HTML, DRAWING_PROFILE)['state'], 'risk_terms')

    def test_excerpt_and_scan_are_bounded(self):
        with patch('detector.drawing_signals.MAX_SCAN', 300):
            data = analyze('a'*220+'\n\n'+HTML+'x'*400, DRAWING_PROFILE)
        self.assertEqual(len(data['preface']['excerpt']), 180)
        self.assertTrue(data['preface']['excerpt_truncated'])
        self.assertTrue(data['scan_truncated'])

    def test_untrusted_markup_is_not_executed_or_mistaken_for_desc(self):
        text = '<!doctype html><html><svg></svg><script>"踩踏"</script></html>'
        data = analyze(text, DRAWING_PROFILE)
        self.assertFalse(data['descriptions']['positive'])
        self.assertEqual(data['source']['positive'], ['踩踏'])
        self.assertIsNone(from_attempts([{'status':'pass'}]))
        self.assertIsNone(from_attempts([{'drawing_signals':{'version':'future'}}]))

    def test_invalid_comment_is_non_fatal(self):
        # The contract is "never crash, always report", on every Python version.
        for broken in ('<html><![oops]><svg></svg></html>',
                       '<html><!-- unterminated <svg></svg></html>'):
            with self.subTest(broken=broken):
                data = analyze(broken, DRAWING_PROFILE)
                self.assertTrue(data['description_parse_error'])
                self.assertEqual(data['version'], VERSION)

    def test_well_formed_source_is_not_flagged_as_a_parse_error(self):
        data = analyze('<!doctype html><html><!-- ok --><svg></svg></html>', DRAWING_PROFILE)
        self.assertFalse(data['description_parse_error'])

    def test_new_observations_preserve_pass_and_publish_compact_history(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.sync([{'id':101,'name':'fixture','platform':'openai','type':'apikey'}],hour(),'manual')
            run = next(r for r in store.pending(hour()) if r['kind']=='drawing')
            api = Mock()
            api.account.return_value = {'id':101,'platform':'openai','type':'apikey'}
            text = '我会用内联 SVG 绘制循环动画。\n'+HTML
            request = Mock(return_value={'text':text})
            router = Mock()
            execute_run(store, api, run, time.monotonic()+3000, request, sleep=lambda _:None, quality_router=router)
            request.assert_called_once()
            router.reconcile.assert_not_called()
            detail = json.loads((store.public/(run['id']+'.json')).read_text())
            self.assertEqual(detail['status'], 'pass')
            self.assertEqual(detail['instructions'], INSTRUCTIONS['drawing'])
            self.assertEqual(detail['response'], text)
            self.assertEqual(detail['drawing_signals']['state'], 'risk_terms')
            self.assertEqual(detail['drawing_signals']['version'], VERSION)
            self.assertEqual((store.public/(run['id']+'.html')).read_text(), HTML)
            store.publish_state()
            state = json.loads((store.public/'state.json').read_text())
            row = next(r for r in state['accounts'][0]['history']['drawing'] if r.get('id')==run['id'])
            self.assertEqual(row['drawing_signals'], detail['drawing_signals'])
            self.assertNotIn('attempt_log', row)
            self.assertNotIn('response', row)

    def test_old_runs_do_not_claim_new_prompt_or_observation(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.sync([{'id':101,'name':'fixture'}],hour(),'manual')
            run = next(r for r in store.pending(hour()) if r['kind']=='drawing')
            store.update(run['id'], status='pass', response=HTML, html=HTML, attempt_log='[{"status":"pass"}]')
            detail = json.loads((store.public/(run['id']+'.json')).read_text())
            self.assertIsNone(detail['drawing_signals'])
            self.assertIsNone(detail['drawing_profile'])
            self.assertEqual(detail['instructions'], LEGACY_DRAWING_INSTRUCTIONS)


if __name__ == '__main__':
    unittest.main()
