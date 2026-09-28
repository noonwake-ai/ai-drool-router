#!/usr/bin/env python3
"""One complete probe round, end to end, inside an isolated sandbox.

Two throwaway servers are started: a Sub2API-shaped admin API on plain HTTP
(accounts, credentials, pricing, and the only two write endpoints this project
uses) and a model upstream on real TLS speaking OpenAI Responses SSE.

Nothing here touches a real gateway, a real credential or the public internet.
The upstream plays three roles so the interesting branches run in one pass:

    relay-good    answers correctly          -> candy passes, artwork passes
    relay-wrong   answers 29 instead of 21   -> candy fails, and is NOT retried
    relay-broken  always returns HTTP 500    -> two upstream errors -> circuit

It runs twice against a fresh data directory:

    writes off (the default) -> the gateway must receive zero writes
    writes on                -> the circuit write must happen and read back

Usage:
    python3 scripts/e2e_smoke.py                 # run, then clean up
    python3 scripts/e2e_smoke.py --keep          # keep the sandbox to poke at
"""
import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

REPO = Path(__file__).resolve().parent.parent
# Running `python3 scripts/e2e_smoke.py` puts `scripts/` on sys.path, not the
# repository root, so make the package importable the same way it is installed.
sys.path.insert(0, str(REPO))

ARTWORK = (
    '<!doctype html><html><head><meta charset="utf-8"><title>smoke</title></head>'
    '<body><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<circle cx="5" cy="5" r="4" fill="#d5b769"/></svg></body></html>'
)

# One account per branch. `behaviour` is what the mock upstream keys off; it is
# exposed only through the credentials endpoint, never through account listings.
ACCOUNTS = [
    {'id': 9001, 'name': 'relay-good', 'behaviour': 'good'},
    {'id': 9002, 'name': 'relay-wrong', 'behaviour': 'wrong'},
    {'id': 9003, 'name': 'relay-broken', 'behaviour': 'broken'},
]


def log(message):
    print(message, flush=True)


# --------------------------------------------------------------- mock gateway

class Gateway:
    """A stand-in for the Sub2API admin API, plus a journal of every write."""

    def __init__(self, upstream_url):
        self.upstream_url = upstream_url
        self.accounts = {
            a['id']: {'id': a['id'], 'name': a['name'], 'platform': 'openai',
                      'type': 'apikey', 'status': 'active', 'schedulable': True,
                      'group_ids': [4], 'priority': 100, 'behaviour': a['behaviour']}
            for a in ACCOUNTS
        }
        self.writes = []
        self.reads = []
        self.server = None
        self.port = None

    def public(self, account):
        return {k: v for k, v in account.items() if k != 'behaviour'}

    def _handler_class(self, gateway):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def send_json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def read_body(self):
                length = int(self.headers.get('Content-Length') or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b'{}')
                except ValueError:
                    return {}

            def path_parts(self):
                parts = urlsplit(self.path)
                return parts.path, parse_qs(parts.query)

            def do_GET(self):
                path, query = self.path_parts()
                gateway.reads.append(path)
                if path == '/health':
                    return self.send_json(200, {'status': 'ok'})
                if path == '/api/v1/admin/channels/model-pricing':
                    model = (query.get('model') or [''])[0]
                    return self.send_json(200, {'code': 0, 'data': {
                        'model': model, 'found': True,
                        'input_price': '0.000002', 'output_price': '0.000008',
                        'cache_read_price': '0.0000005',
                        'cache_write_price': '0.0000025',
                        'cache_write_1h_price': '0.000004'}})
                if path == '/api/v1/admin/accounts/data':
                    raw = (query.get('ids') or [''])[0]
                    wanted = [int(x) for x in raw.split(',') if x.strip().isdigit()]
                    entries = []
                    for ident in wanted:
                        account = gateway.accounts.get(ident)
                        if not account:
                            continue
                        entries.append({
                            'name': account['name'], 'platform': account['platform'],
                            'credentials': {'api_key': account['behaviour'],
                                            'base_url': gateway.upstream_url}})
                    return self.send_json(200, {'code': 0,
                                                'data': {'accounts': entries, 'proxies': []}})
                if path == '/api/v1/admin/accounts':
                    items = [gateway.public(a) for a in gateway.accounts.values()]
                    return self.send_json(200, {'code': 0,
                                                'data': {'items': items, 'total': len(items)}})
                prefix = '/api/v1/admin/accounts/'
                if path.startswith(prefix):
                    ident = path[len(prefix):]
                    account = gateway.accounts.get(int(ident)) if ident.isdigit() else None
                    if not account:
                        return self.send_json(404, {'code': 1, 'message': 'not found'})
                    return self.send_json(200, {'code': 0, 'data': gateway.public(account)})
                return self.send_json(404, {'code': 1, 'message': 'unknown path'})

            def log_write(self, path, payload):
                gateway.writes.append({'method': self.command, 'path': path, 'payload': payload})

            def do_PUT(self):
                path, _ = self.path_parts()
                body = self.read_body()
                self.log_write(path, body)
                prefix = '/api/v1/admin/accounts/'
                if not path.startswith(prefix):
                    return self.send_json(404, {'code': 1, 'message': 'unknown path'})
                ident = path[len(prefix):]
                account = gateway.accounts.get(int(ident)) if ident.isdigit() else None
                if not account:
                    return self.send_json(404, {'code': 1, 'message': 'not found'})
                if 'priority' in body:
                    account['priority'] = body['priority']
                if 'status' in body:
                    account['status'] = body['status']
                return self.send_json(200, {'code': 0, 'data': gateway.public(account)})

            def do_POST(self):
                path, _ = self.path_parts()
                body = self.read_body()
                self.log_write(path, body)
                prefix = '/api/v1/admin/accounts/'
                if path.startswith(prefix) and path.endswith('/schedulable'):
                    ident = path[len(prefix):-len('/schedulable')]
                    account = gateway.accounts.get(int(ident)) if ident.isdigit() else None
                    if not account:
                        return self.send_json(404, {'code': 1, 'message': 'not found'})
                    account['schedulable'] = bool(body.get('schedulable'))
                    return self.send_json(200, {'code': 0, 'data': gateway.public(account)})
                return self.send_json(404, {'code': 1, 'message': 'unknown path'})

        return Handler

    def start(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler_class(self))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.port

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# -------------------------------------------------------------- mock upstream

