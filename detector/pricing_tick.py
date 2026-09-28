"""Refresh only changed rate-driven priorities; never start model evaluations."""
import argparse
import json
import os
from pathlib import Path

from .monitor import Store
from .priority_routing import PriorityRouter, SCORE_VERSION
from .supplier_prices import Prices, price_signature
from .upstream import Sub2API


def tick(root, key_file, base_url):
    os.umask(0o027)
    store = Store(root)
    signature = price_signature(Prices(store.root/'controls').read())
    meta = store.metadata()
    if meta.get('priority_price_signature') == signature and meta.get('priority_score_version') == SCORE_VERSION:
        return {'status': 'unchanged', 'model_requests': 0}
    api = Sub2API(base_url, Path(key_file).read_text().strip() if key_file else (os.environ.get('SUB2API_ADMIN_KEY') or '').strip())
    status = PriorityRouter(store, api, True).reconcile(prices_only=True)
    meta = store.metadata()
    return {'status': status or 'error', 'model_requests': 0,
            'partial': bool(meta.get('priority_partial')), 'error': meta.get('priority_error')}


if __name__ == '__main__':
    from . import config
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=str(config.data_dir()))
    parser.add_argument('--base-url', default=config.get('base_url') or '')
    parser.add_argument('--key-file', default=os.environ.get('SUB2API_ADMIN_KEY_FILE',''))
    args = parser.parse_args()
    try:
        result = tick(args.data, args.key_file, args.base_url)
    except Exception:
        result = {'status': 'error', 'error': 'PRICE_TICK_UNAVAILABLE', 'model_requests': 0}
    print(json.dumps(result), flush=True)
    raise SystemExit(1 if result.get('error') or result.get('partial') else 0)
