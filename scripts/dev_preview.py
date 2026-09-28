#!/usr/bin/env python3
"""Serve the dashboard with synthetic data so the UI can be reviewed offline.

This never contacts Sub2API and never reads a credential. It writes a small
state.json into a temporary data directory and starts the read-only dashboard,
which is what a deployer sees in production minus the live probes.

    python3 scripts/dev_preview.py
    python3 scripts/dev_preview.py --port 4192 --lang-check
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detector import server

HOUR = 3600


def _slot(offset_hours):
    return int(time.time()) - offset_hours * HOUR


def _run_id(slot, salt):
    return '%032x' % ((slot * 2654435761 + salt) % (1 << 128))


ARTIFACT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Demo artwork</title>
<style>html,body{margin:0;height:100%;background:#8fd0e8}svg{display:block;width:100%;height:100%}</style>
</head><body>
<svg viewBox="0 0 640 360" role="img" aria-label="Two flamingos riding a tandem bicycle">
  <rect width="640" height="360" fill="#8fd0e8"/>
  <circle cx="540" cy="66" r="34" fill="#ffe9a3"/>
  <path d="M0 268h640v92H0z" fill="#f0d9a8"/>
  <path d="M0 268c90-26 190-26 280 0s270 26 360 0v20c-90 26-270 26-360 0S90 288 0 288z" fill="#7fc4dd"/>
  <g stroke="#2b3a4a" stroke-width="6" fill="none">
    <circle cx="250" cy="286" r="34"/><circle cx="452" cy="286" r="34"/>
    <path d="M250 286h96l46-70"/><path d="M346 286l44-70"/>
    <animateTransform attributeName="transform" type="translate" values="0 0;4 0;0 0" dur="1.6s" repeatCount="indefinite"/>
  </g>
  <g fill="#ff8fb8" stroke="#d95f8c" stroke-width="3">
    <g><ellipse cx="300" cy="150" rx="44" ry="30"/><circle cx="336" cy="116" r="17"/>
      <path d="M350 118c14 2 22 8 26 14-10 4-20 2-28-4z" fill="#ffd166" stroke="none"/>
      <path d="M330 150c-8 18-6 34 4 44" fill="none"/><path d="M292 176l-10 34" fill="none"/></g>
    <g><ellipse cx="416" cy="168" rx="34" ry="23"/><circle cx="444" cy="140" r="13"/>
      <path d="M455 142c11 2 18 6 21 11-8 3-16 2-22-3z" fill="#ffd166" stroke="none"/>
      <path d="M420 188l-6 26" fill="none"/></g>
  </g>
</svg></body></html>
"""


def _detail(run, name, platform, model, kind):
    """Build the per-run record the dashboard fetches from /api/runs/<id>."""
    expected = 21 if kind == 'candy' else None
    answer = run.get('answer')
    prompt = ('在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状。最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？'
              if kind == 'candy' else
              '创建一个 HTML，用 SVG 绘制一只大火烈鸟和一只小火烈鸟骑双人自行车的 2D 动画，不需要任何测试。')
    response = ('最终答案：%s' % answer) if kind == 'candy' and answer is not None else (
        '<!doctype html><html><body><svg viewBox="0 0 640 360">…</svg></body></html>' if kind == 'drawing' else None)
    detail = {
        'id': run['id'], 'name': name, 'slot': run['slot'], 'kind': kind, 'status': run['status'],
        'attempts': run.get('attempts', 1), 'created_at': run['slot'], 'finished_at': run['slot'] + 96,
        'duration': run.get('duration'), 'error_code': run.get('error_code'), 'error': None,
        'answer': answer, 'response': response, 'actual_model': model, 'source': 'demo',
        'model': model, 'effort': 'medium', 'platform': platform,
        'prompt': prompt, 'instructions': '请独立解答用户的题目。可以先解释推理，最后一行严格使用“最终答案：N”。',
        'expected_answer': expected, 'tokens': {'input_tokens': 128, 'output_tokens': 1024},
        'attempt_log': [{'attempt': 1, 'status': run['status'], 'seconds': run.get('duration') or 12.0,
                         'request_id': _run_id(run['slot'], 909), 'client': {'transport': 'demo'}}],
        'artifact_url': ('/artifacts/%s.html' % run['id']) if kind == 'drawing' and run['status'] == 'pass' else None,
        'bank_version': run.get('bank_version'), 'first_status': run.get('first_status'),
        'phase': run.get('phase'),
    }
    if kind == 'candy':
        question = {'id': 'original-candy', 'title': '原始糖果题', 'prompt': prompt,
                    'instructions': detail['instructions'], 'expected': '21', 'source': 'demo fixture',
                    'version': 'original-candy-v3-fail-fast-demo'}
        detail['questions'] = [question, question] if run['status'] == 'pass' else [question]
        detail['question_results'] = [{'question_id': 'original-candy',
                                       'answer': str(answer) if answer is not None else None,
                                       'status': run['status'], 'response': response}
                                      for _ in detail['questions']]
    return detail