class Upstream:
    """A model upstream speaking OpenAI Responses SSE over real TLS."""

    def __init__(self, cert, key):
        self.cert, self.key = cert, key
        self.requests = []
        self.server = None
        self.port = None

    @staticmethod
    def payload_text(payload):
        chunks = []
        for item in payload.get('input') or []:
            content = item.get('content')
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get('text'), str):
                        chunks.append(part['text'])
        if isinstance(payload.get('instructions'), str):
            chunks.append(payload['instructions'])
        for message in payload.get('messages') or []:
            if isinstance(message.get('content'), str):
                chunks.append(message['content'])
        return '\n'.join(chunks)

    @staticmethod
    def events(behaviour, kind, model):
        if kind == 'drawing':
            answer = ARTWORK
        elif behaviour == 'good':
            answer = '推导过程略。\n最终答案：21'
        else:
            answer = '推导过程略。\n最终答案：29'
        yield {'type': 'response.created',
               'response': {'model': model, 'status': 'in_progress'}}
        yield {'type': 'response.output_text.delta', 'delta': answer}
        yield {'type': 'response.completed', 'response': {
            'status': 'completed', 'model': model,
            'output': [{'type': 'message',
                        'content': [{'type': 'output_text', 'text': answer}]}],
            'usage': {'input_tokens': 120, 'output_tokens': 40, 'total_tokens': 160}}}

    def _handler_class(self, upstream):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def send_json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b'{}'
                try:
                    payload = json.loads(raw or b'{}')
                except ValueError:
                    payload = {}
                behaviour = (self.headers.get('Authorization') or '').replace('Bearer ', '').strip()
                text = upstream.payload_text(payload)
                kind = 'drawing' if 'SVG' in text else 'candy'
                model = payload.get('model') or 'unknown'
                upstream.requests.append({'behaviour': behaviour, 'kind': kind, 'model': model})
                if behaviour == 'broken':
                    return self.send_json(500, {'error': {'message': 'smoke: upstream is down'}})
                body = ''.join('data: %s\n\n' % json.dumps(event, ensure_ascii=False)
                               for event in upstream.events(behaviour, kind, model)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def start(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler_class(self))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self):
        return 'https://127.0.0.1:%d' % self.port

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# -------------------------------------------------------------------- helpers

def mint_certificate(directory):
    """A self-signed localhost certificate that doubles as its own CA."""
    cert = Path(directory) / 'upstream.pem'
    key = Path(directory) / 'upstream.key'
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key), '-out', str(cert), '-days', '2',
        '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
        '-addext', 'basicConstraints=critical,CA:TRUE',
    ], check=True, capture_output=True)
    return cert, key


