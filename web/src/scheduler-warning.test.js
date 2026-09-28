// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import test from 'node:test';
import assert from 'node:assert/strict';
import {schedulerWarning} from './scheduler-warning.js';
const code='NO_ELIGIBLE_PLATFORM_RESERVE';
test('paused Claude and Grok are not reported as GPT failure',()=>{
  const s={quality_routing_error:code,quality_routing_errors:{anthropic:code,grok:code}};
  const a=[{platform:'openai'},{platform:'anthropic',paused:true},{platform:'grok',paused:true}];
  assert.equal(schedulerWarning(s,a),'');
  a[1].paused=false;
  assert.equal(schedulerWarning(s,a),'Claude 调度同步需要核查：没有符合保底条件的评测账号');
});
test('multiple active platform errors retain identity',()=>{
  const message=schedulerWarning({quality_routing_error:code,quality_routing_errors:{anthropic:code,grok:code}},[{platform:'anthropic'},{platform:'grok'}]);
  assert.match(message,/Claude/);assert.match(message,/Grok/);assert.doesNotMatch(message,/GPT/);
});
test('API errors and global errors stay visible even when paused',()=>{
  assert.match(schedulerWarning({quality_routing_error:'HTTP_503',quality_routing_errors:{anthropic:'HTTP_503'}},[{platform:'anthropic',paused:true}]),/Claude.*HTTP_503/);
  assert.match(schedulerWarning({quality_routing_error:'HTTP_502',quality_routing_errors:{anthropic:code}},[{platform:'anthropic',paused:true}]),/HTTP_502/);
  assert.match(schedulerWarning({quality_routing_error:'HTTP_503'}),/HTTP_503/);
});
test('missing, future-resume and quota states are distinct',()=>{
  const s={quality_routing_error:code,quality_routing_errors:{openai:code}};
  assert.equal(schedulerWarning({},[]),'');assert.match(schedulerWarning(s,[]),/GPT/);
  assert.equal(schedulerWarning(s,[{platform:'openai',resume_at:200}],100),'');
  assert.match(schedulerWarning(s,[{platform:'openai',resume_at:99,quota:{limited:true}}],100),/GPT/);
});
