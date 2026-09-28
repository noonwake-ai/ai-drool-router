# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Definite candy verdicts and persisted circuits; retain a reserve per platform."""
import hashlib
import json
import re
import threading
import time

from . import config
from .prompts import BENCHMARKS
from .question_bank import ORIGINAL, TWO_PASS_VERSION, VERSION, first_verdict, two_request_verdict
from .upstream import ProbeError, in_evaluation_scope, oauth_quota_state

REQUEST_POLICY = 'two-request-errors-v1'
UPSTREAM_ERRORS = frozenset(('CONNECT_TIMEOUT','FIRST_RESPONSE_TIMEOUT','STREAM_IDLE_TIMEOUT',
    'REQUEST_TIMEOUT','NETWORK_ERROR','STREAM_INTERRUPTED','UPSTREAM_RESPONSE_ERROR',
    'INCOMPLETE_RESPONSE','INVALID_RESPONSE','EMPTY_RESPONSE'))
# Our own generation wall cut a stream that was still delivering data. The supplier answered;
# the budget ran out. Count it as a failed evaluation, never as an upstream outage.
BUDGET_ERRORS = frozenset(('GENERATION_BUDGET_EXCEEDED',))


def upstream_failure(code):
    return bool(re.fullmatch(r'HTTP_[1-5]\d\d', code or '')) or code in UPSTREAM_ERRORS


def request_failure_class(code, request_called):
    if not request_called:
        return 'local'
    if code in BUDGET_ERRORS:
        return 'budget'
    return 'upstream' if upstream_failure(code) else 'local'


def consecutive_request_errors(log):
    """Only true upstream outages trip a circuit; budget truncation never counts."""
    if len(log) < 2:
        return False
    last = log[-2:]
    return (all(a.get('status')=='error' and a.get('failure_class')=='upstream'
                and upstream_failure(a.get('code')) and a.get('request_id') for a in last)
            and last[0]['request_id'] != last[1]['request_id'])


def account_key(account_id):
    return hashlib.sha256((config.get('identity_salt')+'-account-'+str(account_id)).encode()).hexdigest()[:12]


