import {tr} from './i18n-core.js';

export const PAGE_SIZE = 100;
export const latest = rows => [...rows].reverse().find(row => row.id);
export const firstVerdict = row => row.first_status || (['pass','fail'].includes(row.status) ? row.status : null);
const isFailFast = row => Boolean(row?.bank_version?.startsWith('original-candy-v3-fail-fast-'));
export const isTwoPass = row => isFailFast(row) || Boolean(row?.bank_version?.startsWith('original-candy-v2-two-pass-'));
export const isDisplaySingle = row => ['doubao-single-candy-v1','qwen-single-candy-v1','kimi-single-candy-v1','deepseek-single-candy-v1'].includes(row?.bank_version);
export const stageLabel = (row, index) => isDisplaySingle(row) ? tr('stage.single')
  : isTwoPass(row) ? tr(index ? 'stage.second' : 'stage.first') : tr(index ? 'stage.review' : 'stage.primary');
export function resultLabel(row) {
  if (isDisplaySingle(row)) {
    if (row.status === 'pass') return tr('verdict.singlePass');
    if (row.status === 'fail') return tr('verdict.singleFail');
  }
  if (isTwoPass(row)) {
    if (isFailFast(row) && row.status === 'fail' && row.first_status === 'fail') return tr('verdict.firstFailStop');
    if (row.status === 'running' && row.phase === 'verification') return tr('verdict.secondRunning');
    if (row.status === 'pass') return tr('verdict.doublePass');
    if (row.status === 'fail') return tr('verdict.doubleFail');
    if (row.error_code === 'NON_CONSECUTIVE_PASSES') return tr('verdict.awaitConsecutive');
  }
  if (row?.status === 'running' && row.phase === 'review') return tr('verdict.reviewing');
  if (row?.status === 'pass' && row.first_status === 'fail') return tr('verdict.legacyReviewPass');
  if (row?.status === 'pass' && row.bank_version && !isTwoPass(row) && !isDisplaySingle(row)) return tr('verdict.legacyPass');
  if (row?.status === 'fail' && row.bank_version) return tr(row.bank_version.startsWith('original-candy-') ? 'verdict.twoWrong' : 'verdict.twoQuestionFail');
  if (row?.error_code === 'UNGRADABLE_ANSWER') return tr('verdict.answerPending');
  return null;
}
export function questionResult(detail, index) {
  const result = detail.question_results?.[index];
  return result?.question_id === detail.questions?.[index]?.id ? result : undefined;
}
export const answerText = value => value == null ? tr('answer.unknown') : typeof value === 'object' ? JSON.stringify(value) : String(value);
export function candyAnswers(detail) {
  if (!detail.questions?.length) return [{label:tr('label.modelAnswer'), expected:detail.expected_answer,
    answer:detail.answer, status:detail.status, response:detail.response}];
  const questions = isFailFast(detail) && detail.status === 'fail' && detail.first_status === 'fail'
    ? detail.questions.slice(0, detail.question_results?.length || 1) : detail.questions;
  return questions.map((question, index) => {
    const result = questionResult(detail, index);
    return {label:isDisplaySingle(detail) ? tr('label.singleAnswer')
      : isTwoPass(detail) ? tr('label.answerStage',{stage:stageLabel(detail,index)})
      : tr(index ? 'label.reviewedAnswer' : 'label.firstAnswer'), expected:question.expected,
      answer:result?.answer, status:result?.status || (detail.status === 'quota' ? 'quota' : 'pending'),
      response:result?.response || result?.error || (detail.status === 'quota' ? detail.error : undefined)};
  });
}
export function responseExcerpt(text, limit = 160) {
  const compact = String(text || '').replace(/\s+/g, ' ').trim();
  return compact.length > limit ? compact.slice(0, limit) + '…' : compact;
}
export function logicStats(rows) {
  const answers = rows.map(firstVerdict).filter(value => ['pass','fail'].includes(value));
  const passed = answers.filter(value => value === 'pass').length;
  return {passed, samples:answers.length, rate:answers.length ? passed / answers.length : -1,
    recent:answers.at(-1) === 'pass' ? 1 : 0,
    reviewed:rows.filter(row => !isTwoPass(row) && row.first_status === 'fail' && row.status === 'pass').length};
}
export function showCandyMeme(rows) {
  const completed = [...rows].reverse().find(row => row.id && ['pass', 'fail'].includes(row.status));
  return completed?.status === 'fail';
}
export const CATEGORIES = [{id:'all',label:'全部',labelKey:'category.all'},
  {id:'oauth',label:'OAuth（GPT）',labelKey:'category.oauth'},
  {id:'apikey',label:'第三方 GPT',labelKey:'category.apikey'},
  {id:'grok',label:'Grok',labelKey:'category.grok'},
  {id:'gemini',label:'Gemini',labelKey:'category.gemini'},
  {id:'claude',label:'Claude',labelKey:'category.claude'}];

