# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Composite supplier score: intelligence, cost, stability, and speed."""
import fcntl
import hashlib
import json
import threading
import time

from . import config
from .supplier_prices import Prices, price_signature, valid_multiplier
from .supplier_speed import recent_speed
from .prompts import BENCHMARKS
from .question_bank import VERSION, TWO_PASS_VERSION, two_request_verdict
from .upstream import ProbeError, in_evaluation_scope, oauth_quota_state

_weights = config.get('routing.weights') or {}
WEIGHT_INTELLIGENCE = float(_weights.get('intelligence', 0.36))
WEIGHT_COST = float(_weights.get('cost', 0.36))
WEIGHT_STABILITY = float(_weights.get('stability', 0.18))
WEIGHT_SPEED = float(_weights.get('speed', 0.10))
PRIORITY_MIN = int(config.get('routing.priority_min', 100))
PRIORITY_MAX = int(config.get('routing.priority_max', 100100))
STABILITY_ROUNDS = max(1, int(config.get('routing.stability_rounds', 3)))
QUALITY_ROUNDS = max(1, int(config.get('routing.rounds', 3)))
SCORE_VERSION = 'recent%(rounds)d-int%(intel).0f-cost%(cost).0f-stability%(stab).0f-speed%(speed).0f-v1' % {
    'rounds': QUALITY_ROUNDS,
    'intel': WEIGHT_INTELLIGENCE * 100,
    'cost': WEIGHT_COST * 100,
    'stab': WEIGHT_STABILITY * 100,
    'speed': WEIGHT_SPEED * 100,
}


def account_key(ident):
    return hashlib.sha256((config.get('identity_salt')+'-account-'+str(ident)).encode()).hexdigest()[:12]


def stability_score(successful, failed):
    """Observed request completion rate, not a confidence bound or a quality score."""
    total = successful+failed
    return 100*successful/total if total else None


def recent_stability(rows, local):
    """Use the latest three slots with completed tests; keep failed retry attempts."""
    rounds = {}
    seen = set()
    for row in sorted(rows, key=lambda r: (r['slot'], r['kind']), reverse=True):
        ident = row['account_id']
        if (ident not in local or row['model'] != local[ident]['model']
                or row['status'] not in ('pass', 'fail', 'error')):
            continue
        if row['kind'] == 'candy' and row['bank_version'] not in (VERSION, TWO_PASS_VERSION):
            continue
        attempts = []
        for attempt in json.loads(row['attempt_log']):
            request_id = attempt.get('request_id')
            status = attempt.get('status')
            success = status in ('pass', 'fail', 'unknown')
            failed = status == 'error' and attempt.get('failure_class') in ('upstream','budget')
            if not request_id or (ident, request_id) in seen or not (success or failed):
                continue
            seen.add((ident, request_id))
            attempts.append(success)
        if not attempts:
            continue
        slots = rounds.setdefault(ident, {})
        if row['slot'] not in slots and len(slots) >= STABILITY_ROUNDS:
            continue
        slots.setdefault(row['slot'], []).extend(attempts)
    result = {}
    for ident, slots in rounds.items():
        attempts = [value for values in slots.values() for value in values]
        result[ident] = {'successful_requests': sum(attempts),
            'failed_requests': len(attempts)-sum(attempts), 'stability_rounds': len(slots),
            'start_at': min(slots), 'end_at': max(slots)}
    return result


