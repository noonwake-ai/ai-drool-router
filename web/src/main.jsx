import React, {memo, useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, ArrowDown, ArrowLeft, ArrowRight, ArrowUp, ArrowUpDown, CheckCircle2, ChevronDown, Clock3, Code2, Droplets, Expand, ExternalLink, FileText, Gauge, Languages, LoaderCircle, Palette, Pause, Play, RefreshCw, Search, ShieldCheck, TriangleAlert, X, XCircle} from 'lucide-react';
import {accountCategory, accountPage, answerText, candyAnswers, CATEGORIES, drawingPreview, latest, logicStats, nextSort, questionResult, responseExcerpt, resultLabel, showCandyMeme, SORT_OPTIONS, stageLabel} from './view-model.js';
import {controlLabel, controlMessage, mergeMonitorState, setMonitorPaused, setSupplierPrice} from './control-client.js';
import {readRun} from './run-client.js';
import {LANGUAGES, LanguageProvider, tr, useI18n} from './i18n.js';
import droolingMeme from './assets/drooling-meme.jpg';
import './style.css';
import CostSummary from './cost-summary.jsx';
import PriorityView from './priority-view.jsx';
import DrawingSignals from './drawing-signals.jsx';
import {schedulerWarning} from './scheduler-warning.js';

const FALLBACK_TITLE = 'AI 流口水检测';
const REPO_URL = 'https://github.com/noonwake-ai/ai-drool-detector';
const statusText = status => tr('status.' + (status || 'empty'));
const locale = () => document.documentElement.lang === 'en' ? 'en-US' : 'zh-CN';
const date = (value, options={}) => new Date(value*1000).toLocaleString(locale(),{timeZone:'Asia/Shanghai',hour12:false,...options});
const clock = value => date(value,{hour:'2-digit',minute:'2-digit'});
const duration = value => {if(value==null)return '--';const seconds=Math.round(value);return seconds<60?`${seconds}s`:`${Math.floor(seconds/60)}m ${seconds%60}s`;};
const count = rows => ({pass:rows.filter(r=>r.status==='pass').length,fail:rows.filter(r=>r.status==='fail').length,error:rows.filter(r=>r.status==='error').length,quota:rows.filter(r=>r.status==='quota').length,active:rows.filter(r=>['running','pending'].includes(r.status)).length});

function Status({status,kind,row}) {
  const Icon = status==='pass'?CheckCircle2:status==='fail'?XCircle:status==='error'?TriangleAlert:status==='running'||status==='pending'?LoaderCircle:Clock3;
  return <span className={`status ${status}`}><Icon size={14} className={status==='running'?'spin':''}/>{resultLabel(row)||(kind==='drawing'&&status==='pass'?tr('status.generated'):statusText(status))}</span>;
}

function IconButton({title,children,...props}) {return <button className="icon-button" title={title} aria-label={title} {...props}>{children}</button>;}

function Toast({toast,onClose}) {
  useEffect(()=>{const timer=setTimeout(onClose,4500);return()=>clearTimeout(timer);},[toast.id,onClose]);
  return <div className={`control-toast ${toast.kind}`} role={toast.kind==='error'?'alert':'status'} aria-atomic="true">{toast.kind==='error'?<TriangleAlert size={18}/>:<CheckCircle2 size={18}/>}<span>{toast.message}</span><IconButton title={tr('modal.close')} onClick={onClose}><X size={15}/></IconButton></div>;
}

function ClientLabel({client}) {
  if (client?.codex_protocol_version) return <>Codex {client.codex_protocol_version}</>;
  if (client?.grok_protocol_version) return <>Grok {client.grok_protocol_version}</>;
  return <>{client?.protocol||tr('client.unknown')}</>;
}

function CompatibilityNote({error}) {
  if (/requires a newer version of Codex/i.test(error||'')) return <p className="compatibility-note">{tr('compat.clientVersion')}</p>;
  if (/响应超过 2 MB 安全上限/.test(error||'')) return <p className="compatibility-note">{tr('compat.streamLimit')}</p>;
  return null;
}

