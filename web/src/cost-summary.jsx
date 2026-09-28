// SPDX-FileCopyrightText: 2026 NoonWake.AI
// SPDX-License-Identifier: LGPL-3.0-or-later

import React from 'react';
import {ChevronDown,CircleDollarSign} from 'lucide-react';
import {costRows,formatCost} from './cost-view.js';
import {tr} from './i18n-core.js';

const timestamp = value => new Date(value*1000).toLocaleString(
  document.documentElement.lang === 'en' ? 'en-US' : 'zh-CN',
  {timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});

function Amount({item}) {
  return <td><span className="cost-amount">{formatCost(item?.amount_usd)}</span>
    {item?.unpriced_requests>0&&<small className="cost-incomplete">{tr('cost.unpriced',{n:item.unpriced_requests})}</small>}</td>;
}

export default function CostSummary({costs}) {
  const windows=costs?.windows&&['24h','30d'].map(key=>costs.windows[key]);
  return <details className="cost-summary">
    <summary className="cost-toggle" aria-label={tr('cost.heading')} aria-controls="cost-content"><span><CircleDollarSign size={18} aria-hidden="true"/><span>{tr('cost.heading')}</span><small>{tr('cost.estimate')}</small></span><ChevronDown className="cost-chevron" size={17} aria-hidden="true"/></summary>
    <div id="cost-content" className="cost-content">{!windows?<p>{tr('cost.unavailable')}</p>:<><table className="cost-table"><thead><tr><th scope="col">{tr('cost.model')}</th><th scope="col">{tr('cost.window24h')}</th><th scope="col">{tr('cost.window30d')}</th></tr></thead>
      <tbody>{costRows(costs).map(row=><tr key={row.id}><th scope="row"><i className={`cost-dot ${row.id}`}/>{row.label}</th>{row.windows.map((item,i)=><Amount key={i} item={item}/>)}</tr>)}</tbody>
      <tfoot><tr><th scope="row">{tr('cost.total')}</th>{windows.map((item,i)=><Amount key={i} item={item}/>)}</tr></tfoot></table>
    <div className="cost-notes"><span>{tr('cost.coverage',{time:windows[1]?.coverage_from?timestamp(windows[1].coverage_from):tr('cost.noRecords')})}</span>
      <details><summary>{tr('cost.basis')}</summary><p>{tr('cost.note')}</p>
        {costs.price_errors?.length>0&&<p className="cost-incomplete">{tr('cost.priceError')}</p>}</details></div></>}</div>
  </details>;
}
