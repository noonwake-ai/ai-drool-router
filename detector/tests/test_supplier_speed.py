# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

import json
import unittest

from detector.question_bank import VERSION
from detector.supplier_speed import recent_speed


class Speed(unittest.TestCase):
    def row(self, slot=1, kind='candy', status='pass', latency=10, elapsed=20, output=1000, **fields):
        attempt={'status':status,'request_id':f'{slot}-{kind}', 'stage':0,'question_id':'candy',
            'failure_class':'upstream','tokens':{'output_tokens':output},
            'timings':{'first_text_seconds':latency,'elapsed_seconds':elapsed,'first_data_seconds':0.001}}
        return {'account_id':1,'model':'m','slot':slot,'kind':kind,'status':status,'bank_version':VERSION,
                'attempt_log':json.dumps([attempt]),'question_results':'[]','tokens':'{}',**fields}

    def evaluate(self, rows):
        return recent_speed(rows,{1:{'model':'m'}}).get(1,{})

    def test_half_score_anchors_ignore_first_network_event(self):
        value=self.evaluate([self.row()])
        self.assertEqual(value['speed_score'],50)
        self.assertEqual(value['speed_first_text_seconds'],10)
        self.assertEqual(value['speed_tokens_per_second'],50)

    def test_only_latest_three_rounds_errors_occupy_no_backfill(self):
        rows=[self.row(i) for i in range(1,5)]
        rows.extend(self.row(i,status='error') for i in range(5,8))
        value=self.evaluate(rows)
        self.assertIsNone(value['speed_score'])
        self.assertEqual(value['speed_samples'],0)
        self.assertEqual(value['speed_start_at'],5)

    def test_scope_inflight_and_wrong_answers(self):
        rows=[self.row(status='fail'),self.row(2,status='running'),self.row(3,model='old'),self.row(4,bank_version='old')]
        self.assertEqual(self.evaluate(rows)['speed_samples'],1)

    def test_medians_balance_task_kinds_and_deduplicate_requests(self):
        rows=[self.row(i,latency=v,elapsed=1000) for i,v in enumerate((1,10,900),1)]
        rows.append(self.row(3,'drawing',latency=30,elapsed=1000))
        value=self.evaluate(rows+rows)
        self.assertEqual(value['speed_samples'],4)
        self.assertEqual(value['speed_first_text_seconds'],20)
        self.assertEqual(value['speed_rounds'],3)

    def test_invalid_or_missing_metrics_not_zero(self):
        for fields in ({'latency':None},{'latency':True},{'elapsed':0},{'output':0},
                {'latency':-1},{'latency':30},{'output':float('nan')},{'elapsed':float('inf')}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.evaluate([self.row(**fields)])['speed_score'])

    def test_old_candy_tokens_are_per_stage_not_run_aggregate(self):
        row=self.row();attempts=json.loads(row['attempt_log']);del attempts[0]['tokens']
        row.update(attempt_log=json.dumps(attempts),tokens=json.dumps({'output_tokens':999999}),
            question_results=json.dumps([{'question_id':'candy','status':'pass','tokens':{'output_tokens':1000}}]))
        self.assertEqual(self.evaluate([row])['speed_tokens_per_second'],50)
        row['question_results']='[]'
        self.assertIsNone(self.evaluate([row])['speed_score'])

    def test_old_drawing_only_unambiguous_success_gets_usage(self):
        row=self.row(kind='drawing');attempts=json.loads(row['attempt_log']);del attempts[0]['tokens']
        row.update(attempt_log=json.dumps(attempts),tokens=json.dumps({'output_tokens':1000}))
        self.assertEqual(self.evaluate([row])['speed_tokens_per_second'],50)
        attempts.append({**attempts[0],'request_id':'second'})
        row['attempt_log']=json.dumps(attempts)
        self.assertIsNone(self.evaluate([row])['speed_score'])


if __name__=='__main__':unittest.main()
