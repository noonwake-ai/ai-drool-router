"""Question contract and two-stage confirmation. All responses below are offline fixtures."""
import json
import re
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from detector.monitor import Store, execute_run, hour, public_id
from detector.prompts import BENCHMARKS, CANDY_PROMPT, DRAWING_PROMPT
from detector.quality_routing import definite_verdict
from detector.question_bank import CONFIRMATION, ORIGINAL, QUESTIONS, TEMPLATES, VERSION, grade, select_question
from detector.upstream import ProbeError, Sub2API, build_request

# Inline fixtures keep the json/choice grading paths covered without shipping a
# question set this deployment does not use.
MONEY_FIXTURE = {'id':'fixture-money','template':'fixture-money','family':'fixture',
                 'title':'金额复核','prompt':'两人各出几元？','instructions':'','kind':'number',
                 'expected':'10','unit':'元','source':'offline fixture','version':VERSION}
JSON_FIXTURE = {'id':'fixture-json','template':'fixture-json','family':'fixture',
                'title':'结构题','prompt':'返回 JSON。','instructions':'','kind':'json',
                'expected':{'ok':True,'count':2},'source':'offline fixture','version':VERSION}
CHOICE_FIXTURE = {'id':'fixture-choice','template':'fixture-choice','family':'fixture',
                  'title':'判断题','prompt':'选 A/B/C。','instructions':'','kind':'choice',
                  'expected':'B','choices':['A','B','C'],'source':'offline fixture','version':VERSION}


