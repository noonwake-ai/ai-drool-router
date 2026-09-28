// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import {priceConfig} from './price-config.js';
import {tr} from './i18n-core.js';
import {pauseUrl, priceUrl} from './endpoints.js';

export const controlLabel = paused => tr(paused ? 'control.resume' : 'control.pause');
export const controlMessage = paused => tr(paused ? 'toast.paused' : 'toast.resumed');

export async function setMonitorPaused(account, fetcher = fetch) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetcher(pauseUrl(account.id), {
      method: 'POST', headers: {'Content-Type': 'application/json'}, signal: controller.signal,
      body: JSON.stringify({paused: !account.paused, expected_paused: Boolean(account.paused)}),
    });
    if (!response.ok) {
      const message = response.status === 409 ? tr('error.conflict') :
        response.status === 405 ? tr('error.controls') :
        tr('error.controlFailed', {action: controlLabel(account.paused)});
      throw Object.assign(new Error(message), {status: response.status});
    }
    const updated = await response.json();
    if (updated?.paused !== !account.paused || !Number.isFinite(updated.updated_at)) {
      throw new Error(tr('error.unconfirmed'));
    }
    return updated;
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) {
      throw new Error(tr('error.network'));
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export function mergeMonitorState(current, incoming) {
  if (!current) return incoming;
  const previous = new Map(current.accounts.map(account => [account.id, account]));
  return {...incoming, accounts: incoming.accounts.map(account => {
    const old = previous.get(account.id);
    const oldStamp=old?.price?.updated_at||0, newStamp=account.price?.updated_at||0;
    const price = oldStamp>newStamp || (oldStamp===newStamp && (old?.price?.evaluated_at||0)>(account.price?.evaluated_at||0)) ? old.price : account.price;
    if ((old?.updated_at || 0) <= (account.updated_at || 0)) return {...account,price};
    // A poll started before a successful write must not undo its acknowledged state.
    return {...account,price, paused: old.paused, updated_at: old.updated_at, resume_at: old.resume_at};
  })};
}

export async function setSupplierPrice(account,value,fetcher=fetch) {
  const config=priceConfig(value && typeof value==='object'?value:{mode:'fixed',multiplier:value});
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
  try {
    const response=await fetcher(priceUrl(account.id),{method:'POST',headers:{'Content-Type':'application/json'},
      signal:controller.signal,body:JSON.stringify({config,expected_revision:account.price?.revision??'none'})});
    if(!response.ok)throw Error(response.status===409?tr('error.priceChanged'):tr('error.priceNotSaved'));
    const result=await response.json();
    let confirmed=false;
    try {confirmed=JSON.stringify(priceConfig(result))===JSON.stringify(config);} catch { /* Unconfirmed server payload. */ }
    if(!confirmed||!Number.isFinite(result.updated_at)||!Number.isFinite(result.evaluated_at)||!/^[a-f0-9]{64}$/.test(result.revision??'')||
      (config.mode==='scheduled'&&(!Number.isInteger(result.active_period)||result.multiplier!==config.periods[result.active_period]?.multiplier)))throw Error(tr('error.priceUnconfirmed'));
    return result;
  } catch(error) {
    if(error.name==='AbortError'||error instanceof TypeError)throw Error(tr('error.priceNetwork'));
    throw error;
  } finally { clearTimeout(timer); }
}
