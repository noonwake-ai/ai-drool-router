// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import React, {useEffect, useState} from 'react';
import {Clock3, LoaderCircle, Save, SlidersHorizontal} from 'lucide-react';
import PriceEditor from './price-editor.jsx';
import {tr} from './i18n-core.js';

const number = value => Number.isFinite(value) ? value.toFixed(1) : '--';
const STATUS_KEYS = {applied:'priority.status.applied', ready:'priority.status.ready',
  needs_price:'priority.status.needsPrice', needs_group_prices:'priority.status.needsPrices',
  fallback:'priority.status.fallback', insufficient_traffic:'priority.status.traffic',
  insufficient_quality:'priority.status.quality', paused:'priority.status.paused',
  quota:'priority.status.quota', excluded:'priority.status.excluded',
  price_changed:'priority.status.priceChanged', error:'priority.status.error'};

export default function PriorityView({account, enabled, onPrice}) {
  const [value,setValue] = useState(account.price?.multiplier?.toString() ?? '');
  const [busy,setBusy] = useState(false);
  const [editing,setEditing] = useState(false);
  const scheduled = account.price?.mode === 'scheduled';
  const activePeriod = account.price?.periods?.[account.price?.active_period];
  useEffect(()=>{setValue(account.price?.multiplier?.toString() ?? '');},[account.price?.multiplier]);
  const metric = account.priority;
  const stale = metric && Date.now()/1000-metric.updated_at>1800;
  const changed = (account.price?.multiplier ?? null) !== (value.trim()===''?null:Number(value));
  const pending = metric && (account.price?.multiplier ?? null)!==metric.multiplier;
  const submit = async event => {
    event.preventDefault();
    setBusy(true);
    try { await onPrice(account,value.trim()===''?null:Number(value)); }
    finally { setBusy(false); }
  };
  return <section className="supplier-priority" aria-label={tr('priority.aria',{name:account.name})}>
    <div className="priority-topline"><form className="supplier-price" onSubmit={submit}>
      <label>{scheduled?tr('priority.multiplierCurrent'):tr('priority.multiplier')}<input type="number" aria-label={tr('priority.multiplierAria',{name:account.name})} min="0" max="1000" step="any" inputMode="decimal" placeholder={tr('priority.placeholder')} value={value} onChange={e=>setValue(e.target.value)} readOnly={scheduled} disabled={!enabled||busy}/></label>
      {!scheduled&&<button className="icon-button" title={tr('priority.saveMultiplier')} aria-label={tr('priority.saveMultiplier')} disabled={!enabled||busy||!changed}>{busy?<LoaderCircle size={15} className="spin"/>:<Save size={15}/>}</button>}
      <button type="button" className="icon-button" title={tr('priority.settings',{name:account.name})} aria-label={tr('priority.settings',{name:account.name})} disabled={!enabled||busy} onClick={()=>setEditing(true)}><SlidersHorizontal size={15}/></button>
    </form><div className="priority-total"><span>{tr('priority.score')} <b>{number(metric?.score)}</b></span><span title={tr('priority.priorityTitle')}>{tr('priority.priority')} <b>{metric?.actual_priority ?? '--'}</b></span></div></div>
    <div className="rate-active-period">{scheduled?<><Clock3 size={12}/>{tr('priority.scheduled',{start:activePeriod?.start,end:activePeriod?.end})}</>:tr('priority.fixed')}</div>
    <div className="priority-factors">
      <span title={tr('sort.quality.tip')}>{tr('priority.factor.intelligence')} 36% <b>{number(metric?.quality_score)}</b><small>{tr('priority.qualityDetail',{rounds:metric?.quality_rounds ?? 0,passed:metric?.quality_pass ?? 0,total:(metric?.quality_pass ?? 0)+(metric?.quality_fail ?? 0)})}</small></span>
      <span title={tr('sort.cost.tip')}>{tr('priority.factor.cost')} 36% <b>{number(metric?.cost_score)}</b></span>
      <span>{tr('priority.factor.stability')} 18% <b>{number(metric?.stability_score)}</b></span>
      <span title={tr('priority.speedTitle')}>{tr('priority.factor.speed')} 10% <b>{number(metric?.speed_score)}</b><small>{Number.isFinite(metric?.speed_score)?tr('priority.speedDetail',{rounds:metric?.speed_rounds ?? 0,samples:metric?.speed_samples ?? 0}):tr('priority.speedNeutral')}</small></span>
    </div>
    <div className="priority-speed" title={tr('priority.speedTitle')}>{tr('priority.firstBody')} <b>{Number.isFinite(metric?.speed_first_text_seconds)?`${number(metric.speed_first_text_seconds)} s`:tr('priority.pending')}</b><span>{tr('priority.endToEnd')} <b>{Number.isFinite(metric?.speed_tokens_per_second)?`${number(metric.speed_tokens_per_second)} tok/s`:tr('priority.pending')}</b></span>{metric?.speed_partial&&<small>{tr('priority.partial')}</small>}</div>
    <div className="priority-availability"><span>{tr('priority.availability')} <b>{number(metric?.availability)}%</b><small>{metric?tr('priority.stabilityDetail',{rounds:metric.stability_rounds ?? 0,ok:metric.successful_requests,fail:metric.failed_requests}):tr('priority.stabilityFallback')}</small></span><span className={`priority-sync ${stale||metric?.status==='error'?'warning':''}`}>{stale?tr('priority.sync.stale'):pending?tr('priority.sync.pending'):STATUS_KEYS[metric?.status]?tr(STATUS_KEYS[metric.status]):tr('priority.status.waiting')}</span></div>
    {editing&&<PriceEditor account={account} onSave={onPrice} onClose={()=>setEditing(false)}/>}
  </section>;
}