function Timeline({rows,selected,onSelect,kind}) {
  return <div className="timeline-wrap"><div className="timeline" style={{gridTemplateColumns:`repeat(${rows.length},minmax(0,1fr))`}} aria-label={tr(kind==='candy'?'timeline.candy':'timeline.drawing')}>{rows.map((r,i)=><button key={r.slot} className={`cell ${r.status} ${r.id&&selected===r.id?'selected':''}`} aria-pressed={Boolean(r.id&&selected===r.id)} disabled={!r.id} onClick={()=>onSelect(r)} title={`${date(r.slot,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})} · ${resultLabel(r)||statusText(r.status)}`} aria-label={`${clock(r.slot)} ${resultLabel(r)||statusText(r.status)}`}><span className="sr-only">{i+1}</span></button>)}</div><div className="timeline-axis"><span>{date(rows[0].slot,{month:'2-digit',day:'2-digit'})} {clock(rows[0].slot)}</span><span>{tr('timeline.now')}</span></div></div>;
}

function RoutingStatus({routing,circuit}) {
  const keys={protected:'routing.protected',blocked:'routing.blocked',circuit_open:'routing.circuitOpen',restored:'routing.restored',enabled:'routing.enabled',quota:'routing.quota',error:'routing.error'};
  if(!keys[routing?.action])return null;
  return <div className={`routing-note ${routing.action}`} title={routing.error_code||undefined}>{routing.action==='error'?<TriangleAlert size={15}/>:<Activity size={15}/>}<span>{tr(keys[routing.action])}{routing.action==='protected'&&circuit?.active?tr('routing.retest'):''}</span></div>;
}

function TestRow({kind,rows,selected,onSelect}) {
  const stats=count(rows), current=latest(rows), completed=stats.pass+stats.fail;
  const logic=logicStats(rows), samples=kind==='candy'?logic.samples:completed, passed=kind==='candy'?logic.passed:stats.pass;
  const rate=samples?Math.round(passed/samples*1000)/10:null;
  const times=rows.filter(r=>r.duration!=null).map(r=>r.duration);
  return <section className="test-row"><div className="test-heading"><h3>{kind==='candy'?<Activity size={17}/>:<Palette size={17}/>} {tr(kind==='candy'?'kind.candy':'kind.drawing')}<span>{kind==='candy'?tr('verdict.double'):tr('prompt.drawing')}</span></h3>{current&&<Status status={current.status} kind={kind} row={current}/>}</div>
    <div className="test-stats"><strong className={rate==null?'muted':rate===100?'good':'warn'}>{rate==null?'--':rate+'%'}</strong><span>{passed}/{samples} {tr(kind==='candy'?'metric.firstPass':'metric.generated')}</span>{kind==='candy'&&logic.reviewed>0&&<span className="warn">{tr('metric.legacyReviews',{n:logic.reviewed})}</span>}{stats.error>0&&<span className="warn">{tr('metric.errors',{n:stats.error})}</span>}{stats.quota>0&&<span className="quota-text">{tr('metric.quota',{n:stats.quota})}</span>}{stats.active>0&&<span className="running-text">{tr('metric.pending',{n:stats.active})}</span>}<span className="average">{tr('metric.average',{value:duration(times.length?times.reduce((a,b)=>a+b,0)/times.length:null)})}</span></div>
    <Timeline rows={rows} kind={kind} selected={selected} onSelect={onSelect}/>
  </section>;
}

function ArtFrame({id,large=false,title}) {
  return <div className={large?'large-frame':'frame-window'}><iframe title={title||tr('art.frame')} src={`/artifacts/${id}.html`} sandbox="allow-scripts" referrerPolicy="no-referrer" loading="lazy" tabIndex={large?0:-1}/></div>;
}

