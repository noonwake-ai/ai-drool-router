"""Quota checks use synthetic account state; no credentials or network."""
import copy
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from detector.monitor import Store, execute_run, public_id
from detector.schedule import current_slot, next_slot
from detector.upstream import OAUTH_QUOTA_ERROR, ProbeError, Sub2API, oauth_quota_state


class QuotaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.account = {'platform':'openai', 'type':'oauth', 'credentials':{},
            '_quota_policy':{'account_scheduling_threshold':97, 'default_threshold_5h':0, 'default_threshold_7d':0},
            'extra':{'codex_7d_used_percent':97, 'codex_7d_reset_at':self.now+3600}}

    def state(self):
        return oauth_quota_state(self.account, self.now)

    def test_global_threshold_and_account_specific_window_are_both_respected(self):
        self.assertEqual(self.state()['threshold_percent'],97)
        self.account['extra'].update(auto_pause_7d_threshold=0.96, codex_7d_used_percent=96)
        self.assertEqual(self.state()['threshold_percent'],96)
        self.account['extra'].update(codex_7d_used_percent=95)
        self.assertFalse(self.state()['limited'])

    def test_account_override_does_not_erase_separate_window_policy(self):
        self.account['credentials']['account_scheduling_threshold']=100
        self.assertFalse(self.state()['limited'])
        self.account['extra']['auto_pause_7d_threshold']=0.96
        self.assertEqual(self.state()['threshold_percent'],96)

    def test_disabled_window_ignores_its_threshold_and_global_window_default(self):
        self.account['credentials']['account_scheduling_threshold']=100
        self.account['_quota_policy']['default_threshold_7d']=0.96
        self.assertTrue(self.state()['limited'])
        for flag in (True, 'true', '1', 1):
            self.account['extra']['auto_pause_7d_disabled']=flag
            self.assertFalse(self.state()['limited'])
        self.account['credentials']['account_scheduling_threshold']=97
        self.assertTrue(self.state()['limited'])

    def test_real_exhaustion_is_still_respected_without_a_soft_threshold(self):
        self.account['credentials']['account_scheduling_threshold']=100
        self.account['extra'].update(codex_7d_used_percent=100, auto_pause_7d_disabled=True)
        self.assertTrue(self.state()['limited'])

    def test_expired_absolute_reset_never_falls_back_to_relative_time(self):
        self.account['extra'].update(codex_7d_reset_at=self.now-1,
            codex_7d_reset_after_seconds=3600, codex_usage_updated_at=self.now)
        self.assertFalse(self.state()['limited'])

    def test_relative_reset_is_anchored_and_both_windows_are_checked(self):
        self.account['extra']={'auto_pause_5h_threshold':0.96, 'codex_5h_used_percent':96,
            'codex_5h_reset_after_seconds':600, 'codex_usage_updated_at':self.now-500}
        self.assertEqual(self.state()['window'],'5h')
        self.now += 101
        self.assertFalse(self.state()['limited'])

    def test_snapshot_age_alone_is_not_evidence_of_quota_recovery(self):
        self.account['extra']['codex_usage_updated_at']=self.now-10800
        self.assertTrue(self.state()['limited'])
        self.account['extra']['codex_7d_used_percent']=20
        self.assertFalse(self.state()['limited'])

    def test_identity_mismatch_does_not_apply_another_accounts_snapshot(self):
        self.account['credentials']['chatgpt_account_id']='current-account'
        self.account['extra']['chatgpt_account_id']='old-account'
        self.assertFalse(self.state()['limited'])

    def test_expired_live_pause_is_not_kept_by_retained_reason(self):
        self.account['extra']={}
        self.account.update(temp_unschedulable_until=self.now-1,
            temp_unschedulable_reason=json.dumps({'source':'account_scheduling_threshold', 'until_unix':self.now+3600}))
        self.assertFalse(self.state()['limited'])
        self.account['temp_unschedulable_reason']=json.dumps({'source':'account_scheduling_threshold'})
        self.assertFalse(self.state()['limited'])

    def test_other_oauth_rate_limit_expires_but_api_key_is_out_of_scope(self):
        self.account.update(platform='grok',rate_limit_reset_at=self.now+60)
        self.assertTrue(self.state()['limited'])
        self.account['rate_limit_reset_at']=self.now-1
        self.assertFalse(self.state()['limited'])
        self.account.update(type='apikey',rate_limit_reset_at=self.now+60)
        self.assertFalse(self.state()['limited'])

    def test_policy_read_is_fresh_and_projects_only_allowlisted_fields(self):
        api=Sub2API('https://offline.test','unused')
        def get(path, params=None):
            if path.startswith('/accounts/'):
                return copy.deepcopy(self.account)
            if path=='/settings':
                return {'account_scheduling_thresholds':{'openai':self.threshold}, 'unrelated':'not-projected'}
            return {'openai_account_quota_auto_pause':{'default_threshold_5h':0.96,'default_threshold_7d':0.97}}
        api.get=Mock(side_effect=get)
        self.threshold=97
        self.assertEqual(api.account_state(1)['_quota_policy']['account_scheduling_threshold'],97)
        self.threshold=96
        policy=api.account_state(1)['_quota_policy']
        self.assertEqual(policy,{'account_scheduling_threshold':96,'default_threshold_5h':0.96,'default_threshold_7d':0.97})
        self.assertEqual(api.get.call_count,6)

    def test_missing_policy_prevents_request_instead_of_assuming_available(self):
        api=Sub2API('https://offline.test','unused')
        api.get=Mock(side_effect=[self.account,{},{}])
        with self.assertRaises(ProbeError) as error:
            api.account_state(1)
        self.assertEqual(error.exception.code,'ACCOUNT_QUOTA_UNKNOWN')

    def test_last_moment_quota_guard_prevents_any_scheduling_write(self):
        self.account.update(status='active',schedulable=False,group_ids=[4])
        self.account['extra']['codex_7d_reset_at']=time.time()+3600
        api=Sub2API('https://offline.test','unused')
        api.account_state=Mock(return_value=self.account)
        with patch('detector.upstream.requests.Session') as session:
            for enabled in (True,False):
                with self.assertRaises(ProbeError) as error:
                    api.set_callable(1,enabled)
                self.assertEqual(error.exception.code,OAUTH_QUOTA_ERROR)
            session.assert_not_called()


class QuotaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.slot=current_slot()
        self.account={'id':1,'name':'Offline OAuth','platform':'openai','type':'oauth',
            'status':'active','schedulable':False,'group_ids':[4], 'credentials':{},
            'extra':{'auto_pause_7d_threshold':0.96,'codex_7d_used_percent':96,'codex_7d_reset_at':time.time()+3600}}
        self.store.sync([self.account],self.slot,'initial')
        self.api=Mock();self.api.account.return_value=self.account
        self.router=Mock()
        self.deadline=time.monotonic()+4000

    def detail(self, row):
        return json.loads((self.store.public/(row['id']+'.json')).read_text())

    def test_both_tasks_skip_without_request_ledger_or_control_mutation(self):
        controls=self.store.controls.read()
        with patch.object(self.store.costs,'begin') as ledger:
            for row in self.store.pending(self.slot):
                request=Mock()
                execute_run(self.store,self.api,row,self.deadline,request,quality_router=self.router)
                request.assert_not_called()
                self.assertEqual(self.detail(row)['status'],'quota')
                self.assertEqual(self.detail(row)['attempts'],0)
            ledger.assert_not_called()
        self.router.reconcile.assert_not_called()
        self.assertEqual(self.store.controls.read(),controls)

    def test_next_cycle_rechecks_and_resumes_only_after_reset(self):
        row=self.store.pending(self.slot)[0]
        execute_run(self.store,self.api,row,self.deadline,Mock())
        self.account['extra']['codex_7d_reset_at']=time.time()-1
        slot=next_slot(self.slot)
        self.store.sync([self.account],slot,'timer')
        row=next(r for r in self.store.pending(slot) if r['kind']=='candy')
        request=Mock(return_value={'text':'最终答案：21'})
        execute_run(self.store,self.api,row,self.deadline,request)
        self.assertEqual(request.call_count,2)
        self.assertEqual(self.detail(row)['status'],'pass')
        self.assertEqual(self.api.account.call_count,3)
        self.assertFalse(self.store.controls.read().get(public_id(1),{}).get('paused',False))

    def test_retry_and_review_recheck_quota_without_adding_an_attempt(self):
        for kind, first in (('candy', ProbeError('HTTP_503','offline')), ('drawing', ProbeError('HTTP_503','offline')), ('candy', {'text':'最终答案：21'})):
            with self.subTest(kind=kind,first=type(first).__name__):
                slot=next_slot(self.slot);self.slot=slot
                self.store.sync([self.account],slot,'timer')
                row=next(r for r in self.store.pending(slot) if r['kind']==kind)
                available=copy.deepcopy(self.account);available['extra']['codex_7d_used_percent']=20
                self.api.account.side_effect=[available,self.account]
                request=Mock(side_effect=[first])
                execute_run(self.store,self.api,row,self.deadline,request,sleep=lambda _:None,quality_router=self.router)
                request.assert_called_once()
                detail=self.detail(row)
                self.assertEqual(detail['status'],'quota')
                self.assertEqual(detail['attempts'],1)
                self.assertEqual(len(detail['attempt_log']),1)
        self.router.reconcile.assert_not_called()
