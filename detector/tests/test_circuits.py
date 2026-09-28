"""Offline, account-isolated request circuit and recovery tests."""
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from detector.monitor import Store, execute_run, public_id
from detector.prompts import BENCHMARKS
from detector.quality_routing import QualityRouter, consecutive_request_errors
from detector.question_bank import ORIGINAL
from detector.schedule import current_slot
from detector.tests.test_quality_routing import FakeAPI
from detector.upstream import ProbeError


class CircuitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.slot=current_slot()
        self.accounts=[{'id':i,'name':f'Fixture {i}','platform':p,'type':'apikey','status':'active','schedulable':True,'group_ids':[4]}
                       for i,p in enumerate(('openai','openai','anthropic','grok','gemini'),1)]
        self.accounts += [{'id':i,'name':f'Reserve {i}','platform':p,'type':'apikey','status':'active','schedulable':True}
                          for i,p in ((6,'anthropic'),(7,'grok'),(8,'gemini'))]
        self.api=FakeAPI(self.accounts)
        self.api.account=self.api.account_state
        self.router=QualityRouter(self.store,self.api,True)
        self.store.sync(self.accounts,self.slot,'initial')
        self.pass_run(1)
        for ident in (6,7,8):
            self.pass_run(ident,slot=self.slot-2700)
            self.pass_run(ident)

    def run_for(self, ident, kind='candy', slot=None):
        slot=self.slot if slot is None else slot
        self.store.sync(self.accounts,slot,'timer')
        with self.store.db() as db:
            return dict(db.execute('SELECT * FROM runs WHERE account_id=? AND slot=? AND kind=?',(ident,slot,kind)).fetchone())

    def pass_run(self, ident, slot=None, status='pass'):
        run=self.run_for(ident,slot=slot)
        self.store.update(run['id'],status=status,answer=21 if status=='pass' else 29,
                          question_results=json.dumps([{'question_id':ORIGINAL['id'],'status':status} for _ in range(2)]),
                          attempt_log=json.dumps([{'question_id':ORIGINAL['id'],'status':status,'stage':i,'request_id':run['id']+str(i)} for i in range(2)]),
                          first_status=status,finished_at=time.time())
        return run

    def errors(self, ident, kind='candy', slot=None, log=None):
        run=self.run_for(ident,kind,slot)
        log=log if log is not None else [{'status':'error','code':'HTTP_503','failure_class':'upstream',
                                        'request_id':run['id']+str(i)} for i in range(2)]
        self.store.update(run['id'],status='error',error_code='HTTP_503',attempts=len(log),attempt_log=json.dumps(log),finished_at=time.time())
        return run

    def circuit(self, ident):
        with self.store.db() as db:
            row=db.execute('SELECT * FROM request_circuits WHERE account_id=?',(ident,)).fetchone()
        return dict(row) if row else None

    def test_all_four_platforms_trip_after_two_actual_request_errors(self):
        for ident in (2,3,4,5):self.errors(ident)
        self.router.reconcile()
        self.assertCountEqual(self.api.calls,[(2,False),(3,False),(4,False),(5,False)])
        self.assertTrue(self.api.rows[1]['schedulable'])
        self.assertTrue(all(self.circuit(i)['active'] for i in (2,3,4,5)))

    def test_one_error_then_pass_does_not_trip_and_all_attempts_are_fresh(self):
        run=self.run_for(2)
        request=Mock(side_effect=[ProbeError('HTTP_503','fixture'),{'text':'最终答案：21'},{'text':'最终答案：21'}])
        execute_run(self.store,self.api,run,time.monotonic()+3000,request,sleep=lambda _:None,quality_router=self.router)
        self.assertEqual(request.call_count,3)
        self.assertIsNone(self.circuit(2))
        ids=[c.kwargs['request_id'] for c in request.call_args_list]
        self.assertEqual(len(set(ids)),3)
        self.assertTrue(self.api.rows[2]['schedulable'])

    def test_second_error_trips_immediately_without_third_request(self):
        for ident,kind in ((2,'candy'),(3,'drawing')):
            request=Mock(side_effect=ProbeError('HTTP_429','fixture'))
            execute_run(self.store,self.api,self.run_for(ident,kind),time.monotonic()+3000,request,sleep=lambda _:None,quality_router=self.router)
            self.assertEqual(request.call_count,2)
            self.assertFalse(self.api.rows[ident]['schedulable'])
            self.assertEqual(self.router.state(ident)['action'],'circuit_open')

    def test_local_or_ungraded_errors_do_not_trip(self):
        for code in ('ACCOUNT_SYNC_NETWORK','MODEL_CONFIG_CHANGED','WORKER_ERROR','BATCH_DEADLINE','UNGRADABLE_ANSWER','INCOMPLETE_HTML','RESPONSE_TOO_LARGE'):
            log=[{'status':'error','code':code,'failure_class':'local','request_id':str(i)} for i in range(2)]
            self.errors(2,log=log);self.router.reconcile()
            self.assertIsNone(self.circuit(2))
        self.assertTrue(self.api.rows[2]['schedulable'])

    def test_duplicate_ids_or_intervening_reply_are_not_two_errors(self):
        a={'status':'error','code':'HTTP_503','failure_class':'upstream','request_id':'one'}
        for log in ([a], [a,a], [a,{'status':'fail','request_id':'answer'},dict(a,request_id='two')]):
            self.assertFalse(consecutive_request_errors(log))

    def test_same_round_quality_pass_cannot_close_drawing_circuit(self):
        self.pass_run(3)
        self.errors(3,'drawing')
        self.router.reconcile()
        self.assertFalse(self.api.rows[3]['schedulable'])
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(3,False)])
        self.assertEqual(self.circuit(3)['active'],1)

    def test_non_gpt_connectivity_restores_while_gpt_still_requires_quality(self):
        for ident in (2,3,4,5):self.errors(ident,slot=self.slot-2700)
        self.router.reconcile();self.api.calls.clear()
        for ident in (2,3,4,5):
            self.pass_run(ident,status='fail')
        self.router.reconcile()
        self.assertCountEqual(self.api.calls,[(3,True),(4,True),(5,True)])
        self.assertFalse(self.api.rows[2]['schedulable'])
        for ident in (2,3,4,5):self.pass_run(ident)
        self.router.reconcile()
        self.assertTrue(all(self.api.rows[i]['schedulable'] for i in (2,3,4,5)))
        self.assertTrue(all(not self.circuit(i)['active'] for i in (2,3,4,5)))
        count=len(self.api.calls)
        self.router.reconcile()
        self.assertEqual(len(self.api.calls),count)
        self.assertTrue(all(not self.circuit(i)['active'] for i in (2,3,4,5)))

    def test_paused_supplier_never_trips_or_restores(self):
        self.errors(3)
        self.store.controls.set_paused(public_id(3),True)
        self.router.reconcile()
        self.assertIsNone(self.circuit(3));self.assertEqual(self.api.calls,[])
        self.errors(4,slot=self.slot-2700);self.router.reconcile();self.api.calls.clear()
        self.pass_run(4)
        self.store.controls.set_paused(public_id(4),True)
        self.router.reconcile()
        self.assertEqual(self.api.calls,[]);self.assertFalse(self.api.rows[4]['schedulable'])

    def test_highest_gpt_is_protected_even_when_request_circuit_trips(self):
        self.errors(1,'drawing');self.errors(2)
        self.router.reconcile()
        self.assertTrue(self.api.rows[1]['schedulable'])
        self.assertEqual(self.router.state(1)['action'],'protected')
        self.assertEqual(self.circuit(1)['active'],1)
        self.assertFalse(self.api.rows[2]['schedulable'])

    def test_circuit_survives_restart_and_expired_test_history(self):
        self.errors(3);self.router.reconcile()
        self.store.cleanup(now=time.time()+86401)
        self.api.rows[3]['schedulable']=True
        restarted=QualityRouter(Store(self.tmp.name),self.api,True)
        restarted.reconcile()
        self.assertFalse(self.api.rows[3]['schedulable'])
        self.assertEqual(self.circuit(3)['active'],1)

    def test_legacy_errors_do_not_retroactively_trip(self):
        run=self.errors(3)
        with self.store.db() as db:db.execute('UPDATE runs SET request_policy=NULL WHERE id=?',(run['id'],))
        self.router.reconcile()
        self.assertIsNone(self.circuit(3));self.assertTrue(self.api.rows[3]['schedulable'])

    def test_failed_write_keeps_circuit_and_next_reconcile_confirms_it(self):
        self.errors(3);self.api.fail=True;self.router.reconcile()
        self.assertTrue(self.api.rows[3]['schedulable']);self.assertEqual(self.circuit(3)['active'],1)
        self.api.fail=False;self.router.reconcile()
        self.assertFalse(self.api.rows[3]['schedulable'])

    def test_other_platform_quality_failure_without_circuit_is_not_new_policy(self):
        for ident in (3,4,5):self.pass_run(ident,status='fail')
        self.router.reconcile();self.assertEqual(self.api.calls,[])

    def test_non_gpt_drawing_recovery_does_not_need_a_candy_pass(self):
        for ident in (3,4,5):self.errors(ident,slot=self.slot-2700)
        self.router.reconcile();self.api.calls.clear()
        for ident in (3,4,5):
            run=self.run_for(ident,'drawing')
            self.store.update(run['id'],status='pass',finished_at=time.time(),
                attempt_log=json.dumps([{'status':'pass','request_id':run['id']}]))
        self.router.reconcile()
        self.assertCountEqual(self.api.calls,[(3,True),(4,True),(5,True)])
        self.assertTrue(all(not self.circuit(i)['active'] for i in (3,4,5)))

    def test_non_gpt_complete_ungraded_reply_can_restore_connectivity(self):
        self.errors(3,slot=self.slot-2700);self.router.reconcile();self.api.calls.clear()
        run=self.run_for(3)
        self.store.update(run['id'],status='error',error_code='UNGRADABLE_ANSWER',
            finished_at=time.time(),attempt_log=json.dumps([{'status':'unknown','request_id':run['id']}]))
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(3,True)])
        self.assertFalse(self.circuit(3)['active'])

    def test_non_gpt_same_round_success_cannot_unlock_circuit(self):
        self.errors(3);self.router.reconcile();self.api.calls.clear()
        run=self.run_for(3,'drawing')
        self.store.update(run['id'],status='pass',finished_at=time.time(),
            attempt_log=json.dumps([{'status':'pass','request_id':run['id']}]))
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertTrue(self.circuit(3)['active'])

    def test_non_gpt_newer_incomplete_reply_blocks_earlier_success(self):
        self.errors(3,slot=self.slot-2700);self.router.reconcile();self.api.calls.clear()
        self.pass_run(3,status='fail')
        self.errors(3,'drawing',log=[{'status':'error','request_id':'incomplete',
            'code':'INCOMPLETE_RESPONSE','failure_class':'upstream'}])
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertTrue(self.circuit(3)['active'])

    def test_failed_gpt_reserve_read_does_not_block_other_platform_circuit(self):
        self.errors(3)
        read=self.api.account_state
        self.api.account_state=lambda ident: (_ for _ in ()).throw(ProbeError('HTTP_503','fixture')) if ident==1 else read(ident)
        self.router.reconcile()
        self.assertFalse(self.api.rows[3]['schedulable'])

    def test_failed_supplier_read_does_not_block_following_supplier(self):
        self.errors(3);self.errors(4)
        read=self.api.account_state
        self.api.account_state=lambda ident: (_ for _ in ()).throw(ProbeError('HTTP_503','fixture')) if ident==3 else read(ident)
        self.router.reconcile()
        self.assertEqual(self.router.state(3)['action'],'error')
        self.assertFalse(self.api.rows[4]['schedulable'])

    def test_each_platform_retains_own_reserve_even_when_all_circuits_trip(self):
        for ident in range(1,9):self.errors(ident,'drawing')
        self.router.reconcile()
        for ident in (1,6,7,8):
            self.assertTrue(self.api.rows[ident]['schedulable'])
            self.assertEqual(self.router.state(ident)['action'],'protected')
            self.assertTrue(self.circuit(ident)['active'])
        self.assertEqual(self.store.metadata()['quality_routing_reserves'],
                         {p:public_id(i) for p,i in [('openai',1),('anthropic',6),('grok',7),('gemini',8)]})

    def test_sole_non_gpt_supplier_is_restored_but_circuit_evidence_retained(self):
        for ident in (6,7,8):self.store.controls.set_paused(public_id(ident),True)
        for ident in (3,4,5):
            self.errors(ident);self.api.rows[ident]['schedulable']=False
        self.router.reconcile()
        self.assertCountEqual(self.api.calls,[(3,True),(4,True),(5,True)])
        self.assertTrue(all(self.circuit(i)['active'] for i in (3,4,5)))

    def test_reserve_recovery_precedes_disabling_previous_reserve(self):
        self.errors(3);self.errors(6,'drawing');self.router.reconcile()
        self.api.calls.clear()
        self.pass_run(3,slot=self.slot-2700)
        self.pass_run(3)
        self.pass_run(6,status='fail')
        self.router.reconcile()
        self.assertEqual(self.api.calls,[(3,True),(6,False)])
        self.assertEqual(self.router.state(3)['action'],'protected')
        self.assertTrue(self.circuit(3)['active'])

    def test_failed_non_gpt_reserve_does_not_disable_peers_or_block_other_platform(self):
        for ident in (3,4,5):self.errors(ident)
        read=self.api.account_state
        self.api.account_state=lambda ident: (_ for _ in ()).throw(ProbeError('HTTP_503','fixture')) if ident==6 else read(ident)
        self.router.reconcile()
        self.assertTrue(self.api.rows[3]['schedulable'])
        self.assertFalse(self.api.rows[4]['schedulable'])
        self.assertFalse(self.api.rows[5]['schedulable'])
        self.assertIsNone(self.store.metadata()['quality_routing_reserves']['anthropic'])
        self.assertEqual(self.store.metadata()['quality_routing_errors']['anthropic'],'HTTP_503')

    def test_paused_platform_is_idle_but_resumed_missing_supplier_warns(self):
        for ident in (3,6):self.store.controls.set_paused(public_id(ident),True)
        self.router.reconcile()
        self.assertNotIn('anthropic',self.store.metadata()['quality_routing_errors'])
        self.assertEqual(self.api.calls,[])
        resumed = self.store.controls.set_paused(public_id(3),False)
        self.api.rows.pop(3)
        with patch('detector.quality_routing.time.time',return_value=resumed['resume_at']+1):
            self.router.reconcile()
        self.assertEqual(self.store.metadata()['quality_routing_errors']['anthropic'],
                         'NO_ELIGIBLE_PLATFORM_RESERVE')

    def test_no_configured_platform_is_not_a_routing_failure(self):
        with self.store.db() as db:db.execute("UPDATE accounts SET enabled=0 WHERE platform='grok'")
        self.router.reconcile()
        self.assertNotIn('grok',self.store.metadata()['quality_routing_errors'])
        self.assertIsNone(self.store.metadata()['quality_routing_reserves']['grok'])

    def test_all_paused_category_never_restores_accounts_and_clears_reserve(self):
        self.router.reconcile()
        for ident in (3,6):
            self.store.controls.set_paused(public_id(ident),True)
            self.api.rows[ident]['schedulable']=False
        self.router.reconcile()
        self.assertEqual(self.api.calls,[])
        self.assertIsNone(self.store.metadata()['quality_routing_reserves']['anthropic'])


if __name__=='__main__':unittest.main()
