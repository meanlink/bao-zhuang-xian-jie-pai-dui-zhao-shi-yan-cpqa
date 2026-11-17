import {energy,jobs} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {EnergyPeriodStatus,EnergyAnomalyKind,EnergyRevisionStatus} from '../constants/enums.js';

const canWrite=()=>session.user.role==='admin'||session.user.role==='analyst';
const isAdmin=()=>session.user.role==='admin';

function badge(status){
 const map={open:'待对齐',blocked:'异常隔离',closed:'已封账',revised:'已修订'};
 return '<span class="status '+esc(status)+'">'+esc(EnergyPeriodStatus[status]||status)+'</span>';
}

async function loadAll(pid){
 const [period,preview,entries,anomalies,revisions,allJobs]=await Promise.all([
   energy.period(pid),
   energy.preview(pid).catch(e=>({error:e.message})),
   energy.entries(pid).catch(()=>[]),
   energy.anomalies(pid).catch(()=>[]),
   energy.revisions(pid).catch(()=>[]),
   jobs.list().catch(()=>[])
 ]);
 let book=null;
 try{book=await energy.book(pid);}catch(e){book=null;}
 return {period,preview,entries,anomalies,revisions,book,allJobs};
}

function totalsCard(s){
 if(!s||s.error)return '<div class="panel empty">试算暂不可用：'+esc(s?.error||'')+'</div>';
 const t=s.totals||{};
 const ok=s.identity_ok;
 return '<section class="panel"><div class="section-title"><h2>分摊恒等式核对</h2><span class="'+(ok?'approval':'warning')+'">'+(ok?'恒等式成立':'恒等式不平')+'</span></div>'+
  '<div class="metrics">'+
  '<div><span>表计增量</span><strong>'+esc(fmt(t.meter_delta))+'<small>度</small></strong></div>'+
  '<div><span>片段分摊合计</span><strong>'+esc(fmt(t.allocated))+'<small>度</small></strong></div>'+
  '<div><span>未归属电量</span><strong>'+esc(fmt(t.unassigned))+'<small>度</small></strong></div>'+
  '</div><p class="hint">片段分摊 '+esc(fmt(t.allocated))+' + 未归属 '+esc(fmt(t.unassigned))+' = '+esc(fmt(t.meter_delta))+'；区间 '+esc(t.interval_count)+' 个，隔离 '+esc(t.isolated_interval_count)+' 个。</p></section>';
}

function watermarkPanel(p){
 const wm=p.meter_watermark_ts, ws=p.segment_watermark_ts;
 const ready=wm!=null&&ws!=null&&wm>=p.end_ts&&ws>=p.end_ts;
 return '<section class="panel"><div class="section-title"><h2>双路确认水位</h2><span class="'+(ready?'approval':'warning')+'">'+(ready?'两路已齐':'未齐，拒绝结案')+'</span></div>'+
  '<div class="watermark-grid"><div><span>电表读数水位</span><strong>'+(wm==null?'未确认':date(wm))+'</strong></div><div><span>片段时间水位</span><strong>'+(ws==null?'未确认':date(ws))+'</strong></div></div>'+
  (canWrite()&&p.status==='open'?'<div class="parameter-grid"><button class="button small" data-wm="meter">确认表计水位至期末</button><button class="button small" data-wm="segment">确认片段水位至期末</button></div><p class="hint">两路数据乱序到达，按各自确认水位封账；任一路未齐拒绝结案。</p>':'<p class="hint">账期'+esc(EnergyPeriodStatus[p.status]||'')+'后水位冻结。</p>')+
  '</section>';
}

function ingestPanel(p){
 if(p.status!=='open')return '<section class="panel empty">账期已'+esc(EnergyPeriodStatus[p.status])+'，不再接受直接补传；迟到数据请走「修订差异」开修订账。</section>';
 return '<section class="panel"><div class="section-title"><h2>两路数据接入（乱序/幂等）</h2></div>'+
  '<div class="ingest-cols">'+
  '<form id="meter-form"><h3>电表累计读数</h3><p class="hint">每行：表号,时间戳,累计读数,幂等键</p><textarea name="rows" rows="5" placeholder="M1,0,100,r1&#10;M1,10,110,r2"></textarea><button class="button small" type="submit" '+(canWrite()?'':'disabled')+'>接入表计读数</button></form>'+
  '<form id="segment-form"><h3>片段起止时间</h3><p class="hint">每行：片段号,起,止,幂等键</p><textarea name="rows" rows="5" placeholder="S-A,0,20,s1&#10;S-B,20,40,s2"></textarea><button class="button small" type="submit" '+(canWrite()?'':'disabled')+'>接入片段</button></form>'+
  '</div></section>';
}

