// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mergeMonitorState,setSupplierPrice} from './control-client.js';

test('price save has a narrow payload and validates acknowledgement',async()=>{
  const account={id:'aabbccddeeff',price:{multiplier:null,updated_at:0}};
  const result=await setSupplierPrice(account,.2,async(url,opts)=>{
    assert.equal(url,'/api/accounts/aabbccddeeff/price');assert.equal(opts.method,'POST');
    assert.deepEqual(JSON.parse(opts.body),{config:{mode:'fixed',multiplier:.2},expected_revision:'none'});
    return {ok:true,json:async()=>({mode:'fixed',multiplier:.2,updated_at:100,evaluated_at:100,revision:'a'.repeat(64)})};
  });
  assert.equal(result.multiplier,.2);
});
test('stale polls cannot overwrite a saved price or pause state',()=>{
  const current={accounts:[{id:'a',paused:true,updated_at:20,price:{multiplier:.2,updated_at:25}}]};
  const incoming={accounts:[{id:'a',paused:false,updated_at:10,price:{multiplier:.3,updated_at:15}}]};
  const merged=mergeMonitorState(current,incoming).accounts[0];
  assert.equal(merged.price.multiplier,.2);assert.equal(merged.paused,true);
});
test('invalid costs and conflicting writes cannot masquerade as saved',async()=>{
  await assert.rejects(setSupplierPrice({id:'a'},-1,()=>{throw Error('must not call');}),/倍率必须/);
  await assert.rejects(setSupplierPrice({id:'a'},.2,async()=>({ok:false,status:409})),/已变化/);
  await assert.rejects(setSupplierPrice({id:'a'},.2,async()=>({ok:true,json:async()=>({multiplier:.3,updated_at:20})})),/未确认/);
});