def recent_quality(rows, local):
    """Keep three completed candy rounds; request errors occupy a slot, not a verdict."""
    rounds = {}
    for row in sorted(rows, key=lambda r: r['slot'], reverse=True):
        ident = row['account_id']
        if (ident not in local or row['model'] != local[ident]['model'] or row['kind'] != 'candy'
                or row['status'] not in ('pass', 'fail', 'error')
                or row['bank_version'] not in (VERSION, TWO_PASS_VERSION)):
            continue
        attempts = json.loads(row['attempt_log'])
        if not any(a.get('request_id') and (a.get('status') in ('pass', 'fail', 'unknown')
                or a.get('status') == 'error' and a.get('failure_class') in ('upstream','budget')) for a in attempts):
            continue
        slots = rounds.setdefault(ident, {})
        if row['slot'] in slots or len(slots) >= QUALITY_ROUNDS:
            continue
        verdict = two_request_verdict(json.loads(row['question_results']), attempts,
            fail_fast=row['bank_version'] == VERSION)
        slots[row['slot']] = verdict if verdict in ('pass', 'fail') and verdict == row['status'] else None
    return {ident: {'pass': sum(v == 'pass' for v in slots.values()),
        'fail': sum(v == 'fail' for v in slots.values()), 'quality_rounds': len(slots),
        'quality_start_at': min(slots), 'quality_end_at': max(slots)} for ident, slots in rounds.items()}


def score(multiplier, quality_pass, quality_fail, successful, failed, speed=None):
    quality_total = quality_pass+quality_fail
    cost = 100/(1+multiplier) if multiplier is not None else None
    quality = 100*quality_pass/quality_total if quality_total else None
    stability = stability_score(successful,failed)
    effective_speed = 50 if speed is None else speed
    total = (WEIGHT_COST*cost + WEIGHT_INTELLIGENCE*quality + WEIGHT_STABILITY*stability
             + WEIGHT_SPEED*effective_speed
             if all(v is not None for v in (cost,quality,stability)) else None)
    return {'cost_score':cost,'quality_score':quality,'stability_score':stability,'score':total,
            'speed_score':speed,'speed_effective_score':effective_speed,
            'target_priority':PRIORITY_MIN+round((100-total)*1000) if total is not None else None,
            'quality_pass':quality_pass,'quality_fail':quality_fail,'successful_requests':successful,
            'failed_requests':failed,'availability':100*successful/(successful+failed) if successful+failed else None}