function entriesTable(entries,revisionEntriesOnly){
 const rows=revisionEntriesOnly?entries.filter(e=>e.revision_id!=null):entries;
 if(!rows.length)return '<div class="empty">暂无分摊明细。</div>';
 return '<div class="table-wrap"><table><thead><tr><th>账类</th><th>片段</th><th>电表</th><th>区间起</th><th>区间止</th><th>重叠秒</th><th>耗电/度</th></tr></thead><tbody>'+
  rows.map(e=>'<tr class="'+(e.is_unassigned?'row-unassigned':'')+'"><td>'+(e.revision_id?'修订':e.book_id?'原账':'试算')+'</td><td>'+(e.segment_ref?esc(e.segment_ref):'<span class="warning">未归属</span>')+'</td><td>'+esc(e.meter_serial)+'</td><td>'+esc(date(e.start_ts))+'</td><td>'+esc(date(e.end_ts))+'</td><td>'+esc(fmt(e.overlap_seconds))+'</td><td><strong>'+esc(fmt(e.amount))+'</strong>'+(e.is_unassigned?'<small> '+esc(e.unassigned_reason||'')+'</small>':'')+'</td></tr>').join('')+
  '</tbody></table></div>';
}

function anomalyPanel(anomalies){
 return '<section class="panel"><div class="section-title"><h2>隔离待核对异常</h2><span>'+anomalies.length+' 项</span></div>'+
  (anomalies.length?('<div class="table-wrap"><table><thead><tr><th>类型</th><th>范围</th><th>说明</th><th>定位</th></tr></thead><tbody>'+
   anomalies.map(a=>'<tr><td><span class="warning">'+esc(EnergyAnomalyKind[a.kind]||a.kind)+'</span></td><td>'+esc(a.scope)+'</td><td>'+esc(a.message)+'</td><td><code>'+esc(JSON.stringify(a.ref))+'</code></td></tr>').join('')+'</tbody></table></div>')
   :'<div class="empty">无异常。换表回零、读数缺口、片段交叠会在此隔离并阻止封账。</div>')+'</section>';
}

function revisionPanel(p,revisions){
 const completed=revisions.filter(r=>r.status==='completed');
 const openRev=revisions.find(r=>r.status==='in_progress');
 const diffs=completed.flatMap(r=>(r.diff?.diffs||[]).map(d=>({...d,rev:r.id})));
 return '<section class="panel"><div class="section-title"><h2>修订账与差异</h2><span>'+revisions.length+' 本</span></div>'+
  (canWrite()&&(p.status==='closed'||p.status==='revised')&&!openRev?'<form id="rev-form" class="inline-form"><input name="reason" placeholder="修订原因（迟到读数/片段更正）" required minlength="2"><input name="idempotency_key" placeholder="修订幂等键 rev-..." required><button class="button small" type="submit">开修订账</button></form>':'')+
  (openRev?'<div class="revision-box"><h3>修订账 #'+openRev.id+' · '+esc(EnergyRevisionStatus[openRev.status])+'</h3><p class="hint">断点：已处理 '+esc((openRev.cursor?.processed_count)||0)+' 条更正；中断后从已记位置恢复，重试不重复记。</p>'+
   '<form id="corr-form" class="inline-form" data-rev="'+openRev.id+'"><select name="stream"><option value="meter">表计读数</option><option value="segment">片段</option></select><input name="idempotency_key" placeholder="更正幂等键" required><input name="payload" placeholder=\'JSON，如 {"meter_serial":"M1","reading_ts":50,"reading_value":140}\' required><button class="button small" type="submit" '+(canWrite()?'':'disabled')+'>追加更正</button></form>'+
   (canWrite()?'<button class="primary" id="process-rev" data-rev="'+openRev.id+'">执行/恢复修订</button>':'')+'</div>':'')+
  (diffs.length?'<div class="table-wrap"><table><thead><tr><th>修订账</th><th>片段</th><th>原账耗电</th><th>修订耗电</th><th>差异</th></tr></thead><tbody>'+
   diffs.map(d=>'<tr><td>#'+esc(d.rev)+'</td><td>'+(d.segment_id?esc(d.segment_id):'<span class="warning">未归属</span>')+'</td><td>'+esc(fmt(d.original))+'</td><td>'+esc(fmt(d.revised))+'</td><td class="'+(d.delta>0?'delta-up':d.delta<0?'delta-down':'')+'">'+esc((d.delta>0?'+':'')+fmt(d.delta))+'</td></tr>').join('')+
   '</tbody></table></div>':'<div class="empty">尚无完成的修订。原账与已发简报始终保留，修订另存差异。</div>')+
  revisions.map(r=>'<div class="delivery"><div><strong>修订账 #'+r.id+' · '+esc(EnergyRevisionStatus[r.status]||r.status)+'</strong><p>'+esc(r.reason)+'</p><small>净额 '+esc((r.diff?.net_delta!=null)?fmt(r.diff.net_delta):'—')+' · '+(r.checksum?esc(r.checksum.slice(0,20))+'…':'无校验')+'</small></div>'+(r.briefing_filename?'<a class="text-button" href="'+energy.briefing('revision',r.id)+'" target="_blank">修订简报 ↓</a>':'')+'</div>').join('')+
  '</section>';
}

