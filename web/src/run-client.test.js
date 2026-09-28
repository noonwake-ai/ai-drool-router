import test from 'node:test';
import assert from 'node:assert/strict';
import {readRun} from './run-client.js';

test('history reads saved results with GET and no request body', async () => {
  const controller=new AbortController(),detail={id:'chosen',answer:21};
  const result=await readRun('chosen',controller.signal,async(url,options)=>{
    assert.equal(url,'/api/runs/chosen');
    assert.deepEqual(options,{method:'GET',signal:controller.signal,cache:'no-store'});
    return {ok:true,json:async()=>detail};
  });
  assert.equal(result,detail);
});

test('expired, unavailable and mismatched records cannot show a different answer', async () => {
  for(const status of [410,404,503]) {
    await assert.rejects(readRun('chosen',undefined,async()=>({ok:false,status})),
      status===410?/超过 24 小时/:/无法加载/);
  }
  await assert.rejects(readRun('chosen',undefined,async()=>({ok:true,json:async()=>({id:'other',answer:21})})),/不匹配/);
});

test('changing selection can abort an in-flight history read', async () => {
  const controller=new AbortController();
  const pending=readRun('chosen',controller.signal,async(url,{signal})=>new Promise((resolve,reject)=>{
    signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
  }));
  controller.abort();
  await assert.rejects(pending,{name:'AbortError'});
});
