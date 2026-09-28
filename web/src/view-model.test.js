// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import assert from 'node:assert/strict';
import {test} from 'node:test';
import {accountPage, answerText, candyAnswers, drawingPreview, logicStats, nextSort, PAGE_SIZE, quality, questionResult, responseExcerpt, resultLabel, showCandyMeme, SORT_OPTIONS, stageLabel} from './view-model.js';

const accounts = Array.from({length: 11}, (_, i) => ({id: String(i), name: `Account ${i}`,
  type: i < 5 ? 'oauth' : 'apikey', history: {candy: [{id: 'run', status: i === 7 ? 'error' : 'pass'}], drawing: []}}));

test('call priority uses the gateway value and is reversible', () => {
  const rows=[{...accounts[0],id:'a',name:'a',priority:{actual_priority:80,quality_score:20,stability_score:90}},
    {...accounts[0],id:'b',name:'b',priority:{actual_priority:10,quality_score:80,stability_score:20}},
    {...accounts[0],id:'c',name:'c',priority:{actual_priority:40,quality_score:50,stability_score:50}}];
  // Smaller gateway numbers are called first, so 'b' leads by default.
  assert.deepEqual(accountPage(rows).accounts.map(a=>a.id),['b','c','a']);
  assert.deepEqual(accountPage(rows,{sort:'priority',direction:'asc'}).accounts.map(a=>a.id),['a','c','b']);
  assert.deepEqual(accountPage(rows,{sort:'quality'}).accounts.map(a=>a.id),['b','c','a']);
  assert.deepEqual(accountPage(rows,{sort:'stability'}).accounts.map(a=>a.id),['a','c','b']);
});

test('accounts without a known metric always sort last in both directions', () => {
  const rows=[{...accounts[0],id:'known',name:'known',priority:{actual_priority:500}},
    {...accounts[0],id:'unknown',name:'unknown'}];
  assert.deepEqual(accountPage(rows).accounts.map(a=>a.id),['known','unknown']);
  assert.deepEqual(accountPage(rows,{direction:'asc'}).accounts.map(a=>a.id),['known','unknown']);
});

test('same-question review shows its own answer, never the primary response', () => {
  const detail={questions:[{id:'original-candy'},{id:'original-candy'}],question_results:[
    {question_id:'original-candy',answer:'29',status:'fail'},
    {question_id:'original-candy',answer:'21',status:'pass'}]};
  assert.equal(questionResult(detail,0).answer,'29');
  assert.equal(questionResult(detail,1).answer,'21');
  detail.question_results.pop();
  assert.equal(questionResult(detail,1),undefined);
  assert.equal(resultLabel({status:'fail',bank_version:'original-candy-v1-20260914'}),'两次答错');
  assert.equal(resultLabel({status:'fail',bank_version:'closed-v1-dec7c5b1'}),'两题答错');
});

test('one hundred per page with actual priority ordering, no duplication and clamped pages', () => {
  const many=Array.from({length:101},(_,i)=>({...accounts[i%11],id:String(i),name:`Account ${i}`,
    priority:{actual_priority:i===7?null:i,score:100-i/2},
    history:{candy:[{id:'run',status:i===7?'error':'pass'}],drawing:[]}}));
  assert.equal(PAGE_SIZE,100);
  const first=accountPage(many),second=accountPage(many,{page:2});
  assert.equal(first.accounts.length,100);
  assert.deepEqual(first.accounts.slice(0,3).map(a=>a.id),['0','1','2']);
  assert.deepEqual(second.accounts.map(a => a.id), ['7']);
  assert.equal(second.start,100);
  assert.equal(new Set([...first.accounts,...second.accounts].map(a=>a.id)).size,101);
  assert.equal(accountPage(many, {page: 9}).page, 2);
  assert.equal(accountPage(many.slice(0,100)).pages,1);
  assert.equal(accountPage(many,{page:-1}).page,1);
});