function approvePanel(p,allJobs){
 const completed=allJobs.filter(j=>j.status==='completed');
 const gateOk=p.status==='closed'||p.status==='revised';
 return '<section class="panel"><div class="section-title"><h2>关联分析批准</h2><span class="'+(gateOk?'approval':'warning')+'">'+(gateOk?'封账已完成，可批准':'须封账后批准')+'</span></div>'+
  (completed.length?'<form id="approve-form" class="inline-form"><select name="job_id">'+completed.map(j=>'<option value="'+j.id+'">'+esc(j.name)+(j.approved?'（已批准）':'')+'</option>').join('')+'</select><button class="primary" type="submit" '+(isAdmin()&&gateOk?'':'disabled')+'>以封账电量为据批准</button></form><p class="hint">仅业务负责人可批准；'+(gateOk?'当前账期已封账，分析结论可关联。':'账期未封账，按钮禁用。')+'</p>':'<div class="empty">暂无可关联的已完成分析。</div>')+'</section>';
}

export async function energyPage(id){
 const periods=await energy.periods();
 if(!periods.length){
  return heading('06 / ENERGY','电量对账与封账','按重叠时长把相邻表计读数间耗电分摊到片段，双路水位封账。')+
  '<div class="panel"><div class="section-title"><h2>新建电量账期</h2></div><form id="period-form"><label>账期编号<input name="period_key" required placeholder="P-2026W1"></label><label>名称<input name="name" required value="第1周电量对账"></label><div class="parameter-grid"><label>开始时间戳<input name="start_ts" type="number" step="any" value="0" required></label><label>结束时间戳<input name="end_ts" type="number" step="any" value="100" required></label></div><label>读数缺口阈值/秒（可选）<input name="max_gap_seconds" type="number" step="any" placeholder="如 60"></label><button class="primary" type="submit" '+(canWrite()?'':'disabled')+'>创建账期</button></form></div>';
 }
 const pid=id?Number(id):periods[0].id;
 const data=await loadAll(pid);
 const {period:p,preview,entries,anomalies,revisions,book,allJobs}=data;
 const closed=book;
 return heading('06 / ENERGY','电量对账与封账','按重叠时长分摊相邻读数间耗电；片段耗电＋未归属＝表计增量。',
  '<button class="button" id="refresh-energy">刷新 ↻</button>'+'<a class="button" href="#/energy">切换账期</a>')+
  '<div class="result-selector"><label>账期<select id="select-period">'+periods.map(x=>'<option value="'+x.id+'" '+(x.id===pid?'selected':'')+'>'+esc(x.name)+' · '+esc(x.period_key)+'</option>').join('')+'</select></label>'+badge(p.status)+
  '<span>'+esc(date(p.start_ts))+' → '+esc(date(p.end_ts))+'</span>'+
  (closed?'<a class="text-button" href="'+energy.briefing('book',book.id)+'" target="_blank">固定简报(原账) ↓</a>':'')+'</div>'+
  '<div class="report-grid"><div>'+ingestPanel(p)+watermarkPanel(p)+totalsCard(preview)+
  (isAdmin()&&p.status==='open'?'<section class="panel"><button class="primary" id="close-period" '+(canWrite()?'':'')+'>执行封账（双路水位齐且无异常）</button><p class="hint">封账后原账与简报冻结；迟到更正另开修订账。</p></section>':'')+
  '</div><div>'+anomalyPanel(anomalies)+
  '<section class="panel"><div class="section-title"><h2>分摊明细与未归属</h2><span>'+entries.length+' 行</span></div>'+entriesTable(entries,false)+'</section>'+
  revisionPanel(p,revisions)+approvePanel(p,allJobs)+'</div></div>';
}