function CandyPreview({row,onDetail,onDrawing}) {
  const [detail,setDetail]=useState(null),[error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    setDetail(null);setError('');
    readRun(row.id,controller.signal).then(result=>{if(!controller.signal.aborted)setDetail(result);})
      .catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return()=>controller.abort();
  },[row.id,row.status,row.phase,row.attempts]);
  return <section className="candy-preview" aria-label={tr('art.selectedAnswer')} aria-busy={!detail&&!error}>
    <div className="preview-heading"><span>{tr('art.selectedAnswer')}</span><span>{clock(row.slot)} · {duration(row.duration)}</span></div>
    <div className="candy-verdict"><Status status={row.status} kind="candy" row={row}/><span>{tr('modal.requests',{n:detail?.attempts??row.attempts??0})}</span></div>
    {error?<p className="candy-load-error" role="status">{error}</p>:!detail?<p className="candy-loading"><LoaderCircle size={15} className="spin"/>{tr('modal.loadingAnswer')}</p>:<>
      <div className="candy-answer-list">{candyAnswers(detail).map((answer,index)=><div className="candy-answer" key={index}>
        <div className="candy-answer-label"><span>{answer.label}</span><Status status={answer.status==='unknown'?'error':answer.status}/></div>
        <div className="candy-answer-values"><strong>{answerText(answer.answer)}</strong><span>{tr('modal.expected',{value:answerText(answer.expected)})}</span></div>
        {answer.response&&<p className="candy-excerpt">{responseExcerpt(answer.response)}</p>}
      </div>)}</div>
      {detail.error&&<p className="candy-request-error"><b>{detail.error_code}</b>{responseExcerpt(detail.error)}</p>}
    </>}
    <div className="preview-footer"><button className="text-button" onClick={onDrawing}><Palette size={13}/>{tr('art.backToArtwork')}</button><button className="text-button" onClick={()=>onDetail(row.id,'detail')}><FileText size={13}/>{tr('modal.record')}</button></div>
  </section>;
}

const Account=memo(function Account({account,onDetail,controlsEnabled,onControl,controlling,onPrice}) {
  const [selected,setSelected]=useState(null);
  const drawings=account.history.drawing;
  const preview=drawingPreview(drawings,selected?.kind==='drawing'?selected.id:null), art=preview.art;
  const candy=selected?.kind==='candy'&&account.history.candy.find(row=>row.id===selected.id);
  const c=latest(account.history.candy), d=latest(drawings);
  useEffect(()=>{setSelected(current=>current?.kind==='drawing'?null:current);},[d?.id,d?.status]);
  const state=account.paused?'paused':account.configured===false?'unconfigured':[c,d].some(r=>r?.status==='error')?'error':[c,d].some(r=>r?.status==='fail')?'fail':[c,d].some(r=>r?.status==='quota')?'quota':[c,d].some(r=>['running','pending'].includes(r?.status))?'running':c?.status==='pass'&&d?.status==='pass'?'pass':'empty';
  return <article className={`account ${state}`} data-account-id={account.id}><header className="account-header"><div className="account-title"><span className={`account-indicator ${state}`}/><h2>{account.name}</h2><span className="account-kind">{(()=>{const group=CATEGORIES.find(item=>item.id===accountCategory(account));return group?tr(group.labelKey):tr('account.typeUnsynced');})()}</span><span className="model-name">{account.model||tr('account.modelUnset')}</span>{account.effort&&<span className="effort">{account.effort.toUpperCase()}</span>}</div><div className="account-actions"><div className="account-action-buttons"><Status status={state}/><button className={`account-control ${account.paused?'start':''}`} title={controlsEnabled?controlLabel(account.paused):tr('account.controlDisabled')} aria-label={controlLabel(account.paused)} aria-busy={Boolean(controlling)} disabled={!controlsEnabled||controlling} onClick={()=>onControl(account)}>{controlling?<LoaderCircle size={15} className="spin"/>:account.paused?<Play size={15}/>:<Pause size={15}/>}<span>{controlLabel(account.paused)}</span></button></div>{showCandyMeme(account.history.candy)&&<img className="candy-stamp" src={droolingMeme} width="60" height="60" alt={tr('meme.alt')} title={tr('meme.alt')} loading="lazy" decoding="async"/>}</div></header>
    <RoutingStatus routing={account.routing} circuit={account.circuit}/><PriorityView account={account} enabled={controlsEnabled} onPrice={onPrice}/><div className="account-body"><div className="test-column"><TestRow kind="candy" rows={account.history.candy} selected={candy?.id} onSelect={r=>setSelected({kind:'candy',id:r.id})}/><TestRow kind="drawing" rows={drawings} selected={candy?null:art?.id} onSelect={r=>setSelected({kind:'drawing',id:r.id})}/></div>
      <div className="preview-column">{candy?<CandyPreview key={candy.id} row={candy} onDetail={onDetail} onDrawing={()=>setSelected(null)}/>:<><div className="preview-heading"><span>{tr(preview.selected?'art.selected':preview.previous?'art.previous':'art.latest')}</span><span>{art?`${clock(art.slot)} · ${duration(art.duration)}`:tr('art.none')}</span></div>
        {art?.status==='pass'?<button className="preview-button" onClick={()=>onDetail(art.id,'preview')} title={tr('art.expand')}><ArtFrame id={art.id}/><span className="expand-label"><Expand size={16}/>{tr('art.expand')}</span></button>:<button className={`preview-empty ${art?.status||''}`} disabled={!art?.id} onClick={()=>onDetail(art.id,'detail')}>{art?.status==='error'?<TriangleAlert size={26}/>:['running','pending'].includes(art?.status)?<LoaderCircle size={26} className={art?.status==='running'?'spin':''}/>:<Palette size={26}/>}<strong>{art?statusText(art.status):tr('art.empty')}</strong><span>{art?.error||(art?.status==='running'?tr('art.running'):art?.status==='pending'?tr('art.pending'):art?.status==='quota'?tr('art.quota'):tr('art.noneRound'))}</span></button>}
        {preview.previous&&d&&<button className="preview-current" onClick={()=>onDetail(d.id,'detail')}><span>{tr('art.round')}</span><Status status={d.status} kind="drawing"/>{d.status==='error'&&<span className="preview-error-code">{d.error_code}</span>}</button>}
        <div className="preview-footer"><span>HTML + SVG</span>{art?.id&&<button className="text-button" onClick={()=>onDetail(art.id,'detail')}><FileText size={13}/>{tr('modal.record')}</button>}</div>
        {art?.status==='pass'&&<DrawingSignals key={art.id} signals={art.drawing_signals}/>}
      </>}</div></div></article>;
});