test('inline candy summary keeps primary and review evidence separate', () => {
  const detail={questions:[{id:'original',expected:21},{id:'original',expected:21}],question_results:[
    {question_id:'original',answer:29,status:'fail',response:'首次回答'},
    {question_id:'original',answer:21,status:'pass',response:'复核回答'}]};
  const answers=candyAnswers(detail);
  assert.deepEqual(answers.map(a=>[a.answer,a.status,a.expected]),[[29,'fail',21],[21,'pass',21]]);
  detail.question_results.pop();
  assert.equal(candyAnswers(detail)[1].answer,undefined);
  assert.equal(candyAnswers(detail)[1].status,'pending');
  detail.question_results[0].question_id='different';
  assert.equal(candyAnswers(detail)[0].answer,undefined);
});

test('legacy answers, unknown results and long output remain bounded and honest', () => {
  assert.equal(candyAnswers({answer:29,expected_answer:21,status:'fail'})[0].answer,29);
  assert.equal(answerText(null),'未识别');
  assert.equal(answerText(0),'0');
  assert.equal(answerText({a:21}),'{"a":21}');
  assert.equal(responseExcerpt('  最终答案：\n21  '),'最终答案： 21');
  assert.equal(responseExcerpt('甲'.repeat(200)).length,161);
  assert.equal(responseExcerpt(null),'');
});
test('categories use exact account types and compose with search/status', () => {
  const oauth = accountPage(accounts, {category: 'oauth', page: 2});
  assert.deepEqual(oauth.counts, {all: 11, oauth: 5, apikey: 6, grok: 0, gemini: 0, claude: 0});
  assert.equal(oauth.total, 5);
  assert.equal(oauth.page, 1);
  assert.deepEqual(accountPage(accounts, {category: 'apikey', status: 'issues', query: ' ACCOUNT '}).accounts.map(a => a.id), ['7']);
});
test('non-GPT categories include both OAuth and API types', () => {
  const mixed = ['grok', 'gemini', 'anthropic'].flatMap((platform, i) => ['oauth', 'apikey'].map((type, n) =>
    ({...accounts[0], id:`${i}-${n}`, platform, type})));
  const state = accountPage(mixed);
  assert.deepEqual(state.counts, {all:6, oauth:0, apikey:0, grok:2, gemini:2, claude:2});
  assert.equal(accountPage(mixed, {category:'claude'}).accounts.length, 2);
});
test('first-answer stats and scores do not override actual calling priority', () => {
  const make = (id, statuses) => ({...accounts[0],id,name:id,history:{candy:statuses.map((status,i)=>({id:String(i),status})),drawing:[]}});
  const input = [make('no-data',['error']),make('half',['pass','fail']),make('perfect',['pass','error']),make('wrong',['fail'])];
  input[1].priority={score:50,actual_priority:200};input[2].priority={score:100,actual_priority:300};input[3].priority={score:0,actual_priority:1};
  assert.equal(quality(input[2]).rate,1);
  assert.deepEqual(accountPage(input).accounts.map(a=>a.id),['wrong','half','perfect','no-data']);
  assert.equal(input[0].id,'no-data');
});

test('four sorts toggle descending first and retain unknowns last in either direction', () => {
  const make=(id,priority,quality,stability,cost)=>({...accounts[0],id,name:id,
    priority:{actual_priority:priority,quality_score:quality,stability_score:stability},price:{multiplier:cost}});
  const input=[make('a',70,90,10,.2),make('b',90,10,70,1.5),make('c',10,70,90,0),make('unknown',null,NaN,undefined,null)];
  const expected={priority:['c','a','b'],quality:['a','c','b'],stability:['c','b','a'],cost:['b','a','c']};
  assert.deepEqual(SORT_OPTIONS.map(option=>option.id),Object.keys(expected));
  assert.equal(SORT_OPTIONS[0].label,'调用优先级');
  for(const [sort,ids] of Object.entries(expected)) {
    assert.deepEqual(accountPage(input,{sort}).accounts.map(a=>a.id),[...ids,'unknown']);
    assert.deepEqual(accountPage(input,{sort,direction:'asc'}).accounts.map(a=>a.id),[...ids].reverse().concat('unknown'));
  }
  let selected={key:'priority',direction:'desc'};
  selected=nextSort(selected,'priority');assert.equal(selected.direction,'asc');
  selected=nextSort(selected,'priority');assert.equal(selected.direction,'desc');
  selected=nextSort(selected,'cost');assert.deepEqual(selected,{key:'cost',direction:'desc'});
  assert.deepEqual(nextSort(selected,'priority'),{key:'priority',direction:'desc'});
  assert.deepEqual(input.map(a=>a.id),['a','b','c','unknown']);
});

