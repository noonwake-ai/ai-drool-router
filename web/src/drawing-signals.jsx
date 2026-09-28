// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import React from 'react';
import {Code2} from 'lucide-react';
import {tr} from './i18n-core.js';

const STATE_KEYS = {risk_terms:'signals.risk', positive_terms:'signals.positive',
  mixed:'signals.mixed', no_match:'signals.noMatch', no_preface:'signals.noPreface'};

function Words({value}) {
  const words = [...(value?.risk || []), ...(value?.positive || [])];
  return <span>{words.length ? words.join('、') : tr('signals.none')}</span>;
}

export default function DrawingSignals({signals, expanded=false}) {
  if (!signals) return <div className="drawing-signals legacy"><Code2 size={13}/><span>{tr('signals.notCollected')}</span></div>;
  return <details className={`drawing-signals ${signals.state}`} open={expanded || undefined}>
    <summary><Code2 size={13}/><span>{STATE_KEYS[signals.state]?tr(STATE_KEYS[signals.state]):tr('signals.pending')}</span><small>{tr('signals.legend')}</small></summary>
    <dl>
      <div><dt>{tr('signals.preface')}</dt><dd><Words value={signals.preface}/></dd></div>
      <div><dt>{tr('signals.descriptions')}</dt><dd><Words value={signals.descriptions}/></dd></div>
      <div><dt>{tr('signals.source')}</dt><dd><Words value={signals.source}/></dd></div>
    </dl>
    {signals.preface?.excerpt && <blockquote>{signals.preface.excerpt}{signals.preface.excerpt_truncated ? '…' : ''}</blockquote>}
    <p>{tr('signals.note')}</p>
    {signals.scan_truncated && <p>{tr('signals.truncated')}</p>}
    {signals.description_parse_error && <p>{tr('signals.parseError')}</p>}
  </details>;
}
