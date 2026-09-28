import test from 'node:test';
import assert from 'node:assert/strict';
import {costRows,formatCost} from './cost-view.js';

test('unknown amounts never render as free, small costs keep precision',()=>{
  for(const value of [undefined,null,NaN,Infinity,-1,'1.00'])assert.equal(formatCost(value),'未计价');
  assert.equal(formatCost(0),'$0.00');
  assert.equal(formatCost(0.0007),'$0.0007');
  assert.equal(formatCost(1234.25),'$1,234.25');
});
test('four platforms and two windows do not inherit account filters',()=>{
  const rows=costRows({windows:{'24h':{models:[{platform:'openai',amount_usd:1}]},'30d':{models:[{platform:'gemini',amount_usd:3}]}}});
  assert.deepEqual(rows.map(r=>r.label),['GPT','Gemini','Grok','Claude']);
  assert.equal(rows[0].windows[0].amount_usd,1);
  assert.equal(rows[1].windows[1].amount_usd,3);
  assert.equal(rows[0].windows[1],null);
  assert.equal(costRows(null).length,4);
});