class PriorityRouter:
    def __init__(self, store, api, enabled=False):
        self.store, self.api, self.enabled = store, api, enabled
        self.lock = threading.RLock()

    def reconcile(self, prices_only=False):
        if not self.enabled:
            return
        with self.lock, (self.store.private/'priority.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if prices_only else 0))
            except BlockingIOError:
                return 'busy'
            try:
                prices = Prices(self.store.root/'controls').read()
                meta = self.store.metadata()
                if (prices_only and meta.get('priority_price_signature') == price_signature(prices)
                        and meta.get('priority_score_version') == SCORE_VERSION):
                    return 'unchanged'
                self._reconcile()
                self.store.meta('priority_error',None)
                return 'reconciled'
            except Exception as exc:
                self.store.meta('priority_error',exc.code if isinstance(exc,ProbeError) else 'PRIORITY_DATA_UNAVAILABLE')
            finally:
                self.store.publish_state()

    def _reconcile(self):
        now = time.time()
        prices = Prices(self.store.root/'controls').read(now)
        signature = price_signature(prices)
        controls = self.store.controls.read()
        accounts = self.api.accounts()
        unpriced_platforms = {a['platform'] for a in accounts if in_evaluation_scope(a)
            and not controls.get(account_key(a['id']),{}).get('paused')
            and prices.get(account_key(a['id']),{}).get('multiplier') is None}
        with self.store.db() as db:
            local = {r['account_id']:dict(r) for r in db.execute('SELECT * FROM accounts WHERE enabled=1')}
            rows = [dict(r) for r in db.execute('''SELECT account_id,slot,kind,status,model,bank_version,question_results,
                attempt_log,tokens FROM runs WHERE slot>=? AND status IN ('pass','fail','error')''',(now-86400,))]
        stability = recent_stability(rows, local)
        counts = recent_quality(rows, local)
        speeds = recent_speed(rows, local)
        projected = {}
        def planned_priority(account):
            ident=account['id'];quality=counts.get(ident,{'pass':0,'fail':0});traffic=stability.get(ident,{})
            value=score(prices.get(account_key(ident),{}).get('multiplier'),quality['pass'],quality['fail'],
                        traffic.get('successful_requests',0),traffic.get('failed_requests',0),speeds.get(ident,{}).get('speed_score'))
            return value['target_priority'] or PRIORITY_MAX
        failed_platforms = set()
        for account in sorted(accounts,key=planned_priority,reverse=True):
            ident = account['id']
            if ident not in local or not in_evaluation_scope(account) or account['platform']!=local[ident]['platform']:
                continue
            key = account_key(ident)
            traffic = stability.get(ident)
            quality = counts.get(ident,{'pass':0,'fail':0})
            price = prices.get(key,{}).get('multiplier')
            values = score(price,quality['pass'],quality['fail'],
                traffic['successful_requests'] if traffic else 0,traffic['failed_requests'] if traffic else 0,
                speeds.get(ident,{}).get('speed_score'))
            values.update(speeds.get(ident,{}))
            values.update(multiplier=price,actual_priority=account.get('priority'),
                quality_rounds=quality.get('quality_rounds',0), quality_round_limit=QUALITY_ROUNDS,
                quality_source='recent-candy-rounds', quality_start_at=quality.get('quality_start_at'),
                quality_end_at=quality.get('quality_end_at'),
                stability_rounds=traffic['stability_rounds'] if traffic else 0,
                stability_round_limit=STABILITY_ROUNDS, score_version=SCORE_VERSION,
                stability_source='recent-evaluation-requests',
                start_at=traffic['start_at'] if traffic else None,
                end_at=traffic['end_at'] if traffic else None,updated_at=now,
                status='ready')
            control = controls.get(key,{})
            if control.get('paused') or control.get('resume_at',0)>now:
                values['status'] = 'paused'
            elif price is None:
                values['status'] = 'needs_price'
            elif account['platform'] in unpriced_platforms:
                # Never mix new 100..100100 priorities with unpriced legacy priorities such as 1 or 50.
                values['status'] = 'needs_group_prices'
            elif account['platform'] in failed_platforms:
                values.update(status='error',error_code='PRIORITY_COHORT_INCOMPLETE')
            else:
                try:
                    # Recheck scope, pause and fresh OAuth quota immediately before any write.
                    fresh = self.api.account_state(ident)
                    if not in_evaluation_scope(fresh) or fresh.get('platform') != account['platform']:
                        values['status'] = 'excluded'
                    elif oauth_quota_state(fresh,now)['limited']:
                        values['status'] = 'quota'
                    elif self.store.controls.read().get(key,{}).get('paused'):
                        values['status'] = 'paused'
                    elif price_signature(Prices(self.store.root/'controls').read()) != signature:
                        values['status'] = 'price_changed'
                    else:
                        insufficient = not traffic or values['quality_score'] is None
                        target = PRIORITY_MAX if insufficient else values['target_priority']
                        # Intent is durable before the HTTP write; an uncertain response never triggers a model request.
                        self.store.meta('priority_intent_'+key,{'target':target,'updated_at':now})
                        after = self.api.set_priority(ident,target)
                        values.update(actual_priority=after['priority'],target_priority=target,
                            status='fallback' if insufficient else 'applied')
                        self.store.meta('priority_intent_'+key,None)
                except Exception as exc:
                    failed_platforms.add(account['platform'])
                    values.update(status='error',error_code=exc.code if isinstance(exc,ProbeError) else 'PRIORITY_UPDATE_UNCONFIRMED')
            projected[key] = values
        self.store.meta('supplier_priorities',projected)
        self.store.meta('priority_partial',sorted(failed_platforms))
        self.store.meta('priority_enabled',True)
        if not failed_platforms and not any(value['status']=='price_changed' for value in projected.values()):
            self.store.meta('priority_price_signature',signature)
            self.store.meta('priority_score_version',SCORE_VERSION)


class RoutingChain:
    def __init__(self, quality, priority):
        self.quality, self.priority = quality, priority

    def reconcile(self):
        self.quality.reconcile()
        self.priority.reconcile()