test('priority uses actual values including zero, not pending targets or computed scores', () => {
  const make=(id,actual_priority)=>({...accounts[0],id,name:id,
    priority:{actual_priority,score:100,target_priority:0}});
  const input=[make('unknown-null',null),make('zero',0),make('low',5000),make('high',1),
    make('unknown-missing',undefined),make('unknown-invalid',NaN),make('unknown-string','1'),make('unknown-infinite',Infinity)];
  input[1].priority={actual_priority:0,score:0,target_priority:50000};
  for(const sort of ['priority','score','invalid']) {
    for(const direction of ['desc','asc']) {
      const sorted=accountPage(input,{sort,direction}).accounts;
      assert.deepEqual(sorted.slice(0,3).map(a=>a.id),direction==='desc'?['zero','high','low']:['low','high','zero']);
      assert.ok(sorted.slice(3).every(a=>a.id.startsWith('unknown-')));
    }
  }
});

test('equal priorities sort by name and id regardless of composite score or availability', () => {
  const make=(id,name,score)=>({...accounts[0],id,name,priority:{actual_priority:1,score}});
  const input=[make('b','Account 10',100),make('c','Account 2',20),make('a','Account 2',0)];
  input[2].paused=true;
  for(const direction of ['desc','asc']) {
    assert.deepEqual(accountPage(input,{direction}).accounts.map(a=>a.id),['a','c','b']);
  }
});

test('quality sort never silently falls back to legacy first-answer rates', () => {
  const old={...accounts[0],id:'old',name:'old'};
  const current={...accounts[0],id:'current',name:'current',priority:{quality_score:0}};
  for(const direction of ['desc','asc']) assert.equal(accountPage([old,current],{sort:'quality',direction}).accounts[0].id,'current');
});
test('preview keeps the last successful drawing while queued, running or failed', () => {
  const previous = {id:'old',status:'pass'};
  for (const status of ['pending','running','error','quota']) {
    const rows=[previous,{id:'new',status}];
    assert.equal(drawingPreview(rows).art.id,'old');
    assert.equal(drawingPreview(rows).previous,true);
    assert.equal(drawingPreview(rows).current.status,status);
  }
  assert.equal(drawingPreview([previous,{id:'new',status:'pass'}]).art.id,'new');
});
test('quota skip preserves completed answers and excludes unasked stages from scores', () => {
  const answers=candyAnswers({status:'quota',error:'限额跳过',questions:[{id:'same'},{id:'same'}],
    question_results:[{question_id:'same',answer:29,status:'fail'}]});
  assert.equal(answers[0].status,'fail');
  assert.equal(answers[1].status,'quota');
  assert.equal(answers[1].response,'限额跳过');
  assert.equal(logicStats([{status:'pass'},{status:'quota'}]).rate,1);
});
test('explicit history selection and first-run placeholders remain honest', () => {
  const rows=[{id:'bad',status:'error'},{id:'good',status:'pass'},{id:'active',status:'running'}];
  assert.equal(drawingPreview(rows,'bad').art.id,'bad');
  assert.equal(drawingPreview(rows,'bad').selected,true);
  assert.equal(drawingPreview(rows,'expired').art.id,'good');
  assert.equal(drawingPreview([{id:'active',status:'running'}]).art.status,'running');
  assert.equal(drawingPreview([]).art,undefined);
});
test('unknown categories are not guessed from names and empty pages stay valid', () => {
  const unknown = {...accounts[0], type: 'unknown', name: 'OAuth account'};
  assert.equal(accountPage([unknown], {category: 'oauth'}).total, 0);
  const empty = accountPage(accounts, {query: 'missing', page: 2});
  assert.deepEqual([empty.total, empty.page, empty.pages], [0, 1, 1]);
});
test('candy meme follows only the latest completed result, not the 24-hour score', () => {
  const rows = statuses => statuses.map((status, i) => ({id: String(i), status}));
  assert.equal(showCandyMeme(rows(['pass', 'fail'])), true);
  assert.equal(showCandyMeme(rows(['fail', 'pass'])), false);
  assert.equal(showCandyMeme(rows(['fail', 'error'])), true);
  assert.equal(showCandyMeme(rows(['error'])), false);
  assert.equal(showCandyMeme([]), false);
});
test('in-progress or paused candy runs retain the previous completed meme state', () => {
  for (const status of ['pending', 'running', 'paused', 'empty']) {
    assert.equal(showCandyMeme([{id:'done',status:'fail'},{id:'next',status}]), true);
    assert.equal(showCandyMeme([{id:'done',status:'pass'},{id:'next',status}]), false);
    assert.equal(showCandyMeme([{id:'next',status}]), false);
  }
});
test('confirmation does not inflate primary accuracy and unknown review keeps the stamp', () => {
  const rows=[{id:'a',status:'pass',first_status:'pass'}, {id:'b',status:'pass',first_status:'fail'},
    {id:'c',status:'error',first_status:'fail'}, {id:'d',status:'error'}];
  assert.equal(logicStats(rows).rate,1/3);
  assert.equal(logicStats(rows).samples,3);
  assert.equal(logicStats(rows).reviewed,1);
  assert.equal(resultLabel(rows[1]),'旧规则复核通过');
  assert.equal(resultLabel({status:'running',phase:'review'}),'复核中');
  assert.equal(showCandyMeme([{id:'old',status:'fail'}, {id:'new',status:'running',first_status:'fail'}]),true);
  assert.equal(showCandyMeme([{id:'old',status:'fail'},rows[1]]),false);
});

