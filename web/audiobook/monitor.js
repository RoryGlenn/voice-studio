/* Local telemetry presentation. No network requests or worker controls. */
(function (root) {
  'use strict';
  const metrics = ['cpu', 'gpu', 'ram', 'vram'];
  const colors = ['#8cdbb5', '#91baff', '#c9a4f3', '#efc28b'];
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function mergeSamples(existing, history, sample, now) {
    const map = new Map();
    for (const row of [...history, ...existing, ...(sample ? [sample] : [])]) {
      if (finite(row.at) && row.at >= now - 3600 && row.at <= now) map.set(row.at, row);
    }
    return [...map.values()].sort((a,b)=>a.at-b.at);
  }
  function plotData(samples, key, start, end) {
    const rows = samples.filter(s=>s.at>=start && s.at<=end);
    const x = [start], y = [null];
    for (const row of rows) {
      const previous = x[x.length-1];
      if (row.at - previous > 10) {x.push(previous + (row.at-previous)/2);y.push(null);}
      if (row.at === x[x.length-1]) y[y.length-1] = finite(row[key]) ? row[key] : null;
      else {x.push(row.at);y.push(finite(row[key]) ? row[key] : null);}
    }
    if(x[x.length-1]<end){x.push(end);y.push(null);}
    return [x,y];
  }
  function stats(samples, key, start, end) {
    const values = samples.filter(s=>s.at>=start && s.at<=end && finite(s[key])).map(s=>s[key]);
    return values.length ? {average:values.reduce((a,b)=>a+b,0)/values.length,peak:Math.max(...values),count:values.length} : null;
  }
  function stateSegments(samples, start, end) {
    const rows = samples.filter(s=>s.at<=end);
    const result=[];
    function add(from,to,state){
      from=Math.max(start,from);to=Math.min(end,to);
      if(to<=from)return;
      const last=result[result.length-1];
      if(last && last.state===state && last.to===from)last.to=to;
      else result.push({from,to,state});
    }
    let cursor=start;
    for(let i=0;i<rows.length;i++){
      const row=rows[i],next=rows[i+1]?.at ?? end;
      const to=Math.min(next,row.at+10,end);
      if(to<=start)continue;
      if(row.at>cursor)add(cursor,row.at,'unknown');
      add(row.at,to,row.state || 'unknown');cursor=Math.max(cursor,to);
    }
    if(cursor<end)add(cursor,end,'unknown');
    return result;
  }
  function sortProcesses(rows, key, ascending) {
    return rows.slice().sort((a,b)=>{
      const av=a[key],bv=b[key];
      if(av==null)return bv==null?0:1;if(bv==null)return -1;
      const order=key==='name'?String(av).localeCompare(String(bv)):av-bv;
      return (ascending?order:-order) || a.pid-b.pid;
    });
  }
  const helpers={mergeSamples,plotData,stats,stateSegments,sortProcesses};
  if(typeof module!=='undefined')module.exports=helpers;
  if(!root.document)return;
  const $=id=>document.getElementById(id);
  let samples=[], latest={}, activity={}, range=120, sortKey='cpu_percent', ascending=false, frame=null;
  const inspected={}, charts={}, time = value => new Date(value*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const unit=key=>['cpu','gpu'].includes(key)?'%':' GiB';
  const format=(key,value)=>finite(value)?value.toFixed(1)+unit(key):'Unavailable';
  const stateLabels={rendering:'Narrating',checking:'Checking',recovering:'Recovering',waiting:'GPU wait',retrying:'Retry',packaging:'Packaging',paused:'Paused',complete:'Complete',stopped:'Stopped',failed:'Failed',unknown:'No samples'};
  const stateColor=state=>({rendering:'#589b7b',checking:'#738cc7',waiting:'#b69959',retrying:'#ce7f68',failed:'#ce7f68',packaging:'#a489c4',recovering:'#81908b',complete:'#589b7b',paused:'#63716d',stopped:'#63716d'}[state]||'#263330');
  function queue(){if(frame==null)frame=requestAnimationFrame(()=>{frame=null;render();});}
  function createChart(key,index,data,max){
    const host=$(key+'-chart');
    const plot = new root.uPlot({
      width:Math.max(200,host.clientWidth),height:220,padding:[12,16,0,0],
      legend:{show:false},select:{show:false},
      cursor:{drag:{x:false,y:false},sync:{key:'system-monitor',scales:['x',null]}},
      scales:{x:{time:true},y:{range:()=>[0,max()]}},
      axes:[{stroke:'#9baea7',grid:{show:false},ticks:{show:false},size:38,space:110,font:'11px sans-serif',values:(_,ticks)=>ticks.map(t=>new Date(t*1000).toLocaleTimeString([],range===120?{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}:{hour:'2-digit',minute:'2-digit'}))},
        {stroke:'#9baea7',grid:{stroke:'#2b3b35',width:1},ticks:{show:false},size:54,font:'11px sans-serif',values:(_,ticks)=>ticks.map(v=>Number(v.toFixed(1))+unit(key)),incrs:['cpu','gpu'].includes(key)?[25]:undefined}],
      series:[{}, {label:key.toUpperCase(),stroke:colors[index],width:1.7,fill:colors[index]+'12',spanGaps:false,points:{show:false}}],
      hooks:{setCursor:[u=>{const i=u.cursor.idx;$(key+'-hover').textContent=i==null?'Hover to inspect':time(u.data[0][i])+' · '+format(key,u.data[1][i]);}],
        draw:[u=>{const ctx=u.ctx;ctx.save();ctx.strokeStyle='#78938755';ctx.setLineDash([3,5]);
          for(const segment of stateSegments(samples,u.scales.x.min,u.scales.x.max)){
            if(segment.from<=u.scales.x.min || segment.state==='unknown')continue;
            const x=u.valToPos(segment.from,'x',true);ctx.beginPath();ctx.moveTo(x,u.bbox.top);ctx.lineTo(x,u.bbox.top+u.bbox.height);ctx.stroke();
          }ctx.restore();}]}
    },data,host);
    let keyIndex=null;
    host.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight'].includes(event.key))return;
      event.preventDefault();keyIndex=Math.max(0,Math.min(plot.data[0].length-1,(keyIndex ?? plot.data[0].length-1)+(event.key==='ArrowLeft'?-1:1)));
      inspected[key]=plot.data[0][keyIndex];
      plot.setCursor({left:plot.valToPos(inspected[key],'x'),top:plot.bbox.height/2});
      $(key+'-hover').textContent=time(inspected[key])+' · '+format(key,plot.data[1][keyIndex]);
      $(key+'-hover').setAttribute('aria-live','polite');
    });
    host.addEventListener('pointerenter',()=>{delete inspected[key];});
    return plot;
  }
  function capacity(key) {
    if(key==='cpu'||key==='gpu')return 100;
    const value=key==='ram'?activity.memory?.total/1073741824:activity.gpu?.memory_total/1024;
    return Math.max(finite(value)?value:0,...samples.map(s=>finite(s[key+'_total'])?s[key+'_total']:0),1);
  }
  function render(){
    if($('system').hidden || !latest.observed_at)return;
    const now=Date.now()/1000,start=now-range;
    for(const [index,key] of metrics.entries()){
      const data=plotData(samples,key,start,now),host=$(key+'-chart');
      if(!charts[key])charts[key]=createChart(key,index,data,()=>capacity(key));
      const chart=charts[key],width=Math.max(200,host.clientWidth);
      if(chart.width!==width)chart.setSize({width,height:220});
      chart.setData(data,false);chart.setScale('x',{min:start,max:now});chart.setScale('y',{min:0,max:capacity(key)});
      if(inspected[key]!=null){
        const i=data[0].reduce((best,t,index)=>Math.abs(t-inspected[key])<Math.abs(data[0][best]-inspected[key])?index:best,0);
        $(key+'-hover').textContent=time(data[0][i])+' · '+format(key,data[1][i]);
      }
      const summary=stats(samples,key,start,now);
      $(key+'-stats').textContent=summary?`Sample avg ${format(key,summary.average)} · Peak ${format(key,summary.peak)}`:'No readings in this time range';
    }
    const segments=stateSegments(samples,start,now);
    $('state-timeline').innerHTML=segments.map(s=>`<span style="width:${100*(s.to-s.from)/range}%;background:${stateColor(s.state)}" title="${escape(stateLabels[s.state]||s.state)} · ${time(s.from)}–${time(s.to)}"></span>`).join('');
    const known=segments.filter(s=>s.state!=='unknown');
    $('timeline-details').textContent=known.length?known.map(s=>`${time(s.from)} ${stateLabels[s.state]||s.state}`).join(' · '):'No observed worker states in this time range.';
  }
  function renderProcesses(){
    const rows=sortProcesses(activity.processes || [],sortKey,ascending);
    $('process-list').innerHTML=rows.length?rows.map(p=>`<tr><td title="${escape(p.name)}">${escape(p.name)}</td><td>${Number(p.pid)}</td><td><span class="process-usage" style="--load:${Math.min(100,Math.max(0,p.cpu_percent || 0))}%">${finite(p.cpu_percent)?p.cpu_percent.toFixed(1):'—'}</span></td><td>${finite(p.rss)?Math.round(p.rss/1048576):'—'}</td></tr>`).join(''):'<tr><td colspan="4">Process readings unavailable</td></tr>';
  }
  function update(data){
    latest=data;
    const now=Date.now()/1000,a=data.activity || {},fresh=finite(a.sampled_at) && now-a.sampled_at<10;
    activity=fresh?a:{};
    const memory=activity.memory,gpu=activity.gpu;
    const sample=fresh?{at:a.sampled_at,cpu:a.cpu_percent,gpu:gpu?.utilization,ram:memory?memory.used/1073741824:null,vram:gpu?.memory_used!=null?gpu.memory_used/1024:null,ram_total:memory?memory.total/1073741824:null,vram_total:gpu?.memory_total!=null?gpu.memory_total/1024:null,state:data.state}:null;
    samples=mergeSamples(samples,data.history || [],sample,now);
    for(const key of metrics)$(key+'-load').textContent=format(key,sample?.[key]);
    $('activity-status').textContent=fresh?`Live · ${time(a.sampled_at)} · blank spans mean missing samples`:'Readings unavailable · retained history shown';
    $('cpu-details').textContent=activity.cores?`${activity.cores.length} logical cores · whole-computer utilization`:'CPU readings unavailable';
    $('gpu-details').textContent=gpu?`${gpu.name} · ${gpu.temperature==null?'Temperature unavailable':gpu.temperature+' °C'}`:'GPU readings unavailable';
    $('ram-details').textContent=memory?`${(memory.total/1073741824).toFixed(1)} GiB capacity · ${(memory.available/1073741824).toFixed(1)} GiB available`:'Memory readings unavailable';
    $('vram-details').textContent=gpu?.memory_total!=null?`${(gpu.memory_total/1024).toFixed(1)} GiB capacity · dedicated GPU memory`:'GPU memory readings unavailable';
    $('swap-details').textContent=memory?`Swap ${(memory.swap_used/1073741824).toFixed(1)} / ${(memory.swap_total/1073741824).toFixed(1)} GiB`:'';
    $('cpu-cores').innerHTML=(activity.cores || []).map(c=>`<div class="core-tile" style="--intensity:${finite(c.percent)?Math.min(1,Math.max(0,c.percent/100)):0}" title="${escape(c.name)} utilization"><span>${escape(c.name)}</span><strong>${finite(c.percent)?Math.round(c.percent)+'%':'—'}</strong></div>`).join('')||'<p>Per-core readings unavailable</p>';
    renderProcesses();queue();
  }
  document.querySelectorAll('[data-range]').forEach(button=>button.addEventListener('click',()=>{
    range=Number(button.dataset.range);
    document.querySelectorAll('[data-range]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));queue();
  }));
  document.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{
    ascending=sortKey===button.dataset.sort?!ascending:button.dataset.sort==='name';sortKey=button.dataset.sort;
    document.querySelectorAll('[data-sort]').forEach(b=>{b.parentElement.removeAttribute('aria-sort');b.textContent={name:'Process',pid:'PID',cpu_percent:'CPU %',rss:'RAM MiB'}[b.dataset.sort];});
    button.parentElement.setAttribute('aria-sort',ascending?'ascending':'descending');button.textContent+=ascending?' ↑':' ↓';renderProcesses();
  }));
  new ResizeObserver(queue).observe($('system'));
  root.SystemMonitor={update,render,disconnected:()=>update({...latest,activity:null})};
})(typeof window==='undefined'?globalThis:window);