class QualityRouter:
    def __init__(self, store, api, enabled=False):
        self.store, self.api, self.enabled = store, api, enabled
        self.lock = threading.RLock()

    def state(self, account_id):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM quality_routing WHERE account_id=?',(account_id,)).fetchone()
        return dict(row) if row else {'pending_target':None, 'run_id':None, 'attempts':0}

    def save(self, account_id, **values):
        allowed = {'run_id','pending_target','action','error_code','attempts'}
        if set(values)-allowed:
            raise ValueError('INVALID_ROUTING_FIELD')
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO quality_routing(account_id,updated_at) VALUES (?,?)',(account_id,time.time()))
            db.execute('UPDATE quality_routing SET '+','.join(k+'=?' for k in values)+',updated_at=? WHERE account_id=?',
                       (*values.values(),time.time(),account_id))

    def change(self, account, run_id, target, reason):
        account_id = account['id']
        state = self.state(account_id)
        # Persist intent before a network write so a restart can reconcile an uncertain acknowledgement.
        attempts = state['attempts'] if state['run_id']==run_id and state['pending_target']==int(target) else 0
        if attempts >= 3:
            return False
        self.save(account_id,run_id=run_id,pending_target=int(target),action='updating',attempts=attempts+1,error_code=None)
        try:
            if self.store.controls.read().get(account_key(account_id),{}).get('paused'):
                self.save(account_id,pending_target=None,action='paused')
                return False
            fresh = self.api.account_state(account_id)
            if not in_evaluation_scope(fresh) or fresh.get('platform')!=account.get('platform'):
                self.save(account_id,pending_target=None,action='excluded')
                return False
            if oauth_quota_state(fresh)['limited']:
                self.save(account_id,pending_target=None,action='quota',error_code=None)
                return False
            if type(fresh.get('schedulable')) is not bool:
                raise ProbeError('ROUTING_INVALID_STATE','Sub2API 未提供明确的调度状态')
            if fresh['schedulable'] != target or (target and fresh.get('status')!='active'):
                self.api.set_callable(account_id,target)
            self.save(account_id,pending_target=None,action=reason,error_code=None)
            with self.store.db() as db:
                db.execute('INSERT INTO quality_routing_events(account_id,run_id,created_at,target,action) VALUES (?,?,?,?,?)',
                           (account_id,run_id,time.time(),int(target),reason))
            return True
        except Exception as exc:
            code = exc.code if isinstance(exc,ProbeError) else 'ROUTING_INTERNAL_ERROR'
            self.save(account_id,action='error',error_code=code)
            return False

    def reconcile(self):
        if not self.enabled:
            return
        with self.lock:
            try:
                self._reconcile()
            except Exception as exc:
                self.store.meta('quality_routing_error',exc.code if isinstance(exc,ProbeError) else 'ROUTING_INTERNAL_ERROR')
            finally:
                self.store.publish_state()

    def _reconcile(self):
        now = time.time()
        controls = self.store.controls.read()
        with self.store.db() as db:
            local = {r['account_id']:dict(r) for r in db.execute('SELECT * FROM accounts WHERE enabled=1')}
            runs = [dict(r) for r in db.execute('''SELECT id,account_id,slot,status,answer,model,platform,kind,
                first_status,bank_version,question_results,attempt_log,request_policy,finished_at
                FROM runs WHERE slot>=? ORDER BY slot,finished_at,id''',(now-86400,))]
        histories = {}
        for run in runs:
            if run['model']==BENCHMARKS.get(run['platform'],{}).get('model') and run['kind']=='candy':
                histories.setdefault(run['account_id'],[]).append(run)
            if (run['account_id'] in local and run['platform']==local[run['account_id']]['platform']
                    and run['model']==BENCHMARKS.get(run['platform'],{}).get('model')
                    and run['request_policy']==REQUEST_POLICY and run['status']=='error'
                    and run['finished_at'] and consecutive_request_errors(json.loads(run['attempt_log']))
                    and not self.store.controls.blocked(account_key(run['account_id']),run['slot'])):
                with self.store.db() as db:
                    db.execute('''INSERT INTO request_circuits(account_id,run_id,trigger_slot,opened_at)
                        VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET run_id=excluded.run_id,
                        trigger_slot=excluded.trigger_slot,opened_at=excluded.opened_at,active=1,
                        recovered_run_id=NULL,recovered_at=NULL
                        WHERE excluded.trigger_slot>request_circuits.trigger_slot OR
                        (excluded.trigger_slot=request_circuits.trigger_slot AND excluded.opened_at>request_circuits.opened_at)''',
                        (run['account_id'],run['id'],run['slot'],run['finished_at']))
        with self.store.db() as db:
            circuits = {r['account_id']:dict(r) for r in db.execute('SELECT * FROM request_circuits WHERE active=1')}
        candidates = {platform:[] for platform in BENCHMARKS}
        expected = {a['platform'] for ident,a in local.items()
                    if not controls.get(account_key(ident),{}).get('paused')
                    and controls.get(account_key(ident),{}).get('resume_at',0)<=now}
        for account in self.api.accounts():
            ident = account['id']
            if ident not in local or account.get('platform')!=local[ident]['platform'] or not in_evaluation_scope(account):
                continue
            control = controls.get(account_key(ident),{})
            if control.get('paused') or control.get('resume_at',0)>now:
                continue
            # Sub2API may make an OpenAI OAuth account temporarily
            # unschedulable at an account-specific 96%/97% threshold. Keep
            # quality routing read-only until the fresh account state recovers.
            if account.get('platform') == 'openai' and account.get('type') == 'oauth':
                try:
                    fresh = self.api.account_state(ident)
                except Exception:
                    # Preserve the existing isolation contract: a failed GPT
                    # state read must not prevent other platforms from being
                    # reconciled in this pass.
                    continue
                if (fresh.get('platform') != account.get('platform') or
                        oauth_quota_state(fresh, now)['limited']):
                    continue
            rows = histories.get(ident,[])
            state = self.state(ident)
            answered = [first_verdict(r) for r in rows if first_verdict(r) in ('pass','fail')]
            rate = answered.count('pass')/len(answered) if answered else -1
            score = (rate, int(bool(answered) and answered[-1]=='pass'),len(answered),
                     int(ident not in circuits),-ident)
            candidates[account['platform']].append((score,account,rows,state))
        reserves, errors = {}, {}
        for platform, entries in candidates.items():
            # An intentionally idle category has no reserve obligation. Quota,
            # eligibility and API failures for active categories still warn.
            if platform not in expected:
                reserves[platform] = None
                continue
            try:
                reserve, error = self.reconcile_platform(platform,entries,circuits)
            except Exception as exc:
                reserve = None
                error = exc.code if isinstance(exc,ProbeError) else 'ROUTING_INTERNAL_ERROR'
            reserves[platform] = account_key(reserve) if reserve is not None else None
            if error:
                errors[platform] = error
        self.store.meta('quality_routing_reserves',reserves)
        self.store.meta('quality_routing_errors',errors)
        # Keep the original GPT metadata for older readers.
        self.store.meta('quality_routing_reserve',reserves.get('openai'))
        self.store.meta('quality_routing_error',errors.get('openai') or next(iter(errors.values()),None))

    def reconcile_platform(self, platform, candidates, circuits):
        if not candidates:
            return None, 'NO_ELIGIBLE_GPT_RESERVE' if platform=='openai' else 'NO_ELIGIBLE_PLATFORM_RESERVE'
        candidates.sort(key=lambda item:item[0],reverse=True)
        _, best, best_rows, _ = candidates[0]
        best_id = best['id']
        # Restore/verify the reserve before removing any other supplier from scheduling.
        current_best = self.api.account_state(best_id)
        best_control = self.store.controls.read().get(account_key(best_id),{})
        if (best_control.get('paused') or best_control.get('resume_at',0)>time.time()
                or current_best.get('platform')!=platform
                or not in_evaluation_scope(current_best) or oauth_quota_state(current_best)['limited']):
            return None, 'RESERVE_STATE_CHANGED'
        evidence = best_rows[-1]['id'] if best_rows else 'reserve'
        if current_best.get('schedulable') is not True or current_best.get('status')!='active':
            if not self.change(best,evidence,True,'protected'):
                return None, 'RESERVE_RESTORE_FAILED'
        self.save(best_id,pending_target=None,action='protected',error_code=None)
        for _, account, rows, state in candidates[1:]:
            self.reconcile_candidate(account,rows,state,circuits.get(account['id']),best_id)
        # Keep circuit evidence until the platform's recovery contract is met.
        circuit = circuits.get(best_id)
        recovery = self.recovery_run(best_rows,circuit) if circuit else None
        if recovery:
            self.close_circuit(best_id,recovery['id'])
        return best_id, None

    def reconcile_candidate(self, account, rows, state, circuit, best_id=None):
        try:
            self.reconcile_account(account,rows,state,circuit,best_id)
        except Exception as exc:
            self.save(account['id'],action='error',error_code=exc.code if isinstance(exc,ProbeError) else 'ROUTING_INTERNAL_ERROR')

    def recovery_run(self, rows, circuit):
        with self.store.db() as db:
            account = db.execute('SELECT platform,model FROM accounts WHERE account_id=? AND enabled=1',
                                 (circuit['account_id'],)).fetchone()
            if account and account['platform'] in ('anthropic','grok','gemini'):
                # Quality only affects priority for these platforms. Use the latest
                # completed request test, including drawing, never an earlier success
                # hidden by a newer failure or a success from the tripping round.
                latest = db.execute('''SELECT * FROM runs WHERE account_id=? AND platform=?
                    AND model=? AND request_policy=? AND slot>? AND finished_at>?
                    AND kind IN ('candy','drawing') AND status IN ('pass','fail','error')
                    ORDER BY finished_at DESC,slot DESC,id DESC LIMIT 1''',
                    (circuit['account_id'],account['platform'],account['model'],REQUEST_POLICY,
                     circuit['trigger_slot'],circuit['opened_at'])).fetchone()
                if not latest or self.store.controls.blocked(account_key(circuit['account_id']),latest['slot']):
                    return None
                attempts = json.loads(latest['attempt_log'])
                if (attempts and attempts[-1].get('request_id')
                        and attempts[-1].get('status') in ('pass','fail','unknown')):
                    return dict(latest)
                return None
        run = rows[-1] if rows else None
        if (run and run['slot']>circuit['trigger_slot'] and run['status']=='pass' and definite_verdict(run)
                and not self.store.controls.blocked(account_key(run['account_id']),run['slot'])):
            return run
        return None

    def close_circuit(self, account_id, run_id):
        with self.store.db() as db:
            db.execute('UPDATE request_circuits SET active=0,recovered_run_id=?,recovered_at=? WHERE account_id=?',
                       (run_id,time.time(),account_id))

    def reconcile_account(self, account, rows, state, circuit, best_id=None):
        run = rows[-1] if rows else None
        recovery = self.recovery_run(rows,circuit) if circuit else None
        if circuit:
            target = recovery is not None
            run_id = recovery['id'] if recovery else circuit['run_id']
            reason = 'restored' if target else 'circuit_open'
        else:
            if (account['platform']!='openai' or not run or not definite_verdict(run)
                    or self.store.controls.blocked(account_key(account['id']),run['slot'])):
                # Remove an obsolete reserve badge without changing callability.
                if state.get('action')=='protected':
                    fresh = self.api.account_state(account['id'])
                    self.save(account['id'],action='enabled' if fresh.get('schedulable') is True and fresh.get('status')=='active' else 'excluded')
                return
            target = run['status']=='pass'
            run_id = run['id']
            reason = 'restored' if target else 'blocked'
        if self.store.controls.read().get(account_key(account['id']),{}).get('paused'):
            return
        if not target and best_id:
            reserve = self.api.account_state(best_id)
            if (reserve.get('schedulable') is not True or reserve.get('status')!='active'
                    or reserve.get('platform')!=account['platform'] or not in_evaluation_scope(reserve)
                    or self.store.controls.read().get(account_key(best_id),{}).get('paused')
                    or oauth_quota_state(reserve)['limited']):
                self.store.meta('quality_routing_error','RESERVE_STATE_CHANGED')
                return
        fresh = self.api.account_state(account['id'])
        if fresh.get('platform')!=account['platform'] or not in_evaluation_scope(fresh):
            return
        if oauth_quota_state(fresh)['limited']:
            return
        already_set = fresh.get('schedulable') is target and (not target or fresh.get('status')=='active')
        if already_set:
            action = reason if circuit or not target else 'enabled'
            if state.get('run_id')!=run_id or state.get('action') not in (action,'restored') or state['pending_target'] is not None:
                self.save(account['id'],run_id=run_id,pending_target=None,action=action,error_code=None)
            changed = True
        else:
            changed = self.change(account,run_id,target,reason)
        if changed and circuit and target:
            self.close_circuit(account['id'],run_id)


def definite_verdict(run):
    if run.get('bank_version') in (VERSION,TWO_PASS_VERSION):
        verdict = two_request_verdict(json.loads(run['question_results']),json.loads(run.get('attempt_log') or '[]'),
                                      fail_fast=run['bank_version']==VERSION)
        return verdict is not None and run['status']==verdict
    # Old passes do not meet the new recovery contract; preserve old failure evidence.
    if run['status']=='pass':
        return False
    if not run.get('bank_version'):
        return False
    results = json.loads(run['question_results'])
    if run['status']!='fail' or len(results)!=2 or not all(r['status']=='fail' for r in results):
        return False
    if run['bank_version']=='original-candy-v1-20260914':
        # Same-question confirmation needs two separately issued wrong replies.
        attempts = [a for a in json.loads(run.get('attempt_log') or '[]') if a.get('status')=='fail']
        return (all(r['question_id']==ORIGINAL['id'] for r in results)
                and len(attempts)==2 and [a.get('stage') for a in attempts]==[0,1]
                and all(a.get('question_id')==ORIGINAL['id'] and a.get('request_id') for a in attempts)
                and attempts[0]['request_id']!=attempts[1]['request_id'])
    return results[0]['question_id']!=results[1]['question_id']
