# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Account inventory, adaptive timer, and rolling public results. POS: isolated worker."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from . import config
from .controls import Controls
from .costs import CostLedger
from .drawing_signals import analyze as analyze_drawing, from_attempts as drawing_signals_from_attempts
from .question_bank import CONFIRMATION, ORIGINAL, TEMPLATES, VERSION, grade, select_question, two_request_verdict
from .prompts import BENCHMARKS, DRAWING_PROFILE, EFFORT, EXPECTED_ANSWER, INSTRUCTIONS, LEGACY_DRAWING_INSTRUCTIONS, MODEL, PROMPTS, TITLE
from .upstream import CLIENT_INFO, OAUTH_QUOTA_ERROR, ProbeError, Sub2API, client_info, oauth_quota_state, probe, redact, request_budget
from .quality_routing import BUDGET_ERRORS, QualityRouter, REQUEST_POLICY, consecutive_request_errors, request_failure_class, upstream_failure
from .priority_routing import Prices, PriorityRouter, RoutingChain
from .schedule import current_slot, next_slot, schedule_info, window_slots

HISTORY_SECONDS = int(config.get('retention.artifacts_hours', 24)) * 3600
AUDIT_SECONDS = 24 * 3600


def hour(now=None):
    return int(time.time() if now is None else now) // 3600 * 3600


def public_id(account_id):
    # Accounts are only ever exposed as a salted digest, never as gateway identifiers.
    return hashlib.sha256((config.get('identity_salt') + '-account-' + str(account_id)).encode()).hexdigest()[:12]


def parse_answer(text):
    normalized = re.sub(r'[*`_#]', '', text)
    matches = re.findall(r'最终答案\s*[:：]\s*(?:\\\(?\\boxed\{)?\s*(\d+)\s*(?:\})?', normalized)
    if matches:
        return int(matches[-1])
    boxed = re.findall(r'\\boxed\{\s*(\d+)\s*\}', normalized)
    if boxed:
        return int(boxed[-1])
    last = normalized.strip().splitlines()[-1] if normalized.strip() else ''
    match = re.fullmatch(r'\s*(?:答案[为是：:\s]*)?(\d+)\s*(?:个(?:糖果)?)?[。.!！]?\s*', last)
    return int(match[1]) if match else None


def extract_html(text):
    start = re.search(r'<!doctype\s+html[^>]*>|<html\b', text, re.I)
    end = list(re.finditer(r'</html\s*>', text, re.I))
    if not start or not end or end[-1].end() <= start.start():
        raise ProbeError('INCOMPLETE_HTML', '模型未返回完整的 HTML 文档')
    html = text[start.start():end[-1].end()]
    if not re.search(r'<svg\b', html, re.I) or not re.search(r'</svg\s*>', html, re.I):
        raise ProbeError('MISSING_SVG', '返回的 HTML 中缺少完整 SVG')
    return html