def _candy(slot, status, answer, ident=None):
    row = {'slot': slot, 'kind': 'candy', 'status': status, 'attempts': 1}
    if status in ('pass', 'fail'):
        row.update({'id': ident or _run_id(slot, 11), 'answer': answer, 'first_status': status,
                    'phase': 'complete', 'duration': 42.5,
                    'bank_version': 'original-candy-v3-fail-fast-demo'})
    return row


def _drawing(slot, status, ident=None):
    row = {'slot': slot, 'kind': 'drawing', 'status': status,
           'attempts': 1 if status == 'pass' else 2}
    if ident:
        row.update({'id': ident, 'duration': 96.4,
                    'error_code': None if status == 'pass' else 'GENERATION_BUDGET_EXCEEDED'})
    return row


def _metric(**overrides):
    metric = {'score': 84.2, 'actual_priority': 1680, 'quality_score': 100.0, 'cost_score': 71.4,
              'stability_score': 100.0, 'speed_score': 63.1, 'quality_rounds': 3,
              'stability_rounds': 3, 'speed_rounds': 3, 'speed_samples': 6,
              'quality_pass': 3, 'quality_fail': 0, 'successful_requests': 6, 'failed_requests': 0,
              'availability': 100.0, 'speed_first_text_seconds': 12.4, 'speed_tokens_per_second': 48.2,
              'status': 'applied', 'updated_at': time.time() - 600, 'multiplier': 0.4}
    metric.update(overrides)
    return metric


def _price(multiplier, revision, age=600):
    stamp = time.time() - age
    return {'multiplier': multiplier, 'mode': 'fixed', 'updated_at': stamp,
            'evaluated_at': stamp, 'revision': revision * 64}


def accounts():
    now = time.time()
    return [
        {'id': 'aaa111bbb222', 'name': 'relay-alpha', 'type': 'apikey', 'platform': 'openai',
         'model': 'gpt-6-astra', 'effort': 'medium', 'configured': True, 'paused': False, 'resume_at': 0,
         'routing': {'action': 'enabled', 'error_code': None, 'updated_at': now - 600},
         'circuit': {'active': 0, 'opened_at': now - 9000, 'trigger_slot': _slot(3)},
         'priority': _metric(),
         'price': _price(0.4, 'a'),
         'history': {
             'candy': [_candy(_slot(i + 1), 'pass' if i % 4 else 'fail',
                              21 if i % 4 else 29, _run_id(_slot(i + 1), 101)) for i in range(18)],
             'drawing': [_drawing(_slot(i + 1), 'pass' if i % 5 else 'error',
                                  _run_id(_slot(i + 1), 202) if i % 5 else None) for i in range(18)]}},
        {'id': 'ccc333ddd444', 'name': 'relay-beta', 'type': 'oauth', 'platform': 'anthropic',
         'model': 'claude-opus-5-5', 'effort': 'medium', 'configured': True, 'paused': False, 'resume_at': 0,
         'routing': {'action': 'restored', 'error_code': None, 'updated_at': now - 1200},
         'circuit': {'active': 0, 'opened_at': now - 4000, 'trigger_slot': _slot(2)},
         'priority': _metric(score=76.8, actual_priority=2320, quality_score=66.7, cost_score=90.9,
                             stability_score=83.3, speed_score=55.0, quality_pass=2, quality_fail=1,
                             successful_requests=5, failed_requests=1, availability=83.3,
                             speed_first_text_seconds=None, speed_tokens_per_second=None,
                             status='ready', multiplier=0.1, updated_at=now - 1200),
         'price': _price(0.1, 'b', 1200),
         'history': {
             'candy': [_candy(_slot(i + 1), 'pass' if i % 3 else 'fail',
                              21 if i % 3 else 29, _run_id(_slot(i + 1), 303)) for i in range(18)],
             'drawing': [_drawing(_slot(i + 1), 'pass' if i % 6 else 'running',
                                  _run_id(_slot(i + 1), 404) if i % 6 else None) for i in range(18)]}},
        {'id': 'eee555fff666', 'name': 'relay-gamma', 'type': 'apikey', 'platform': 'grok',
         'model': 'grok-4.7', 'effort': 'high', 'configured': True, 'paused': True, 'resume_at': 0,
         'routing': {'action': 'circuit_open', 'error_code': None, 'updated_at': now - 300},
         'circuit': {'active': 1, 'opened_at': now - 300, 'trigger_slot': _slot(1)},
         'priority': _metric(score=61.4, actual_priority=3500, quality_score=0.0, cost_score=100.0,
                             stability_score=50.0, speed_score=40.0, quality_pass=0, quality_fail=3,
                             successful_requests=3, failed_requests=3, availability=50.0,
                             status='paused', multiplier=0.0, updated_at=now - 300),
         'price': _price(0.0, 'c', 300),
         'history': {
             'candy': [_candy(_slot(i + 1), 'fail' if i % 2 else 'error',
                              29 if i % 2 else None, _run_id(_slot(i + 1), 505)) for i in range(18)],
             'drawing': [_drawing(_slot(i + 1), 'error') for i in range(18)]}},
    ]


