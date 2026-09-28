import {tr} from './i18n-core.js';

export const COST_MODELS = [
  {id:'openai',label:'GPT'}, {id:'gemini',label:'Gemini'},
  {id:'grok',label:'Grok'}, {id:'anthropic',label:'Claude'},
];

export function formatCost(value) {
  if (typeof value!=='number'||!Number.isFinite(value)||value<0) return tr('cost.unpricedLabel');
  return '$'+value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4});
}

export function costRows(costs) {
  return COST_MODELS.map(model=>({...model, windows:['24h','30d'].map(key=>
    costs?.windows?.[key]?.models?.find(item=>item.platform===model.id) || null)}));
}
