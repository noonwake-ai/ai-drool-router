"""Offline routing contract; never calls production or reads credentials."""
import copy
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from detector import config
from detector.monitor import Store, public_id
from detector.quality_routing import QualityRouter, consecutive_request_errors, definite_verdict, request_failure_class
from detector.question_bank import ORIGINAL, TWO_PASS_VERSION, VERSION
from detector.schedule import current_slot
from detector.upstream import ProbeError, Sub2API, build_request


class FakeAPI:
    def __init__(self, accounts):
        self.rows={a['id']:dict(a) for a in accounts}
        self.calls=[]
        self.fail=False

    def accounts(self):return copy.deepcopy(list(self.rows.values()))
    def account_state(self, ident):return dict(self.rows[ident])
    def set_callable(self, ident, value):
        if self.fail:raise ProbeError('ROUTING_HTTP_503','offline failure')
        self.rows[ident]['schedulable']=value
        if value:self.rows[ident]['status']='active'
        self.calls.append((ident,value))
        return dict(self.rows[ident])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.accounts=[{'id':i,'name':f'Fixture {i}','platform':'openai','type':'apikey','status':'active','schedulable':True,'group_ids':[4]} for i in (1,2,3)]
        self.api=FakeAPI(self.accounts)
        self.router=QualityRouter(self.store,self.api,True)
        self.slot=current_slot()
        self.store.sync(self.accounts,self.slot,'initial')

    def result(self, ident, answer, status=None, slot=None):
        slot=self.slot if slot is None else slot
        self.store.sync(self.accounts,slot,'timer')
        with self.store.db() as db:
            row=db.execute("SELECT id FROM runs WHERE account_id=? AND slot=? AND kind='candy'",(ident,slot)).fetchone()
        state=status or ('pass' if answer==21 else 'fail')
        results=([{'question_id':ORIGINAL['id'],'status':'pass'} for _ in range(2)] if answer==21 else
                 [{'question_id':ORIGINAL['id'],'status':'fail'}] if answer is not None else [])
        attempts=[{'question_id':ORIGINAL['id'],'status':r['status'],'stage':i,'request_id':f'{row["id"]}-{i}'}
                  for i,r in enumerate(results)]
        self.store.update(row['id'],status=state,answer=answer,finished_at=time.time(),
                          first_status=state if results else None,question_results=json.dumps(results),attempt_log=json.dumps(attempts))
        return row['id']

    def test_first_failure_requires_one_valid_completed_request(self):
        ident=self.result(2,29)
        with self.store.db() as db:row=dict(db.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone())
        self.assertEqual(row['bank_version'],VERSION)
        self.assertTrue(definite_verdict(row))
        attempts=json.loads(row['attempt_log'])
        invalid=([],attempts*2,[{**attempts[0],'request_id':None}],
                 [{**attempts[0],'status':'error'}],[{**attempts[0],'stage':1}])
        for log in invalid:
            with self.subTest(log=log):
                self.assertFalse(definite_verdict({**row,'attempt_log':json.dumps(log)}))

    def test_archived_two_pass_evidence_keeps_its_original_contract(self):
        for answer in (21,29):
            ident=self.result(2,answer)
            with self.store.db() as db:row=dict(db.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone())
            row['bank_version']=TWO_PASS_VERSION
            self.assertEqual(definite_verdict(row),answer==21)
            if answer==29:
                results=json.loads(row['question_results'])*2
                attempts=json.loads(row['attempt_log'])
                attempts.append({**attempts[0],'stage':1,'request_id':'old-second'})
                row.update(question_results=json.dumps(results),attempt_log=json.dumps(attempts))
                self.assertTrue(definite_verdict(row))

    def test_archived_bank_retains_distinct_question_confirmation(self):
        row={'status':'fail','bank_version':'closed-v1-dec7c5b1','question_results':json.dumps([
            {'question_id':'old-first','status':'fail'},{'question_id':'old-review','status':'fail'}])}
        self.assertTrue(definite_verdict(row))
        row['question_results']=json.dumps([{'question_id':'old-first','status':'fail'}]*2)
        self.assertFalse(definite_verdict(row))

    def test_historical_single_wrong_cannot_trigger_new_block(self):
        self.result(1,21)
        ident=self.result(2,29)
        self.store.update(ident,bank_version=None,question_results='[]')
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])

    def test_old_review_pass_cannot_restore_but_two_current_passes_can(self):
        self.result(1,21); ident=self.result(2,29)
        self.router.reconcile(); self.api.calls.clear()
        self.store.update(ident,status='error',first_status='fail',question_results=json.dumps([
            {'question_id':'first','status':'fail'},{'question_id':'review','status':'error'}]))
        self.router.reconcile(); self.assertEqual(self.api.calls,[])
        self.store.update(ident,status='pass',bank_version='original-candy-v1-20260914',question_results=json.dumps([
            {'question_id':'first','status':'fail'},{'question_id':'review','status':'pass'}]))
        self.router.reconcile(); self.assertEqual(self.api.calls,[])
        self.store.update(ident,bank_version=VERSION)
        self.result(2,21)
        self.router.reconcile(); self.assertEqual(self.api.calls,[(2,True)])

    def test_current_pass_requires_two_valid_distinct_consecutive_requests(self):
        ident=self.result(2,21)
        with self.store.db() as db:row=dict(db.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone())
        self.assertTrue(definite_verdict(row))
        attempts=json.loads(row['attempt_log'])
        for log in ([],attempts[:1],[attempts[0],{**attempts[1],'request_id':attempts[0]['request_id']}],
                    [attempts[0],{'status':'error','request_id':'interrupt'},attempts[1]]):
            self.assertFalse(definite_verdict({**row,'attempt_log':json.dumps(log)}))
        for version in (None,'original-candy-v1-20260914','closed-v1-dec7c5b1'):
            self.assertFalse(definite_verdict({**row,'bank_version':version}))

    def test_mixed_answers_disable_without_reporting_two_wrong_answers(self):
        self.result(1,21)
        for statuses in (['fail','pass'],['pass','fail']):
            ident=self.result(2,29)
            results=[{'question_id':ORIGINAL['id'],'status':s} for s in statuses]
            attempts=[{**r,'stage':i,'request_id':f'{ident}-{i}'} for i,r in enumerate(results)]
            self.store.update(ident,status='fail',first_status=statuses[0],question_results=json.dumps(results),attempt_log=json.dumps(attempts))
            self.api.rows[2]['schedulable']=True;self.api.calls.clear()
            self.router.reconcile()
            self.assertEqual(self.api.calls,[(2,False)])

    def test_failed_supplier_disabled_and_best_kept_callable(self):
        self.result(1,21);self.result(2,29);self.result(3,29)
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(2,False),(3,False)])
        self.assertTrue(self.api.rows[1]['schedulable'])
        self.assertEqual(self.router.state(1)['action'],'protected')
        self.router.reconcile()
        self.assertEqual(len(self.api.calls),2)

    def test_all_fail_still_reserves_highest_and_single_supplier_never_disabled(self):
        for ident in (1,2,3):self.result(ident,29)
        self.router.reconcile()
        self.assertEqual(sum(a['schedulable'] for a in self.api.rows.values()),1)
        self.assertTrue(self.api.rows[1]['schedulable'])

    def test_new_best_restored_before_old_best_is_disabled(self):
        self.result(1,21);self.result(2,29);self.result(3,29)
        self.router.reconcile();self.api.calls.clear()
        self.result(1,29);self.result(2,21)
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(2,True),(1,False)])
        self.assertEqual(self.router.state(2)['action'],'protected')

    def test_errors_and_unknown_answers_keep_previous_scheduling(self):
        self.result(1,21);self.result(2,29);self.router.reconcile();self.api.calls.clear()
        self.result(2,None,'error');self.result(3,None,'fail')
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertFalse(self.api.rows[2]['schedulable'])

    def test_paused_accounts_and_other_platforms_never_modified(self):
        self.result(1,21);self.result(2,29);self.result(3,29)
        self.store.controls.set_paused(public_id(2),True)
        self.api.rows[3]['platform']='anthropic'
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])

    def test_oauth_quota_threshold_is_not_reopened_by_quality_routing(self):
        thresholded={**self.accounts[1], 'type':'oauth', 'schedulable':False,
            'temp_unschedulable_reason':json.dumps({'source':'account_scheduling_threshold',
                'window':'7d','used_percent':97,'threshold_percent':97,
                'until_unix':int(time.time())+3600})}
        self.accounts[1]=thresholded
        self.api.rows[2]=dict(thresholded)
        self.store.sync(self.accounts,self.slot,'metadata')
        self.result(1,21)
        self.result(2,21)
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertFalse(self.api.rows[2]['schedulable'])

    def test_quota_exhausted_best_does_not_override_the_limit(self):
        self.api.rows[1].update(type='oauth',schedulable=False,rate_limit_reset_at=time.time()+3600)
        self.accounts[0]=dict(self.api.rows[1])
        self.result(1,21);self.result(2,29);self.result(3,29)
        self.router.reconcile()
        self.assertFalse(self.api.rows[1]['schedulable'])
        self.assertTrue(self.api.rows[2]['schedulable'])
        self.assertEqual(self.router.state(2)['action'],'protected')

    def test_quota_appearing_during_change_is_not_reopened(self):
        self.api.rows[2].update(type='oauth',schedulable=False,rate_limit_reset_at=time.time()+3600)
        self.assertFalse(self.router.change(self.accounts[1],'offline',True,'restored'))
        self.assertEqual(self.api.calls,[])

    def test_previous_manual_disabled_or_inactive_states_follow_new_definite_results(self):
        self.result(1,21);self.result(2,21);self.result(3,21)
        self.api.rows[2]['schedulable']=False
        self.api.rows[3]['status']='inactive'
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(2,True),(3,True)])
        self.assertTrue(self.api.rows[2]['schedulable'])
        self.assertEqual(self.api.rows[3]['status'],'active')

    def test_scope_leaving_accounts_are_never_written(self):
        scope={'group_ids':[4],'group_names':[],'exclude_names':['生图','image']}
        with patch.dict(config.CONFIG['platforms']['openai'],scope):
            self.result(1,21);self.result(2,29);self.result(3,29)
            self.api.rows[3].update(group_ids=[4,10],name='fixture 生图')
            self.router.reconcile()
        # 3 left the set by name, so only the in-scope failing supplier 2 is written.
        self.assertEqual(self.api.calls,[(2,False)])

    def test_supplier_pass_restores_even_without_winning(self):
        self.result(1,21);self.result(2,29)
        self.router.reconcile();self.api.calls.clear()
        self.result(1,21,slot=self.slot-2700)
        self.result(2,21)
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(2,True)])
        self.assertEqual(self.router.state(2)['action'],'restored')

    def test_failed_reserve_recovery_does_not_disable_any_more_suppliers(self):
        self.result(1,21);self.result(2,29);self.router.reconcile();self.api.calls.clear()
        self.result(1,29);self.result(2,21)
        self.api.fail=True
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertTrue(self.api.rows[1]['schedulable'])
        self.assertEqual(self.store.metadata()['quality_routing_error'],'RESERVE_RESTORE_FAILED')

    def test_pending_write_recovers_after_restart_without_extra_model_calls(self):
        self.result(1,21);ident=self.result(2,29)
        self.router.save(2,run_id=ident,pending_target=0,attempts=1,action='updating')
        self.api.rows[2]['schedulable']=False
        restarted=QualityRouter(Store(self.tmp.name),self.api,True)
        restarted.reconcile()
        self.assertEqual(restarted.state(2)['action'],'blocked')
        self.assertEqual(self.api.calls,[])

    def test_disabled_feature_does_not_read_or_write_upstream(self):
        api=Mock();QualityRouter(self.store,api,False).reconcile()
        api.accounts.assert_not_called();api.set_callable.assert_not_called()

    def test_three_failed_control_attempts_do_not_loop_forever(self):
        self.result(1,21);self.result(2,29);self.api.fail=True
        for _ in range(5):self.router.reconcile()
        self.assertEqual(self.router.state(2)['attempts'],3)
        self.assertTrue(self.api.rows[1]['schedulable'])

    def test_paused_highest_is_not_reenabled_and_next_highest_is_reserve(self):
        self.result(1,21);self.result(2,29);self.result(3,29)
        self.store.controls.set_paused(public_id(1),True)
        self.api.rows[1]['schedulable']=False
        self.router.reconcile()
        self.assertFalse(self.api.rows[1]['schedulable'])
        self.assertTrue(self.api.rows[2]['schedulable'])
        self.assertEqual(self.api.calls,[(3,False)])

    def test_resume_skips_results_from_before_resume(self):
        self.result(1,21);self.result(2,29)
        self.store.controls.set_paused(public_id(2),True)
        self.store.controls.set_paused(public_id(2),False)
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])


