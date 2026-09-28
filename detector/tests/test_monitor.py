"""Offline contract tests. Never load a real key or contact a provider."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from detector.monitor import Store, execute_run, extract_html, hour, parse_answer
from detector.prompts import CANDY_PROMPT, DRAWING_PROMPT, EFFORT, MODEL
from detector.question_bank import ORIGINAL
from detector.server import make_server
from detector.upstream import CLIENT_INFO, CODEX_VERSION, ProbeError, build_request, parse_events, proxy_url, redact, sse_lines

HTML='<!doctype html><html><body><svg viewBox="0 0 100 60"><rect width="100" height="60" fill="#00c895"/></svg></body></html>'


class ContractTests(unittest.TestCase):
    def test_exact_drawing_prompt(self):
        self.assertEqual(DRAWING_PROMPT,'创建一个 HTML，用 SVG 绘制一只大火烈鸟和一只小火烈鸟骑双人自行车的 2D 动画，不需要任何测试。')

    def test_answer_not_leaked_to_request(self):
        a={'type':'apikey','status':'active','credentials':{'api_key':'fake-key','base_url':'https://example.test/v1'}}
        _,_,payload=build_request(a,'candy')
        self.assertEqual(payload['input'][0]['content'][0]['text'],CANDY_PROMPT)
        self.assertNotIn('21',payload['instructions'].split('\n',1)[1])
        self.assertEqual(payload['model'],MODEL)
        self.assertEqual(payload['reasoning'],{'effort':EFFORT})

    def test_model_remap_fails_closed(self):
        with self.assertRaises(ProbeError):
            build_request({'credentials':{'model_mapping':{MODEL:'other'}}},'candy')

    def test_oauth_protocol_version_and_identity(self):
        a={'type':'oauth','status':'active','credentials':{'access_token':'fake-token','chatgpt_account_id':'fake-account'}}
        url,headers,payload=build_request(a,'candy')
        self.assertEqual(url,'https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual(headers['Version'],CODEX_VERSION)
        self.assertIn('/'+CODEX_VERSION+' ',headers['User-Agent'])
        self.assertEqual(headers['ChatGPT-Account-Id'],'fake-account')
        self.assertEqual(payload['model'],'gpt-6-astra')
        self.assertEqual(payload['reasoning'],{'effort':'medium'})

    def test_chat_effort(self):
        _,_,p=build_request({'status':'active','type':'apikey','credentials':{'api_key':'fake'},'extra':{'openai_responses_supported':False}},'candy')
        self.assertEqual(p['reasoning_effort'],'medium')

    def test_answer_parser(self):
        self.assertEqual(parse_answer('推导可知。\n最终答案：21'),21)
        self.assertEqual(parse_answer('最终答案：**21**'),21)
        self.assertEqual(parse_answer(r'\boxed{21}'),21)
        self.assertEqual(parse_answer('21 个。'),21)
        self.assertEqual(parse_answer('21 不是答案。\n最终答案：29'),29)
        self.assertIsNone(parse_answer('这里出现过 21 这个数字'))

    def test_html(self):
        self.assertEqual(extract_html('```html\n'+HTML+'\n```'),HTML)
        for value in ('<html><svg>','<html><body>Hi</body></html>','no html'):
            with self.assertRaises(ProbeError):extract_html(value)

    def test_sse_completion_required(self):
        line='data: '+json.dumps({'type':'response.output_text.delta','delta':'21'})
        with self.assertRaises(ProbeError):parse_events([line],time.monotonic()+10)

    def test_sse_canonical_text_not_duplicated(self):
        lines=['data: '+json.dumps(x) for x in [{'type':'response.output_text.delta','delta':'21'}, {'type':'response.completed','response':{'status':'completed','model':MODEL,'output':[{'type':'message','content':[{'type':'output_text','text':'21'}]}]}}]]
        self.assertEqual(parse_events(lines,time.monotonic()+10)['text'],'21')

    def test_sse_envelope_over_two_mb_does_not_limit_small_output(self):
        metadata='data: '+json.dumps({'type':'response.reasoning_summary_text.delta','delta':'x'*700000})
        final='data: '+json.dumps({'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':HTML}]}]}})
        self.assertEqual(parse_events([metadata]*4+[final],time.monotonic()+10)['text'],HTML)

    def test_output_limit_counts_utf8_not_characters(self):
        lines=['data: '+json.dumps({'type':'response.output_text.delta','delta':'糖'*30})]
        with patch('detector.upstream.MAX_OUTPUT_BYTES',80),self.assertRaises(ProbeError) as exc:
            parse_events(lines,time.monotonic()+10)
        self.assertEqual(exc.exception.code,'RESPONSE_TOO_LARGE')

    def test_stream_and_single_event_are_bounded_before_decode(self):
        response=Mock()
        response.iter_content.return_value=iter([b'data: '+b'x'*50,b'x'*50])
        with patch('detector.upstream.MAX_EVENT_BYTES',80),self.assertRaises(ProbeError) as exc:
            list(sse_lines(response,time.monotonic()+10))
        self.assertEqual(exc.exception.code,'EVENT_TOO_LARGE')
        response.iter_content.return_value=iter([b': keepalive\n']*10)
        with patch('detector.upstream.MAX_STREAM_BYTES',80),self.assertRaises(ProbeError) as exc:
            list(sse_lines(response,time.monotonic()+10))
        self.assertEqual(exc.exception.code,'STREAM_TOO_LARGE')

    def test_sse_lines_handles_split_unicode_crlf_and_final_line(self):
        response=Mock()
        wire='data: 糖果\r\n\ndata: [DONE]'.encode()
        response.iter_content.return_value=(wire[i:i+2] for i in range(0,len(wire),2))
        self.assertEqual(list(sse_lines(response,time.monotonic()+10)),['data: 糖果','','data: [DONE]'])

    def test_error_event(self):
        with self.assertRaises(ProbeError):parse_events(['data: {"type":"response.failed","response":{"error":{"message":"bad"}}}'],time.monotonic()+10)

    def test_chat_truncation(self):
        with self.assertRaises(ProbeError):parse_events(['data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":"length"}]}','data: [DONE]'],time.monotonic()+10)

    def test_redaction(self):
        text='sk-abcdefghijklmnop Bearer private-token-value test@example.com https://example.com/?secret=x'
        result=redact(text,['private-token-value'],True)
        for secret in ('abcdefghijklmnop','private-token-value','test@example.com','secret=x'):
            self.assertNotIn(secret,result)

    def test_proxy(self):
        self.assertEqual(proxy_url({'protocol':'socks5','host':'host','port':1080,'username':'u@x','password':'x:y'}),'socks5h://u%40x:x%3Ay@host:1080')


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.store.sync([{'id':101,'name':'Offline fixture'}],hour(),'timer')

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotent_hour(self):
        self.store.sync([{'id':101,'name':'Offline fixture'}],hour(),'timer')
        self.assertEqual(len(self.store.pending(hour())),2)

    def test_24_hour_window_preserves_legacy_timestamps_without_fabrication(self):
        from detector.schedule import window_slots
        self.store.publish_state()
        state=json.loads((self.store.public/'state.json').read_text())
        for rows in state['accounts'][0]['history'].values():
            self.assertEqual({r['slot'] for r in rows},set(window_slots())|{hour()})
            self.assertEqual(sum(r['status']=='empty' for r in rows),len(rows)-1)
        self.assertNotIn('account_id',json.dumps(state))

    def test_metadata_sync_removes_deleted_supplier_and_its_artifacts_without_new_runs(self):
        row=self.store.pending(hour())[0]
        self.store.publish_run(row['id'])
        self.store.sync([],None,'metadata')
        self.store.publish_state()
        state=json.loads((self.store.public/'state.json').read_text())
        self.assertEqual(state['accounts'],[])
        self.assertFalse((self.store.public/(row['id']+'.json')).exists())
        self.assertEqual(self.store.pending(hour()),[])

    def test_account_type_metadata_refresh_never_schedules_requests(self):
        before=self.store.pending(hour())
        self.store.sync([{'id':101,'name':'Offline fixture','type':'oauth'}, {'id':102,'name':'API fixture','type':'apikey'}],None,'metadata')
        self.store.publish_state()
        state=json.loads((self.store.public/'state.json').read_text())
        self.assertEqual([a['type'] for a in state['accounts']],['oauth','apikey'])
        self.assertEqual([r['id'] for r in self.store.pending(hour())],[r['id'] for r in before])
        self.assertEqual(state['request_client'],CLIENT_INFO)

    def test_historical_client_is_not_backfilled_with_current_version(self):
        run=self.store.pending(hour())[0]
        self.store.update(run['id'],status='error',error_code='HTTP_400',error='old',attempt_log='[{"attempt":1,"status":"error"}]')
        detail=json.loads((self.store.public/(run['id']+'.json')).read_text())
        self.assertIsNone(detail['request_client'])

    def test_old_account_schema_migrates_without_losing_results(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            private=Path(root)/'private';private.mkdir()
            with sqlite3.connect(private/'state.sqlite3') as db:
                db.execute('CREATE TABLE accounts (account_id INTEGER PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,last_sync INTEGER NOT NULL)')
                db.execute('INSERT INTO accounts VALUES (101,\'Historical account\',1,?)',(hour(),))
            migrated=Store(root)
            with migrated.db() as db:
                row=db.execute('SELECT * FROM accounts').fetchone()
                self.assertEqual(row['name'],'Historical account')
                self.assertEqual(row['account_type'],'unknown')

    def test_wrong_answer_ends_after_one_request(self):
        r=next(x for x in self.store.pending(hour()) if x['kind']=='candy')
        api=Mock(); request=Mock(return_value={'text':'最终答案：29'})
        execute_run(self.store,api,r,time.monotonic()+3000,request,sleep=lambda _:None)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs['prompt'],ORIGINAL['prompt'])
        detail=json.loads((self.store.public/(r['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'fail')

    def test_retry_limit_and_preserved_errors(self):
        r=self.store.pending(hour())[0]
        api=Mock(); request=Mock(side_effect=ProbeError('HTTP_429','限流'))
        execute_run(self.store,api,r,time.monotonic()+3000,request,sleep=lambda _:None)
        self.assertEqual(request.call_count,2)
        detail=json.loads((self.store.public/(r['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'error')
        self.assertEqual(len(detail['attempt_log']),2)
        self.assertEqual(detail['request_client'],CLIENT_INFO)
        self.assertTrue(all(a['client']==CLIENT_INFO for a in detail['attempt_log']))
        self.assertEqual(self.store.pending(hour())[0]['kind'],'drawing')

    def test_retry_then_pass(self):
        r=next(x for x in self.store.pending(hour()) if x['kind']=='candy')
        request=Mock(side_effect=[ProbeError('TIMEOUT','超时'),{'text':'最终答案：21'},{'text':'最终答案：21'}])
        execute_run(self.store,Mock(),r,time.monotonic()+3000,request,sleep=lambda _:None)
        self.assertEqual(request.call_count,3)
        detail=json.loads((self.store.public/(r['id']+'.json')).read_text())
        self.assertEqual(detail['status'],'pass')
        self.assertIsNone(detail['error'])

    def test_cleanup(self):
        r=self.store.pending(hour())[0]
        with self.store.db() as db:db.execute('UPDATE runs SET slot=? WHERE id=?',(hour()-24*3600,r['id']))
        self.store.publish_run(r['id'])
        self.store.cleanup()
        self.assertFalse((self.store.public/(r['id']+'.json')).exists())

    def test_drawing_projection(self):
        r=next(x for x in self.store.pending(hour()) if x['kind']=='drawing')
        execute_run(self.store,Mock(),r,time.monotonic()+3000,Mock(return_value={'text':HTML}),sleep=lambda _:None)
        self.assertEqual((self.store.public/(r['id']+'.html')).read_text(),HTML)

    def test_public_http_boundary(self):
        r=next(x for x in self.store.pending(hour()) if x['kind']=='drawing')
        execute_run(self.store,Mock(),r,time.monotonic()+3000,Mock(return_value={'text':HTML}),sleep=lambda _:None)
        server=make_server('127.0.0.1',0,Path(__file__).parent/'dist',self.store.public)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/artifacts/'+r['id']+'.html') as response:
                csp=response.headers['Content-Security-Policy']
                self.assertIn('sandbox allow-scripts;',csp)
                self.assertNotIn('allow-same-origin',csp)
                self.assertIn("connect-src 'none'",csp)
            for path in ('/private/state.sqlite3','/credentials/sub2api-admin.cred','/monitor.py','/api/trigger','/artifacts/../../etc/passwd'):
                with self.assertRaises(HTTPError) as e:urlopen(base+path)
                self.assertEqual(e.exception.code,404)
            with self.assertRaises(HTTPError) as e:urlopen(Request(base+'/api/state',data=b'{}'))
            self.assertEqual(e.exception.code,405)
            with self.store.db() as db:db.execute('UPDATE runs SET slot=? WHERE id=?',(hour()-24*3600,r['id']))
            self.store.publish_run(r['id'])
            with self.assertRaises(HTTPError) as e:urlopen(base+'/artifacts/'+r['id']+'.html')
            self.assertEqual(e.exception.code,410)
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':
    unittest.main()