function RequestContext({detail}) {
  const requests=(detail.attempt_log||[]).filter(a=>a.request_id);
  if(!requests.length)return null;
  return <details className="request-context"><summary>{tr('modal.freshRequests',{n:requests.length})}</summary><p>{tr('modal.freshBody')}</p>{requests.map(a=><p key={a.request_id}>{tr('modal.attempt',{n:a.attempt})} · <code>{a.request_id}</code></p>)}</details>;
}

function QuestionEvidence({detail,rawOnly=false}) {
  if (detail.kind==='drawing'&&!rawOnly) return <><RequestContext detail={detail}/><h3>{tr('modal.sourceSignals')}</h3><DrawingSignals signals={detail.drawing_signals} expanded/><h3>{tr('modal.sentPrompt')}</h3><p className="prompt-text">{detail.prompt}</p><details className="request-context"><summary>{tr('modal.outputContract')}</summary><p>{detail.instructions}</p></details></>;
  if (!detail.questions?.length) return rawOnly?<pre>{detail.response||detail.error||tr('modal.noAnswer')}</pre>:<><RequestContext detail={detail}/><h3>{tr('modal.sentPrompt')}</h3><p className="prompt-text">{detail.prompt}</p>{detail.kind==='candy'&&<><p>{tr('modal.answerLine',{expected:answerText(detail.expected_answer),answer:answerText(detail.answer)})}</p><pre>{detail.response||tr('modal.noAnswer')}</pre></>}</>;
  return detail.questions.map((q,index)=>{
    const result=questionResult(detail,index);
    if(index>=candyAnswers(detail).length)return null;
    return <section className="question-evidence" key={`${q.id}-${index}`}>
      {index===0&&!rawOnly&&<RequestContext detail={detail}/>}
      <h3>{stageLabel(detail,index)} {result&&<Status status={result.status==='unknown'?'error':result.status}/>}</h3>
      {!rawOnly&&<><p className="prompt-text">{q.prompt}</p><p className="question-answer">{tr('modal.expected',{value:answerText(q.expected)})}<span>{tr('modal.modelAnswered',{value:answerText(result?.answer)})}</span></p><details><summary>{tr('modal.questionContract')}</summary><p>{q.instructions}</p><p className="footnote">{q.source}</p><p className="footnote">{tr('modal.bankVersion',{version:q.version,id:q.id})}</p></details></>}
      <pre>{result?.response||result?.error||tr('modal.waiting')}</pre>
    </section>;
  });
}

