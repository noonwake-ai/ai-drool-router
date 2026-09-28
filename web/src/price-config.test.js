// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {PRICE_TIMEZONE,priceConfig,splitPeriod,removePeriod,timeMinute} from './price-config.js';
import {mergeMonitorState,setSupplierPrice} from './control-client.js';
const schedule = {mode:'scheduled',timezone:PRICE_TIMEZONE,periods:[{start:'00:00',end:'08:00',multiplier:.1},{start:'08:00',end:'24:00',multiplier:.3}]};
test('exclusive configuration and exact daily coverage',()=>{
  assert.deepEqual(priceConfig(schedule),schedule);
  assert.deepEqual(priceConfig({...schedule,mode:'fixed',multiplier:.2}),{mode:'fixed',multiplier:.2});
  for(const periods of [[],[{start:'01:00',end:'24:00',multiplier:.2}], [{start:'00:00',end:'23:00',multiplier:.2}],
    [schedule.periods[0],{start:'07:59',end:'24:00',multiplier:.1}], [{start:'00:00',end:'24:00',multiplier:null}]]) assert.throws(()=>priceConfig({...schedule,periods}));
  for(const value of ['24:00','7:30','12:60',''])assert.throws(()=>timeMinute(value));
  assert.equal(timeMinute('24:00',true),1440);
});
test('split and remove keep a contiguous day including first/last rows',()=>{
  const periods=splitPeriod(schedule.periods,1);
  assert.equal(periods.length,3);assert.equal(periods[1].end,'16:00');
  for(const index of [0,1,2]) assert.doesNotThrow(()=>priceConfig({...schedule,periods:removePeriod(periods,index)}));
  const single=removePeriod(schedule.periods,0);assert.equal(single[0].start,'00:00');assert.deepEqual(removePeriod(single,0),single);
});
test('schedule client submits one mode with revision and validates its effective rate',async()=>{
  const account={id:'a',price:{revision:'b'.repeat(64)}};
  const response={...schedule,multiplier:.3,active_period:1,updated_at:100,evaluated_at:101,revision:'a'.repeat(64)};
  await setSupplierPrice(account,schedule,async(url,options)=>{
    assert.deepEqual(JSON.parse(options.body),{config:schedule,expected_revision:'b'.repeat(64)});
    return {ok:true,json:async()=>response};
  });
  await assert.rejects(setSupplierPrice(account,schedule,async()=>({ok:true,json:async()=>({...response,multiplier:.9})})),/未确认/);
});
test('older same-revision polls cannot undo a boundary change, newer writes still win',()=>{
  const current={accounts:[{id:'a',price:{updated_at:10,evaluated_at:200,multiplier:.3}}]};
  const incoming={accounts:[{id:'a',price:{updated_at:10,evaluated_at:199,multiplier:.1}}]};
  assert.equal(mergeMonitorState(current,incoming).accounts[0].price.multiplier,.3);
  incoming.accounts[0].price.updated_at=11;
  assert.equal(mergeMonitorState(current,incoming).accounts[0].price.multiplier,.1);
});
