import test from 'node:test';
import assert from 'node:assert/strict';
import {controlLabel, controlMessage, mergeMonitorState, setMonitorPaused} from './control-client.js';

test('pause and start use the exact requested labels and toast copy', () => {
  assert.equal(controlLabel(false), '暂停评测');
  assert.equal(controlLabel(true), '开始评测');
  assert.equal(controlMessage(true), '将暂停对该模型进行降智检测。');
  assert.equal(controlMessage(false), '开始对该模型进行检测。');
});

test('pause and start await persisted server acknowledgement', async () => {
  for (const paused of [false, true]) {
    let calls = 0;
    const result = await setMonitorPaused({id:'0123456789ab',paused}, async (url, options) => {
      calls++;
      assert.equal(url, '/api/accounts/0123456789ab/pause');
      assert.equal(options.method, 'POST');
      assert.deepEqual(JSON.parse(options.body), {paused:!paused,expected_paused:paused});
      return {ok:true,json:async()=>({paused:!paused,updated_at:123,resume_at:0})};
    });
    assert.equal(calls,1);
    assert.equal(result.paused,!paused);
  }
});

test('failed and conflicting writes are not reported as successful', async () => {
  for (const status of [403,405,409,503]) {
    await assert.rejects(setMonitorPaused({id:'0123456789ab',paused:false},async()=>({ok:false,status})),
      error=>error.status===status && error.message!==controlMessage(true));
  }
  await assert.rejects(setMonitorPaused({id:'0123456789ab',paused:false},async()=>{
    throw new TypeError('network');
  }),/操作结果未确认/);
});

test('invalid acknowledgement cannot update button state', async () => {
  for (const updated of [null,{}, {paused:false,updated_at:123}, {paused:true}]) {
    await assert.rejects(setMonitorPaused({id:'0123456789ab',paused:false},async()=>({ok:true,json:async()=>updated})),/未确认/);
  }
});

test('stale polling cannot revert pause or start while fresh server changes win', () => {
  for (const paused of [false,true]) {
    const current={accounts:[{id:'a',paused,updated_at:20,resume_at:30}]};
    const incoming={accounts:[{id:'a',paused:!paused,updated_at:10,history:'new'}, {id:'b',paused:false}]};
    const merged=mergeMonitorState(current,incoming);
    assert.equal(merged.accounts[0].paused,paused);
    assert.equal(merged.accounts[0].history,'new');
    assert.equal(merged.accounts[0].updated_at,20);
    assert.equal(merged.accounts[1].paused,false);
    assert.equal(incoming.accounts[0].paused,!paused);
    incoming.accounts[0].updated_at=21;
    assert.equal(mergeMonitorState(current,incoming).accounts[0].paused,!paused);
  }
});