def write_config(sandbox, gateway_url, ca_bundle, data_dir):
    config = {
        'title': 'AI 流口水检测',
        'base_url': gateway_url,
        'data_dir': str(data_dir),
        'web': {'host': '127.0.0.1', 'port': 4191, 'public_controls': False},
        'upstream': {'ca_bundle': str(ca_bundle)},
        'schedule': {'timezone': 'Asia/Shanghai', 'regular_minutes': 45,
                     'quiet_start': '04:00', 'quiet_end': '08:00',
                     'quiet_minutes': 90, 'history_hours': 24},
        'platforms': {
            'openai': {'enabled': True, 'label': 'GPT', 'protocol': 'openai_responses',
                       'model': 'smoke-model', 'effort': 'medium',
                       'group_ids': [], 'group_names': [], 'exclude_names': []}},
        'budgets': {'default_seconds': 60, 'idle_seconds': 20,
                    'gemini_high_idle_seconds': 60, 'max_attempts': 3},
        'routing': {'weights': {'intelligence': 0.36, 'cost': 0.36,
                                'stability': 0.18, 'speed': 0.10},
                    'rounds': 3, 'stability_rounds': 3,
                    'write_priority': False, 'write_callable': False,
                    'stability_endpoint': ''},
        'retention': {'artifacts_hours': 24, 'cost_days': 30},
        'privacy': {'account_names': 'full'},
        'identity_salt': 'e2e-smoke-sandbox',
    }
    path = Path(sandbox) / 'config.json'
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    return path


def latest_run(store, account_id, kind):
    with store.db() as db:
        row = db.execute(
            'SELECT * FROM runs WHERE account_id=? AND kind=? '
            'ORDER BY slot DESC, created_at DESC LIMIT 1', (account_id, kind)).fetchone()
    return dict(row) if row else None


def run_round(data_dir, config_path, gateway, ca_bundle, writes_on, label):
    """One full scheduled pass against the sandbox gateway and upstream."""
    os.environ['DROOL_CONFIG'] = str(config_path)
    os.environ['DROOL_CA_BUNDLE'] = str(ca_bundle)

    from detector import config as config_module
    config_module.CONFIG.clear()
    config_module.CONFIG.update(config_module.load(str(config_path)))
    config_module.CONFIG['routing']['write_priority'] = writes_on
    config_module.CONFIG['routing']['write_callable'] = writes_on

    from detector.monitor import Store, collect
    from detector.priority_routing import account_key
    from detector.supplier_prices import Prices

    store = Store(data_dir)
    # Seed a rate so the priority path runs instead of stopping at "needs price".
    prices = Prices(store.root / 'controls')
    for account in ACCOUNTS:
        key = account_key(account['id'])
        if prices.read().get(key, {}).get('multiplier') is None:
            prices.set(key, 0.5, None)

    key_file = Path(data_dir).parent / 'admin.key'
    key_file.write_text('e2e-smoke-admin-key-0123456789')

    before = len(gateway.writes)
    log('  running one full round (%s)...' % label)
    code = collect(str(data_dir), str(key_file), gateway.url, source='initial',
                   quality_routing=True, priority_routing=True)
    if code != 0:
        raise SystemExit('collect() exited %d during %s' % (code, label))
    return store, before, len(gateway.writes)


def check(condition, message):
    if not condition:
        raise SystemExit('E2E FAILED: ' + message)


