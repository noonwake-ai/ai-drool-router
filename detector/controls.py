"""INPUT: monitor-only pause decisions. OUTPUT: atomic control state, never upstream writes."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid
from .schedule import next_slot


class ControlConflict(Exception):
    pass


class Controls:
    def __init__(self, root):
        self.root = Path(root)

    def read(self):
        try:
            data = json.loads((self.root/'accounts.json').read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or len(data) > 5000:
            raise ValueError('INVALID_CONTROL_STATE')
        for key, value in data.items():
            if not re.fullmatch(r'[a-f0-9]{12}', key) or not isinstance(value, dict) or type(value.get('paused')) is not bool:
                raise ValueError('INVALID_CONTROL_STATE')
        return data

    def set_paused(self, account_id, paused, expected=None, now=None):
        if not re.fullmatch(r'[a-f0-9]{12}', account_id) or type(paused) is not bool:
            raise ValueError('INVALID_CONTROL')
        self.root.mkdir(mode=0o750, exist_ok=True)
        now = time.time() if now is None else now
        with (self.root/'write.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            current = data.get(account_id, {'paused': False, 'resume_at': 0})
            if expected is not None and current['paused'] != expected:
                raise ControlConflict('CONTROL_CHANGED')
            if current['paused'] == paused:
                return current
            data[account_id] = {'paused': paused, 'updated_at': now,
                                'resume_at': 0 if paused else next_slot(now)}
            target = self.root/('.'+uuid.uuid4().hex+'.tmp')
            try:
                with os.fdopen(os.open(target, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o640), 'w') as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(target, self.root/'accounts.json')
            finally:
                target.unlink(missing_ok=True)
            return data[account_id]

    def blocked(self, account_id, slot):
        state = self.read().get(account_id, {})
        return state.get('paused', False) or state.get('resume_at', 0) > slot


if __name__ == '__main__':
    from . import config
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=str(config.data_dir()))
    parser.add_argument('--account', required=True)
    parser.add_argument('--paused', choices=('true','false'), required=True)
    args = parser.parse_args()
    state = json.loads((Path(args.data)/'public/state.json').read_text())
    if args.account not in {a['id'] for a in state['accounts']}:
        parser.error('Unknown monitor account')
    print(json.dumps(Controls(Path(args.data)/'controls').set_paused(args.account, args.paused=='true')))
