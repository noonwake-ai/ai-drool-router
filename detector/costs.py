# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Token-only, 30-day ledger. Amounts are Sub2API base-price estimates, not invoices."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import time

from . import config
from .prompts import BENCHMARKS

RETENTION = max(1, int(config.get('retention.cost_days', 30))) * 86400
PRICE_FIELDS = ('input_price', 'output_price', 'cache_read_price', 'cache_write_price', 'cache_write_1h_price')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_creation_input_tokens',
                'cache_creation_1h_input_tokens', 'reasoning_tokens', 'total_tokens')


def token_count(value):
    return value if type(value) is int and 0 <= value <= 100_000_000 else None


def normalize_usage(raw, platform='openai'):
    if not isinstance(raw, dict):
        return {}
    if raw.get('_normalized') == 1:
        return {k:raw[k] for k in (*TOKEN_FIELDS, '_normalized') if token_count(raw.get(k)) is not None}
    if platform == 'gemini' and 'promptTokenCount' in raw:
        fields = {'input_tokens':raw.get('promptTokenCount'), 'output_tokens':raw.get('candidatesTokenCount'),
                  'cached_input_tokens':raw.get('cachedContentTokenCount', 0),
                  'reasoning_tokens':raw.get('thoughtsTokenCount', 0), 'total_tokens':raw.get('totalTokenCount')}
        if token_count(fields['output_tokens']) is not None and token_count(fields['reasoning_tokens']) is not None:
            fields['output_tokens'] += fields['reasoning_tokens']
    else:
        if any(raw.get(key) is not None and not isinstance(raw[key], dict) for key in
               ('input_tokens_details','prompt_tokens_details','output_tokens_details','completion_tokens_details','cache_creation')):
            return {}
        input_details = raw.get('input_tokens_details') or raw.get('prompt_tokens_details') or {}
        output_details = raw.get('output_tokens_details') or raw.get('completion_tokens_details') or {}
        creation = raw.get('cache_creation') or {}
        fields = {'input_tokens':raw.get('input_tokens', raw.get('prompt_tokens')),
                  'output_tokens':raw.get('output_tokens', raw.get('completion_tokens')),
                  'cached_input_tokens':raw.get('cache_read_input_tokens', input_details.get('cached_tokens', 0)),
                  'cache_creation_input_tokens':raw.get('cache_creation_input_tokens', 0),
                  'cache_creation_1h_input_tokens':creation.get('ephemeral_1h_input_tokens', 0),
                  'reasoning_tokens':output_details.get('reasoning_tokens', 0), 'total_tokens':raw.get('total_tokens')}
        if platform == 'anthropic' and all(token_count(fields[k]) is not None for k in ('input_tokens','cached_input_tokens','cache_creation_input_tokens')):
            # Anthropic excludes cached reads/writes from input_tokens; our ledger includes them.
            fields['input_tokens'] += fields['cached_input_tokens'] + fields['cache_creation_input_tokens']
    result = {k:v for k,v in fields.items() if token_count(v) is not None}
    if not any(k in result for k in ('input_tokens','output_tokens')):
        return {}
    return {**result, '_normalized':1}


def validated_price(raw, fetched_at):
    if not isinstance(raw, dict) or raw.get('found') is not True:
        return None
    result = {'fetched_at':fetched_at, 'source':'sub2api-base-price', 'currency':'USD'}
    try:
        for field in PRICE_FIELDS:
            value = raw.get(field)
            if value is None:
                if field in ('input_price','output_price'):
                    return None
                continue
            if isinstance(value, bool):
                return None
            number = Decimal(str(value))
            if not number.is_finite() or number < 0 or number > 1:
                return None
            result[field] = str(number)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result


def estimate(usage, price):
    if not usage or any(token_count(usage.get(k)) is None for k in ('input_tokens','output_tokens')):
        return None, 'missing_usage'
    if not price:
        return None, 'missing_price'
    incoming, outgoing = usage['input_tokens'], usage['output_tokens']
    cached, created, hour = (usage.get(k, 0) for k in ('cached_input_tokens','cache_creation_input_tokens','cache_creation_1h_input_tokens'))
    if cached + created > incoming or hour > created:
        return None, 'invalid_usage'
    if incoming > 200_000:
        return None, 'unsupported_context_tier'
    parts = ((incoming-cached-created, 'input_price'), (outgoing, 'output_price'),
             (cached, 'cache_read_price'), (created-hour, 'cache_write_price'), (hour, 'cache_write_1h_price'))
    if any(count and field not in price for count,field in parts):
        return None, 'missing_cache_price'
    amount = sum(Decimal(count) * Decimal(price.get(field, '0')) for count,field in parts)
    return int((amount * 1_000_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP)), None


