"""Offline priority scope, evidence, persistence and narrow-write contracts."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from detector import config
from detector.controls import ControlConflict
from detector.monitor import Store, public_id
from detector.priority_routing import Prices, PriorityRouter, score, stability_score, recent_stability, recent_quality, SCORE_VERSION
from detector.question_bank import ORIGINAL, VERSION, TWO_PASS_VERSION
from detector.schedule import current_slot
from detector.supplier_prices import price_signature
from detector.upstream import ProbeError, Sub2API


class Scores(unittest.TestCase):
    def test_weights_and_priority_direction(self):
        value=score(.2,9,1,98,2)
        self.assertAlmostEqual(value['score'],.36*(100/1.2)+.36*90+.18*98+.1*50)
        self.assertAlmostEqual(score(.2,9,1,98,2,100)['score']-score(.2,9,1,98,2,0)['score'],10)
        self.assertLess(score(.2,9,1,98,2,90)['target_priority'],value['target_priority'])
        self.assertIsNone(value['speed_score'])
        self.assertEqual(value['speed_effective_score'],50)
        self.assertLess(score(.1,9,1,98,2)['target_priority'],value['target_priority'])
        self.assertLess(score(.2,10,0,98,2)['target_priority'],value['target_priority'])
        self.assertLess(score(.2,9,1,100,0)['target_priority'],value['target_priority'])
        self.assertTrue(100<=value['target_priority']<=100100)

    def test_small_window_uses_observed_rate_not_wilson_penalty(self):
        self.assertEqual(stability_score(2,0),100)
        self.assertEqual(stability_score(2,2),50)

    def test_missing_inputs_never_fabricated(self):
        for values in ((None,9,1,98,2),(.2,0,0,98,2),(.2,9,1,0,0)):
            self.assertIsNone(score(*values)['target_priority'])


class RecentStability(unittest.TestCase):
    def row(self, slot, kind='candy', status='pass', attempts=None, **fields):
        return {'account_id':1, 'model':'test-model', 'slot':slot, 'kind':kind,
            'bank_version':VERSION, 'status':status, 'attempt_log':json.dumps(attempts or
                [{'request_id':str(slot)+kind, 'status':'pass'}]), **fields}

    def evaluate(self, rows):
        return recent_stability(rows, {1:{'model':'test-model'}})

    def test_only_three_latest_rounds_and_both_kinds(self):
        rows=[self.row(i) for i in range(1,5)]+[self.row(4,'drawing')]
        value=self.evaluate(rows)[1]
        self.assertEqual(value, {'successful_requests':4,'failed_requests':0,
            'stability_rounds':3,'start_at':2,'end_at':4})

    def test_retry_error_is_not_erased_and_wrong_answer_is_connected(self):
        attempts=[{'request_id':'a','status':'error','failure_class':'upstream'},
                  {'request_id':'b','status':'fail'}, {'request_id':'c','status':'unknown'}]
        value=self.evaluate([self.row(1,status='error',attempts=attempts)])[1]
        self.assertEqual((value['successful_requests'],value['failed_requests']), (2,1))

    def test_streaming_budget_wall_counts_as_a_failed_request(self):
        attempts=[{'request_id':'a','status':'error','failure_class':'budget'}]
        value=self.evaluate([self.row(1,status='error',attempts=attempts)])[1]
        self.assertEqual((value['successful_requests'],value['failed_requests']), (0,1))

    def test_no_requests_and_current_inflight_do_not_evict_previous_rounds(self):
        rows=[self.row(i) for i in range(1,4)]
        rows += [self.row(4,status='running'),self.row(5,status='quota'),self.row(6,status='paused'),
            self.row(7,status='error',attempts=[{'request_id':'local','status':'error','failure_class':'local'}])]
        self.assertEqual(self.evaluate(rows)[1]['start_at'],1)
        self.assertEqual(self.evaluate(rows)[1]['end_at'],3)

    def test_model_history_duplicates_and_missing_request_evidence(self):
        rows=[self.row(1),self.row(2,model='old-model'),self.row(3,bank_version='old'),
              self.row(4,attempts=[{'status':'pass'}])]
        rows.append(self.row(1,'drawing',attempts=[{'request_id':'1candy','status':'pass'}]))
        self.assertEqual(self.evaluate(rows)[1]['successful_requests'],1)


class RecentQuality(unittest.TestCase):
    def row(self, slot, status='pass', **fields):
        statuses = ['pass', 'pass'] if status == 'pass' else ['fail'] if status == 'fail' else ['error']
        attempts = [{'request_id':f'{slot}-{stage}', 'stage':stage, 'question_id':ORIGINAL['id'],
            'status':value, **({'failure_class':'upstream'} if value == 'error' else {})}
            for stage, value in enumerate(statuses)]
        results = [{'question_id':ORIGINAL['id'], 'status':value} for value in statuses if value != 'error']
        return {'account_id':1, 'model':'test-model', 'slot':slot, 'kind':'candy',
            'bank_version':VERSION, 'status':status, 'attempt_log':json.dumps(attempts),
            'question_results':json.dumps(results), **fields}

    def evaluate(self, rows):
        return recent_quality(rows, {1:{'model':'test-model'}, 2:{'model':'test-model'}})

    def test_old_successes_do_not_dilute_three_recent_failures(self):
        rows=[self.row(i) for i in range(1,9)]+[self.row(i,'fail') for i in range(9,12)]
        self.assertEqual(self.evaluate(rows)[1], {'pass':0,'fail':3,'quality_rounds':3,
            'quality_start_at':9,'quality_end_at':11})

    def test_old_failures_do_not_dilute_three_recent_successes(self):
        rows=[self.row(i,'fail') for i in range(1,9)]+[self.row(i) for i in range(9,12)]
        value=self.evaluate(list(reversed(rows)))[1]
        self.assertEqual((value['pass'],value['fail'],value['quality_rounds']),(3,0,3))

    def test_errors_occupy_window_without_inventing_wrong_answers_or_old_successes(self):
        rows=[self.row(1), self.row(2), self.row(3,'fail'), self.row(4,'error')]
        value=self.evaluate(rows)[1]
        self.assertEqual((value['pass'],value['fail'],value['quality_rounds']),(1,1,3))
        self.assertEqual(value['quality_start_at'],2)
        value=self.evaluate([self.row(1)]+[self.row(i,'error') for i in range(2,5)])[1]
        self.assertIsNone(score(.2,value['pass'],value['fail'],1,3)['quality_score'])

    def test_scope_skips_and_local_failures_preserve_last_completed_rounds(self):
        rows=[self.row(1), self.row(2,'fail')]
        rows += [self.row(3,kind='drawing'), self.row(4,model='old'), self.row(5,bank_version='old'),
            self.row(6,'pending'), self.row(7,'running'), self.row(8,'paused'), self.row(9,'quota'),
            self.row(10,'error',attempt_log=json.dumps([{'request_id':'local','status':'error','failure_class':'local'}]))]
        value=self.evaluate(rows)[1]
        self.assertEqual((value['pass'],value['fail'],value['quality_rounds']),(1,1,2))
        self.assertEqual(value['quality_end_at'],2)

    def test_single_pass_or_error_between_correct_answers_is_not_a_quality_pass(self):
        single=self.row(1)
        single['attempt_log']=json.dumps(json.loads(single['attempt_log'])[:1])
        single['question_results']=json.dumps(json.loads(single['question_results'])[:1])
        interrupted=self.row(2)
        attempts=json.loads(interrupted['attempt_log'])
        attempts.insert(1,{'request_id':'retry','status':'error','failure_class':'upstream'})
        interrupted['attempt_log']=json.dumps(attempts)
        value=self.evaluate([single,interrupted])[1]
        self.assertEqual((value['pass'],value['fail'],value['quality_rounds']),(0,0,2))

    def test_per_account_window_legacy_two_pass_and_duplicate_slot(self):
        first=self.row(1,bank_version=TWO_PASS_VERSION)
        rows=[first,first,self.row(2,'fail')]+[self.row(i,account_id=2) for i in range(3,7)]
        value=self.evaluate(rows)
        self.assertEqual((value[1]['pass'],value[1]['fail'],value[1]['quality_rounds']),(1,1,2))
        self.assertEqual((value[2]['pass'],value[2]['quality_rounds']),(3,3))


class PriceStorage(unittest.TestCase):
    def test_atomic_conflicts_and_validation(self):
        with tempfile.TemporaryDirectory() as root:
            controls=Prices(root);key=public_id(1)
            self.assertEqual(controls.read(),{})
            controls.set(key,.2,None)
            self.assertEqual(Prices(root).read()[key]['multiplier'],.2)
            with self.assertRaises(ControlConflict):controls.set(key,.3,None)
            for value in (True,'0.2',-1,1001,float('nan'),float('inf')):
                with self.assertRaises(ValueError):controls.set(key,value,.2)
            controls.set(key,None,.2)
            self.assertIsNone(controls.read()[key]['multiplier'])


class FakeAPI:
    def __init__(self, accounts):
        self.rows={a['id']:dict(a) for a in accounts};self.calls=[]
    def accounts(self):return copy.deepcopy(list(self.rows.values()))
    def account_state(self, ident):return copy.deepcopy(self.rows[ident])
    def business_stability(self):raise AssertionError('Priority must not read 24-hour business traffic')
    def set_priority(self, ident, value):
        self.calls.append((ident,value));self.rows[ident]['priority']=value
        return self.account_state(ident)


class Routing(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.accounts=[{'id':1,'name':'Dual OAuth','platform':'openai','type':'oauth','group_ids':[4,10],
                        'status':'active','schedulable':False,'priority':1},
                       {'id':2,'name':'Image only','platform':'openai','type':'apikey','group_ids':[10],
                        'status':'active','schedulable':True,'priority':10},
                       {'id':3,'name':'Gemini','platform':'gemini','type':'apikey','group_ids':[5],
                        'status':'active','schedulable':False,'priority':30}]
        self.api=FakeAPI(self.accounts)
        self.slot=current_slot()
        self.store.sync(self.accounts,self.slot,'initial')
        for account in self.accounts:
            Prices(self.store.root/'controls').set(public_id(account['id']),.2,None)
            with self.store.db() as db:
                ident=db.execute("SELECT id FROM runs WHERE account_id=? AND kind='candy'",(account['id'],)).fetchone()['id']
            results=[{'question_id':ORIGINAL['id'],'status':'pass'} for _ in range(2)]
            attempts=[{**row,'stage':i,'request_id':ident+str(i)} for i,row in enumerate(results)]
            self.store.update(ident,status='pass',question_results=json.dumps(results),attempt_log=json.dumps(attempts))

    def test_scope_includes_dual_oauth_excludes_image_and_preserves_disabled(self):
        PriorityRouter(self.store,self.api,True).reconcile()
        self.assertEqual({i for i,p in self.api.calls},{1,3})
        self.assertFalse(self.api.rows[1]['schedulable']);self.assertFalse(self.api.rows[3]['schedulable'])
        self.assertEqual(self.api.rows[2]['priority'],10)

    def test_rate_tick_changes_priority_once_and_preserves_pause_and_callability(self):
        from detector.supplier_prices import price_signature
        prices=Prices(self.store.root/'controls');key=public_id(1)
        value=prices.read()[key]
        prices.set_config(key,{'mode':'scheduled','timezone':'Asia/Shanghai','periods':[
            {'start':'00:00','end':'08:00','multiplier':.1},
            {'start':'08:00','end':'24:00','multiplier':.8}]},value['revision'])
        router=PriorityRouter(self.store,self.api,True)
        from datetime import datetime
        now=datetime.now().astimezone()
        from zoneinfo import ZoneInfo
        morning=now.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=7,minute=59,second=59,microsecond=0).timestamp()
        with patch('detector.supplier_prices.time.time',return_value=morning):
            self.assertEqual(router.reconcile(prices_only=True),'reconciled')
            first=self.api.rows[1]['priority'];self.api.calls.clear()
            self.assertEqual(router.reconcile(prices_only=True),'unchanged');self.assertFalse(self.api.calls)
        with patch('detector.supplier_prices.time.time',return_value=morning+1):
            self.assertEqual(router.reconcile(prices_only=True),'reconciled')
            self.assertGreater(self.api.rows[1]['priority'],first)
            self.assertEqual(self.store.metadata()['priority_price_signature'],price_signature(prices.read()))
        self.assertFalse(self.api.rows[1]['schedulable']);self.assertEqual(self.api.rows[2]['priority'],10)

    def test_pause_and_oauth_quota_block_writes(self):
        self.store.controls.set_paused(public_id(3),True)
        self.api.rows[1].update(temp_unschedulable_until=time.time()+3600,
            temp_unschedulable_reason=json.dumps({'source':'account_scheduling_threshold','used_percent':97,'threshold_percent':97}))
        PriorityRouter(self.store,self.api,True).reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.store.metadata()['supplier_priorities'][public_id(1)]['status'],'quota')

    def test_missing_price_preserves_and_partial_window_reports_actual_sample(self):
        Prices(self.store.root/'controls').set(public_id(1),None,.2)
        PriorityRouter(self.store,self.api,True).reconcile()
        metric=self.store.metadata()['supplier_priorities'][public_id(3)]
        self.assertEqual(self.api.calls,[(3,11100)])
        self.assertEqual(metric['stability_rounds'],1)
        self.assertEqual(metric['quality_rounds'],1)
        self.assertEqual(metric['quality_round_limit'],3)
        self.assertEqual(metric['quality_start_at'],self.slot)
        self.assertEqual(metric['successful_requests'],2)
        self.assertEqual(metric['score_version'],SCORE_VERSION)

    def test_partial_group_prices_never_mix_new_and_old_priority_scales(self):
        self.api.rows[3].update(platform='openai',group_ids=[4])
        self.store.sync(self.api.accounts(),self.slot,'timer')
        Prices(self.store.root/'controls').set(public_id(1),None,.2)
        PriorityRouter(self.store,self.api,True).reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.store.metadata()['supplier_priorities'][public_id(3)]['status'],'needs_group_prices')

    def test_invalid_local_evidence_fails_closed(self):
        with self.store.db() as db:
            db.execute("UPDATE runs SET attempt_log='invalid' WHERE kind='candy'")
        with patch.object(self.store,'publish_state'):
            PriorityRouter(self.store,self.api,True).reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.store.metadata()['priority_error'],'PRIORITY_DATA_UNAVAILABLE')

    def test_single_correct_or_fail_then_pass_not_quality_pass(self):
        with self.store.db() as db:
            db.execute("UPDATE runs SET question_results='[]',attempt_log='[]' WHERE kind='candy'")
        PriorityRouter(self.store,self.api,True).reconcile()
        self.assertEqual(self.api.calls,[(1,100100),(3,100100)])

    def test_applied_priority_uses_recent_three_quality_rounds(self):
        fixture=RecentQuality()
        with self.store.db() as db:
            model=db.execute('SELECT model FROM accounts WHERE account_id=1').fetchone()['model']
            for i,status in enumerate(('pass','pass','pass','fail','fail')):
                slot=self.slot-(5-i)*2700
                row=fixture.row(slot,status,model=model)
                db.execute('''INSERT INTO runs(id,account_id,slot,kind,model,created_at,status,bank_version,
                    question_results,attempt_log) VALUES(?,1,?,'candy',?,?,?,?,?,?)''',
                    (f'quality-window-{i}',slot,model,slot,status,VERSION,row['question_results'],row['attempt_log']))
        PriorityRouter(self.store,self.api,True).reconcile()
        metric=self.store.metadata()['supplier_priorities'][public_id(1)]
        self.assertEqual((metric['quality_pass'],metric['quality_fail'],metric['quality_rounds']),(1,2,3))
        self.assertAlmostEqual(metric['quality_score'],100/3)
        self.assertEqual(self.api.rows[1]['priority'],35100)

    def test_formula_migration_recalculates_unchanged_prices_once(self):
        self.store.meta('priority_price_signature',price_signature(Prices(self.store.root/'controls').read()))
        self.store.meta('priority_score_version','old')
        router=PriorityRouter(self.store,self.api,True)
        self.assertEqual(router.reconcile(prices_only=True),'reconciled')
        self.assertEqual(len(self.api.calls),2)
        self.assertEqual(router.reconcile(prices_only=True),'unchanged')

    def test_speed_changes_native_priority_and_preserves_callability(self):
        with self.store.db() as db:
            for ident,latency in ((1,1),(3,100)):
                row=db.execute("SELECT id,attempt_log FROM runs WHERE account_id=? AND kind='candy'",(ident,)).fetchone()
                attempts=json.loads(row['attempt_log'])
                for a in attempts:
                    a.update(tokens={'output_tokens':10000},timings={'first_text_seconds':latency,'elapsed_seconds':200})
                db.execute('UPDATE runs SET attempt_log=? WHERE id=?',(json.dumps(attempts),row['id']))
        PriorityRouter(self.store,self.api,True).reconcile()
        self.assertLess(self.api.rows[1]['priority'],self.api.rows[3]['priority'])
        self.assertFalse(self.api.rows[1]['schedulable'])
        self.assertEqual(self.store.metadata()['supplier_priorities'][public_id(1)]['speed_samples'],2)


class NarrowWrite(unittest.TestCase):
    def test_payload_only_priority_and_readback(self):
        api=Sub2API('https://example.test','fake-admin-not-a-real-key')
        before={'id':4,'group_ids':[4,10],'type':'oauth','platform':'openai','priority':1,'schedulable':False}
        api.account_state=Mock(side_effect=[before,{**before,'priority':9900}])
        session=Mock();session.put.return_value.status_code=200;session.put.return_value.json.return_value={'code':0}
        with patch('detector.upstream.requests.Session') as factory:
            factory.return_value.__enter__.return_value=session
            api.set_priority(4,9900)
        self.assertEqual(session.put.call_args.kwargs['json'],{'priority':9900})
        self.assertFalse(session.put.call_args.kwargs['allow_redirects'])

    def test_image_only_no_network_write(self):
        scope={'group_ids':[4],'group_names':[],'exclude_names':['生图','image']}
        api=Sub2API('https://example.test','fake-admin-not-a-real-key')
        api.account_state=Mock(return_value={'platform':'openai','group_ids':[10]})
        with patch.dict(config.CONFIG['platforms']['openai'],scope):
            with self.assertRaises(ProbeError),patch('detector.upstream.requests.Session') as factory:
                api.set_priority(2,9900)
        factory.assert_not_called()


if __name__=='__main__':unittest.main()