function parseRows(text,n){
 return text.trim().split(/\n+/).map(line=>line.split(',').map(s=>s.trim())).filter(parts=>parts.length===n&&parts.every(Boolean));
}

export function bindEnergy(refresh){
 const periodForm=document.querySelector('#period-form');
 if(periodForm){submitOnce(periodForm,async f=>{
   const gap=f.get('max_gap_seconds');
   await energy.createPeriod({period_key:f.get('period_key'),name:f.get('name'),start_ts:Number(f.get('start_ts')),end_ts:Number(f.get('end_ts')),max_gap_seconds:gap?Number(gap):null});
   notify('账期已创建');await refresh();
 });return;}
 document.querySelector('#refresh-energy')?.addEventListener('click',refresh);
 document.querySelector('#select-period')?.addEventListener('change',e=>{location.hash='#/energy/'+e.target.value;});
 const pid=Number(document.querySelector('#select-period')?.value);
 if(!pid)return;

 const meterForm=document.querySelector('#meter-form');
 meterForm&&submitOnce(meterForm,async f=>{
  const items=parseRows(f.get('rows'),4).map(c=>({meter_serial:c[0],reading_ts:Number(c[1]),reading_value:Number(c[2]),idempotency_key:c[3]}));
  if(!items.length)throw new Error('请按 表号,时间戳,读数,幂等键 每行一条');
  const r=await energy.ingest(pid,'meter',items);notify('表计接入 '+r.accepted+' 条，重复跳过 '+r.skipped_duplicate);await refresh();
 });
 const segForm=document.querySelector('#segment-form');
 segForm&&submitOnce(segForm,async f=>{
  const items=parseRows(f.get('rows'),4).map(c=>({segment_ref:c[0],label:c[0],start_ts:Number(c[1]),end_ts:Number(c[2]),idempotency_key:c[3]}));
  if(!items.length)throw new Error('请按 片段号,起,止,幂等键 每行一条');
  const r=await energy.ingest(pid,'segment',items);notify('片段接入 '+r.accepted+' 条，重复跳过 '+r.skipped_duplicate);await refresh();
 });
 document.querySelectorAll('[data-wm]').forEach(b=>b.addEventListener('click',async()=>{
  const p=await energy.period(pid);
  await energy.watermark(pid,b.dataset.wm,p.end_ts);notify('已确认 '+(b.dataset.wm==='meter'?'表计':'片段')+' 水位');await refresh();
 }));
 document.querySelector('#close-period')?.addEventListener('click',async()=>{
  try{const r=await energy.close(pid);notify('封账完成，原账 #'+r.id+' 已冻结');await refresh();}catch(e){notify(e.message,true);}
 });
 const revForm=document.querySelector('#rev-form');
 revForm&&submitOnce(revForm,async f=>{await energy.openRevision(pid,{reason:f.get('reason'),idempotency_key:f.get('idempotency_key')});notify('修订账已开立，可追加迟到更正');await refresh();});
 const corrForm=document.querySelector('#corr-form');
 corrForm&&submitOnce(corrForm,async f=>{
  let payload;try{payload=JSON.parse(f.get('payload'));}catch(e){throw new Error('更正负载必须是合法 JSON');}
  const revId=Number(corrForm.dataset.rev);
  await energy.addCorrection(revId,{stream:f.get('stream'),idempotency_key:f.get('idempotency_key'),payload});notify('更正已暂存（幂等）');await refresh();
 });
 document.querySelector('#process-rev')?.addEventListener('click',async e=>{
  try{const r=await energy.processRevision(Number(e.currentTarget.dataset.rev));notify('修订完成，净额 '+fmt(r.diff?.net_delta??0));await refresh();}catch(err){notify(err.message,true);}
 });
 const approveForm=document.querySelector('#approve-form');
 approveForm&&submitOnce(approveForm,async f=>{await energy.approveAnalysis({job_id:Number(f.get('job_id')),period_id:pid});notify('已在封账依据下批准关联分析');await refresh();});
}

// 本地轻量 submit 封装，避免循环依赖 hooks/forms（其仅依赖 notify）
function submitOnce(form,handler){
 form.addEventListener('submit',async e=>{e.preventDefault();const btn=form.querySelector('[type=submit]');if(btn)btn.disabled=true;try{await handler(new FormData(form));}catch(err){notify(err.message,true);}finally{if(btn)btn.disabled=false;}});
}