function Modal({selection,onClose,data}) {
  const dialog=useRef(null), returnFocus=useRef(document.activeElement);
  const [detail,setDetail]=useState(null),[error,setError]=useState(''),[tab,setTab]=useState(selection.tab||'detail');
  useEffect(()=>{dialog.current?.showModal();return()=>{dialog.current?.close();returnFocus.current?.focus();};},[]);
  useEffect(()=>{if(selection.type==='prompts')return;const ctl=new AbortController();readRun(selection.id,ctl.signal).then(result=>{if(!ctl.signal.aborted)setDetail(result);}).catch(e=>{if(!ctl.signal.aborted)setError(e.message);});return()=>ctl.abort();},[selection]);
  const title=selection.type==='prompts'?tr('modal.rules'):detail?.name||tr('modal.record');
  return <dialog ref={dialog} className={tab==='preview'?'dialog fullscreen':'dialog'} onCancel={e=>{e.preventDefault();onClose();}} onClick={e=>{if(e.target===dialog.current)onClose();}}><div className="dialog-shell"><header className="dialog-header"><div><h2>{title}</h2>{detail&&<span>{tr(detail.kind==='candy'?'kind.candy':'kind.drawing')} · {date(detail.slot,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})} · {detail.model} {detail.effort||''}</span>}</div><IconButton title={tr('modal.close')} onClick={onClose}><X size={20}/></IconButton></header>
      {selection.type==='prompts'?<div className="dialog-content">
        <h3>{tr('rules.candy.heading')}</h3><p>{tr('rules.candy.body')}</p>
        <p>{tr('rules.candy.judge')}</p>
        <h3>{tr('rules.candy.title')} <span className="good">{tr('rules.candy.answer')}</span></h3><p className="prompt-text">{data.prompts.candy}</p><h3>{tr('rules.drawing.heading')}</h3><p className="prompt-text">{data.prompts.drawing}</p>
        <h3>{tr('rules.pipeline.heading')}</h3><p>{data.method||tr('detail.transport')}</p><p>{tr('rules.schedule')}</p>
        <p>{tr('rules.scoring')}</p>
        <p>{tr('rules.circuit')}</p>
        <p>{tr('rules.recovery')}</p>
        <p>{tr('rules.pause')}</p>
      </div>:error?<div className="dialog-content error-text">{error}</div>:!detail?<div className="loading"><LoaderCircle className="spin"/>{tr('modal.loading')}</div>:<>
      <div className="tabs" role="tablist"><button role="tab" aria-selected={tab==='detail'} className={tab==='detail'?'active':''} onClick={()=>setTab('detail')}><FileText size={15}/>{tr('modal.tab.detail')}</button>{detail.artifact_url&&<button role="tab" aria-selected={tab==='preview'} className={tab==='preview'?'active':''} onClick={()=>setTab('preview')}><Expand size={15}/>{tr('modal.tab.artwork')}</button>}<button role="tab" aria-selected={tab==='source'} className={tab==='source'?'active':''} onClick={()=>setTab('source')}><Code2 size={15}/>{tr('modal.tab.source')}</button></div>
      {tab==='preview'&&detail.artifact_url?<ArtFrame id={detail.id} large title={`${detail.name} · ${tr('kind.drawing')}`}/>:tab==='source'?<div className="dialog-content"><QuestionEvidence detail={detail} rawOnly/></div>:<div className="dialog-content"><div className="result-meta"><Status status={detail.status} kind={detail.kind} row={detail}/><span>{tr('modal.elapsed',{value:duration(detail.duration)})}</span><span>{tr('modal.requests',{n:detail.attempts})}</span><span>{tr('detail.retry')}</span></div><p className="client-meta">{tr('detail.transport')} · <ClientLabel client={detail.request_client}/>{detail.kind==='candy'&&!detail.bank_version?tr('detail.legacy'):''}</p>{detail.error&&<div className="error-message"><TriangleAlert size={17}/><div><b>{detail.error_code}</b><p>{detail.error}</p><CompatibilityNote error={detail.error}/></div></div>}<QuestionEvidence detail={detail}/>{detail.attempt_log?.length>0&&<><h3>{tr('modal.requestLog')}</h3><div className="attempts">{detail.attempt_log.map(a=><div key={a.attempt}><span>{a.stage==null?tr('modal.request'):stageLabel(detail,a.stage)} · {tr('modal.attempt',{n:a.stage_attempt||a.attempt})}</span><Status status={a.status==='unknown'?'error':a.status} kind={detail.kind}/><span>{duration(a.seconds)}</span>{a.client&&<span><ClientLabel client={a.client}/></span>}{a.message&&<p>{a.message}</p>}</div>)}</div></>}{detail.actual_model&&<p className="footnote">{tr('modal.upstreamModel',{model:detail.actual_model})}</p>}</div>}</>}
    </div></dialog>;
}

