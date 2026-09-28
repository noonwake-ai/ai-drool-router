# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Offline provider and pause contracts. Never load real credentials or call upstreams."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from detector import config
from detector.controls import ControlConflict, Controls
from detector.monitor import Store, execute_run, hour, public_id
from detector.prompts import BENCHMARKS, CANDY_PROMPT, DRAWING_PROMPT
from detector.server import make_server
from detector.upstream import CLAUDE_VERSION, GROK_VERSION, OAUTH_QUOTA_ERROR, ProbeError, Sub2API, build_request, oauth_quota_state, parse_events, probe, request_budget


def native(events):
    return parse_events(['data: '+json.dumps(e) for e in events],time.monotonic()+10)


class ProviderTests(unittest.TestCase):
    def account(self,platform,auth='apikey'):
        return {'platform':platform,'type':auth,'status':'active',
                'credentials':{'api_key':'offline-key','access_token':'offline-token'}}

    def test_oauth_quota_uses_sub2api_threshold_reason_at_97_percent(self):
        now=1_800_000_000
        account=self.account('openai','oauth')
        account.update(temp_unschedulable_reason=json.dumps({
            'source':'account_scheduling_threshold','window':'7d',
            'used_percent':97,'threshold_percent':97,'until_unix':now+3600}))
        state=oauth_quota_state(account,now)
        self.assertTrue(state['limited'])
        self.assertEqual(state['code'],OAUTH_QUOTA_ERROR)
        self.assertEqual(state['window'],'7d')
        self.assertEqual(state['threshold_percent'],97)

    def test_oauth_quota_ignores_expired_threshold_reason_and_reset_window(self):
        now=1_800_000_000
        account=self.account('openai','oauth')
        account.update(temp_unschedulable_reason=json.dumps({
            'source':'account_scheduling_threshold','window':'5h',
            'used_percent':99,'threshold_percent':97,'until_unix':now-1}))
        account['extra']={'codex_5h_used_percent':99,'codex_5h_reset_at':now-1}
        self.assertFalse(oauth_quota_state(account,now)['limited'])

    def test_oauth_quota_fallback_uses_per_account_threshold_snapshot(self):
        now=1_800_000_000
        account=self.account('openai','oauth')
        account['credentials']['account_scheduling_threshold']=96
        account['extra']={'codex_7d_used_percent':96,
                          'codex_7d_reset_at':now+3600}
        state=oauth_quota_state(account,now)
        self.assertTrue(state['limited'])
        self.assertEqual(state['window'],'7d')
        self.assertEqual(state['threshold_percent'],96)

    def test_oauth_quota_does_not_treat_quality_pause_or_api_key_as_quota(self):
        now=1_800_000_000
        quality_paused=self.account('openai','oauth')
        quality_paused.update(schedulable=False)
        self.assertFalse(oauth_quota_state(quality_paused,now)['limited'])
        api_key=self.account('openai','apikey')
        api_key['extra']={'codex_7d_used_percent':100,'codex_7d_reset_at':now+3600}
        self.assertFalse(oauth_quota_state(api_key,now)['limited'])

    def test_claude_native_request_exact_model_and_prompt(self):
        url,headers,body=build_request(self.account('anthropic'),'candy')
        self.assertEqual(url,'https://api.anthropic.com/v1/messages?beta=true')
        self.assertEqual(headers['anthropic-version'],'2023-06-01')
        self.assertEqual(headers['x-api-key'],'offline-key')
        self.assertEqual(body['model'],'claude-opus-5-5')
        self.assertEqual(body['messages'][0]['content'][0]['text'],CANDY_PROMPT)
        self.assertNotIn('21',body['system'][1]['text'].split('\n',1)[1])
        self.assertNotIn('reasoning',body)
        self.assertEqual(body['output_config'],{'effort':'medium'})
        self.assertEqual(headers['User-Agent'],f'claude-cli/{CLAUDE_VERSION} (external, cli)')
        self.assertEqual(headers['X-App'],'cli')
        self.assertIn('claude-code-20250219',headers['anthropic-beta'])
        self.assertNotIn('oauth',headers['anthropic-beta'])
        metadata=json.loads(body['metadata']['user_id'])
        self.assertEqual(metadata['account_uuid'],'')
        self.assertEqual(len(metadata['device_id']),64)
        self.assertNotIn('tools',body)
        self.assertNotIn('fallbacks',body)

    def test_claude_drawing_preserves_prompt_and_auth_scheme(self):
        account=self.account('anthropic')
        account['credentials']['base_url']='https://example.test/v1'
        account['extra']={'anthropic_apikey_auth_scheme':'authorization_bearer'}
        url,headers,body=build_request(account,'drawing')
        self.assertEqual(url,'https://example.test/v1/messages?beta=true')
        self.assertEqual(body['messages'][0]['content'][0]['text'],DRAWING_PROMPT)
        self.assertEqual(body['model'],'claude-opus-5-5')
        self.assertEqual(headers['Authorization'],'Bearer offline-key')
        self.assertNotIn('x-api-key',headers)
        self.assertEqual(body['output_config']['effort'],'medium')

    def test_gemini_native_request_exact_model_and_prompt(self):
        url,headers,body=build_request(self.account('gemini'),'drawing')
        self.assertEqual(url,'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse')
        self.assertEqual(headers['x-goog-api-key'],'offline-key')
        self.assertEqual(body['contents'][0]['parts'][0]['text'],DRAWING_PROMPT)
        self.assertNotIn('offline-key',url)
        self.assertEqual(body['generationConfig']['thinkingConfig'],{'thinkingLevel':'HIGH'})

    def test_gemini_candy_and_drawing_share_bounded_high_thinking_budget(self):
        ids=[]
        for kind,prompt in (('candy',CANDY_PROMPT),('drawing',DRAWING_PROMPT)):
            _,headers,body=build_request(self.account('gemini'),kind)
            self.assertEqual(body['generationConfig'],{'maxOutputTokens':32768,
                             'thinkingConfig':{'thinkingLevel':'HIGH'}})
            self.assertEqual(body['contents'],[{'role':'user','parts':[{'text':prompt}]}])
            self.assertNotIn('cachedContent',body)
            ids.append(headers['X-Client-Request-Id'])
        self.assertNotEqual(*ids)

    def test_gemini_high_allows_delayed_first_data_with_separate_total_budget(self):
        event={'modelVersion':'gemini-3.8-flash','candidates':[{
               'content':{'parts':[{'text':'最终答案：21'}]},'finishReason':'STOP'}]}
        response=Mock(status_code=200,headers={'Content-Type':'text/event-stream'})
        with patch('detector.upstream.time.monotonic',return_value=0) as clock:
            def chunks(*_,**__):
                clock.return_value=160
                yield ('data: '+json.dumps(event)+'\n\n').encode()
            response.iter_content.side_effect=chunks
            with patch('detector.upstream.requests.Session') as factory:
                session=factory.return_value.__enter__.return_value
                session.post.return_value.__enter__.return_value=response
                result=probe(self.account('gemini'),'candy')
                self.assertEqual(session.post.call_args.kwargs['timeout'],(12,300))
                self.assertEqual(session.post.call_count,1)
                self.assertEqual(result['text'],'最终答案：21')
                self.assertEqual(result['timings']['first_data_seconds'],160)
                self.assertEqual(result['timings']['total_budget_seconds'],900)
                self.assertEqual(result['timings']['finish_reason'],'STOP')
                self.assertEqual(result['timings']['max_output_tokens'],32768)
                self.assertEqual(result['timings']['thinking_level'],'HIGH')

    def test_gemini_timeout_never_extends_callers_remaining_budget(self):
        import requests
        for budget,read_limit in ((900,300),(250,250),(3,3)):
            with self.subTest(budget=budget),patch('detector.upstream.requests.Session') as factory:
                session=factory.return_value.__enter__.return_value
                session.post.side_effect=requests.ReadTimeout()
                with self.assertRaises(ProbeError) as exc:
                    probe(self.account('gemini'),'candy',timeout=budget)
                self.assertEqual(exc.exception.code,'FIRST_RESPONSE_TIMEOUT')
                self.assertEqual(session.post.call_args.kwargs['timeout'],(min(12,budget),read_limit))
                self.assertEqual(exc.exception.timings['idle_timeout_seconds'],read_limit)
                self.assertEqual(session.post.call_count,1)

    def test_gemini_token_limit_keeps_usage_and_never_accepts_partial_answer(self):
        event={'modelVersion':'gemini-3.8-flash','candidates':[{
               'content':{'parts':[{'thought':True,'text':'private thought'},{'text':'最终答案：21'}]},
               'finishReason':'MAX_TOKENS'}],
               'usageMetadata':{'promptTokenCount':240,'candidatesTokenCount':328,
                                'thoughtsTokenCount':7860,'totalTokenCount':8428}}
        for content_type in ('text/event-stream','application/json'):
            response=Mock(status_code=200,headers={'Content-Type':content_type})
            raw=json.dumps(event)
            response.iter_content.return_value=[(raw if content_type=='application/json' else 'data: '+raw+'\n\n').encode()]
            with self.subTest(content_type=content_type),patch('detector.upstream.requests.Session') as factory:
                session=factory.return_value.__enter__.return_value
                session.post.return_value.__enter__.return_value=response
                with self.assertRaises(ProbeError) as exc:probe(self.account('gemini'),'candy')
                self.assertEqual(exc.exception.code,'INCOMPLETE_RESPONSE')
                self.assertEqual(exc.exception.tokens['output_tokens'],8188)
                self.assertEqual(exc.exception.tokens['reasoning_tokens'],7860)
                self.assertEqual(exc.exception.timings['finish_reason'],'MAX_TOKENS')
                self.assertIn('预算 32768',exc.exception.message)
                self.assertIn('已生成 8188（含思考 7860）',exc.exception.message)
                self.assertNotIn('private thought',exc.exception.message)
                self.assertNotIn('21',exc.exception.message)
                self.assertEqual(session.post.call_count,1)

    def test_grok_oauth_and_api_have_distinct_hosts_and_identity(self):
        model=BENCHMARKS['grok']['model'];effort=BENCHMARKS['grok']['effort']
        url,headers,body=build_request(self.account('grok','oauth'),'candy')
        self.assertEqual(url,'https://cli-chat-proxy.grok.com/v1/responses')
        self.assertEqual(headers['X-XAI-Token-Auth'],'xai-grok-cli')
        self.assertEqual(headers['x-grok-client-version'],GROK_VERSION)
        self.assertEqual(headers['Authorization'],'Bearer offline-token')
        self.assertEqual(body['model'],model)
        self.assertEqual(body['reasoning'],{'effort':effort})
        url,headers,body=build_request(self.account('grok'),'candy')
        self.assertEqual(url,'https://api.x.ai/v1/responses')
        self.assertEqual(headers['Authorization'],'Bearer offline-key')
        self.assertNotIn('X-XAI-Token-Auth',headers)
        self.assertEqual(body['reasoning'],{'effort':effort})
        for auth_type in ('oauth','apikey'):
            _,_,drawing=build_request(self.account('grok',auth_type),'drawing')
            self.assertEqual(drawing['model'],model)
            self.assertEqual(drawing['reasoning'],{'effort':effort})

    def test_requested_effort_is_explicit_for_both_tasks_on_every_platform(self):
        for platform,config in BENCHMARKS.items():
            effort=config['effort']
            self.assertTrue(effort)
            for kind in ('candy','drawing'):
                url,_,body=build_request(self.account(platform),kind)
                if 'model' in body:self.assertEqual(body['model'],config['model'])
                if platform=='anthropic':self.assertEqual(body['output_config']['effort'],effort)
                elif platform=='gemini':
                    self.assertIn('models/'+config['model'],url)
                    self.assertEqual(body['generationConfig']['thinkingConfig']['thinkingLevel'],effort.upper())
                else:self.assertEqual(body['reasoning']['effort'],effort)

    def test_slow_reasoning_uses_requested_read_budget(self):
        events=[{'type':'message_start','message':{'model':'claude-opus-5'}},
                {'type':'content_block_delta','delta':{'type':'text_delta','text':'最终答案：21'}},
                {'type':'message_delta','delta':{'stop_reason':'end_turn'}},{'type':'message_stop'}]
        response=Mock(status_code=200,headers={'Content-Type':'text/event-stream'})
        response.iter_content.return_value=[('data: '+json.dumps(e)+'\n\n').encode() for e in events]
        with patch('detector.upstream.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value
            session.post.return_value.__enter__.return_value=response
            result=probe(self.account('anthropic'),'candy')
            self.assertEqual(result['text'],'最终答案：21')
            self.assertEqual(session.post.call_args.kwargs['timeout'],(12,120))
            self.assertEqual(session.post.call_args.kwargs['json']['output_config']['effort'],'medium')
            self.assertEqual(result['timings']['total_budget_seconds'],900)
            self.assertIn('first_data_seconds',result['timings'])
            self.assertIn('first_text_seconds',result['timings'])

    def test_short_read_budget_is_not_extended(self):
        import requests
        with patch('detector.upstream.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value
            session.post.side_effect=requests.ReadTimeout()
            with self.assertRaises(ProbeError) as exc:probe(self.account('anthropic'),'candy',timeout=3)
            self.assertEqual(exc.exception.code,'FIRST_RESPONSE_TIMEOUT')
            self.assertEqual(session.post.call_args.kwargs['timeout'],(3,3))
            self.assertEqual(session.post.call_count,1)

    def test_connect_timeout_reports_connect_budget_not_token_budget(self):
        import requests
        with patch('detector.upstream.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value
            session.post.side_effect=requests.ConnectTimeout()
            with self.assertRaises(ProbeError) as exc:probe(self.account('grok'),'drawing')
            self.assertEqual(exc.exception.code,'CONNECT_TIMEOUT')
            self.assertIn('12 秒',exc.exception.message)
            self.assertNotIn('first_data_seconds',exc.exception.timings)
            self.assertEqual(session.post.call_count,1)

    def test_idle_timeout_after_data_is_not_first_response_timeout(self):
        import requests
        from urllib3.exceptions import ReadTimeoutError
        response=Mock(status_code=200,headers={'Content-Type':'text/event-stream'})
        def chunks(*args):
            yield b'data: {"type":"response.created"}\n\n'
            raise requests.ConnectionError(ReadTimeoutError(None,None,'offline timeout'))
        response.iter_content.side_effect=chunks
        with patch('detector.upstream.requests.Session') as factory:
            factory.return_value.__enter__.return_value.post.return_value.__enter__.return_value=response
            with self.assertRaises(ProbeError) as exc:probe(self.account('grok'),'drawing')
            self.assertEqual(exc.exception.code,'STREAM_IDLE_TIMEOUT')
            self.assertIn('first_data_seconds',exc.exception.timings)
            self.assertNotIn('first_text_seconds',exc.exception.timings)

    def test_drawing_budget_widens_only_where_streaming_evidence_requires_it(self):
        self.assertEqual(request_budget('grok','drawing'),1500)
        self.assertEqual(request_budget('grok','candy'),900)
        for platform in ('openai','anthropic','gemini','doubao'):
            self.assertEqual(request_budget(platform,'drawing'),900)
            self.assertEqual(request_budget(platform,'candy'),900)
        self.assertEqual(request_budget('unknown','drawing',600),600)

    def test_streaming_budget_wall_is_client_truncation_not_upstream_outage(self):
        response=Mock(status_code=200,headers={'Content-Type':'text/event-stream'})
        with patch('detector.upstream.requests.Session') as factory,patch('detector.upstream.time.monotonic',return_value=0) as clock:
            session=factory.return_value.__enter__.return_value
            session.post.return_value.__enter__.return_value=response
            def chunks(*_,**__):
                yield b'data: {"type":"response.reasoning_text.delta","delta":"private thought"}\n\n'
                clock.return_value=901
                yield b'data: {"type":"response.reasoning_text.delta","delta":"more private thought"}\n\n'
            response.iter_content.side_effect=chunks
            with self.assertRaises(ProbeError) as exc:probe(self.account('grok'),'drawing',timeout=900)
            error=exc.exception
            self.assertEqual(error.code,'GENERATION_BUDGET_EXCEEDED')
            self.assertEqual(error.timings['first_data_seconds'],0)
            self.assertNotIn('first_text_seconds',error.timings)
            self.assertGreater(error.timings['received_bytes'],0)
            self.assertEqual(error.timings['output_chars'],0)
            self.assertEqual(error.timings['reasoning_events'],1)
            self.assertIn('客户端预算截断',error.message)
            self.assertIn('不是上游无响应',error.message)
            self.assertNotIn('private thought',error.message)
            self.assertEqual(session.post.call_count,1)

    def test_active_stream_can_finish_after_240_seconds_but_requires_completion(self):
        with patch('detector.upstream.time.monotonic',return_value=0) as clock:
            def lines():
                yield 'data: {"type":"response.output_text.delta","delta":"HTML"}'
                clock.return_value=300
                yield 'data: {"type":"response.completed","response":{"status":"completed"}}'
            on_text=Mock()
            result=parse_events(lines(),900,on_text)
            self.assertEqual(result['text'],'HTML')
            on_text.assert_called_once()
            clock.return_value=901
            with self.assertRaises(ProbeError) as exc:parse_events(lines(),900)
            self.assertEqual(exc.exception.code,'REQUEST_TIMEOUT')
        with self.assertRaises(ProbeError) as exc:native([{'type':'response.output_text.delta','delta':'partial'}])
        self.assertEqual(exc.exception.code,'STREAM_INTERRUPTED')

    def test_remaps_never_call_alternatives(self):
        for platform in BENCHMARKS:
            account=self.account(platform)
            account['credentials']['model_mapping']={BENCHMARKS[platform]['model']:'other-model'}
            with self.assertRaises(ProbeError) as exc:build_request(account,'candy')
            self.assertEqual(exc.exception.code,'MODEL_MAPPING_MISMATCH')
        # A platform with no model configured must refuse to probe before any
        # credential is used. Force that through live config, not the display snapshot.
        platform = config.CONFIG['platforms']['gemini']
        with patch.dict(platform, {'model': ''}):
            with self.assertRaises(ProbeError) as exc:build_request(self.account('gemini'),'candy')
            self.assertEqual(exc.exception.code,'MODEL_NOT_CONFIGURED')

    def test_grok_oauth_rejects_unverified_custom_host(self):
        account=self.account('grok','oauth')
        account['credentials']['base_url']='https://example.test'
        with self.assertRaises(ProbeError) as exc:build_request(account,'candy')
        self.assertEqual(exc.exception.code,'UNSAFE_OAUTH_HOST')

    def test_anthropic_requires_stop_and_discards_thinking(self):
        events=[{'type':'message_start','message':{'model':'claude-opus-5','usage':{'input_tokens':100}}},
                {'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'private'}},
                {'type':'content_block_delta','delta':{'type':'text_delta','text':'最终答案：21'}},
                {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':9}},
                {'type':'message_stop'}]
        result=native(events)
        self.assertEqual(result['text'],'最终答案：21')
        self.assertEqual(result['actual_model'],'claude-opus-5')
        self.assertEqual((result['tokens']['input_tokens'],result['tokens']['output_tokens']),(100,9))
        self.assertEqual(result['tokens']['_normalized'],1)
        with self.assertRaises(ProbeError):native(events[:-1])
        events[3]['delta']['stop_reason']='max_tokens'
        with self.assertRaises(ProbeError):native(events)

    def test_gemini_requires_stop_discards_thoughts_and_records_usage(self):
        event={'modelVersion':'gemini-3.8-flash','candidates':[{'content':{'parts':[{'thought':True,'text':'private'},{'text':'最终答案：21'}]},'finishReason':'STOP'}],
               'usageMetadata':{'promptTokenCount':20,'candidatesTokenCount':5,'totalTokenCount':25}}
        result=native([event])
        self.assertEqual(result['text'],'最终答案：21')
        self.assertEqual(result['tokens']['total_tokens'],25)
        event['candidates'][0].pop('finishReason')
        with self.assertRaises(ProbeError):native([event])
        event['candidates'][0]['finishReason']='MAX_TOKENS'
        with self.assertRaises(ProbeError):native([event])
        with self.assertRaises(ProbeError):native([{'promptFeedback':{'blockReason':'SAFETY'}}])

    def test_discovery_includes_all_platforms_but_not_image_accounts(self):
        api=Sub2API('https://example.test','offline-key')
        rows=[dict(self.account(p),id=i,name=p,group_ids=[4]) for i,p in enumerate(BENCHMARKS,1)]
        api.get=Mock(return_value={'total':6,'items':rows+[{'id':8,'platform':'openai','name':'生图账号','group_ids':[10]},{'id':9,'platform':'other','name':'Other'}]})
        self.assertEqual([r['platform'] for r in api.accounts()],list(BENCHMARKS))
        self.assertNotIn('platform',api.get.call_args.args[1])


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.store.sync([{'id':101,'name':'Offline fixture','type':'apikey'}],hour(),'initial')

    def test_model_platform_and_effort_are_snapshotted_per_run(self):
        self.store.sync([{'id':102,'name':'Claude','platform':'anthropic','type':'apikey'},
                         {'id':103,'name':'Gemini','platform':'gemini','type':'apikey'}],hour(),'timer')
        rows=self.store.pending(hour())
        self.assertEqual({r['model'] for r in rows},{'claude-opus-5-5','gemini-3.8-flash'})
        for row in rows:
            self.store.publish_run(row['id'])
            detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
            self.assertEqual(detail['model'],row['model']);self.assertEqual(detail['effort'],BENCHMARKS[row['platform']]['effort'])
        self.store.sync([{'id':102,'name':'Renamed','platform':'openai'}],None,'metadata')
        self.store.publish_run(rows[0]['id'])
        detail=json.loads((self.store.public/(rows[0]['id']+'.json')).read_text())
        self.assertEqual(detail['model'],rows[0]['model'])

    def test_changed_effort_never_runs_under_old_snapshot(self):
        row=self.store.pending(hour())[0]
        row['effort']=None
        request=Mock()
        execute_run(self.store,Mock(),row,time.monotonic()+3000,request,sleep=lambda _:None)
        request.assert_not_called()
        detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
        self.assertEqual(detail['error_code'],'MODEL_CONFIG_CHANGED')

    def test_drawing_budget_wall_fails_fast_without_burning_a_second_full_budget(self):
        self.store.sync([{'id':104,'name':'Grok fixture','platform':'grok','type':'apikey'}],hour(),'timer')
        row=next(r for r in self.store.pending(hour()) if r['kind']=='drawing' and r['platform']=='grok')
        receipt={'total_budget_seconds':1500,'first_data_seconds':2.4,'received_bytes':4096,
                 'output_chars':0,'elapsed_seconds':1500}
        request=Mock(side_effect=ProbeError('GENERATION_BUDGET_EXCEEDED',
            '上游持续有响应，但 1500 秒生成预算已用完，未完整结束。',receipt,{}))
        execute_run(self.store,Mock(),row,time.monotonic()+4000,request,sleep=lambda _:None)
        self.assertEqual(request.call_count,1)
        self.assertEqual(request.call_args.kwargs['timeout'],1500)
        detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'error')
        self.assertEqual(detail['error_code'],'GENERATION_BUDGET_EXCEEDED')
        self.assertEqual(detail['attempts'],1)
        self.assertEqual(detail['attempt_log'][0]['failure_class'],'budget')

    def test_batch_budget_bounds_probe_and_persists_timing_receipt(self):
        row=next(r for r in self.store.pending(hour()) if r['kind']=='candy')
        receipt={'first_data_seconds':1,'first_text_seconds':260,'elapsed_seconds':300}
        request=Mock(return_value={'text':'最终答案：21','timings':receipt})
        execute_run(self.store,Mock(),row,time.monotonic()+600,request)
        self.assertLessEqual(request.call_args.kwargs['timeout'],595)
        self.assertGreater(request.call_args.kwargs['timeout'],590)
        detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
        self.assertEqual(detail['attempt_log'][0]['timings'],receipt)

    def test_pause_stops_queued_work_and_future_slots(self):
        self.store.controls.set_paused(public_id(101),True)
        api=Mock();request=Mock();row=self.store.pending(hour())[0]
        execute_run(self.store,api,row,time.monotonic()+3000,request)
        api.account.assert_not_called();request.assert_not_called()
        detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'paused');self.assertEqual(detail['attempts'],0)
        self.store.sync([{'id':101,'name':'Offline fixture'}],hour()+3600,'timer')
        self.assertEqual(self.store.pending(hour()+3600),[])

    def test_pause_stops_retry(self):
        row=self.store.pending(hour())[0]
        def pause_after_error(_):self.store.controls.set_paused(public_id(101),True)
        request=Mock(side_effect=ProbeError('HTTP_429','limit'))
        execute_run(self.store,Mock(),row,time.monotonic()+3000,request,sleep=pause_after_error)
        self.assertEqual(request.call_count,1)
        detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'paused');self.assertEqual(len(detail['attempt_log']),1)

    def test_oauth_quota_skips_model_request_without_cost_or_attempt(self):
        row=self.store.pending(hour())[0]
        api=Mock()
        api.account.return_value={'platform':'openai','type':'oauth','status':'active',
            'temp_unschedulable_reason':json.dumps({'source':'account_scheduling_threshold',
                'window':'7d','used_percent':97,'threshold_percent':97,
                'until_unix':int(time.time())+3600}),
            'credentials':{}}
        request=Mock()
        execute_run(self.store,api,row,time.monotonic()+3000,request,sleep=lambda _:None)
        request.assert_not_called()
        detail=json.loads((self.store.public/(row['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'quota')
        self.assertEqual(detail['error_code'],OAUTH_QUOTA_ERROR)
        self.assertEqual(detail['attempts'],0)
        self.assertEqual(detail['attempt_log'],[])

    def test_oauth_quota_does_not_open_request_circuit(self):
        row=self.store.pending(hour())[0]
        api=Mock()
        api.account.return_value={'platform':'openai','type':'oauth','status':'active',
            'temp_unschedulable_reason':json.dumps({'source':'account_scheduling_threshold',
                'window':'5h','used_percent':96,'threshold_percent':96,
                'until_unix':int(time.time())+3600}),
            'credentials':{}}
        execute_run(self.store,api,row,time.monotonic()+3000,Mock(),sleep=lambda _:None)
        with self.store.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM request_circuits WHERE account_id=?',(row['account_id'],)).fetchone())

    def test_in_flight_success_keeps_artifact_after_pause(self):
        row=next(r for r in self.store.pending(hour()) if r['kind']=='drawing')
        html='<!doctype html><html><svg></svg></html>'
        def request(*_,**kwargs):
            self.store.controls.set_paused(public_id(101),True)
            return {'text':html}
        execute_run(self.store,Mock(),row,time.monotonic()+3000,request)
        self.assertEqual((self.store.public/(row['id']+'.html')).read_text(),html)

    def test_resume_waits_next_cycle_and_persists_across_instances(self):
        from detector.schedule import next_slot
        ident=public_id(101);now=hour()+100
        self.store.controls.set_paused(ident,True,now=now)
        state=self.store.controls.set_paused(ident,False,now=now+1)
        controls=Controls(Path(self.tmp.name)/'controls')
        self.assertEqual(state['resume_at'],next_slot(now+1))
        self.assertTrue(controls.blocked(ident,hour()));self.assertFalse(controls.blocked(ident,next_slot(now+1)))
        with self.assertRaises(ControlConflict):controls.set_paused(ident,True,expected=True)

    def test_http_read_only_default_origin_types_and_stale_controls(self):
        self.store.publish_state()
        server=make_server('127.0.0.1',0,Path(__file__).parent/'dist',self.store.public)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base='http://127.0.0.1:'+str(server.server_port);ident=public_id(101)
        def send(body,origin=base):
            return urlopen(Request(base+'/api/accounts/'+ident+'/pause',data=json.dumps(body).encode(),
                headers={'Content-Type':'application/json','Origin':origin}),timeout=3)
        with self.assertRaises(HTTPError) as exc:send({'paused':True,'expected_paused':False})
        self.assertEqual(exc.exception.code,405)
        server.controls_enabled=True
        with self.assertRaises(HTTPError) as exc:send({'paused':True,'expected_paused':False},'https://other.test')
        self.assertEqual(exc.exception.code,403)
        with send({'paused':True,'expected_paused':False}) as response:self.assertTrue(json.load(response)['paused'])
        with urlopen(base+'/api/state') as response:self.assertTrue(json.load(response)['accounts'][0]['paused'])
        with self.assertRaises(HTTPError) as exc:send({'paused':False,'expected_paused':False})
        self.assertEqual(exc.exception.code,409)
        with self.assertRaises(HTTPError) as exc:send({'paused':'false','expected_paused':True})
        self.assertEqual(exc.exception.code,400)
        for body in (['paused','expected_paused'],None,True,1,'paused'):
            with self.subTest(body=body):
                with self.assertRaises(HTTPError) as exc:send(body)
                self.assertEqual(exc.exception.code,400)
        with self.assertRaises(HTTPError) as exc:send({'paused':False,'expected_paused':True},'https://[')
        self.assertEqual(exc.exception.code,403)
        with urlopen(base+'/api/state') as response:self.assertTrue(json.load(response)['accounts'][0]['paused'])


if __name__=='__main__':unittest.main()
