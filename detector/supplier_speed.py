"""Recent completed-response speed, never network-heartbeat latency."""
import json
import math
from statistics import mean, median

from .question_bank import VERSION, TWO_PASS_VERSION

SPEED_ROUNDS = 3
LATENCY_REFERENCE_SECONDS = 10
THROUGHPUT_REFERENCE = 50


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def request_tokens(row, attempt, attempts):
    if isinstance(attempt.get('tokens'), dict):
        return attempt['tokens']
    # Old records have per-stage candy usage or one successful drawing response.
    successful = [a for a in attempts if a.get('status') in ('pass', 'fail', 'unknown')]
    if row['kind'] == 'drawing' and len(successful) == 1:
        return json.loads(row.get('tokens', '{}'))
    stage = attempt.get('stage')
    results = json.loads(row.get('question_results', '[]'))
    if (row['kind'] == 'candy' and isinstance(stage, int) and not isinstance(stage, bool)
            and 0 <= stage < len(results)
            and sum(a.get('stage') == stage for a in successful) == 1):
        result = results[stage]
        if result.get('status') == attempt.get('status') and result.get('question_id') == attempt.get('question_id'):
            return result.get('tokens', {})
    return {}


def recent_speed(rows, local):
    rounds, seen = {}, set()
    for row in sorted(rows, key=lambda r: (r['slot'], r['kind']), reverse=True):
        ident = row['account_id']
        if (ident not in local or row['model'] != local[ident]['model']
                or row['status'] not in ('pass', 'fail', 'error')
                or row['kind'] not in ('candy', 'drawing')
                or row['kind'] == 'candy' and row['bank_version'] not in (VERSION, TWO_PASS_VERSION)):
            continue
        attempts = json.loads(row['attempt_log'])
        actual = [a for a in attempts if a.get('request_id') and (
            a.get('status') in ('pass', 'fail', 'unknown')
            or a.get('status') == 'error' and a.get('failure_class') == 'upstream')]
        if not actual:
            continue
        slots = rounds.setdefault(ident, {})
        if row['slot'] not in slots and len(slots) >= SPEED_ROUNDS:
            continue
        samples = slots.setdefault(row['slot'], [])
        for attempt in actual:
            key = (ident, attempt['request_id'])
            if key in seen:
                continue
            seen.add(key)
            if attempt.get('status') not in ('pass', 'fail', 'unknown'):
                continue
            timings = attempt.get('timings', {})
            latency, elapsed = timings.get('first_text_seconds'), timings.get('elapsed_seconds')
            output = request_tokens(row, attempt, attempts).get('output_tokens')
            if not all(finite(v) for v in (latency, elapsed, output)):
                continue
            if elapsed <= 0 or not 0 <= latency <= elapsed or output <= 0:
                continue
            # Includes thinking time/tokens. This is not post-first-token decode speed.
            throughput = output / elapsed
            if finite(throughput):
                samples.append((row['kind'], latency, throughput))
    result = {}
    for ident, slots in rounds.items():
        samples = [sample for values in slots.values() for sample in values]
        kinds = {}
        for kind in ('candy', 'drawing'):
            values = [sample for sample in samples if sample[0] == kind]
            if not values:
                continue
            latency = median(v[1] for v in values)
            throughput = median(v[2] for v in values)
            points = (100 * LATENCY_REFERENCE_SECONDS / (LATENCY_REFERENCE_SECONDS + latency)
                      + 100 * throughput / (throughput + THROUGHPUT_REFERENCE)) / 2
            kinds[kind] = {'first_text_seconds': latency, 'tokens_per_second': throughput,
                           'score': points, 'samples': len(values)}
        result[ident] = {'speed_score': mean(v['score'] for v in kinds.values()) if kinds else None,
            'speed_first_text_seconds': mean(v['first_text_seconds'] for v in kinds.values()) if kinds else None,
            'speed_tokens_per_second': mean(v['tokens_per_second'] for v in kinds.values()) if kinds else None,
            'speed_samples': len(samples), 'speed_rounds': len(slots), 'speed_kinds': kinds,
            'speed_source': 'first-body-and-end-to-end-output',
            'speed_start_at': min(slots), 'speed_end_at': max(slots)}
    return result
