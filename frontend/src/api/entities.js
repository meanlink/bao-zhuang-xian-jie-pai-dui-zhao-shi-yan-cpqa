import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};

// 电量对账封账：两路水位、试算、封账、修订账、固定简报、封账门控批准
export const energy={
 periods:()=>api('/energy/periods'),
 createPeriod:data=>api('/energy/periods',{method:'POST',body:JSON.stringify(data)}),
 period:id=>api('/energy/periods/'+id),
 ingest:(id,stream,items)=>api('/energy/periods/'+id+'/ingest',{method:'POST',body:JSON.stringify({stream,items})}),
 watermark:(id,stream,watermark_ts)=>api('/energy/periods/'+id+'/watermark',{method:'POST',body:JSON.stringify({stream,watermark_ts})}),
 preview:id=>api('/energy/periods/'+id+'/preview'),
 close:id=>api('/energy/periods/'+id+'/close',{method:'POST'}),
 book:id=>api('/energy/periods/'+id+'/book'),
 entries:id=>api('/energy/periods/'+id+'/entries'),
 anomalies:id=>api('/energy/periods/'+id+'/anomalies'),
 revisions:id=>api('/energy/periods/'+id+'/revisions'),
 openRevision:(id,data)=>api('/energy/periods/'+id+'/revisions',{method:'POST',body:JSON.stringify(data)}),
 addCorrection:(revId,data)=>api('/energy/revisions/'+revId+'/corrections',{method:'POST',body:JSON.stringify(data)}),
 processRevision:revId=>api('/energy/revisions/'+revId+'/process',{method:'POST'}),
 corrections:revId=>api('/energy/revisions/'+revId+'/corrections'),
 approveAnalysis:data=>api('/energy/approve-analysis',{method:'POST',body:JSON.stringify(data)}),
 briefing:(kind,refId)=>'/api/energy/briefing/'+kind+'/'+refId
};
