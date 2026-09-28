// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import {tr} from './i18n-core.js';

export const PRICE_TIMEZONE = 'Asia/Shanghai';
export function timeMinute(value, end = false) {
  if (end && value === '24:00') return 1440;
  if (typeof value !== 'string' || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value)) throw Error(tr('rate.invalidTime'));
  const [hour, minute] = value.split(':').map(Number);
  return hour * 60 + minute;
}
const clock = minutes => `${String(Math.floor(minutes/60)).padStart(2,'0')}:${String(minutes%60).padStart(2,'0')}`;
const valid = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1000;
export function priceConfig(value) {
  if (value?.mode === 'fixed') {
    if (value.multiplier !== null && !valid(value.multiplier)) throw Error(tr('rate.invalidMultiplier'));
    return {mode:'fixed', multiplier:value.multiplier};
  }
  if (value?.mode !== 'scheduled' || value.timezone !== PRICE_TIMEZONE || !Array.isArray(value.periods) || !value.periods.length || value.periods.length > 24) throw Error(tr('rate.invalidPeriods'));
  let boundary = 0;
  const periods = value.periods.map(row => {
    const start = timeMinute(row.start), end = timeMinute(row.end, true);
    if (start !== boundary || end <= start) throw Error(tr('rate.overlap'));
    if (!valid(row.multiplier)) throw Error(tr('rate.segmentMultiplier'));
    boundary = end;
    return {start:row.start, end:row.end, multiplier:row.multiplier};
  });
  if (boundary !== 1440) throw Error(tr('rate.coverage'));
  return {mode:'scheduled', timezone:PRICE_TIMEZONE, periods};
}
export function splitPeriod(periods, index) {
  if (periods.length >= 24) return periods;
  const row = periods[index], start = timeMinute(row.start), end = timeMinute(row.end, true);
  if (end - start < 2) return periods;
  const middle = clock(Math.floor((start + end)/2));
  return [...periods.slice(0,index), {...row,end:middle}, {...row,start:middle}, ...periods.slice(index+1)];
}
export function removePeriod(periods, index) {
  if (periods.length <= 1) return periods;
  const next = periods.map(row => ({...row}));
  if (index) next[index-1].end = next[index].end;
  else next[1].start = '00:00';
  next.splice(index,1);
  return next;
}