def _models(platform, amount, unpriced=0):
    return {'platform': platform, 'amount_usd': amount, 'unpriced_requests': unpriced}


def state():
    now = time.time()
    return {
        'title': 'AI 流口水检测', 'model': 'gpt-6-astra', 'effort': 'medium', 'expected_answer': 21,
        'server_time': now, 'generated_at': now, 'next_run_at': now + 1260,
        'history_hours': 24, 'interval_seconds': 2700, 'max_retries': 2, 'refresh_seconds': 30,
        'schedule': {'timezone': 'Asia/Shanghai', 'regular_seconds': 2700, 'quiet_seconds': 5400,
                     'quiet_start': '04:00', 'quiet_end': '08:00', 'current_slot': _slot(1),
                     'next_slot': now + 1260, 'interval_seconds': 2700},
        'accounts': accounts(),
        'scheduler': {'running': False, 'last_source': 'demo', 'last_completed_slot': _slot(1),
                      'last_account_sync': now - 300, 'quality_routing_enabled': True,
                      'priority_routing_enabled': True, 'sync_error': None},
        'costs': {'windows': {
            '24h': {'models': [_models('openai', 1.284), _models('gemini', 0.412),
                               _models('grok', 0.077), _models('anthropic', 0.663)],
                    'amount_usd': 2.436},
            '30d': {'models': [_models('openai', 31.902), _models('gemini', 9.881),
                               _models('grok', 2.045, 1), _models('anthropic', 17.334)],
                    'amount_usd': 61.162}}},
        'request_policy': 'two-request-errors-v1',
        'question_bank': {'version': 'original-candy-v3-fail-fast-demo', 'templates': 1,
                          'confirmation': 'two_consecutive_correct_independent_requests',
                          'failure': 'first_wrong_answer_stops', 'ranking': 'first_answer_rate'},
        'prompts': {
            'candy': '在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？ 苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4',
            'drawing': '创建一个 HTML，用 SVG 绘制一只大火烈鸟和一只小火烈鸟骑双人自行车的 2D 动画，不需要任何测试。'},
        'instructions': {
            'candy': '请独立解答用户的题目。可以先解释推理，最后一行严格使用“最终答案：N”，其中 N 为你的整数答案。',
            'drawing': '先用一小段自然语言简述实现方案，然后给出用户要求的完整、可独立打开的 HTML 源代码。不要调用工具，不要运行测试。'},
        'request_client': {'transport': 'direct-http', 'codex_protocol_version': 'demo'},
        'benchmarks': {'openai': {'model': 'gpt-6-astra', 'effort': 'medium'},
                       'anthropic': {'model': 'claude-opus-5-5', 'effort': 'medium'},
                       'gemini': {'model': 'gemini-3.8-flash', 'effort': 'high'},
                       'grok': {'model': 'grok-4.7', 'effort': 'high'}},
        'routing_writes': {'priority': True, 'callable': False},
        'controls_enabled': True,
        'method': 'HTTP 直连适配器（不是 CLI 子进程）。读取 Sub2API 账户配置，使用对应账户的上游凭据和代理独立请求，不经过分组网关或负载均衡。',
    }


def main():
    parser = argparse.ArgumentParser(description='Preview the dashboard with synthetic data.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4191)
    parser.add_argument('--data', default='')
    parser.add_argument('--write-only', action='store_true',
                        help='write the synthetic state and exit; print its directory')
    args = parser.parse_args()
    root = Path(args.data) if args.data else Path(tempfile.mkdtemp(prefix='drool-preview-'))
    public = root / 'public'
    public.mkdir(parents=True, exist_ok=True)
    (root / 'controls').mkdir(parents=True, exist_ok=True)
    snapshot = state()
    (public / 'state.json').write_text(json.dumps(snapshot, ensure_ascii=False))
    written = 0
    for account in snapshot['accounts']:
        for kind in ('candy', 'drawing'):
            for run in account['history'][kind]:
                if not run.get('id'):
                    continue
                detail = _detail(run, account['name'], account['platform'], account['model'], kind)
                (public / (run['id'] + '.json')).write_text(json.dumps(detail, ensure_ascii=False))
                written += 1
                if detail['artifact_url']:
                    (public / (run['id'] + '.html')).write_text(ARTIFACT_TEMPLATE)
    if args.write_only:
        print(root)
        return
    dist = Path(__file__).resolve().parent.parent / 'web' / 'dist'
    if not (dist / 'index.html').exists():
        raise SystemExit('The dashboard has not been built yet: run `cd web && npm ci && npm run build`.')
    httpd = server.make_server(args.host, args.port, dist, public, True)
    print('preview: http://%s:%d/  (data: %s)' % (args.host, args.port, root), flush=True)
    print('Ctrl+C to stop.', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if not args.data:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
