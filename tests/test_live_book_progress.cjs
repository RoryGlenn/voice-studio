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
const plots=[];
class Plot {constructor(options,data){this.options=options;this.data=data;this.width=options.width;plots.push(this);}setData(data){this.data=data;}setScale(){}setSize(size){this.width=size.width;}}
const context={document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},uPlot:Plot,ResizeObserver:class{observe(){}},requestAnimationFrame:()=>1,Date};
context.window=context;
runInNewContext(readFileSync('web/audiobook/monitor.js','utf8'),context);
const now=Date.now()/1000;
const state={observed_at:now,state:'rendering',history:[{at:now-5,cpu:10,ram:7,ram_total:64}],activity:{sampled_at:now,cpu_percent:25,gpu:null,memory:{used:8*1073741824,total:64*1073741824,available:56*1073741824,swap_used:0,swap_total:0},cores:[{name:'cpu0',percent:100}],processes:[{pid:1,name:'<script>',cpu_percent:150,rss:1048576}]}};
context.SystemMonitor.update(state);
assert.equal(elements['cpu-load'].textContent,'25.0%');assert.equal(elements['ram-load'].textContent,'8.0 GiB');assert.equal(elements['gpu-load'].textContent,'Unavailable');
assert.match(elements['process-list'].innerHTML,/&lt;script&gt;/);
elements.system.hidden=false;context.SystemMonitor.render();assert.equal(plots.length,4);
assert.equal(plots[0].options.series[1].spanGaps,false);
context.SystemMonitor.disconnected();context.SystemMonitor.render();
assert.equal(elements['cpu-load'].textContent,'Unavailable');assert.match(elements['cpu-stats'].textContent,/Peak 25.0%/);
assert.ok(plots[0].data[1].includes(25));
console.log('Monitor history, gaps, statistics, sorting, and unavailable states passed.');
const source=readFileSync('web/audiobook/app.js','utf8'),diffContext={};
runInNewContext(source.slice(source.indexOf('const escapeHtml'),source.indexOf('function setView'))+source.slice(source.indexOf('function diffMarkup'),source.indexOf('function renderReview')),diffContext);
assert.equal(diffContext.diffMarkup([{text:'<script>',changed:true},{text:' & safe',changed:false}],''),'<mark>&lt;script&gt;</mark> &amp; safe');
console.log('Review highlighting safely escapes transcript text.');