EXPECTED = (
    ('relay-good', 'candy', 'pass'),
    ('relay-good', 'drawing', 'pass'),
    ('relay-wrong', 'candy', 'fail'),
    ('relay-wrong', 'drawing', 'pass'),
    ('relay-broken', 'candy', 'error'),
    ('relay-broken', 'drawing', 'error'),
)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sandbox', default='')
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()

    sandbox = Path(args.sandbox) if args.sandbox else Path(tempfile.mkdtemp(prefix='drool-e2e-'))
    sandbox.mkdir(parents=True, exist_ok=True)
    log('sandbox: %s' % sandbox)

    cert, key = mint_certificate(sandbox)
    upstream = Upstream(cert, key).start()
    gateway = Gateway(upstream.url).start()
    log('mock upstream: %s (TLS, self-signed)' % upstream.url)
    log('mock gateway:  %s' % gateway.url)

    try:
        # ---- round 1: default posture, no gateway writes allowed -----------
        data_off = sandbox / 'round-off'
        config_off = write_config(sandbox, gateway.url, cert, data_off)
        store, before, after = run_round(data_off, config_off, gateway, cert,
                                        writes_on=False, label='writes off')
        check(before == after,
              'gateway received %d writes while routing writes were disabled' % (after - before))
        log('  ✓ writes off: gateway received 0 writes')

        state = json.loads((store.public / 'state.json').read_text())
        by_name = {a['name']: a for a in state['accounts']}
        check(len(by_name) == 3, 'expected 3 accounts in public state, got %d' % len(by_name))

        for name, kind, expected in EXPECTED:
            account = next(a for a in ACCOUNTS if a['name'] == name)
            row = latest_run(store, account['id'], kind)
            check(row is not None, 'no %s run for %s' % (kind, name))
            check(row['status'] == expected, '%s %s: expected %s, got %s (%s)' %
                  (name, kind, expected, row['status'], row.get('error_code')))
            log('  ✓ %-13s %-8s -> %s' % (name, kind, row['status']))

        good = next(a for a in ACCOUNTS if a['name'] == 'relay-good')
        wrong = next(a for a in ACCOUNTS if a['name'] == 'relay-wrong')
        broken = next(a for a in ACCOUNTS if a['name'] == 'relay-broken')

        good_candy = latest_run(store, good['id'], 'candy')
        check(good_candy['attempts'] == 2,
              'a passing candy round needs two fresh requests, used %s' % good_candy['attempts'])
        wrong_candy = latest_run(store, wrong['id'], 'candy')
        check(wrong_candy['attempts'] == 1,
              'a definite wrong answer must not be retried, used %s attempts' % wrong_candy['attempts'])
        check(str(wrong_candy['answer']) == '29', 'the wrong answer should be recorded as 29')
        log('  ✓ fail-fast: pass used %d requests, wrong answer used %d' %
            (good_candy['attempts'], wrong_candy['attempts']))

        broken_candy = latest_run(store, broken['id'], 'candy')
        failures = [a for a in json.loads(broken_candy['attempt_log'])
                    if a.get('status') == 'error']
        check(failures and all(a.get('failure_class') == 'upstream' for a in failures),
              'a real HTTP 500 must be classified as an upstream failure')
        log('  ✓ HTTP 500 classified as upstream failure, circuit armed')

        good_drawing = latest_run(store, good['id'], 'drawing')
        check((store.public / (good_drawing['id'] + '.html')).exists(),
              'a passing drawing must publish an artifact')
        log('  ✓ artwork published to disk')

        window = store.costs.summary()['windows']['24h']
        check(window['amount_usd'] > 0, 'the cost ledger recorded no amount')
        log('  ✓ cost ledger estimated $%.6f' % window['amount_usd'])

        blob = json.dumps(state, ensure_ascii=False)
        for forbidden in ('sk-', 'Bearer ', 'api_key', 'access_token', 'https://127.0.0.1'):
            check(forbidden not in blob, 'public state leaked %r' % forbidden)
        log('  ✓ public projection carries no credentials or upstream URLs')

        # ---- the dashboard over real HTTP ---------------------------------
        from detector import server as server_module
        httpd = server_module.make_server('127.0.0.1', 0, REPO / 'web' / 'dist',
                                          store.public, controls_enabled=False)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for path, needle in (('/api/state', b'"accounts"'),
                                 ('/api/runs/' + good_candy['id'], b'"answer"'),
                                 ('/artifacts/' + good_drawing['id'] + '.html', b'<svg')):
                with urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=10) as response:
                    body = response.read()
                    check(response.status == 200, '%s returned %s' % (path, response.status))
                    check(needle in body, '%s did not contain %r' % (path, needle))
                log('  ✓ GET %s -> 200' % path)
        finally:
            httpd.shutdown()
            httpd.server_close()

        # ---- round 2: writes authorised -----------------------------------
        data_on = sandbox / 'round-on'
        config_on = write_config(sandbox, gateway.url, cert, data_on)
        store_on, before_on, after_on = run_round(data_on, config_on, gateway, cert,
                                                 writes_on=True, label='writes on')
        new_writes = gateway.writes[before_on:after_on]
        check(new_writes, 'routing writes were authorised but the gateway saw none')
        paths = {w['path'] for w in new_writes}
        check(any(p.endswith('/schedulable') for p in paths),
              'expected a callable write for the tripped circuit, saw %s' % sorted(paths))
        check(gateway.accounts[broken['id']]['schedulable'] is False,
              'the gateway did not record the callable write')
        log('  ✓ writes on: %d gateway write(s), read back verified' % len(new_writes))

        state_on = json.loads((store_on.public / 'state.json').read_text())
        circuits = {a['name']: (a.get('circuit') or {}).get('active') for a in state_on['accounts']}
        check(circuits.get('relay-broken') == 1,
              'the broken supplier should show an open circuit, got %r' % circuits)
        log('  ✓ circuit published for the broken supplier only')

        log('')
        log('E2E PASSED — one complete round behaved exactly as specified.')
        if args.keep:
            log('sandbox kept at: %s' % sandbox)
        return 0
    finally:
        upstream.stop()
        gateway.stop()
        if not args.keep and not args.sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
