import React, {useEffect, useRef, useState} from 'react';
import {Clock3, LoaderCircle, Plus, Save, Trash2, X} from 'lucide-react';
import {PRICE_TIMEZONE, priceConfig, removePeriod, splitPeriod, timeMinute} from './price-config.js';
import {tr} from './i18n-core.js';

const numeric = value => value.trim() === '' ? null : Number(value);

export default function PriceEditor({account, onSave, onClose}) {
  const snapshot = useRef(account);
  const dialog = useRef(null), focus = useRef(document.activeElement);
  const initial = snapshot.current.price;
  const [mode,setMode] = useState(initial?.mode || 'fixed');
  const [fixed,setFixed] = useState(initial?.multiplier?.toString() ?? '');
  const [periods,setPeriods] = useState(() => (initial?.periods || [
    {start:'00:00',end:'12:00',multiplier:initial?.multiplier ?? null},
    {start:'12:00',end:'24:00',multiplier:initial?.multiplier ?? null},
  ]).map(row => ({...row,multiplier:row.multiplier?.toString() ?? ''})));
  const [busy,setBusy] = useState(false), [error,setError] = useState('');
  useEffect(() => {const node=dialog.current;node.showModal();return()=>{node.close();focus.current?.focus();};},[]);
  const close = () => {if (!busy) onClose();};
  let config, invalid = '';
  try {config=priceConfig(mode==='fixed'?{mode,multiplier:numeric(fixed)}:{mode,timezone:PRICE_TIMEZONE,periods:periods.map(row=>({...row,multiplier:numeric(row.multiplier)}))});}
  catch (issue) {invalid=issue.message;}
  const update = (index,key,value) => setPeriods(current => current.map((row,i) =>
    i===index?{...row,[key]:value}:key==='end'&&i===index+1?{...row,start:value}:row));
  const submit = async event => {
    event.preventDefault();if (!config || busy) return;
    setBusy(true);setError('');
    try {const result=await onSave(snapshot.current,config);if(result) onClose();else setError(tr('rate.unsaved'));}
    catch {setError(tr('rate.failed'));}
    finally {setBusy(false);}
  };
  return <dialog ref={dialog} className="rate-dialog" aria-labelledby="rate-title" onCancel={event=>{event.preventDefault();close();}} onClick={event=>{if(event.target===dialog.current)close();}}>
    <form onSubmit={submit}>
      <header className="rate-header"><div><h2 id="rate-title">{tr('rate.title')}</h2><p>{account.name}</p></div><button type="button" className="icon-button" aria-label={tr('rate.close')} title={tr('rate.close')} onClick={close} disabled={busy}><X size={18}/></button></header>
      <div className="rate-body"><fieldset className="rate-modes" disabled={busy}><legend className="sr-only">{tr('rate.mode')}</legend>{[['fixed','rate.mode.fixed'],['scheduled','rate.mode.scheduled']].map(([key,label])=><label key={key} className={mode===key?'selected':''}><input type="radio" name="rate-mode" value={key} checked={mode===key} onChange={()=>{setMode(key);setError('');}}/>{tr(label)}</label>)}</fieldset>
      {mode==='fixed'?<label className="rate-fixed">{tr('rate.fixedLabel')}<input autoFocus type="number" min="0" max="1000" step="any" placeholder={tr('rate.placeholder')} value={fixed} disabled={busy} onChange={e=>setFixed(e.target.value)}/></label>:<>
        <div className="rate-timezone"><Clock3 size={15}/>{tr('rate.timezone')}<span>{tr('rate.segments',{n:periods.length})}</span></div>
        <div className="rate-table" role="group" aria-label={tr('rate.group')}><div className="rate-table-head"><span>{tr('rate.start')}</span><span>{tr('rate.end')}</span><span>{tr('rate.multiplier')}</span><span/></div>{periods.map((row,index)=><div className="rate-row" key={index}>
          <span className="rate-start">{row.start || '--:--'}</span>
          {index===periods.length-1?<span className="rate-start">24:00</span>:<input type="time" aria-label={tr('rate.segmentEnd',{n:index+1})} value={row.end} disabled={busy} required onChange={e=>update(index,'end',e.target.value)}/>}
          <input type="number" aria-label={tr('rate.segmentRate',{n:index+1})} min="0" max="1000" step="any" required placeholder={tr('rate.multiplier')} value={row.multiplier} disabled={busy} onChange={e=>update(index,'multiplier',e.target.value)}/>
          <div className="rate-row-actions"><button type="button" className="icon-button" title={tr('rate.split',{n:index+1})} aria-label={tr('rate.split',{n:index+1})} disabled={busy||Boolean(invalid)||periods.length>=24||(!invalid&&timeMinute(row.end,true)-timeMinute(row.start)<2)} onClick={()=>setPeriods(splitPeriod(periods,index))}><Plus size={15}/></button><button type="button" className="icon-button" title={tr('rate.delete',{n:index+1})} aria-label={tr('rate.delete',{n:index+1})} disabled={busy||periods.length===1} onClick={()=>setPeriods(removePeriod(periods,index))}><Trash2 size={15}/></button></div>
        </div>)}</div></>}
        {(error||invalid)&&<p className="rate-error" role="alert">{error||invalid}</p>}
      </div><footer className="rate-footer"><button type="button" className="command" disabled={busy} onClick={close}>{tr('rate.cancel')}</button><button className="command rate-save" disabled={busy||!config}>{busy?<LoaderCircle size={16} className="spin"/>:<Save size={16}/>}{tr('rate.save')}</button></footer>
    </form>
  </dialog>;
}