class CostLedger:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cost_requests (
                    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, account_id INTEGER NOT NULL,
                    platform TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
                    created_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                    usage TEXT NOT NULL DEFAULT '{}', price TEXT NOT NULL DEFAULT '{}',
                    amount_nusd INTEGER, reason TEXT, origin TEXT NOT NULL DEFAULT 'live');
                CREATE INDEX IF NOT EXISTS cost_requests_created ON cost_requests(created_at);
            ''')

    def refresh_prices(self, api, now=None, force=False):
        now = time.time() if now is None else now
        meta = self.store.metadata()
        if not force and now - meta.get('cost_prices_checked_at', 0) < 6*3600:
            return
        prices = meta.get('cost_prices', {})
        errors = []
        for config in BENCHMARKS.values():
            model = config['model']
            try:
                price = validated_price(api.get('/channels/model-pricing', {'model':model}), now)
                if price is None:
                    prices.pop(model, None)
                    errors.append(model)
                else:
                    prices[model] = price
            except Exception:
                errors.append(model)
        self.store.meta('cost_prices', prices)
        self.store.meta('cost_prices_checked_at', now)
        self.store.meta('cost_price_errors', errors)

    def begin(self, run, request_id, created_at=None, origin='live'):
        now = time.time() if created_at is None else created_at
        price = self.store.metadata().get('cost_prices', {}).get(run['model'])
        if price and time.time() - price['fetched_at'] > 86400:
            price = None
        with self.store.db() as db:
            db.execute('''INSERT OR IGNORE INTO cost_requests
                (request_id,run_id,account_id,platform,model,kind,created_at,status,price,reason,origin)
                VALUES (?,?,?,?,?,?,?,'pending',?,'pending',?)''',
                (request_id,run['id'],run['account_id'],run['platform'],run['model'],run['kind'],now,json.dumps(price or {}),origin))

    def finish(self, request_id, status, tokens=None, *, sent=True, finished_at=None):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM cost_requests WHERE request_id=?', (request_id,)).fetchone()
            if not row or row['status'] != 'pending':
                return
            usage = normalize_usage(tokens or {}, row['platform'])
            amount, reason = estimate(usage, json.loads(row['price'])) if sent else (None, 'not_sent')
            db.execute('UPDATE cost_requests SET status=?,finished_at=?,usage=?,amount_nusd=?,reason=? WHERE request_id=?',
                       (status if sent else 'not_sent', time.time() if finished_at is None else finished_at,
                        json.dumps(usage),amount,reason,request_id))

    def backfill(self):
        if self.store.metadata().get('cost_backfill_version') == 1:
            return
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM runs WHERE slot>=? AND status NOT IN ('pending','running')", (time.time()-86400,))]
        for run in rows:
            log = json.loads(run['attempt_log'])
            results = json.loads(run['question_results'])
            for index, attempt in enumerate(log):
                request_id = attempt.get('request_id') or f"legacy-{run['id']}-{index}"
                tokens = {}
                if attempt.get('status') in ('pass','fail','unknown'):
                    stage = attempt.get('stage', 0)
                    if run['kind']=='candy' and results and stage < len(results):
                        tokens = results[stage].get('tokens', {})
                    elif index == len(log)-1:
                        tokens = json.loads(run['tokens'])
                self.begin(run, request_id, run['created_at'], 'legacy')
                self.finish(request_id, attempt.get('status', 'error'), tokens, finished_at=run['finished_at'])
        self.store.meta('cost_backfill_version', 1)
        self.store.meta('cost_tracking_started_at', time.time())

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.store.db() as db:
            db.execute('DELETE FROM cost_requests WHERE created_at<?', (now-RETENTION,))

    def summary(self, now=None):
        now = time.time() if now is None else now
        windows = {}
        with self.store.db() as db:
            first = db.execute("SELECT MIN(created_at) FROM cost_requests WHERE status!='not_sent'").fetchone()[0]
            for key, duration in (('24h',86400), ('30d',RETENTION)):
                items = {p:{'platform':p,'requests':0,'priced_requests':0,'unpriced_requests':0,
                            'legacy_requests':0,'pending_requests':0,'amount_nusd':0} for p in BENCHMARKS}
                for row in db.execute('''SELECT platform,COUNT(*) AS requests,COUNT(amount_nusd) AS priced_requests,
                      SUM(amount_nusd) AS amount_nusd,SUM(origin='legacy') AS legacy_requests,
                      SUM(status='pending') AS pending_requests FROM cost_requests
                      WHERE created_at>=? AND created_at<=? AND status!='not_sent' GROUP BY platform''', (now-duration,now)):
                    if row['platform'] in items:
                        item = items[row['platform']]
                        item.update(dict(row))
                        item['unpriced_requests'] = item['requests']-item['priced_requests']
                for item in items.values():
                    item['amount_usd'] = item.pop('amount_nusd')
                    if item['amount_usd'] is not None:
                        item['amount_usd'] /= 1_000_000_000
                total = sum(Decimal(str(i['amount_usd'])) for i in items.values() if i['amount_usd'] is not None)
                missing = sum(i['unpriced_requests'] for i in items.values())
                priced = sum(i['priced_requests'] for i in items.values())
                windows[key] = {'models':list(items.values()),'amount_usd':float(total) if priced or not missing else None,
                                'unpriced_requests':missing, 'priced_requests':priced,
                                'coverage_from':max(now-duration, first or now)}
        meta = self.store.metadata()
        return {'currency':'USD','basis':'sub2api-base-price-estimate','retention_days':30,
                'tracking_started_at':meta.get('cost_tracking_started_at'), 'windows':windows,
                'price_errors':meta.get('cost_price_errors', []),
                'note':'按上游返回 token 与 Sub2API 基准价估算，含糖果题、复核、动画及有用量的失败请求；非实际账单，不含订阅费或中转折扣。历史用量可能缺少缓存与思考细分，未记录的费用无法追溯。'}