export function accountCategory(account) {
  const platform = account.platform || 'openai';
  if (platform === 'openai') return ['oauth', 'apikey'].includes(account.type) ? account.type : 'unknown';
  return {grok:'grok', gemini:'gemini', anthropic:'claude', claude:'claude'}[platform] || 'unknown';
}

export function drawingPreview(rows, selectedId = null) {
  const current = latest(rows);
  const selected = selectedId && rows.find(row => row.id === selectedId);
  const previous = [...rows].reverse().find(row => row.id && row.status === 'pass');
  const art = selected || previous || current;
  return {art, current, selected: Boolean(selected), previous: Boolean(!selected && art?.id && art.id !== current?.id)};
}

export function quality(account) {
  return logicStats(account.history.candy);
}

export const SORT_OPTIONS = [
  {id:'priority',label:'调用优先级',labelKey:'sort.priority',description:'Sub2API 实际调用优先级（数值越小越优先）',descriptionKey:'sort.priority.tip'},
  {id:'quality',label:'智力水平',labelKey:'sort.quality',description:'最近 3 轮通过率',descriptionKey:'sort.quality.tip'},
  {id:'stability',label:'稳定性',labelKey:'sort.stability',description:'最近 3 轮评测请求成功率',descriptionKey:'sort.stability.tip'},
  {id:'cost',label:'成本',labelKey:'sort.cost',description:'供货倍率，越低越优先',descriptionKey:'sort.cost.tip'},
];

export function nextSort(current, key) {
  return {key, direction:current.key === key && current.direction === 'desc' ? 'asc' : 'desc'};
}

function sortValue(account, key) {
  const metric=account.priority;
  const value = key === 'cost' ? account.price?.multiplier : metric?.[
    {priority:'actual_priority',quality:'quality_score',stability:'stability_score'}[key]];
  return Number.isFinite(value) ? value : null;
}

function compareAccounts(a, b, key, direction) {
  const left = sortValue(a,key), right = sortValue(b,key);
  // Unknown is not zero and stays last in both directions.
  if (left === null && right !== null) return 1;
  if (right === null && left !== null) return -1;
  // Sub2API's smaller numbers mean higher calling priority.
  const order = (direction === 'asc' ? 1 : -1) * (key === 'priority' ? -1 : 1);
  return (left === null ? 0 : (left-right)*order) ||
    a.name.localeCompare(b.name,'zh-CN',{numeric:true}) || a.id.localeCompare(b.id);
}

export function accountPage(accounts, {query = '', category = 'all', status = 'all', page = 1, sort = 'priority', direction = 'desc'} = {}) {
  const counts = Object.fromEntries(CATEGORIES.map(item => [item.id, item.id === 'all' ? accounts.length : 0]));
  accounts.forEach(account => {const group = accountCategory(account); if (Object.hasOwn(counts, group)) counts[group]++;});
  const filtered = accounts.filter(account => category === 'all' || accountCategory(account) === category)
    .filter(account => account.name.toLowerCase().includes(query.trim().toLowerCase()))
    .filter(account => status === 'all' || account.circuit?.active || ['candy', 'drawing'].some(kind => ['fail', 'error', 'quota'].includes(latest(account.history[kind])?.status)))
    .sort((a,b)=>compareAccounts(a,b,SORT_OPTIONS.some(option=>option.id===sort)?sort:'priority',direction));
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const current = Math.max(1, Math.min(page, pages));
  return {counts, total: filtered.length, pages, page: current, start: (current - 1) * PAGE_SIZE,
    accounts: filtered.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE)};
}
