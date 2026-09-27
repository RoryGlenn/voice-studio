const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const assert = require('node:assert/strict');
const {mergeSamples,plotData,stats,stateSegments,sortProcesses} = require('../web/audiobook/monitor.js');
// Legacy CPU/GPU-only samples survive while newer memory readings are added.
const history=[{at:900,cpu:10,gpu:80,state:'rendering'},{at:906,cpu:20,gpu:null,ram:8,vram:5,state:'waiting'}];
const samples=mergeSamples([],history,{at:908,cpu:30,gpu:null,ram:9,vram:6,state:'waiting'},910);
assert.equal(samples.length,3);
assert.deepEqual(plotData(samples,'gpu',900,910),[[900,906,908,910],[80,null,null,null]]);
assert.deepEqual(stats(samples,'cpu',900,910),{average:20,peak:30,count:3});
assert.deepEqual(stats(samples,'ram',900,910),{average:8.5,peak:9,count:2});
assert.equal(stats(samples,'ram',890,901),null);
// A time gap breaks the line instead of connecting observations across downtime.
const gap=plotData([{at:900,cpu:10},{at:960,cpu:40}],'cpu',900,970);
assert.deepEqual(gap,[[900,930,960,970],[10,null,40,null]]);
const duplicate=mergeSamples(samples,history,{at:908,cpu:31},910);
assert.equal(duplicate.length,3);assert.equal(duplicate[2].cpu,31);
assert.equal(mergeSamples([{at:1}],[],null,4000).length,0);
assert.deepEqual(stateSegments(history,900,930),[{from:900,to:906,state:'rendering'},{from:906,to:916,state:'waiting'},{from:916,to:930,state:'unknown'}]);
assert.deepEqual(sortProcesses([{pid:1,name:'z',rss:3},{pid:2,name:'a',rss:null},{pid:3,name:'b',rss:5}],'rss',false).map(p=>p.pid),[3,1,2]);
assert.deepEqual(sortProcesses([{pid:1,name:'z'},{pid:2,name:'a'}],'name',true).map(p=>p.pid),[2,1]);
// Data/summary updates work without a GPU and do not remove other histories.
const html=readFileSync('web/audiobook/index.html','utf8');
const elements=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],{hidden:true,clientWidth:500,setAttribute(){},addEventListener(){}}]));
const plots=[],monitorEvents={};
let themeColors={'--cpu-color':'#8cdbb5','--gpu-color':'#91baff','--ram-color':'#c9a4f3','--vram-color':'#efc28b','--muted':'#adbfba','--border':'#3e5554','--error':'#ffb4a4'};
class Plot {constructor(options,data){this.options=options;this.data=data;this.width=options.width;plots.push(this);}setData(data){this.data=data;}setScale(){}redraw(){this.redrawn=true;}setSize(size){this.width=size.width;}}
const context={document:{documentElement:{},getElementById:id=>elements[id],querySelectorAll:()=>[],addEventListener:(name,handler)=>{monitorEvents[name]=handler;}},getComputedStyle:()=>({getPropertyValue:name=>themeColors[name]}),uPlot:Plot,ResizeObserver:class{observe(){}},requestAnimationFrame:()=>1,Date};
context.window=context;
runInNewContext(readFileSync('web/audiobook/monitor.js','utf8'),context);
const now=Date.now()/1000;
const state={observed_at:now,state:'rendering',history:[{at:now-5,cpu:10,ram:7,ram_total:64}],activity:{sampled_at:now,cpu_percent:25,gpu:null,memory:{used:8*1073741824,total:64*1073741824,available:56*1073741824,swap_used:0,swap_total:0},cores:Array.from({length:20},(_,i)=>({name:'cpu'+i,percent:25})),processes:[{pid:1,name:'<script>',cpu_percent:150,rss:1048576},{pid:2,name:'busier',cpu_percent:300,rss:1048576}]}};
context.SystemMonitor.update(state);
assert.equal(elements['cpu-load'].textContent,'25.0%');assert.equal(elements['ram-load'].textContent,'8.0 GiB');assert.equal(elements['gpu-load'].textContent,'Unavailable');
assert.match(elements['process-list'].innerHTML,/&lt;script&gt;/);
// Process percentages and bars use the same whole-machine scale as the chart.
const processRows=elements['process-list'].innerHTML.split('</tr>');
assert.match(processRows[0],/busier/);assert.match(processRows[0],/--load:15%">15\.0%<\/span><small class="process-cores">3 cores/);
assert.match(processRows[1],/--load:7\.5%">7\.5%<\/span><small class="process-cores">1\.5 cores/);
for(const [cpu,coreCount,expected] of [[0,20,'0.0%'],[2000,20,'100.0%'],[100,1,'100.0%'],[150,0,'—'],[null,20,'—']]){
  context.SystemMonitor.update({...state,activity:{...state.activity,cores:state.activity.cores.slice(0,coreCount),processes:[{pid:1,name:'worker',cpu_percent:cpu,rss:1}]}});
  const row=elements['process-list'].innerHTML;
  assert.ok(row.includes('>'+expected+'</span>'));
  if(cpu==null)assert.ok(!row.includes('process-cores'),'unknown CPU must not look idle');
  if(coreCount===0)assert.match(row,/1\.5 cores/,'retain core usage when total capacity is unavailable');
  if(cpu===100)assert.match(row,/>1 core<\/small>/);
}
context.SystemMonitor.update(state);
elements.system.hidden=false;context.SystemMonitor.render();assert.equal(plots.length,4);
assert.equal(plots[0].options.series[1].spanGaps,false);
context.SystemMonitor.disconnected();context.SystemMonitor.render();
assert.equal(elements['cpu-load'].textContent,'Unavailable');assert.match(elements['cpu-stats'].textContent,/Peak 25.0%/);
assert.ok(plots[0].data[1].includes(25));
// Recolor existing canvas charts without losing samples or creating new plots.
assert.equal(plots[0].options.series[1].stroke(),'#8cdbb5');
themeColors={...themeColors,'--cpu-color':'#206c50','--muted':'#62675e','--border':'#c1b7a5'};
monitorEvents.themechange();
assert.equal(plots.length,4);assert.ok(plots.every(plot=>plot.redrawn));
assert.equal(plots[0].options.series[1].stroke(),'#206c50');
assert.equal(plots[0].options.axes[0].stroke(),'#62675e');
assert.ok(plots[0].data[1].includes(25));
// A saved light theme is restored before paint; invalid or blocked storage is safe.
const bootstrap=html.match(/<script>([\s\S]*?)<\/script>/)[1];
for(const [saved,expected] of [['paper','paper'],['invalid','forest'],[null,'forest']]){
  const document={documentElement:{dataset:{}}};
  runInNewContext(bootstrap,{document,localStorage:{getItem:()=>saved}});
  assert.equal(document.documentElement.dataset.theme,expected);
}
const blockedDocument={documentElement:{dataset:{}}};
runInNewContext(bootstrap,{document:blockedDocument,localStorage:{getItem(){throw Error('Storage blocked');}}});
assert.equal(blockedDocument.documentElement.dataset.theme,'forest');

console.log('Monitor history, gaps, statistics, sorting, and unavailable states passed.');
const source=readFileSync('web/audiobook/app.js','utf8'),diffContext={};
runInNewContext(source.slice(source.indexOf('const escapeHtml'),source.indexOf('function setView'))+source.slice(source.indexOf('function diffMarkup'),source.indexOf('function renderReview')),diffContext);
assert.equal(diffContext.diffMarkup([{text:'<script>',changed:true},{text:' & safe',changed:false}],''),'<mark>&lt;script&gt;</mark> &amp; safe');
console.log('Review highlighting safely escapes transcript text.');

(async()=>{
  const log={textContent:'Loading',scrollTop:0,scrollHeight:200,clientHeight:100};
  const status={textContent:''},system={hidden:false};
  let calls=0,respond;
  const logContext={document:{hidden:false},$:id=>({'worker-log':log,'log-status':status,system})[id],route:path=>'/jobs/test'+path,AbortSignal,Date,
    fetch:(url)=>{assert.equal(url,'/jobs/test/api/log');calls++;return new Promise(resolve=>{respond=resolve;});}};
  runInNewContext(source.slice(source.indexOf('let logLoading'),source.indexOf("$('refresh-log').addEventListener")),logContext);
  let pending=logContext.refreshWorkerLog();
  await logContext.refreshWorkerLog();assert.equal(calls,1,'requests cannot overlap');
  respond({ok:true,json:async()=>({lines:['first']})});await pending;
  assert.equal(log.textContent,'first');assert.equal(log.scrollTop,200,'first load follows output');
  log.scrollTop=20;
  pending=logContext.refreshWorkerLog();respond({ok:true,json:async()=>({lines:['second']})});await pending;
  assert.equal(log.textContent,'second');assert.equal(log.scrollTop,20,'reading position retained');
  pending=logContext.refreshWorkerLog();respond({ok:false,json:async()=>({error:'Offline'})});await pending;
  assert.equal(log.textContent,'second');assert.match(status.textContent,/retrying automatically/);
  pending=logContext.refreshWorkerLog();respond({ok:true,json:async()=>({lines:['recovered']})});await pending;
  assert.equal(log.textContent,'recovered');
  const before=calls;system.hidden=true;await logContext.refreshWorkerLog();system.hidden=false;logContext.document.hidden=true;await logContext.refreshWorkerLog();assert.equal(calls,before,'hidden views do not poll');
  console.log('Worker log refresh, overlap protection, scroll position, recovery, and visibility passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