function LanguageSwitch() {
  const {language,setLanguage}=useI18n();
  return <div className="lang-switch" role="group" aria-label={tr('app.language')}><Languages size={15} aria-hidden="true"/>{LANGUAGES.map(item=><button key={item.id} type="button" className={language===item.id?'active':''} aria-pressed={language===item.id} title={item.label} onClick={()=>setLanguage(item.id)}>{item.short}</button>)}</div>;
}

function Hero() {
  return <section className="hero">
    <div className="hero-copy">
      <p className="hero-eyebrow">{tr('app.subtitle')}</p>
      <h2 className="hero-slogan">{tr('app.slogan')}</h2>
      <p className="hero-body">{tr('app.hero.body')}</p>
      <ul className="hero-points">
        <li><Gauge size={15}/><span>{tr('app.hero.point.scope')}</span></li>
        <li><ShieldCheck size={15}/><span>{tr('app.hero.point.probe')}</span></li>
        <li><Activity size={15}/><span>{tr('app.hero.point.control')}</span></li>
      </ul>
      <div className="hero-links">
        <a className="command" href={REPO_URL} target="_blank" rel="noreferrer noopener"><ExternalLink size={15}/>{tr('app.hero.source')}</a>
        <a className="command ghost" href={`${REPO_URL}#deploy`} target="_blank" rel="noreferrer noopener">{tr('app.hero.deploy')}</a>
      </div>
    </div>
    <div className="hero-art" aria-hidden="true"><Droplets size={120}/></div>
  </section>;
}

