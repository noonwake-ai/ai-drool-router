import copy
from datetime import datetime
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from detector.controls import ControlConflict
from detector.monitor import Store, public_id
from detector.server import make_server
from detector.supplier_prices import Prices, normalize_config, price_signature, project_price
from detector.pricing_tick import tick
from detector.priority_routing import PriorityRouter

KEY = public_id(1)
CONFIG = {'mode':'scheduled','timezone':'Asia/Shanghai','periods':[
    {'start':'00:00','end':'08:00','multiplier':.1},
    {'start':'08:00','end':'24:00','multiplier':.3}]}
def at(clock):
    return datetime.fromisoformat('2026-09-16T'+clock+'+08:00').timestamp()


class Schedule(unittest.TestCase):
    def test_boundary_midnight_and_timezone(self):
        for clock, value, index in [('00:00:00',.1,0),('07:59:59',.1,0),('08:00:00',.3,1),('23:59:59',.3,1)]:
            result=project_price(CONFIG,at(clock))
            self.assertEqual((result['multiplier'],result['active_period']),(value,index))
            self.assertGreater(result['next_change_at'],at(clock))
        self.assertEqual(project_price(CONFIG,at('00:00:00')+86400)['multiplier'],.1)
        self.assertEqual(project_price(CONFIG,at('23:59:59'))['next_change_at'],at('00:00:00')+86400)

    def test_signature_ignores_clock_but_tracks_boundary_and_edit(self):
        signature=lambda clock:price_signature({KEY:project_price(CONFIG,at(clock))})
        self.assertEqual(signature('06:00:00'),signature('07:59:59'))
        self.assertNotEqual(signature('07:59:59'),signature('08:00:00'))

    def test_invalid_coverage_types_and_exclusive_modes(self):
        invalid=[{**CONFIG,'timezone':'UTC'}, {**CONFIG,'multiplier':.2}, {'mode':'fixed','multiplier':.2,'periods':[]}]
        for start,end,value in [('01:00','24:00',.2),('00:00','23:59',.2),('00:00','24:00',None),('00:00','24:00',True),('00:00','24:00',float('nan')),('00:00','24:00',-1),('24:00','24:00',.2),('00:00','25:00',.2)]:
            invalid.append({**CONFIG,'periods':[{'start':start,'end':end,'multiplier':value}]})
        for start in ['07:59','08:01','']:
            value=copy.deepcopy(CONFIG);value['periods'][1]['start']=start;invalid.append(value)
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_config(value)
        self.assertEqual(normalize_config({'mode':'fixed','multiplier':None})['multiplier'],None)

    def test_24_hourly_periods_and_limit(self):
        periods=[{'start':f'{h:02d}:00','end':f'{h+1:02d}:00','multiplier':h/100} for h in range(24)]
        self.assertEqual(len(normalize_config({**CONFIG,'periods':periods})['periods']),24)
        with self.assertRaises(ValueError):normalize_config({**CONFIG,'periods':periods+[periods[-1]]})


class Storage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.prices=Prices(self.temp.name)

    def test_legacy_read_and_mode_switch_revision_atomicity(self):
        path=Path(self.temp.name)/'prices.json'
        path.write_text(json.dumps({KEY:{'multiplier':.2,'updated_at':1}}))
        legacy=self.prices.read()[KEY]
        self.assertEqual(legacy['mode'],'fixed')
        saved=self.prices.set_config(KEY,CONFIG,legacy['revision'])
        raw=json.loads(path.read_text())[KEY]
        self.assertNotIn('multiplier',raw)
        with self.assertRaises(ControlConflict):self.prices.set(KEY,.4,saved['multiplier'])
        with self.assertRaises(ControlConflict):self.prices.set_config(KEY,CONFIG,legacy['revision'])
        self.assertEqual(json.loads(path.read_text())[KEY],raw)
        fixed=self.prices.set_config(KEY,{'mode':'fixed','multiplier':.5},saved['revision'])
        self.assertNotIn('periods',fixed)
        self.assertEqual(self.prices.read()[KEY]['multiplier'],.5)

    def test_new_record_and_bad_schedule_preserve_file(self):
        saved=self.prices.set_config(KEY,CONFIG,'none')
        before=(Path(self.temp.name)/'prices.json').read_bytes()
        with self.assertRaises(ValueError):self.prices.set_config(KEY,{**CONFIG,'periods':[]},saved['revision'])
        self.assertEqual((Path(self.temp.name)/'prices.json').read_bytes(),before)
        with self.assertRaises(ControlConflict):self.prices.set_config(KEY,CONFIG,'none')