test('strict two-pass results and stages cannot be mistaken for old review passes', () => {
  const current={bank_version:'original-candy-v2-two-pass-20260915'};
  assert.equal(resultLabel({...current,status:'pass'}),'连续两次答对');
  assert.equal(resultLabel({...current,status:'fail',first_status:'fail'}),'未连续答对');
  assert.equal(resultLabel({...current,status:'fail',first_status:'pass'}),'未连续答对');
  assert.equal(resultLabel({...current,status:'running',phase:'verification'}),'第二次检测中');
  assert.equal(resultLabel({...current,status:'error',error_code:'NON_CONSECUTIVE_PASSES'}),'待连续通过');
  assert.equal(stageLabel(current,1),'第二次');
  const answers=candyAnswers({...current,status:'fail',questions:[{id:'same',expected:21},{id:'same',expected:21}],
    question_results:[{question_id:'same',status:'fail',answer:29},{question_id:'same',status:'pass',answer:21}]});
  assert.deepEqual(answers.map(a=>a.label),['第一次作答','第二次作答']);
  assert.deepEqual(answers.map(a=>a.answer),[29,21]);
  assert.equal(showCandyMeme([{id:'new',...current,status:'fail',first_status:'fail'}]),true);
  assert.equal(resultLabel({bank_version:'original-candy-v1-20260914',status:'pass'}),'旧规则通过');
});

test('first wrong answer ends immediately without showing an unasked second stage', () => {
  const detail={bank_version:'original-candy-v3-fail-fast-20260915',status:'fail',first_status:'fail',
    questions:[{id:'original-candy',expected:21},{id:'original-candy',expected:21}],
    question_results:[{question_id:'original-candy',status:'fail',answer:29}]};
  assert.equal(resultLabel(detail),'首次答错 · 已结束');
  assert.equal(candyAnswers(detail).length,1);
  assert.equal(candyAnswers(detail)[0].label,'第一次作答');
  assert.equal(candyAnswers(detail)[0].answer,29);
  assert.equal(showCandyMeme([{...detail,id:'run'}]),true);
  assert.equal(resultLabel({...detail,status:'pass',first_status:'pass'}),'连续两次答对');
  assert.equal(resultLabel({...detail,first_status:'pass'}),'未连续答对');
  assert.equal(candyAnswers({...detail,status:'running',first_status:'pass'}).length,2);
});