def atomic_write(path, content, mode=0o640):
    path = Path(path)
    temp = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.private = self.root/'private'
        self.public = self.root/'public'
        self.controls = Controls(self.root/'controls')
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.public.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, last_sync INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, slot INTEGER NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    finished_at REAL, duration REAL, error_code TEXT, error TEXT,
                    answer INTEGER, response TEXT, html TEXT, actual_model TEXT,
                    tokens TEXT NOT NULL DEFAULT '{}', attempt_log TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'timer', UNIQUE(account_id,slot,kind));
                CREATE INDEX IF NOT EXISTS runs_slot ON runs(slot);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quality_routing (
                    account_id INTEGER PRIMARY KEY,
                    run_id TEXT, pending_target INTEGER, action TEXT, error_code TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS quality_routing_events (
                    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, run_id TEXT,
                    created_at REAL NOT NULL, target INTEGER NOT NULL, action TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS request_circuits (
                    account_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, trigger_slot INTEGER NOT NULL,
                    opened_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    recovered_run_id TEXT, recovered_at REAL);
            ''')
            if 'account_type' not in {r['name'] for r in db.execute('PRAGMA table_info(accounts)')}:
                db.execute("ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT 'unknown'")
            for table, columns in {
                'accounts': {'platform': "TEXT NOT NULL DEFAULT 'openai'", 'model': 'TEXT', 'effort': 'TEXT'},
                'runs': {'platform': "TEXT NOT NULL DEFAULT 'openai'", 'model': "TEXT NOT NULL DEFAULT 'gpt-6-astra'", 'effort': "TEXT DEFAULT 'medium'",
                         'bank_version':'TEXT', 'question_plan':"TEXT NOT NULL DEFAULT '[]'",
                         'question_results':"TEXT NOT NULL DEFAULT '[]'", 'first_status':'TEXT', 'phase':'TEXT',
                         'request_policy':'TEXT'},
            }.items():
                existing = {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
                for name, definition in columns.items():
                    if name not in existing:
                        db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
        os.chmod(self.private/'state.sqlite3', 0o600)
        self.costs = CostLedger(self)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.private/'state.sqlite3', timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def meta(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key,json.dumps(value,ensure_ascii=False)))

    def metadata(self):
        with self.db() as db:
            return {r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM metadata')}

    def sync(self, accounts, slot, source):
        now = int(time.time())
        removed_runs = []
        renamed_runs = []
        with self.db() as db:
            old_names = {r['account_id']:r['name'] for r in db.execute('SELECT account_id,name FROM accounts')}
            db.execute('UPDATE accounts SET enabled=0')
            # Never accept a synthetic display identity from the gateway inventory.
            managed = [a for a in accounts if a.get('id', 0) > 0 and a.get('platform', 'openai') in BENCHMARKS]
            for account in managed:
                account_type = account.get('type') if account.get('type') in ('oauth','apikey') else 'unknown'
                platform = account.get('platform', 'openai')
                policy = REQUEST_POLICY
                spec = BENCHMARKS[platform]
                if account['id'] in old_names and old_names[account['id']] != redact(account['name'])[:200]:
                    renamed_runs.extend(r['id'] for r in db.execute('SELECT id FROM runs WHERE account_id=?',(account['id'],)))
                db.execute('INSERT INTO accounts (account_id,name,enabled,last_sync,account_type,platform,model,effort) VALUES (?,?,1,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET name=excluded.name,enabled=1,last_sync=excluded.last_sync,account_type=excluded.account_type,platform=excluded.platform,model=excluded.model,effort=excluded.effort',
                           (account['id'],redact(account['name'])[:200],now,account_type,platform,spec['model'],spec['effort']))
                if slot is None or not spec['model'] or self.controls.blocked(public_id(account['id']), slot):
                    continue
                for kind in PROMPTS:
                    question = [select_question(slot),select_question(slot)] if kind=='candy' else []
                    db.execute('INSERT OR IGNORE INTO runs (id,account_id,slot,kind,created_at,source,platform,model,effort,bank_version,question_plan,request_policy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (uuid.uuid4().hex,account['id'],slot,kind,time.time(),source,platform,spec['model'],spec['effort'],VERSION if question else None,json.dumps(question,ensure_ascii=False),policy))
            removed_runs = [r['id'] for r in db.execute('SELECT id FROM runs WHERE account_id IN (SELECT account_id FROM accounts WHERE enabled=0)')]
            for table in ('runs','quality_routing','quality_routing_events','request_circuits'):
                db.execute('DELETE FROM '+table+' WHERE account_id IN (SELECT account_id FROM accounts WHERE enabled=0)')
            db.execute('DELETE FROM accounts WHERE enabled=0')
        for ident in removed_runs:
            for extension in ('.json','.html'):
                (self.public/(ident+extension)).unlink(missing_ok=True)
        for ident in renamed_runs:
            self.publish_run(ident)
        self.meta('last_account_sync', now)
        self.meta('sync_error', None)

    def pending(self, slot):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs WHERE slot=? AND status IN ('pending','running') AND account_id IN (SELECT account_id FROM accounts WHERE enabled=1) ORDER BY kind, account_id",(slot,))]

    def update(self, run_id, **fields):
        allowed = {'status','attempts','finished_at','duration','error_code','error','answer','response','html','actual_model','tokens','attempt_log',
                   'bank_version','question_plan','question_results','first_status','phase'}
        if set(fields) - allowed:
            raise ValueError('INVALID_UPDATE_FIELDS')
        with self.db() as db:
            db.execute('UPDATE runs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(),run_id))
        self.publish_run(run_id)
        self.publish_state()

    def publish_run(self, run_id):
        with self.db() as db:
            row = db.execute('SELECT r.*,a.name FROM runs r JOIN accounts a ON a.account_id=r.account_id WHERE r.id=?',(run_id,)).fetchone()
            if not row:
                return
            r = dict(row)
            detail = {k:r[k] for k in ('id','name','slot','kind','status','attempts','created_at','finished_at','duration','error_code','error','answer','response','actual_model','source')}
            detail['name'] = config.public_account_name(r['account_id'], r['name'])
            detail.update({'model':r['model'], 'effort':r['effort'], 'platform':r['platform'], 'prompt':PROMPTS[r['kind']], 'instructions':INSTRUCTIONS[r['kind']],
                           'expected_answer':EXPECTED_ANSWER if r['kind']=='candy' else None,
                           'tokens':json.loads(r['tokens']), 'attempt_log':json.loads(r['attempt_log']),
                           'artifact_url':'/artifacts/'+r['id']+'.html' if r['html'] and r['status']=='pass' else None})
            detail.update({'bank_version':r['bank_version'], 'first_status':r['first_status'], 'phase':r['phase'],
                           'questions':json.loads(r['question_plan']), 'question_results':json.loads(r['question_results']),
                           'request_policy':r['request_policy']})
            if detail['questions']:
                last = detail['questions'][-1]
                detail.update(prompt=last['prompt'], instructions=last['instructions'], expected_answer=last['expected'])
            detail['request_client'] = next((a['client'] for a in reversed(detail['attempt_log']) if a.get('client')), None)
            if r['kind'] == 'drawing':
                detail['drawing_signals'] = drawing_signals_from_attempts(detail['attempt_log'])
                profile = next((a['drawing_profile'] for a in reversed(detail['attempt_log']) if a.get('drawing_profile')), None)
                detail['drawing_profile'] = profile
                if profile != DRAWING_PROFILE:
                    detail['instructions'] = LEGACY_DRAWING_INSTRUCTIONS
            atomic_write(self.public/(run_id+'.json'), json.dumps(detail,ensure_ascii=False))
            if detail['artifact_url']:
                atomic_write(self.public/(run_id+'.html'), r['html'])

    def publish_state(self):
        with self.db() as db:
            now = time.time()
            start = now - HISTORY_SECONDS
            slots = window_slots(now)
            accounts = []
            priorities = self.metadata().get('supplier_priorities',{})
            prices = Prices(self.root/'controls').read()
            for a in db.execute('SELECT * FROM accounts WHERE enabled=1 ORDER BY account_id'):
                histories = {}
                for kind in PROMPTS:
                    rows = [dict(r) for r in db.execute('SELECT id,slot,kind,status,attempts,duration,answer,finished_at,error_code,error,bank_version,first_status,phase,attempt_log FROM runs WHERE account_id=? AND kind=? AND slot>=? ORDER BY slot',(a['account_id'],kind,start))]
                    for row in rows:
                        attempts = json.loads(row.pop('attempt_log'))
                        if kind == 'drawing':
                            row['drawing_signals'] = drawing_signals_from_attempts(attempts)
                    by_slot = {r['slot']:r for r in rows}
                    # Keep legacy hourly timestamps intact during a cadence transition.
                    histories[kind] = [by_slot.get(s,{'slot':s,'kind':kind,'status':'empty'}) for s in sorted(set(slots)|set(by_slot))]
                control = self.controls.read().get(public_id(a['account_id']), {})
                routing = db.execute('SELECT action,error_code,updated_at FROM quality_routing WHERE account_id=?',(a['account_id'],)).fetchone()
                circuit = db.execute('SELECT active,opened_at,trigger_slot FROM request_circuits WHERE account_id=?',(a['account_id'],)).fetchone()
                accounts.append({'id':public_id(a['account_id']),
                                 'name':config.public_account_name(a['account_id'],a['name']),
                                 'type':a['account_type'], 'platform':a['platform'],
                                 'model':a['model'], 'effort':a['effort'], 'configured':bool(a['model']),
                                 'paused':control.get('paused',False), 'resume_at':control.get('resume_at',0), 'history':histories,
                                 'routing':dict(routing) if routing else None, 'circuit':dict(circuit) if circuit else None,
                                 'priority':priorities.get(public_id(a['account_id'])),
                                 'price':prices.get(public_id(a['account_id']),{'multiplier':None,'updated_at':0})})
            meta = {r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM metadata')
                    if not r['key'].startswith(('cost_','priority_intent_')) and r['key']!='supplier_priorities'}
            out = {'title':TITLE,'model':MODEL,'effort':EFFORT,'expected_answer':EXPECTED_ANSWER,
                   'server_time':now,'generated_at':now,'next_run_at':next_slot(now),
                   'history_hours':int(config.get('retention.artifacts_hours',24)),
                   'interval_seconds':schedule_info(now)['interval_seconds'],'schedule':schedule_info(now),
                   'max_retries':max(0,config.max_attempts()-1),'refresh_seconds':30,
                   'accounts':accounts,'scheduler':meta,
                   'costs':self.costs.summary(now), 'request_policy':REQUEST_POLICY,
                   'question_bank':{'version':VERSION,'templates':len(TEMPLATES),'confirmation':CONFIRMATION,
                                    'failure':'first_wrong_answer_stops','ranking':'first_answer_rate'},
                   'prompts':PROMPTS,'instructions':INSTRUCTIONS,'request_client':CLIENT_INFO, 'benchmarks':BENCHMARKS,
                   'routing_writes':{'priority':bool(config.get('routing.write_priority')),
                                     'callable':bool(config.get('routing.write_callable'))},
                   'method_locale':'zh',
                   'method':'HTTP 直连适配器（不是 CLI 子进程）。读取 Sub2API 账户配置，使用对应账户的上游凭据和代理独立请求，不经过分组网关或负载均衡。'}
            atomic_write(self.public/'state.json',json.dumps(out,ensure_ascii=False))

    def cleanup(self, now=None):
        cutoff = (time.time() if now is None else now) - HISTORY_SECONDS
        self.costs.cleanup(now)
        with self.db() as db:
            old = [r['id'] for r in db.execute('SELECT id FROM runs WHERE slot<?',(cutoff,))]
            db.execute('DELETE FROM runs WHERE slot<?',(cutoff,))
            db.execute('DELETE FROM quality_routing_events WHERE created_at<?',(time.time()-AUDIT_SECONDS,))
            db.execute('DELETE FROM accounts WHERE enabled=0 AND last_sync<?',(cutoff,))
            # A killed process never remains a green result or permanent running cell.
            db.execute("UPDATE runs SET status='error',error_code='WORKER_INTERRUPTED',error='上一轮检测未完成，进程可能被中断',finished_at=? WHERE slot<? AND status IN ('running','pending')",(time.time(),current_slot(now)))
            interrupted = [r['id'] for r in db.execute("SELECT id FROM runs WHERE error_code='WORKER_INTERRUPTED'")]
        for ident in old:
            for ext in ('.json','.html'):
                (self.public/(ident+ext)).unlink(missing_ok=True)
        for ident in interrupted:
            self.publish_run(ident)


def execute_run(store, api, run, batch_deadline, request_fn=probe, sleep=time.sleep, quality_router=None):
    if run['kind']=='candy':
        return execute_logic_run(store,api,run,batch_deadline,request_fn,sleep,quality_router)
    start = time.monotonic()
    attempt_log = json.loads(run['attempt_log'])
    client = client_info(run['platform'])
    limit = config.max_attempts()
    for attempt in range(run['attempts']+1, limit+1):
        if store.controls.blocked(public_id(run['account_id']), run['slot']):
            store.update(run['id'],status='paused',finished_at=time.time(),duration=time.monotonic()-start)
            return
        if time.monotonic() > batch_deadline-250:
            store.update(run['id'],status='error',error_code='BATCH_DEADLINE',error='本轮达到运行时间上限',finished_at=time.time(),duration=time.monotonic()-start)
            return
        store.update(run['id'],status='running',attempts=attempt)
        attempt_start = time.monotonic()
        request_id = str(uuid.uuid4())
        request_called = False
        try:
            account = api.account(run['account_id'])
            if isinstance(account, dict) and account.get('platform', 'openai') != run['platform']:
                raise ProbeError('ACCOUNT_IDENTITY_MISMATCH', '账户平台已变化，本轮未调用')
            spec = BENCHMARKS.get(run['platform'], {})
            if spec.get('model') != run['model'] or spec.get('effort') != run['effort']:
                raise ProbeError('MODEL_CONFIG_CHANGED', '本轮模型或思考等级已变化，等待下一轮检测')
            quota = oauth_quota_state(account)
            if quota['limited']:
                message = quota_message(quota)
                store.update(run['id'],status='quota',attempts=len(attempt_log),finished_at=time.time(),
                             duration=time.monotonic()-start,error_code=OAUTH_QUOTA_ERROR,error=message,
                             attempt_log=json.dumps(attempt_log,ensure_ascii=False))
                return
            store.costs.begin(run, request_id)
            request_called = True
            result = request_fn(account, run['kind'], timeout=min(request_budget(run['platform'],run['kind']),batch_deadline-time.monotonic()-5),request_id=request_id)
            store.costs.finish(request_id, 'received', result.get('tokens'))
            html = extract_html(result['text']) if run['kind']=='drawing' else None
            answer = parse_answer(result['text']) if run['kind']=='candy' else None
            status = 'pass' if run['kind']=='drawing' or answer==EXPECTED_ANSWER else 'fail'
            attempt_log.append({'attempt':attempt,'status':status,'seconds':round(time.monotonic()-attempt_start,2),'client':client,'tokens':result.get('tokens',{}),
                                'timings':result.get('timings',{}),'request_id':request_id,
                                'drawing_profile':DRAWING_PROFILE,
                                'drawing_signals':analyze_drawing(result['text'], DRAWING_PROFILE)})
            store.update(run['id'],status=status,finished_at=time.time(),duration=time.monotonic()-start,
                         response=result['text'],html=html,answer=answer,actual_model=result.get('actual_model'),
                         error_code=None,error=None,tokens=json.dumps(result.get('tokens',{})),attempt_log=json.dumps(attempt_log,ensure_ascii=False))
        except ProbeError as exc:
            code,message = exc.code,redact(exc.message,errors=True)[:1800]
            timings = exc.timings
            store.costs.finish(request_id, 'error', exc.tokens, sent=bool(timings.get('request_sent') or request_called and upstream_failure(code)))
        except Exception:
            code,message = 'WORKER_ERROR','检测执行发生内部错误（敏感细节未公开）'
            timings = {}
            store.costs.finish(request_id, 'error')
        else:
            if quality_router and run['platform'] in ('anthropic','grok','gemini'):
                quality_router.reconcile()
            return
        attempt_log.append({'attempt':attempt,'status':'error','seconds':round(time.monotonic()-attempt_start,2),'code':code,'message':message,'client':client,'timings':timings,'request_id':request_id,
                            'drawing_profile':DRAWING_PROFILE,
                            'failure_class':request_failure_class(code,request_called)})
        trip = run.get('request_policy')==REQUEST_POLICY and consecutive_request_errors(attempt_log)
        # A budget truncation repeats the same wall; resending would burn another full budget.
        walled = code in BUDGET_ERRORS
        store.update(run['id'],error_code=code,error=message,attempt_log=json.dumps(attempt_log,ensure_ascii=False),
                     **({'status':'error','finished_at':time.time(),'duration':time.monotonic()-start} if attempt>=limit or trip or walled else {}))
        if trip:
            if quality_router:
                quality_router.reconcile()
            return
        if walled:
            return
        if attempt<limit:
            sleep((5,15)[min(attempt-1,len((5,15))-1)])
    if run['attempts'] >= limit:
        store.update(run['id'],status='error',error_code='ATTEMPTS_EXHAUSTED',
                     error='已用完 1 次请求和最多 %d 次重试' % (limit-1),finished_at=time.time())


def execute_logic_run(store, api, run, batch_deadline, request_fn, sleep, quality_router):
    start = time.monotonic()
    plan = json.loads(run['question_plan'])
    if (run.get('bank_version') != VERSION or len(plan) != 2 or
            any(q != ORIGINAL for q in plan)):
        store.update(run['id'],status='error',phase='complete',finished_at=time.time(),
                     error_code='QUALITY_POLICY_CHANGED',error='检测规则已更新，旧轮次不补测，等待下一轮两次独立检测')
        return
    results = json.loads(run['question_results'])
    log = json.loads(run['attempt_log'])
    first_status = run['first_status']
    client = client_info(run['platform'])

    def save(**fields):
        store.update(run['id'],question_plan=json.dumps(plan,ensure_ascii=False),
                     question_results=json.dumps(results,ensure_ascii=False),
                     attempt_log=json.dumps(log,ensure_ascii=False),attempts=len(log),
                     first_status=first_status,bank_version=plan[0]['version'],**fields)

    def finish(status, **fields):
        save(status=status,phase='complete',finished_at=time.time(),duration=time.monotonic()-start,**fields)
        connectivity_reply = (run['platform'] in ('anthropic','grok','gemini') and log
                              and log[-1].get('request_id') and log[-1].get('status')=='unknown')
        if quality_router and (status in ('pass','fail') or connectivity_reply
                               or status=='error' and consecutive_request_errors(log)):
            quality_router.reconcile()

    # A restart must not turn a persisted wrong answer into another paid request.
    if two_request_verdict(results,log,fail_fast=True) == 'fail':
        finish('fail',error_code=None,error=None)
        return

    while len(results) < len(plan):
        index = len(results)
        question = plan[index]
        phase = 'verification' if index else 'primary'
        prior_attempts = sum(a.get('stage',0)==index for a in log)
        save(status='running',phase=phase,error_code=None,error=None)
        limit = config.max_attempts()
        for attempt in range(prior_attempts+1, limit+1):
            if store.controls.blocked(public_id(run['account_id']),run['slot']):
                finish('paused')
                return
            if time.monotonic() > batch_deadline-250:
                finish('error',error_code='BATCH_DEADLINE',error='本轮时间预算不足，未作不通过判定')
                return
            entry = {'attempt':len(log)+1,'stage':index,'stage_attempt':attempt,'request_id':str(uuid.uuid4()),
                     'question_id':question['id'],'status':'running','client':client}
            log.append(entry)
            save(status='running',phase=phase)
            attempt_start = time.monotonic()
            request_called = False
            try:
                account = api.account(run['account_id'])
                if isinstance(account,dict) and account.get('platform','openai') != run['platform']:
                    raise ProbeError('ACCOUNT_IDENTITY_MISMATCH','账户平台已变化，本轮未调用')
                spec = BENCHMARKS.get(run['platform'], {})
                if spec.get('model')!=run['model'] or spec.get('effort')!=run['effort']:
                    raise ProbeError('MODEL_CONFIG_CHANGED','本轮模型配置已变化，等待下一轮')
                quota = oauth_quota_state(account)
                if quota['limited']:
                    log.pop()
                    finish('quota',error_code=OAUTH_QUOTA_ERROR,error=quota_message(quota))
                    return
                store.costs.begin(run, entry['request_id'])
                request_called = True
                reply = request_fn(account,'candy',timeout=min(request_budget(run['platform'],'candy'),batch_deadline-time.monotonic()-5),
                                   prompt=question['prompt'],instructions=question['instructions'],request_id=entry['request_id'])
                store.costs.finish(entry['request_id'], 'received', reply.get('tokens'))
                verdict, answer = grade(question,reply['text'])
                entry.update(status=verdict,seconds=round(time.monotonic()-attempt_start,2),timings=reply.get('timings',{}),tokens=reply.get('tokens',{}))
                result = {'question_id':question['id'],'status':verdict,'answer':answer,'response':reply['text'],
                          'actual_model':reply.get('actual_model'),'tokens':reply.get('tokens',{}),'attempts':attempt}
                results.append(result)
                if index==0:
                    first_status = verdict if verdict in ('pass','fail') else None
                fields = {'response':reply['text'],'answer':json.dumps(answer,ensure_ascii=False) if isinstance(answer,dict) else answer,
                          'actual_model':reply.get('actual_model'),'tokens':json.dumps({k:sum(r.get('tokens',{}).get(k,0) for r in results)
                            for k in {key for r in results for key in r.get('tokens',{}) if not key.startswith('_')}}), 'error_code':None,'error':None}
                if verdict=='unknown':
                    fields.update(error_code='UNGRADABLE_ANSWER',error='已收到回复，但未能识别明确且无冲突的答案，本轮不改变调用状态')
                    finish('error',**fields)
                    return
                if verdict=='fail':
                    finish('fail',**fields)
                    return
                if index==0:
                    save(status='running',phase='verification',**fields)
                    break
                final_verdict = two_request_verdict(results,log)
                if final_verdict is None:
                    fields.update(error_code='NON_CONSECUTIVE_PASSES',error='两次答对之间存在请求中断，未达到连续通过标准；等待下一轮，不恢复调用')
                finish(final_verdict or 'error',**fields)
                return
            except ProbeError as exc:
                if entry['status'] in ('pass','fail','unknown'):
                    raise
                code, message, timings = exc.code,redact(exc.message,errors=True)[:1800],exc.timings
                store.costs.finish(entry['request_id'], 'error', exc.tokens, sent=bool(timings.get('request_sent') or request_called and upstream_failure(code)))
            except Exception:
                # Post-answer persistence/routing failures must not resend the question.
                if entry['status'] in ('pass','fail','unknown'):
                    raise
                code, message, timings = 'WORKER_ERROR','检测执行发生内部错误（敏感细节未公开）',{}
                store.costs.finish(entry['request_id'], 'error')
            entry.update(status='error',code=code,message=message,seconds=round(time.monotonic()-attempt_start,2),timings=timings,
                         failure_class=request_failure_class(code,request_called))
            if attempt>=limit or run.get('request_policy')==REQUEST_POLICY and consecutive_request_errors(log):
                results.append({'question_id':question['id'],'status':'error','error_code':code,'error':message,'attempts':attempt})
                finish('error',error_code=code,error=message)
                return
            save(error_code=code,error=message)
            if attempt < limit:
                sleep((5,15)[min(attempt-1,len((5,15))-1)])
        else:
            finish('error',error_code='ATTEMPTS_EXHAUSTED',
                   error='本题已用完 1 次请求和最多 %d 次重试，未作答错判定' % (limit-1))
            return


def quota_message(state):
    if state.get('window') and state.get('used_percent') is not None and state.get('threshold_percent') is not None:
        window = {'5h':'5 小时', '7d':'7 天'}.get(state['window'], state['window'])
        return ('OAuth 账号达到 Sub2API 的 %s 停调阈值（已用 %.1f%%，阈值 %.1f%%），'
                '本次请求已跳过；下一轮重新读取状态，额度恢复后自动检测。' %
                (window, state['used_percent'], state['threshold_percent']))
    return 'Sub2API 当前 OAuth 账号已达到限额或被限流，本次请求已跳过；下一轮会重新读取状态。'


def read_admin_key(key_file):
    if key_file:
        return Path(key_file).read_text().strip()
    return (os.environ.get('SUB2API_ADMIN_KEY') or '').strip()


def collect(root, key_file, base_url, source='timer', metadata_only=False,
            quality_routing=None, reconcile_only=False, priority_routing=None):
    """Run one scheduled pass.

    ``quality_routing`` (callable circuit writes) and ``priority_routing``
    (priority writes) are resolved from ``config.json``: see
    ``config.write_enabled``. Passing an explicit boolean only takes effect when
    config already authorises that write, so no CLI flag or unit file can turn a
    gateway mutation on behind the deployer's back.
    """
    requested_quality = bool(quality_routing)
    requested_priority = bool(priority_routing)
    quality_routing = config.write_enabled('callable') and requested_quality
    priority_routing = config.write_enabled('priority') and requested_priority
    if (requested_quality and not quality_routing) or (requested_priority and not priority_routing):
        print(json.dumps({'status': 'write_disabled_by_config',
                          'detail': 'routing.write_callable / routing.write_priority are false'}), flush=True)
    os.umask(0o027)
    store = Store(root)
    with open(store.private/'worker.lock','w') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status':'already_running'}),flush=True)
            return 75 if metadata_only else 0
        slot = current_slot()
        if metadata_only or reconcile_only:
            api = Sub2API(base_url, read_admin_key(key_file))
            accounts = api.accounts()
            store.sync(accounts, None, 'metadata')
            store.costs.refresh_prices(api)
            store.costs.backfill()
            store.meta('last_metadata_sync',time.time())
            if reconcile_only:
                store.meta('quality_routing_enabled',quality_routing)
                QualityRouter(store,api,quality_routing).reconcile()
                PriorityRouter(store,api,priority_routing).reconcile()
            store.publish_state()
            print(json.dumps({'status':'metadata_refreshed','accounts':len(accounts),'model_requests':0}),flush=True)
            return 0
        store.cleanup()
        if (store.metadata().get('last_completed_slot') or 0) >= slot:
            if priority_routing:
                api = Sub2API(base_url, read_admin_key(key_file))
                PriorityRouter(store,api,True).reconcile()
            print(json.dumps({'status':'not_due','slot':slot,'next_run_at':next_slot()}),flush=True)
            return 0
        store.meta('last_started_at',time.time())
        store.meta('last_source',source)
        store.meta('running',True)
        store.publish_state()
        try:
            key = read_admin_key(key_file)
            if len(key)<16:
                raise ProbeError('CREDENTIAL_UNAVAILABLE','检测凭据不可用')
            api = Sub2API(base_url,key)
            store.costs.refresh_prices(api)
            store.costs.backfill()
            accounts = api.accounts()
            store.sync(accounts,slot,source)
            router = RoutingChain(QualityRouter(store,api,quality_routing),PriorityRouter(store,api,priority_routing))
            store.meta('quality_routing_enabled',quality_routing)
            store.publish_state()
            rows = store.pending(slot)
            deadline = time.monotonic()+min(3000,max(0,next_slot()-time.time()-45))
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda r:execute_run(store,api,r,deadline,quality_router=router),rows))
            router.reconcile()
            store.meta('last_completed_at',time.time())
            store.meta('last_completed_slot',slot)
            print(json.dumps({'status':'complete','accounts':len(accounts),'scheduled_tests':len(rows),'slot':slot,'source':source}),flush=True)
            return 0
        except Exception as exc:
            error = {'code':exc.code,'message':exc.message} if isinstance(exc,ProbeError) else {'code':'SYNC_FAILED','message':'账户同步失败，未发起新的模型请求'}
            store.meta('sync_error',error)
            print(json.dumps({'status':'error','code':error['code']}),flush=True)
            return 1
        finally:
            store.meta('running',False)
            store.cleanup()
            store.publish_state()


if __name__=='__main__':
    """Entry point for the scheduled worker.

    Credentials come from ``SUB2API_ADMIN_KEY`` or from a file passed with
    ``--key-file`` so operators can keep the secret out of the process list.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--data',default=str(config.data_dir()))
    parser.add_argument('--base-url',default=config.get('base_url') or '')
    parser.add_argument('--key-file',default=os.environ.get('SUB2API_ADMIN_KEY_FILE',''))
    parser.add_argument('--source',choices=('timer','initial'),default='timer')
    parser.add_argument('--metadata-only',action='store_true')
    parser.add_argument('--enable-quality-routing',action='store_true',
                        help='Request callable/circuit writes. Ignored unless config.json sets routing.write_callable=true.')
    parser.add_argument('--enable-priority-routing',action='store_true',
                        help='Request priority writes. Ignored unless config.json sets routing.write_priority=true.')
    parser.add_argument('--reconcile-quality',action='store_true',help='Reconcile latest existing results without model requests')
    args = parser.parse_args()
    if not args.base_url:
        raise SystemExit('缺少 Sub2API 地址：请在 config.json 设置 base_url，或使用 --base-url。')
    if not args.key_file and not os.environ.get('SUB2API_ADMIN_KEY'):
        raise SystemExit('缺少 Sub2API 管理员密钥：请设置 SUB2API_ADMIN_KEY，或使用 --key-file 指向密钥文件。')
    raise SystemExit(collect(args.data,args.key_file,args.base_url,args.source,args.metadata_only,args.enable_quality_routing,args.reconcile_quality,args.enable_priority_routing))