class API(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);self.store.sync([{'id':1,'name':'fixture','platform':'openai','type':'apikey'}],at('08:00:00'),'fixture');self.store.publish_state()
        self.server=make_server('127.0.0.1',0,self.temp.name,self.store.public,True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def stop(self):
        self.server.shutdown();self.thread.join();self.server.server_close()

    def post(self,body,key=KEY,origin=None):
        request=Request(self.url+'/api/accounts/'+key+'/price',data=json.dumps(body).encode(),headers={'Origin':origin or self.url,'Content-Type':'application/json'})
        try:
            with urlopen(request) as response:return response.status,json.load(response)
        except HTTPError as response:return response.code,json.load(response)

    def test_save_conflict_legacy_protection_and_fresh_read(self):
        status,saved=self.post({'config':CONFIG,'expected_revision':'none'});self.assertEqual(status,200)
        self.assertEqual(self.post({'config':CONFIG,'expected_revision':'none'})[0],409)
        self.assertEqual(self.post({'multiplier':.2,'expected_multiplier':saved['multiplier']})[0],409)
        with patch('detector.supplier_prices.time.time',return_value=at('07:59:59')):
            with urlopen(self.url+'/api/state') as response:before=json.load(response)['accounts'][0]['price']
        with patch('detector.supplier_prices.time.time',return_value=at('08:00:00')):
            with urlopen(self.url+'/api/state') as response:after=json.load(response)['accounts'][0]['price']
        self.assertEqual((before['multiplier'],after['multiplier']),(.1,.3));self.assertEqual(before['revision'],after['revision'])

    def test_validation_and_security(self):
        body={'config':CONFIG,'expected_revision':'none'}
        self.assertEqual(self.post(body,origin='https://other.test')[0],403)
        self.assertEqual(self.post(body,key='abcdef123456')[0],404)
        self.assertEqual(self.post({**body,'config':{**CONFIG,'periods':[]}})[0],400)
        self.assertEqual(self.post(body)[0],200)


class Tick(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)

    def test_unchanged_tick_does_not_read_key_or_request_upstream(self):
        from detector.priority_routing import SCORE_VERSION
        self.store.meta('priority_score_version',SCORE_VERSION)
        self.store.meta('priority_price_signature',price_signature({}))
        with patch('detector.pricing_tick.Sub2API') as api:
            self.assertEqual(tick(self.temp.name,'missing-key','https://unused.test'),{'status':'unchanged','model_requests':0})
            api.assert_not_called()

    def test_minute_tick_defers_to_running_reconcile(self):
        with (self.store.private/'priority.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with patch('detector.priority_routing.PriorityRouter._reconcile') as reconcile:
                self.assertEqual(PriorityRouter(self.store,None,True).reconcile(prices_only=True),'busy')
                reconcile.assert_not_called()

    def test_failure_does_not_acknowledge_new_signature(self):
        router=PriorityRouter(self.store,None,True)
        with patch.object(router,'_reconcile',side_effect=ValueError('bad data')):
            self.assertIsNone(router.reconcile(prices_only=True))
        self.assertNotIn('priority_price_signature',self.store.metadata())
        self.assertEqual(self.store.metadata()['priority_error'],'PRIORITY_DATA_UNAVAILABLE')


if __name__=='__main__':unittest.main()
