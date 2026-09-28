// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import {tr} from './i18n-core.js';

const NO_RESERVE = new Set(['NO_ELIGIBLE_GPT_RESERVE','NO_ELIGIBLE_PLATFORM_RESERVE']);
const REASON_KEYS = {
  NO_ELIGIBLE_GPT_RESERVE:'warning.reserve.none',
  NO_ELIGIBLE_PLATFORM_RESERVE:'warning.reserve.none',
  RESERVE_RESTORE_FAILED:'warning.reserve.failed',
  RESERVE_STATE_CHANGED:'warning.reserve.changed',
};

const PLATFORM_NAMES = {openai:'GPT',anthropic:'Claude',grok:'Grok',gemini:'Gemini'};

const reason = code => REASON_KEYS[code] ? tr(REASON_KEYS[code]) : code;

export function schedulerWarning(scheduler={},accounts=[],now=Date.now()/1000) {
  const errors=scheduler.quality_routing_errors;
  const entries=errors && typeof errors==='object' ? Object.entries(errors) : [];
  const messages=entries.filter(([platform,code])=>{
    // Pausing a whole category takes effect immediately, before the next worker
    // replaces its stored metadata. Never suppress real API/write failures.
    if(!NO_RESERVE.has(code))return Boolean(code);
    const matching=accounts.filter(a=>a.platform===platform);
    return !matching.length || matching.some(a=>!a.paused && !(a.resume_at>now));
  }).map(([platform,code])=>tr('warning.platform',{platform:PLATFORM_NAMES[platform]||platform.toUpperCase(),reason:reason(code)}));
  const legacy=scheduler.quality_routing_error;
  if(legacy && !entries.some(([,code])=>code===legacy)) {
    messages.push(tr('warning.generic',{reason:reason(legacy)}));
  }
  return messages.join('；');
}