class BankTests(unittest.TestCase):
    def test_explicit_allowlist_and_original(self):
        self.assertEqual(TEMPLATES,('original-candy',))
        self.assertEqual(QUESTIONS,[ORIGINAL])
        self.assertEqual(ORIGINAL['prompt'],CANDY_PROMPT)
        self.assertEqual(ORIGINAL['expected'],'21')
        self.assertTrue(all(q['kind'] in ('number','choice','json') for q in QUESTIONS))

    def test_supplier_inventory_requires_complete_pages(self):
        api=Sub2API('https://example.test','fake')
        entries=[{'id':i,'name':str(i),'platform':'openai','group_ids':[4]} for i in range(1,102)]
        api.get=Mock(side_effect=[{'items':entries[:100],'total':101},{'items':entries[100:],'total':101}])
        self.assertEqual(len(api.accounts()),101)
        for pages in ([{'items':entries[:10],'total':101}],
                      [{'items':entries[:100],'total':101},{'items':[],'total':101}],
                      [{'items':entries[:100],'total':101},{'items':entries[100:],'total':102}],
                      [{'items':[entries[0],entries[0]],'total':2}]):
            api.get=Mock(side_effect=pages)
            with self.assertRaises(ProbeError):api.accounts()

    def test_primary_and_review_always_use_independent_copies_of_original(self):
        for slot in range(0,864000,2700):
            first=select_question(slot)
            second=select_question(slot,first)
            self.assertEqual(first,ORIGINAL)
            self.assertEqual(second,ORIGINAL)
            self.assertIsNot(first,second)
            first['prompt']='changed fixture'
            self.assertEqual(second['prompt'],CANDY_PROMPT)
            self.assertEqual(select_question(slot,JSON_FIXTURE),ORIGINAL)

    def test_every_answer_can_be_graded_and_wrong_answer_is_not_passed(self):
        for q in (ORIGINAL,MONEY_FIXTURE,JSON_FIXTURE,CHOICE_FIXTURE):
            if q['kind']=='json':
                good=json.dumps(q['expected']); bad=json.dumps({**q['expected'],'ok':False})
            elif q['kind']=='choice':
                good='最终答案：'+q['expected'];bad='最终答案：'+next(c for c in q['choices'] if c!=q['expected'])
            elif q['kind']=='number' and q['id']==MONEY_FIXTURE['id']:
                good='最终答案：10 元';bad='最终答案：11 元'
            else:
                good='最终答案：'+q['expected'];bad='最终答案：9999'
            with self.subTest(q=q['id']):
                self.assertEqual(grade(q,good)[0],'pass')
                self.assertEqual(grade(q,bad)[0],'fail')
                self.assertEqual(grade(q,'我无法确定答案')[0],'unknown')

    def test_numeric_parser_never_truncates_or_uses_reasoning_numbers(self):
        for text in ('21/0','最终答案：21abc','最终答案：21 或 29','推理中有21\n但没有最终结论'):
            self.assertEqual(grade(ORIGINAL,text)[0],'unknown')
        self.assertEqual(grade(ORIGINAL,'最终答案：21.5')[0],'fail')
        self.assertEqual(grade(ORIGINAL,'最终答案：-21')[0],'fail')
        self.assertEqual(grade(ORIGINAL,'最终答案：29\n上面讨论过21')[0],'fail')
        self.assertEqual(grade(MONEY_FIXTURE,'最终答案：10 元')[0],'pass')
        self.assertEqual(grade(MONEY_FIXTURE,'最终答案：1/20 元')[0],'fail')
        self.assertEqual(grade(ORIGINAL,'最终答案：**21**')[0],'pass')

    def test_json_type_and_duplicate_keys_are_not_accepted(self):
        q=JSON_FIXTURE
        self.assertEqual(grade(q,json.dumps({**q['expected'],'ok':1}))[0],'fail')
        self.assertEqual(grade(q,'{"ok":true,"ok":false}')[0],'unknown')
        self.assertEqual(grade(q,'```json\n'+json.dumps(q['expected'])+'\n```')[0],'unknown')

    def test_choice_answers_need_one_letter_and_are_never_inferred(self):
        self.assertEqual(grade(CHOICE_FIXTURE,'最终答案：B'),('pass','B'))
        self.assertEqual(grade(CHOICE_FIXTURE,'最终答案：A'),('fail','A'))
        self.assertEqual(grade(CHOICE_FIXTURE,'最终答案：D'),('unknown',None))
        self.assertEqual(grade(CHOICE_FIXTURE,'最终答案：B（也许）'),('unknown',None))

    def test_candy_natural_and_latex_conclusions(self):
        cases = [
            '最少需要取出 **21 个糖果**。\n\n下面说明理由。',
            '**最少取出 21 个：选择 9 个圆形糖果和 12 个五角星形糖果。**\n\n下面说明理由。',
            r'答案是 **\(\boxed{21\text{ 个}}\)**。',
            r'最终答案：\(\boxed{21\text{ 个}}\)',
            '所以最少需要\n\\[\n\\boxed{21\\text{ 个}}。\n\\]',
            '推导里讨论了 20 和 29。\n\n因此，最少需要 21 个。',
            '最少取出 21 个。\n\n如果不允许按形状选择，答案则是 29 个。',
            '最少取出 21 个。\n\n如果不允许按形状选择，答案是 \\boxed{29}。',
            '取 9 个圆形和 12 个五角星形。\n\n\\[\\boxed{21}\\]',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(grade(ORIGINAL,text),('pass','21'))
        for text in ('最少取出 29 个。',r'答案是 \(\boxed{29\text{ 个}}\)。'):
            self.assertEqual(grade(ORIGINAL,text),('fail','29'))

    def test_candy_compatibility_does_not_guess(self):
        cases = [
            '最少取出 21 或 29 个。', '最少取出 21 个或者 29 个。',
            '最少取出 21 个。\n\n答案是 29 个。',
            '最少取出 21 个。\n\n我不能确定最终答案。',
            '如果能按形状选，答案是 21 个。\n\n如果不能，答案是 29 个。',
            '推理中讨论过 21 个。\n\n这还不是结论。',
            '假设答案是 \\boxed{21}。\n\n仍需验证。',
            '最少取出 21 个，这个答案不对。',
            '最少取出 21abc。', '最少取出 21+8 个。', '答案是 21.5abc。',
            r'答案是 \boxed{21', '答案是 21，但正确答案是 29。',
            '最少取出 21/0 个。', '最少取出 21 个可能是不够的。',
            '> 答案是 21 个。\n\n这是别人说的。',
            '```\n答案是 21 个。\n```\n我没有结论。',
            '```\n最终答案：21\n```\n我没有结论。',
            '> 最终答案：21\n我没有结论。',
            '最终答案：21 或 29\n\n答案是 21 个。',
            '最终答案：21\n最终答案：29',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(grade(ORIGINAL,text),('unknown',None))

    def test_candy_quantity_units_and_minimum_conclusions(self):
        for unit in ('个','颗','粒','颗糖','颗糖果','粒糖果'):
            for prefix in ('最终答案：','最少取出','故最少为 ','因此，最少是 '):
                for answer,status in (('21','pass'),('29','fail')):
                    text=f'{prefix}**{answer}{unit}**。'
                    with self.subTest(text=text):
                        self.assertEqual(grade(ORIGINAL,text),(status,answer))
        for text in (r'最终答案：\(\boxed{21\text{ 颗糖果}}\)',
                     r'故最少为 **\(\boxed{21}\) 个**。',
                     '**最少取出21颗**，方法是取9颗圆形和12颗五角星形。\n\n若不能按形状选，则需要29颗。',
                     '按这种理解，答案是21个。\n\n故最少为 **\\(\\boxed{21}\\) 个**。\n\n如果不能按形状选，答案是29个。'):
            with self.subTest(text=text):
                self.assertEqual(grade(ORIGINAL,text),('pass','21'))

    def test_new_candy_units_do_not_loosen_ambiguity_guards(self):
        for text in ('最少取出21颗或者29颗。','故最少为21颗，但正确答案是29颗。',
                     '故最少为21颗。\n\n答案是29颗。','故最少为21颗abc。',
                     '最少取出21颗+8颗。','最少取出21颗可能不够。',
                     '> 故最少为21颗。','```\n最终答案：21颗\n```\n没有结论。',
                     '假设最少为21颗。','如果能选择形状，最少取出21颗。',
                     '故最少为21颗，答案有误。','推理中讨论了21颗，并未得出结论。'):
            with self.subTest(text=text):
                self.assertEqual(grade(ORIGINAL,text),('unknown',None))

    def test_spaced_units_parentheses_and_final_clauses_are_answer_neutral(self):
        templates = [
            '最少取出 **{n}个** 糖果。',
            '最少取出 **{n} 颗**\n糖果。',
            '最终答案：{n} 粒 糖果',
            r'最终答案：\(\boxed{{{n}\text{{ 个 糖果}}}}\)',
            '**最少取出 {n} 颗**（利用题目条件，可以凭手感选择形状）。',
            '答案是 **{n} 个** (利用形状可辨条件)。',
            '所以更少不能保证，答案为 **{n} 个**。',
            '因此，更少仍可能失败，最少就是 **{n} 个**。',
            '利用手感区分形状、按形状选取，最少需要取出 {n} 个。',
        ]
        for template in templates:
            for n in ('21','29','20','210','21.5'):
                with self.subTest(template=template,n=n):
                    self.assertEqual(grade(ORIGINAL,template.format(n=n)),
                                     ('pass' if n=='21' else 'fail',n))

    def test_final_clause_guards_and_conflicts_remain_ungraded(self):
        cases = [
            '最少取出 21个 糖果，但正确答案为29。',
            '最少取出 21 个（这个答案有误）。',
            '最少取出 21 个（也许不够）。',
            '最少取出 21 个（利用形状可辨条件）+8。',
            '最少取出 21 个（说明未结束。',
            '最少取出 21 个 糖果abc。',
            '最少取出 21 个 糖果+8。',
            '所以，答案为21个，最少为29个。',
            '答案是21个（利用形状可辨条件）。\n\n答案是29个。',
            '我不能确定，答案是21个。',
            '假设取出一些糖果，答案是21个。',
            '因为条件不明确，如果能选择形状，答案是21个。',
            '这里引用“分析过程，答案是21个”。',
            '这是错误的推断，答案为21个。',
            '> 所以更少不够，答案为21个。',
            '```\n所以更少不够，答案为21个。\n```',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(grade(ORIGINAL,text),('unknown',None))

    def test_wrong_spaced_answer_is_not_overridden_by_reasoning(self):
        text = '最少取出 **29个** 糖果。\n\n讨论过21，但没有采用。\n\n\\[\\boxed{28+1=29}\\]'
        self.assertEqual(grade(ORIGINAL,text),('fail','29'))
        self.assertEqual(grade(ORIGINAL,'推导得到28+1=29，讨论过21。'),('unknown',None))

    def test_proof_possibilities_do_not_erase_explicit_conclusion(self):
        for n in ('21','29'):
            text=f'答案是 **{n} 个**。\n\n最坏情况只可能是：\n- 先取西瓜。\n- 再取苹果。\n\n因此最少需要取出 {n} 个糖果。'
            self.assertEqual(grade(ORIGINAL,text),('pass' if n=='21' else 'fail',n))
        self.assertEqual(grade(ORIGINAL,'答案是21个。\n\n我不能确定最终答案。'),('unknown',None))
        self.assertEqual(grade(ORIGINAL,'也许答案是21个。'),('unknown',None))

    def test_labeled_minimum_and_checked_addition_conclusions(self):
        for n in (21,29):
            for prefix in ('答案：','最终答案：'):
                text=f'**{prefix}最少取出 {n} 个。**\n\n所以最少取 **{n-1} + 1 = {n}** 个。'
                self.assertEqual(grade(ORIGINAL,text),('pass' if n==21 else 'fail',str(n)))
        for text in ('答案：最少取出21个或29个。',
                     '答案是21个。\n\n所以最少取28+1=29个。',
                     '所以最少取28+1=21个。','所以最少取21+8个。',
                     '所以最少取28+1=29-8个。'):
            self.assertEqual(grade(ORIGINAL,text),('unknown',None))

    def test_empty_relay_envelope_suffix_is_strict_and_answer_neutral(self):
        for n in ('21','29'):
            self.assertEqual(grade(ORIGINAL,f'证明略。\n\n最终答案：{n}","calls":[]}}'),
                             ('pass' if n=='21' else 'fail',n))
        for slot in ('21","calls":[{"answer":29}]}','21","calls":[],"extra":29}',
                     '21","calls":[],"calls":[]}', '21","calls":[]',
                     r'21","ca\u006cls":[],"calls":[]}',
                     '21","answer":"29","calls":[]}', '21 或 29","calls":[]}'):
            self.assertEqual(grade(ORIGINAL,'最终答案：'+slot),('unknown',None))

    def test_question_override_is_isolated_and_drawing_is_unchanged(self):
        for platform in BENCHMARKS:
            for kind in ('candy','drawing'):
                account={'platform':platform,'type':'apikey','status':'active','credentials':{'api_key':'fake'}}
                _,_,body=build_request(account,kind,prompt='private question fixture',instructions='private instruction fixture')
                self.assertIn('private question fixture',json.dumps(body))
                self.assertNotIn(CANDY_PROMPT,json.dumps(body,ensure_ascii=False))
                _,_,ordinary=build_request(account,'drawing')
                self.assertIn(DRAWING_PROMPT,json.dumps(ordinary,ensure_ascii=False))


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.slot=hour()
        self.account={'id':101,'name':'Offline fixture','platform':'openai'}
        self.store.sync([self.account],self.slot,'timer')
        self.run=next(r for r in self.store.pending(self.slot) if r['kind']=='candy')
        self.reload()
        self.router=Mock()

    def reload(self):
        with self.store.db() as db:self.run=dict(db.execute('SELECT * FROM runs WHERE id=?',(self.run['id'],)).fetchone())

    def execute(self,replies):
        request=Mock(side_effect=replies)
        execute_run(self.store,Mock(),self.run,time.monotonic()+3000,request,sleep=lambda _:None,quality_router=self.router)
        self.reload()
        return request,json.loads((self.store.public/(self.run['id']+'.json')).read_text())

    def test_two_correct_requests_are_required_before_passing(self):
        def reply(*args,**kwargs):
            self.reload()
            self.assertEqual(self.run['status'],'running')
            self.router.reconcile.assert_not_called()
            return {'text':'最终答案：21'}
        request,detail=self.execute(reply)
        self.assertEqual((request.call_count,detail['status'],detail['first_status']),(2,'pass','pass'))
        self.assertEqual(len({c.kwargs['request_id'] for c in request.call_args_list}),2)
        self.router.reconcile.assert_called_once()
        self.assertTrue(definite_verdict(self.run))

    def test_first_wrong_stops_without_spending_a_confirmation(self):
        request,detail=self.execute([{'text':'最终答案：29'},{'text':'最终答案：21'}])
        self.assertEqual((request.call_count,detail['status'],detail['first_status']),(1,'fail','fail'))
        self.assertEqual(request.call_args.kwargs['prompt'],CANDY_PROMPT)
        self.assertEqual([r['status'] for r in detail['question_results']],['fail'])
        self.assertEqual(detail['attempts'],1)
        self.router.reconcile.assert_called_once()
        self.assertTrue(definite_verdict(self.run))

    def test_compatible_answers_still_need_two_fresh_requests(self):
        request,detail=self.execute([
            {'text':'最少需要取出 **21 个糖果**。\n\n下面说明理由。'},
            {'text':r'所以最少需要 \(\boxed{21\text{ 个}}\)。'},
        ])
        self.assertEqual((request.call_count,detail['status']),(2,'pass'))
        self.assertEqual(len({call.kwargs['request_id'] for call in request.call_args_list}),2)
        self.assertEqual([r['answer'] for r in detail['question_results']],['21','21'])
        self.assertTrue(definite_verdict(self.run))

    def test_compatible_first_wrong_still_stops_immediately(self):
        request,detail=self.execute([{'text':'最少取出 29 个。'},{'text':'最终答案：21'}])
        self.assertEqual((request.call_count,detail['status']),(1,'fail'))

    def test_spaced_wrong_answer_stops_and_reaches_quality_router(self):
        request,detail=self.execute([{'text':'最少取出 **29个** 糖果。'},{'text':'最终答案：21'}])
        self.assertEqual((request.call_count,detail['status'],detail['answer']),(1,'fail',29))
        self.assertTrue(definite_verdict(self.run))
        self.router.reconcile.assert_called_once()

    def test_parenthetical_answer_still_needs_independent_confirmation(self):
        request,detail=self.execute([
            {'text':'答案是21个（利用形状可辨条件）。'},
            {'text':'因此，更少仍可能失败，最少就是21个。'},
        ])
        self.assertEqual((request.call_count,detail['status']),(2,'pass'))
        self.assertEqual(len({call.kwargs['request_id'] for call in request.call_args_list}),2)

    def test_quantity_unit_compatibility_still_requires_two_independent_answers(self):
        request,detail=self.execute([{'text':'最少取出21颗。'},{'text':r'故最少为 \(\boxed{21}\) 个。'}])
        self.assertEqual((request.call_count,detail['status']),(2,'pass'))
        self.assertEqual(len({call.kwargs['request_id'] for call in request.call_args_list}),2)
        self.assertTrue(definite_verdict(self.run))

    def test_quantity_unit_wrong_answer_still_stops_immediately(self):
        request,detail=self.execute([{'text':'最少取出29颗。'},{'text':'最终答案：21'}])
        self.assertEqual((request.call_count,detail['status']),(1,'fail'))
        self.assertTrue(definite_verdict(self.run))

    def test_correct_then_wrong_is_a_failure(self):
        request,detail=self.execute([{'text':'最终答案：21'},{'text':'最终答案：29'}])
        self.assertEqual((request.call_count,detail['status'],detail['first_status']),(2,'fail','pass'))
        self.assertTrue(definite_verdict(self.run))

    def test_routing_error_after_wrong_answer_never_resends_the_question(self):
        for error in (RuntimeError('offline'),ProbeError('ROUTING_HTTP_503','offline')):
            with self.subTest(error=type(error).__name__):
                self.router.reconcile.side_effect=error
                request=Mock(return_value={'text':'最终答案：29'})
                with self.assertRaises(type(error)):
                    execute_run(self.store,Mock(),self.run,time.monotonic()+3000,request,
                                sleep=lambda _:None,quality_router=self.router)
                request.assert_called_once()
                with self.store.db() as db:
                    saved=dict(db.execute('SELECT * FROM runs WHERE id=?',(self.run['id'],)).fetchone())
                self.assertEqual(saved['status'],'fail')
                self.assertTrue(definite_verdict(saved))

    def test_first_wrong_short_circuits_all_four_platforms(self):
        for platform in ('openai','anthropic','gemini','grok'):
            with self.subTest(platform=platform),tempfile.TemporaryDirectory() as root:
                store=Store(root)
                store.sync([{**self.account,'platform':platform}],self.slot,'timer')
                run=next(r for r in store.pending(self.slot) if r['kind']=='candy')
                request=Mock(return_value={'text':'最终答案：29'})
                execute_run(store,Mock(),run,time.monotonic()+3000,request,sleep=lambda _:None)
                request.assert_called_once()
                with store.db() as db:
                    saved=dict(db.execute('SELECT * FROM runs WHERE id=?',(run['id'],)).fetchone())
                self.assertEqual(saved['status'],'fail')
                self.assertEqual(saved['attempts'],1)

    def test_intervening_request_error_does_not_count_as_consecutive_passes(self):
        request,detail=self.execute([{'text':'最终答案：21'},ProbeError('HTTP_503','offline'),{'text':'最终答案：21'}])
        self.assertEqual((request.call_count,detail['status'],detail['error_code']),(3,'error','NON_CONSECUTIVE_PASSES'))
        self.assertFalse(definite_verdict(self.run))
        self.router.reconcile.assert_not_called()

    def test_initial_error_then_two_consecutive_passes_can_pass(self):
        request,detail=self.execute([ProbeError('HTTP_503','offline'),{'text':'最终答案：21'},{'text':'最终答案：21'}])
        self.assertEqual((request.call_count,detail['status']),(3,'pass'))
        self.assertTrue(definite_verdict(self.run))

    def test_old_pending_plan_waits_for_next_round_without_request_or_rewrite(self):
        old={**ORIGINAL,'version':'original-candy-v1-20260914'}
        self.store.update(self.run['id'],bank_version=old['version'],question_plan=json.dumps([old]))
        self.reload()
        request,detail=self.execute([])
        request.assert_not_called()
        self.assertEqual(detail['error_code'],'QUALITY_POLICY_CHANGED')
        self.assertEqual(detail['bank_version'],old['version'])
        self.assertEqual(detail['questions'],[old])

    def test_request_error_then_wrong_stops_without_confirmation(self):
        request,detail=self.execute([ProbeError('HTTP_503','offline'),{'text':'最终答案：29'}])
        self.assertEqual((request.call_count,detail['status']),(2,'fail'))
        self.assertNotEqual(request.call_args_list[0].kwargs['request_id'],request.call_args_list[1].kwargs['request_id'])
        self.assertEqual(len(detail['question_results']),1)
        self.router.reconcile.assert_called_once()
        self.assertTrue(definite_verdict(self.run))

    def test_two_request_errors_end_run_before_third_attempt(self):
        err=ProbeError('HTTP_503','offline error')
        request,detail=self.execute([err,err,{'text':'最终答案：29'},err,err,err])
        self.assertEqual((request.call_count,detail['status'],detail['first_status']),(2,'error',None))
        self.assertEqual([a['stage_attempt'] for a in detail['attempt_log']],[1,2])
        self.assertEqual(len({a['request_id'] for a in detail['attempt_log']}),2)
        self.assertEqual(len({c.kwargs['request_id'] for c in request.call_args_list}),2)
        self.router.reconcile.assert_called_once()

    def test_fresh_context_headers_payload_and_oauth_session_each_time(self):
        for platform in BENCHMARKS:
            for auth in (('oauth','apikey') if platform in ('openai','grok') else ('apikey',)):
                account={'platform':platform,'type':auth,'status':'active','credentials':{'api_key':'fake','access_token':'fake'}}
                _,h1,b1=build_request(account,'candy')
                _,h2,b2=build_request(account,'candy')
                self.assertNotEqual(h1['X-Client-Request-Id'],h2['X-Client-Request-Id'])
                self.assertNotEqual(b1,b2)
                self.assertIn(h1['X-Client-Request-Id'],json.dumps(b1))
                for forbidden in ('previous_response_id','conversation','cachedContent','cache_control','prompt_cache_key'):
                    self.assertNotIn(forbidden,json.dumps(b1))
                self.assertEqual(h1['Cache-Control'],'no-cache, no-store')
                if platform=='openai' and auth=='oauth':self.assertNotEqual(h1['session_id'],h2['session_id'])

    def test_unknown_answer_does_not_trigger_review(self):
        request,detail=self.execute([{'text':'推导里有21，但我不能确定最终答案'}])
        self.assertEqual((request.call_count,detail['status'],detail['error_code']),(1,'error','UNGRADABLE_ANSWER'))
        self.router.reconcile.assert_not_called()

    def test_pause_after_first_response_prevents_confirmation(self):
        def reply(*args,**kwargs):
            self.store.controls.set_paused(public_id(101),True)
            return {'text':'最终答案：21'}
        request,detail=self.execute(reply)
        self.assertEqual((request.call_count,detail['status']),(1,'paused'))
        self.router.reconcile.assert_not_called()

    def test_restart_after_persisted_wrong_answer_does_not_issue_a_request(self):
        first={'question_id':ORIGINAL['id'],'status':'fail','answer':'29','response':'最终答案：29'}
        self.store.update(self.run['id'],question_plan=json.dumps([ORIGINAL,ORIGINAL]),question_results=json.dumps([first]),
                          first_status='fail',attempt_log=json.dumps([{'attempt':1,'stage':0,'status':'fail','question_id':ORIGINAL['id'],'request_id':'first'},
                                                                   {'attempt':2,'stage':1,'status':'running','request_id':'interrupted'}]))
        self.reload()
        request,detail=self.execute([{'text':'最终答案：21'}])
        request.assert_not_called()
        self.assertEqual(len(detail['attempt_log']),2)
        self.assertEqual(detail['status'],'fail')
        self.assertTrue(definite_verdict(self.run))
        self.router.reconcile.assert_called_once()

    def test_published_contract_has_one_question_and_existing_ranking(self):
        self.store.publish_state()
        state=json.loads((self.store.public/'state.json').read_text())
        self.assertEqual(state['question_bank'],{'version':VERSION,'templates':1,
                         'confirmation':CONFIRMATION,'failure':'first_wrong_answer_stops','ranking':'first_answer_rate'})

    def test_compatible_answers_keep_two_fresh_requests_and_fail_fast(self):
        request,detail=self.execute([
            {'text':'答案是21个。\n\n最坏情况只可能是：先取西瓜。'},
            {'text':'最终答案：21","calls":[]}'}])
        self.assertEqual((request.call_count,detail['status']),(2,'pass'))
        ids=[call.kwargs['request_id'] for call in request.call_args_list]
        self.assertEqual(len(set(ids)),2)

    def test_labeled_wrong_answer_stops_without_confirmation(self):
        request,detail=self.execute([{'text':'答案：最少取出29个。\n\n所以最少取28+1=29个。'}])
        self.assertEqual((request.call_count,detail['status']),(1,'fail'))

    def test_old_completed_question_evidence_is_not_rewritten(self):
        old={**MONEY_FIXTURE,'version':'closed-v1-dec7c5b1'}
        self.store.update(self.run['id'],status='pass',bank_version=old['version'],question_plan=json.dumps([old]),
                          question_results=json.dumps([{'question_id':old['id'],'status':'pass','answer':'10'}]))
        self.store.sync([self.account],None,'metadata')
        self.store.publish_run(self.run['id'])
        detail=json.loads((self.store.public/(self.run['id']+'.json')).read_text())
        self.assertEqual(detail['questions'],[old])
        self.assertEqual(detail['bank_version'],old['version'])

    def test_metadata_renames_history_preserves_pause_and_adds_without_spend(self):
        self.execute([{'text':'最终答案：21'},{'text':'最终答案：21'}])
        self.store.controls.set_paused(public_id(101),True)
        self.store.sync([{**self.account,'name':'Renamed fixture'},{'id':102,'name':'New fixture'}],None,'metadata')
        self.store.publish_state()
        detail=json.loads((self.store.public/(self.run['id']+'.json')).read_text())
        state=json.loads((self.store.public/'state.json').read_text())
        self.assertEqual(detail['name'],'Renamed fixture')
        self.assertTrue(state['accounts'][0]['paused'])
        self.assertEqual(len(state['accounts']),2)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM runs WHERE account_id=102').fetchone()[0],0)

    def test_historical_effort_is_not_rewritten_by_metadata(self):
        with self.store.db() as db:db.execute("UPDATE runs SET effort='medium',platform='grok' WHERE id=?",(self.run['id'],))
        self.store.sync([{**self.account,'platform':'grok'}],None,'metadata')
        self.reload()
        self.assertEqual(self.run['effort'],'medium')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT effort FROM accounts').fetchone()[0],BENCHMARKS['grok']['effort'])

    def test_unconfirmed_current_bank_failure_cannot_disable(self):
        row={**self.run,'status':'fail','bank_version':VERSION,'question_results':json.dumps([{'question_id':'one','status':'fail'}])}
        self.assertFalse(definite_verdict(row))