function App() {
  const [data,setData]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false),[selection,setSelection]=useState(null),[query,setQuery]=useState(''),[filter,setFilter]=useState('all'),[category,setCategory]=useState('all'),[page,setPage]=useState(1),[tick,setTick]=useState(Date.now());
  const [sort,setSort]=useState({key:'priority',direction:'desc'});
  const mounted=useRef(true),pending=useRef(null);
  const [toast,setToast]=useState(null),[busyControls,setBusyControls]=useState({});
  const controlRequests=useRef(new Set()),toastSerial=useRef(0);
  const closeToast=useCallback(()=>setToast(null),[]);
  const openDetail=useCallback((id,tab)=>setSelection({id,tab}),[]);
  const refresh=useCallback(async()=>{if(pending.current)return;const ctl=new AbortController();pending.current=ctl;setLoading(true);try{const r=await fetch('/api/state',{signal:ctl.signal,cache:'no-store'});if(!r.ok)throw Error(tr('error.unavailable'));const result=await r.json();if(mounted.current){setData(current=>mergeMonitorState(current,result));setError('');}}catch(e){if(mounted.current&&e.name!=='AbortError')setError(tr('error.stale'));}finally{pending.current=null;if(mounted.current)setLoading(false);}},[]);
  useEffect(()=>{mounted.current=true;refresh();const a=setInterval(()=>{if(!document.hidden)refresh();},30000);const b=setInterval(()=>setTick(Date.now()),1000);return()=>{mounted.current=false;clearInterval(a);clearInterval(b);pending.current?.abort();};},[refresh]);
  const control=useCallback(async account=>{
    if(controlRequests.current.has(account.id))return;
    controlRequests.current.add(account.id);setBusyControls(current=>({...current,[account.id]:true}));
    try{
      const updated=await setMonitorPaused(account);
      if(mounted.current){setData(current=>({...current,accounts:current.accounts.map(a=>a.id===account.id?{...a,...updated}:a)}));setToast({id:++toastSerial.current,kind:'success',message:controlMessage(updated.paused)});}
    }catch(e){if(mounted.current){setToast({id:++toastSerial.current,kind:'error',message:e.message});refresh();}}
    finally{controlRequests.current.delete(account.id);if(mounted.current)setBusyControls(current=>({...current,[account.id]:false}));}
  },[refresh]);
  const savePrice=useCallback(async(account,multiplier)=>{
    try {
      const updated=await setSupplierPrice(account,multiplier);
      if(mounted.current){setData(current=>({...current,accounts:current.accounts.map(a=>a.id===account.id?{...a,price:updated}:a)}));setToast({id:++toastSerial.current,kind:'success',message:tr('toast.priceSaved')});}
      return updated;
    } catch(error) {if(mounted.current){setToast({id:++toastSerial.current,kind:'error',message:error.message});refresh();}}
  },[refresh]);
  if(!data)return <main className="boot"><Droplets size={36}/><h1>{FALLBACK_TITLE}</h1>{error?<><p>{error}</p><button className="command" onClick={refresh}><RefreshCw size={16}/>{tr('app.reconnect')}</button></>:<p><LoaderCircle size={17} className="spin"/>{tr('app.loading')}</p>}</main>;
  const all=data.accounts.flatMap(a=>a.history.candy),stats=logicStats(all),rate=stats.samples?Math.round(stats.rate*1000)/10:null;
  const errorAccounts=data.accounts.filter(a=>a.circuit?.active||['candy','drawing'].some(k=>['fail','error'].includes(latest(a.history[k])?.status))).length;
  const elapsed=Math.max(0,Math.floor((data.next_run_at*1000-tick)/1000));
  const stale=tick/1000-(data.scheduler.last_account_sync||data.generated_at)>6300;
  const progress=Math.max(0,Math.min(100,100-elapsed/(data.interval_seconds||2700)*100));
  const view=accountPage(data.accounts,{query,category,status:filter,page,sort:sort.key,direction:sort.direction});
  const changeCategory=id=>{setCategory(id);setPage(1);};
  const pager=<div className="pagination"><span>{tr('page.summary',{start:view.total?view.start+1:0,end:view.start+view.accounts.length,total:view.total})}</span><div><IconButton title={tr('page.prev')} disabled={view.page<=1} onClick={()=>setPage(view.page-1)}><ArrowLeft size={16}/></IconButton><span className="page-count">{tr('page.label',{page:view.page,pages:view.pages})}</span><IconButton title={tr('page.next')} disabled={view.page>=view.pages} onClick={()=>setPage(view.page+1)}><ArrowRight size={16}/></IconButton></div></div>;
  const platforms=[...new Set(data.accounts.map(a=>({openai:'GPT',anthropic:'Claude',gemini:'Gemini',grok:'Grok'}[a.platform]||a.platform)))].join(' · ');
  return <><main className="dashboard"><header className="page-header"><div className="brand"><div className="brand-mark"><Droplets size={25}/></div><div><h1>{tr('app.title')||data.title||FALLBACK_TITLE}</h1><p>{platforms} <span>·</span> {tr('timeline.windowTitle',{hours:data.history_hours||24})}</p></div></div><div className="header-actions"><LanguageSwitch/><span className="update-time">{tr('app.updated',{time:date(data.generated_at,{hour:'2-digit',minute:'2-digit',second:'2-digit'})})}</span><IconButton title={tr('app.refreshTitle')} disabled={loading} onClick={refresh}><RefreshCw size={17} className={loading?'spin':''}/></IconButton></div></header>
    {(error||data.scheduler.sync_error||stale||schedulerWarning(data.scheduler,data.accounts))&&<div className="connection-warning"><TriangleAlert size={17}/>{error||data.scheduler.sync_error?.message||schedulerWarning(data.scheduler,data.accounts)||tr('error.warning')}</div>}
    <Hero/>
    <section className="overview"><div className="overview-main"><span className="overview-icon"><Activity size={24}/></span><div><h2>{tr('overview.heading')}</h2><p>{tr('overview.accounts',{n:data.accounts.length})} <span className="sep">/</span> {tr('overview.method')} <span className="sep">/</span> {tr('overview.rule')}</p><div className="overview-facts"><span><i className={errorAccounts?'dot fail':'dot pass'}/>{errorAccounts?tr('overview.bad',{n:errorAccounts}):tr('overview.ok')}</span><span title={tr('overview.cadenceTitle')}><Clock3 size={13}/>{tr('overview.cadence')}</span><button className="text-button" onClick={()=>setSelection({type:'prompts'})}><FileText size={13}/>{tr('overview.rules')}</button></div></div></div><div className="overview-score"><span>{tr('overview.rate')} <small>{tr('overview.window')}</small></span><strong>{rate==null?'--':rate+'%'}</strong><div className="countdown"><Clock3 size={14}/>{data.scheduler.running?tr('overview.running'):tr('overview.next',{time:`${String(Math.floor(elapsed/60)).padStart(2,'0')}:${String(elapsed%60).padStart(2,'0')}`})}</div><div className="progress"><div style={{width:`${progress}%`}}/></div></div></section>
    <CostSummary costs={data.costs}/>
    <div className="account-controls"><div className="category-tabs" role="tablist" aria-label={tr('tabs.label')}>{CATEGORIES.map((item,index)=><button key={item.id} id={`category-${item.id}`} role="tab" aria-selected={category===item.id} aria-controls="account-results" tabIndex={category===item.id?0:-1} onClick={()=>changeCategory(item.id)} onKeyDown={e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const length=CATEGORIES.length;const next=e.key==='Home'?0:e.key==='End'?length-1:(index+(e.key==='ArrowRight'?1:length-1))%length;changeCategory(CATEGORIES[next].id);e.currentTarget.parentElement.children[next].focus();}}>{tr(item.labelKey)}<span>{view.counts[item.id]}</span></button>)}</div><div className="filters"><label className="search"><Search size={15}/><input type="search" placeholder={tr('search.placeholder')} aria-label={tr('search.label')} value={query} onChange={e=>{setQuery(e.target.value);setPage(1);}}/></label><label className="filter"><select aria-label={tr('filter.label')} value={filter} onChange={e=>{setFilter(e.target.value);setPage(1);}}><option value="all">{tr('filter.all')}</option><option value="issues">{tr('filter.issues')}</option></select><ChevronDown size={14}/></label></div></div>
    <div className="toolbar"><div className="legend">{['pass','fail','error','quota','running','empty'].map(s=><span key={s}><i className={`dot ${s}`}/>{statusText(s)}</span>)}</div>{pager}</div>
    <div className="sort-tabs" role="group" aria-label={tr('sort.group')}>{SORT_OPTIONS.map(option=>{
      const active=sort.key===option.id, descending=sort.direction==='desc';
      const Icon=active?(descending?ArrowDown:ArrowUp):ArrowUpDown;
      return <button key={option.id} type="button" className={active?'active':''} aria-pressed={active}
        aria-label={`${tr(option.labelKey)}${active?tr(descending?'sort.desc':'sort.asc'):''}`}
        title={`${tr(option.descriptionKey)}${active?tr(descending?'sort.titleDesc':'sort.titleAsc'):tr('sort.titleDefault')}`}
        onClick={()=>{setSort(current=>nextSort(current,option.id));setPage(1);}}>{tr(option.labelKey)}<Icon size={14} aria-hidden="true"/></button>;
    })}</div>
    <div id="account-results" role="tabpanel" aria-labelledby={`category-${category}`} className="account-list">{view.accounts.map(a=><Account key={a.id} account={a} controlsEnabled={data.controls_enabled} controlling={busyControls[a.id]} onControl={control} onPrice={savePrice} onDetail={openDetail}/>)}{!view.accounts.length&&<div className="no-results"><Search size={25}/><h2>{tr('empty.title')}</h2><button className="text-button" onClick={()=>{setQuery('');setFilter('all');changeCategory('all');}}>{tr('empty.clear')}</button></div>}</div><nav aria-label={tr('pagination.label')} className="bottom-pagination">{pager}</nav>
    <footer className="page-footer"><span>{tr('app.title')} <span className="sep">/</span> {tr('footer.by')}</span><span>{tr('footer.refresh')} <span className="sep">·</span> {tr('footer.retention')}</span></footer>
  </main>{selection&&<Modal key={selection.id||selection.type} selection={selection} onClose={()=>setSelection(null)} data={data}/>}{toast&&<Toast toast={toast} onClose={closeToast}/>}</>;
}

function Root(){
  const {language}=useI18n();
  // Remount on language change so every module-scope tr() call re-reads the active table.
  return <App key={language}/>;
}

createRoot(document.getElementById('root')).render(<LanguageProvider><Root/></LanguageProvider>);