class RoutingAPITests(unittest.TestCase):
    def test_only_status_and_schedulable_are_written_and_readback_required(self):
        api=Sub2API('https://example.test','fake-admin-key')
        api.account_state=Mock(side_effect=[{'platform':'openai','name':'fixture','status':'inactive','schedulable':False,'group_ids':[4]},
                                           {'platform':'openai','status':'active','schedulable':True}])
        with patch('detector.upstream.requests.Session') as session:
            client=session.return_value.__enter__.return_value
            client.request.return_value.status_code=200
            client.request.return_value.json.return_value={'code':0}
            api.set_callable(10,True)
            calls=client.request.call_args_list
            self.assertEqual([call.args[0] for call in calls],['PUT','POST'])
            self.assertEqual([call.kwargs['json'] for call in calls],[{'status':'active'},{'schedulable':True}])
            self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in calls))
        self.assertEqual(api.account_state.call_count,2)

    def test_unsupported_platform_cannot_be_written(self):
        api=Sub2API('https://example.test','fake-admin-key')
        api.account_state=Mock(return_value={'platform':'unsupported','name':'fixture','status':'active'})
        with patch('detector.upstream.requests.Session') as session,self.assertRaises(ProbeError):
            api.set_callable(10,False)
        session.assert_not_called()

    def test_inactive_gpt_is_still_tested_to_obtain_a_new_verdict(self):
        _,_,payload=build_request({'platform':'openai','type':'apikey','status':'inactive','credentials':{'api_key':'fake'}},'candy')
        self.assertEqual(payload['model'],'gpt-6-astra')


if __name__=='__main__':unittest.main()


class BudgetWallTests(unittest.TestCase):
    def test_streaming_budget_walls_never_trip_a_circuit(self):
        log=[{'status':'error','code':'GENERATION_BUDGET_EXCEEDED','failure_class':'budget','request_id':'a'},
             {'status':'error','code':'GENERATION_BUDGET_EXCEEDED','failure_class':'budget','request_id':'b'}]
        self.assertFalse(consecutive_request_errors(log))

    def test_failure_classes_separate_outage_budget_and_local_errors(self):
        self.assertEqual(request_failure_class('GENERATION_BUDGET_EXCEEDED',True),'budget')
        self.assertEqual(request_failure_class('REQUEST_TIMEOUT',True),'upstream')
        self.assertEqual(request_failure_class('HTTP_500',True),'upstream')
        self.assertEqual(request_failure_class('WORKER_ERROR',True),'local')
        self.assertEqual(request_failure_class('HTTP_500',False),'local')
