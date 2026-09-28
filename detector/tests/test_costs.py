"""Offline token normalization, currency arithmetic and retention contracts."""
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from detector.costs import RETENTION, estimate, normalize_usage, validated_price
from detector.monitor import Store, execute_run, hour
from detector.upstream import ProbeError, parse_events, probe

RAW_PRICE = {'found':True,'input_price':0.00001,'output_price':0.00005,
             'cache_read_price':0.000001,'cache_write_price':0.0000125,'cache_write_1h_price':0.00002}


class UsageTests(unittest.TestCase):
    def test_responses_and_chat_usage_include_cache_without_double_reasoning(self):
        for raw in ({'input_tokens':100,'output_tokens':40,'input_tokens_details':{'cached_tokens':30},'output_tokens_details':{'reasoning_tokens':20}},
                    {'prompt_tokens':100,'completion_tokens':40,'prompt_tokens_details':{'cached_tokens':30},'completion_tokens_details':{'reasoning_tokens':20}}):
            usage=normalize_usage(raw)
            self.assertEqual((usage['input_tokens'],usage['output_tokens'],usage['cached_input_tokens'],usage['reasoning_tokens']),(100,40,30,20))
            self.assertEqual(normalize_usage(usage),usage)

    def test_anthropic_cache_creation_is_separate_from_uncached_input(self):
        usage=normalize_usage({'input_tokens':100,'output_tokens':40,'cache_read_input_tokens':30,
                               'cache_creation_input_tokens':20,'cache_creation':{'ephemeral_1h_input_tokens':5}},'anthropic')
        self.assertEqual(usage['input_tokens'],150)
        price=validated_price(RAW_PRICE,time.time())
        amount,reason=estimate(usage,price)
        self.assertIsNone(reason)
        self.assertEqual(amount,3_317_500)

    def test_gemini_thoughts_are_billed_once(self):
        usage=normalize_usage({'promptTokenCount':100,'candidatesTokenCount':40,'thoughtsTokenCount':20,'cachedContentTokenCount':10},'gemini')
        self.assertEqual((usage['input_tokens'],usage['output_tokens'],usage['reasoning_tokens']),(100,60,20))
        self.assertEqual(normalize_usage(usage,'gemini'),usage)

    def test_malformed_usage_details_are_unknown_not_a_worker_error(self):
        self.assertEqual(normalize_usage({'input_tokens':100,'output_tokens':20,'input_tokens_details':['bad']}),{})

    def test_invalid_missing_and_long_context_are_not_free(self):
        price=validated_price(RAW_PRICE,time.time())
        for usage,reason in (({},'missing_usage'),({'input_tokens':1},'missing_usage'),
                             ({'input_tokens':-1,'output_tokens':3},'missing_usage'),
                             ({'input_tokens':10,'output_tokens':3,'cached_input_tokens':20},'invalid_usage'),
                             ({'input_tokens':200001,'output_tokens':3},'unsupported_context_tier')):
            self.assertEqual(estimate(usage,price),(None,reason))
        self.assertEqual(estimate({'input_tokens':1,'output_tokens':1},None),(None,'missing_price'))
        for value in (float('nan'),float('inf'),-1,True,'not a price'):
            self.assertIsNone(validated_price({**RAW_PRICE,'input_price':value},time.time()))

    def test_stream_error_keeps_received_usage(self):
        seen=[]
        events=[{'type':'message_start','message':{'usage':{'input_tokens':50}}},
                {'type':'message_delta','usage':{'output_tokens':10},'delta':{'stop_reason':'max_tokens'}}]
        with self.assertRaises(ProbeError):
            parse_events(['data: '+json.dumps(e) for e in events],time.monotonic()+10,on_usage=seen.append)
        self.assertEqual((seen[-1]['input_tokens'],seen[-1]['output_tokens']),(50,10))

    def test_json_fallback_records_usage(self):
        account={'platform':'openai','type':'apikey','credentials':{'api_key':'offline'}}
        body={'status':'completed','model':'gpt-6-astra','output':[{'type':'message','content':[{'type':'output_text','text':'21'}]}],
              'usage':{'input_tokens':5,'output_tokens':10,'input_tokens_details':{'cached_tokens':2}}}
        with patch('detector.upstream.requests.Session') as session:
            response=session.return_value.__enter__.return_value.post.return_value.__enter__.return_value
            response.status_code=200;response.headers={'Content-Type':'application/json'}
            response.iter_content.return_value=iter([json.dumps(body).encode()])
            result=probe(account,'candy')
        self.assertEqual(result['tokens']['cached_input_tokens'],2)
        self.assertEqual(result['tokens']['output_tokens'],10)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.store.sync([{'id':1,'name':'Fixture','platform':'openai','type':'apikey'}],hour(),'initial')
        self.run=next(r for r in self.store.pending(hour()) if r['kind']=='candy')
        self.store.meta('cost_prices',{'gpt-6-astra':validated_price(RAW_PRICE,time.time())})

    def record(self, ident, age=0, tokens=None):
        self.store.costs.begin(self.run,ident,time.time()-age)
        self.store.costs.finish(ident,'received',tokens or {'input_tokens':100,'output_tokens':40})

    def test_windows_idempotence_and_provider_deletion_keep_spent_cost(self):
        self.record('now');self.record('week',7*86400);self.record('old',31*86400)
        self.record('now')
        self.store.costs.finish('now','error',{'input_tokens':999,'output_tokens':999})
        result=self.store.costs.summary()['windows']
        self.assertEqual(result['24h']['amount_usd'],0.003)
        self.assertEqual(result['30d']['amount_usd'],0.006)
        self.assertEqual(len(result['30d']['models']),4)
        self.store.sync([],None,'metadata')
        self.assertEqual(self.store.costs.summary()['windows']['30d']['amount_usd'],0.006)
        self.store.cleanup()
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM cost_requests').fetchone()[0],2)

    def test_no_usage_and_inflight_are_reported_unknown_not_zero(self):
        self.store.costs.begin(self.run,'missing');self.store.costs.finish('missing','error')
        self.store.costs.begin(self.run,'inflight')
        window=self.store.costs.summary()['windows']['24h']
        self.assertIsNone(window['amount_usd'])
        self.assertEqual(window['unpriced_requests'],2)
        self.assertEqual(window['models'][0]['pending_requests'],1)

    def test_not_sent_is_not_a_billable_request(self):
        self.store.costs.begin(self.run,'bad-config')
        self.store.costs.finish('bad-config','error',sent=False)
        self.assertEqual(self.store.costs.summary()['windows']['24h']['models'][0]['requests'],0)

    def test_price_snapshot_does_not_reprice_history(self):
        self.record('original')
        self.store.meta('cost_prices',{'gpt-6-astra':validated_price({**RAW_PRICE,'output_price':0.0001},time.time())})
        self.record('new')
        self.assertEqual(self.store.costs.summary()['windows']['24h']['amount_usd'],0.008)

    def test_primary_review_and_retry_each_have_one_record(self):
        request=Mock(side_effect=[ProbeError('HTTP_503','fixture'),{'text':'最终答案：21','tokens':{'input_tokens':100,'output_tokens':40}},
                                  {'text':'最终答案：21','tokens':{'input_tokens':100,'output_tokens':40}}])
        execute_run(self.store,Mock(),self.run,time.monotonic()+3000,request,sleep=lambda _:None)
        window=self.store.costs.summary()['windows']['24h']
        self.assertEqual((window['priced_requests'],window['unpriced_requests'],window['amount_usd']),(2,1,0.006))
        with self.store.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM cost_requests')]
        self.assertEqual(len(rows),3)
        self.assertNotIn('text',json.dumps(rows))
        self.assertNotIn('最终答案',json.dumps(rows))

    def test_first_wrong_answer_has_no_confirmation_cost(self):
        request=Mock(return_value={'text':'最终答案：29','tokens':{'input_tokens':100,'output_tokens':40}})
        execute_run(self.store,Mock(),self.run,time.monotonic()+3000,request,sleep=lambda _:None)
        request.assert_called_once()
        window=self.store.costs.summary()['windows']['24h']
        self.assertEqual((window['priced_requests'],window['unpriced_requests'],window['amount_usd']),(1,0,0.003))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cost_requests').fetchone()[0],1)

    def test_legacy_backfill_once_does_not_fabricate_30_days(self):
        self.store.update(self.run['id'],status='pass',finished_at=time.time(),tokens=json.dumps({'input_tokens':100,'output_tokens':40}),
                          attempt_log=json.dumps([{'request_id':'legacy-r','status':'pass'}]))
        self.store.costs.backfill();self.store.costs.backfill()
        summary=self.store.costs.summary()
        self.assertGreater(summary['windows']['30d']['coverage_from'],time.time()-86401)
        self.assertEqual(summary['windows']['24h']['models'][0]['legacy_requests'],1)
        self.assertEqual(summary['windows']['24h']['amount_usd'],0.003)

    def test_price_refresh_errors_keep_fresh_snapshot_but_expire_after_day(self):
        api=Mock();api.get.side_effect=RuntimeError('offline')
        self.store.costs.refresh_prices(api,force=True)
        self.assertEqual(api.get.call_count,4)
        self.record('cached')
        self.assertEqual(self.store.costs.summary()['windows']['24h']['amount_usd'],0.003)
        self.store.meta('cost_prices',{'gpt-6-astra':validated_price(RAW_PRICE,time.time()-86401)})
        self.record('expired')
        self.assertEqual(self.store.costs.summary()['windows']['24h']['unpriced_requests'],1)


if __name__=='__main__':unittest.main()
