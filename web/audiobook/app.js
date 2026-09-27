'use strict';
const $ = id => document.getElementById(id);
let activitySamples = [], lastActivity = null;
  function activity(data) {
    const fresh = data && data.sampled_at != null && Date.now() / 1000 - data.sampled_at < 10;
    const cpu = fresh ? data.cpu_percent : null;
    const gpu = fresh ? data.gpu : null;
    const gpuLoad = gpu ? gpu.utilization : null;
    const memory = fresh ? data.memory : null;
    const ram = memory ? memory.percent : null;
    const gib = value => `${(value / 1073741824).toFixed(1)} GiB`;
    $('ram-load').textContent = ram == null ? 'Unavailable' : `${ram.toFixed(1)}%`;
    $('ram-bar').value = ram == null ? 0 : ram;
    $('ram-details').textContent = memory ? `${gib(memory.used)} used / ${gib(memory.total)} total · ${gib(memory.available)} available` : 'RAM readings unavailable';
    $('swap-details').textContent = memory ? `Swap: ${gib(memory.swap_used)} / ${gib(memory.swap_total)}` : '';
    const vram = gpu && gpu.memory_total > 0 && gpu.memory_used != null ? gpu.memory_used / gpu.memory_total * 100 : null;
    $('vram-load').textContent = vram == null ? 'Unavailable' : `${vram.toFixed(1)}%`;
    $('vram-bar').value = vram == null ? 0 : vram;
    const coreRows = (fresh && data.cores || []).map(core => {
      const filled = core.percent == null ? 0 : Math.round(core.percent / 10);
      return `${core.name.padEnd(5)} ${'█'.repeat(filled)}${'░'.repeat(10 - filled)} ${core.percent == null ? '   —' : core.percent.toFixed(0).padStart(3) + '%'}`;
    });
    const paired = [];
    for (let i = 0; i < coreRows.length; i += 2) paired.push(coreRows[i] + (coreRows[i + 1] ? '  ' + coreRows[i + 1] : ''));
    $('cpu-cores').textContent = paired.join('\n') || 'Per-core readings unavailable';
    const processes = fresh ? data.processes : null;
    $('process-list').textContent = processes ? '   PID  CPU %  RAM MiB  NAME\n' + processes.map(p => `${String(p.pid).padStart(6)} ${p.cpu_percent == null ? '     —' : p.cpu_percent.toFixed(1).padStart(6)} ${Math.round(p.rss / 1048576).toString().padStart(8)}  ${p.name}`).join('\n') : 'Process readings unavailable';
    for (const [name, value] of [['cpu', cpu], ['gpu', gpuLoad]]) {
      $(name + '-load').textContent = value == null ? 'Unavailable' : `${value.toFixed(1)}%`;
      $(name + '-bar').value = value == null ? 0 : value;
    }
    $('activity-status').textContent = fresh ? `Updated ${new Date(data.sampled_at * 1000).toLocaleTimeString()}` : 'Activity readings unavailable';
    $('gpu-details').textContent = gpu ? `${gpu.name} · VRAM ${gpu.memory_used == null ? 'unavailable' : (gpu.memory_used / 1024).toFixed(1) + ' GiB'} / ${gpu.memory_total == null ? 'unavailable' : (gpu.memory_total / 1024).toFixed(1) + ' GiB'} · ${gpu.temperature == null ? 'Temperature unavailable' : gpu.temperature + ' °C'}` : 'GPU readings unavailable';
    if (!fresh || cpu == null || gpuLoad == null) activitySamples = [];
    if (fresh && data.sampled_at !== lastActivity) {
      if (lastActivity != null && data.sampled_at - lastActivity > 10) activitySamples = [];
      activitySamples.push({time: data.sampled_at, cpu, gpu: gpuLoad, ram});
      lastActivity = data.sampled_at;
    }
    activitySamples = activitySamples.filter(s => fresh && data.sampled_at - s.time <= 120);
    for (const name of ['cpu', 'gpu', 'ram']) {
      const points = activitySamples.filter(s => s[name] != null).map(s => `${300 - (data.sampled_at - s.time) * 2.5},${100 - s[name]}`).join(' ');
      $(name + '-trend').setAttribute('points', points);
    }
  }
let current = null, selectedReview = null, reviewSignature = '', chapterSignature = '', passages = [], lastEvent = 0;
const base = location.pathname.startsWith('/jobs/') ? location.pathname.replace(/\/$/, '') : '';
const route = path => base + path;
document.querySelector('.cover').src = route('/cover');
$('download').href = route('/download');

const duration = seconds => seconds == null ? 'Measuring…' : seconds < 60 ? 'Under 1 min' : seconds < 3600 ? `${Math.ceil(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)} hr`;
const age = seconds => seconds < 5 ? 'just now' : seconds < 60 ? `${Math.floor(seconds)}s ago` : `${Math.floor(seconds / 60)}m ago`;
const escapeHtml = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setView(name) {
  if (!['overview','review','system','history'].includes(name)) name = 'overview';
  document.querySelectorAll('.view').forEach(el => {el.hidden = el.id !== name;});
  document.querySelectorAll('[data-view]').forEach(el => {const active = el.dataset.view === name; el.classList.toggle('active', active); el.setAttribute('aria-pressed', String(active));});
  history.replaceState(null, '', '#' + name);
}
function show(data) {
  if (!data.book) return;
  current = data;
  $('job-name').textContent = data.job_id + (data.controls_enabled ? '' : ' · View-only: configure a workspace service to enable controls');
  const selectedOption=Array.from($('book-select').options).find(o=>o.value==='/jobs/'+encodeURIComponent(data.job_id)+'/');if(selectedOption)$('book-select').value=selectedOption.value;
  $('completion').textContent = data.completion;
  $('alerts').innerHTML = (data.alerts || []).map(text => `<p class="note warning">${escapeHtml(text)}</p>`).join('');
  $('job-settings').textContent = `Engine: ${typeof data.settings?.engine === 'string' ? data.settings.engine : JSON.stringify(data.settings?.engine || 'Unknown')} · Free disk: ${data.disk_free_gib} GiB`;
  $('batch-history').innerHTML = (data.batches || []).length ? `<div class="history-scroll"><table><thead><tr><th>Started</th><th>Stage</th><th>Duration</th><th>Result</th><th>Batch limits (render/check)</th></tr></thead><tbody>${data.batches.slice().reverse().map(b=>`<tr><td>${escapeHtml(new Date(b.started_at*1000).toLocaleString())}</td><td>${escapeHtml(b.stage)}</td><td>${Number(b.seconds).toFixed(1)} s</td><td>${b.exit_code===0?'Completed':'Failed ('+Number(b.exit_code)+')'}</td><td>${Number(b.render_batch_size)} / ${Number(b.check_batch_size)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="note">No measured batch history yet. Recording starts when the updated supervisor next launches. Existing runs are not given estimated durations.</p>';

  $('connection').textContent = data.workspace_error ? 'Update needs attention' : '● Live · local';
  $('book-title').textContent = data.book.title;
  $('book-author').textContent = data.book.author.split(';').filter(Boolean).join(' · ');
  $('stage').textContent = data.pause_requested && data.state !== 'paused' ? 'Pause requested · finishing current work' : data.stage;
  $('render-count').textContent = `${data.generated.toLocaleString()} / ${data.total.toLocaleString()} passages`;
  $('check-count').textContent = `${data.checked.toLocaleString()} / ${data.total.toLocaleString()} checked`;
  $('render-percent').textContent = `${data.render_percent.toFixed(1)}%`;
  $('render-bar').value = data.render_percent;
  $('remaining').textContent = `${data.remaining.toLocaleString()} passages left`;
  $('audio-duration').textContent = `${(data.audio_seconds / 3600).toFixed(1)} hours`;
  $('estimate-label').textContent = data.state === 'checking' ? 'Quality-check time remaining' : 'Narration time remaining';
  $('eta').textContent = data.download_ready ? 'Ready' : data.remaining === 0 && data.state !== 'checking' ? 'Narration complete' : duration(data.estimate_seconds);
  $('pace').textContent = data.estimate_seconds == null ? 'Waiting for 1 minute of steady progress' : 'Current stage only · packaging excluded';
  $('last-save').textContent = `Last save ${age(data.saved_age_seconds)}`;
  const load = data.activity || {}; const gpu = load.gpu || {};
  $('compact-load').textContent = `${load.cpu_percent == null ? '—' : Math.round(load.cpu_percent)}% CPU · ${gpu.utilization == null ? '—' : Math.round(gpu.utilization)}% GPU`;
  if (!activitySamples.length && data.history) activitySamples = data.history.filter(h => Date.now()/1000-h.at<120).map(h=>({time:h.at,cpu:h.cpu,gpu:h.gpu,ram:null}));
  activity(load);
  const phase = data.download_ready ? 3 : data.state === 'packaging' ? 2 : data.remaining === 0 ? 1 : 0;
  ['rendering','checking','packaging','complete'].forEach((name,i)=>{const el=$('step-'+name);el.classList.toggle('active',i===phase);el.classList.toggle('done',i<phase);});
  $('package-label').textContent = data.download_ready ? 'Validated' : data.state === 'packaging' ? 'Building files' : 'Waiting';
  const busy = data.action.state === 'running';
  $('pause').hidden = data.state === 'paused';
  $('pause').textContent = data.state === 'complete' ? 'Pause for review' : 'Pause after passage';
  $('pause').disabled = !data.controls_enabled || data.pause_requested || busy;
  $('resume').hidden = !['paused','stopped','failed','complete'].includes(data.state);
  $('resume').disabled = busy || !data.resume_supported || !data.controls_enabled;
  $('resume').textContent = data.state === 'complete' ? 'Rebuild updated draft' : 'Resume conversion';
  $('resume').hidden = !['paused','stopped','failed','complete'].includes(data.state) || (data.state === 'complete' && data.download_ready);
  $('download').hidden = !data.download_ready;
  let actionMessage = data.action.message || '';
  if (actionMessage.startsWith('Pause requested') && data.state === 'paused') actionMessage = 'Conversion paused. Listening review is available.';
  if (actionMessage.startsWith('Resume requested') && !['paused','stopped','failed'].includes(data.state)) actionMessage = '';
  $('action-message').textContent = data.workspace_error || actionMessage;
  $('review-badge').textContent = data.held;
  $('review-summary').textContent = `${data.verified} passed · ${data.held} flagged`;
  $('chapter-count').textContent = `${data.chapters.filter(c=>c.generated===c.total).length} / ${data.chapters.length} fully narrated`;
  const signature = JSON.stringify(data.chapters.map(c=>[c.number,c.title,c.total]));
  if (signature !== chapterSignature) {
    $('chapters').innerHTML = data.chapters.map(c=>`<article class="chapter" data-track="${c.number}"><span class="number">${String(c.number).padStart(2,'0')}</span><div><h3>${escapeHtml(c.title)}</h3><progress max="${c.total}" value="${c.generated}" aria-label="${escapeHtml(c.title)} narration"></progress><small>${c.generated}/${c.total} narrated · ${c.checked} checked${c.flagged?' · '+c.flagged+' flagged':''} · ${Math.round(c.seconds/60)} min saved</small></div><button class="secondary" data-chapter="${c.number}" ${c.generated?'':'disabled'}>Listen</button></article>`).join('');
    chapterSignature = signature;
  }
  for (const c of data.chapters) {
    const el=document.querySelector(`[data-track="${c.number}"]`);
    el.querySelector('progress').value=c.generated;
    el.querySelector('small').textContent=`${c.generated}/${c.total} narrated · ${c.checked} checked${c.flagged?' · '+c.flagged+' flagged':''} · ${Math.round(c.seconds/60)} min saved`;
    el.querySelector('button').disabled=!c.generated;
  }
  $('recent').innerHTML = data.recent.slice(0,5).map(r=>`<div class="recent-item">${escapeHtml(r.chapter)}<small>Passage ${r.id.split('/')[1]} · ${escapeHtml(r.status.replaceAll('_',' '))} · ${age(Date.now()/1000-r.at)}</small></div>`).join('');
  renderReview();
  for (const [id,value] of Object.entries({heartbeat:age(data.heartbeat_age_seconds || 0),retries:data.failures,output:data.download_ready?'Validated draft ready':'Not ready',verified:data.verified,'held-count':data.held,awaiting:data.awaiting_check,unchecked:data.unchecked})) $(id).textContent=value;
  $('next-step').textContent = 'Narrate → check speech → package and validate the draft.';
  $('freshness').textContent = `Updated ${new Date(data.observed_at*1000).toLocaleTimeString()}`;
  $('message').textContent = data.message;
}
function diffMarkup(spans, fallback) {return spans ? spans.map(s=>s.changed ? `<mark>${escapeHtml(s.text)}</mark>` : escapeHtml(s.text)).join('') : escapeHtml(fallback);}
function renderReview(force=false) {
  if (!current) return;
  const filter=$('review-filter').value;
  const allowed=current.controls_enabled && current.pause_requested && ['paused','stopped','complete'].includes(current.state) && current.action.state!=='running';
  const rows=current.review.filter(r=>filter==='all'||(filter==='wording'?r.reasons.includes('Wording differs'):r.reasons.some(s=>s!=='Wording differs')));
  const signature=JSON.stringify([rows,allowed]);
  if (!force && signature===reviewSignature) return;
  reviewSignature=signature;
  $('review-list').innerHTML=rows.length?rows.map(r=>`<article class="review-card"><div class="section-title"><h3>${escapeHtml(r.chapter)} · passage ${r.id.split('/')[1]}</h3><span>Attempt ${r.attempt}</span></div><div class="tags">${r.reasons.map(escapeHtml).join(' · ')}</div><p class="diff-legend">${r.diff?.has_changes?'Highlighted text differs between the script and recognition; listen to confirm.':'No word or punctuation differences found. Check the flag reason and listen for timing or signal problems.'}</p><div class="compare"><div><label>Expected wording</label><p>${diffMarkup(r.diff?.expected,r.expected)}</p></div><div><label>Speech recognition heard</label><p>${r.recognized?diffMarkup(r.diff?.recognized,r.recognized):'No transcript available'}</p></div></div><audio controls preload="none" src="${route('/audio?id=')}${encodeURIComponent(r.id)}"></audio><div class="controls"><button class="secondary" data-approve="${r.id}" ${allowed?'':'disabled'}>Record listening review</button><button class="secondary" data-regenerate="${r.id}" ${allowed?'':'disabled'}>Regenerate passage</button></div></article>`).join(''):'<p class="note">No flagged passages match this view. Unchecked passages may still produce new flags.</p>';
}
async function action(payload) {
  const response=await fetch(route('/api/action'),{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Token':document.querySelector('meta[name="workspace-token"]').content},body:JSON.stringify(payload)});
  const result=await response.json(); if(!response.ok) throw Error(result.error||'Action failed');
  $('action-message').textContent=result.message; return result;
}
async function safeAction(payload) {try{await action(payload);}catch(error){$('action-message').textContent=error.message;}}
async function openChapter(number) {
  try {
    const response=await fetch(route('/api/passages?chapter=')+number);if(!response.ok)throw Error('Could not load chapter');
    passages=await response.json();const chapter=current.chapters.find(c=>c.number===number);
    $('listen-title').textContent=chapter.title;
    $('passage-select').innerHTML=passages.map(p=>`<option value="${p.id}" ${p.status==='pending'?'disabled':''}>Passage ${p.number} · ${escapeHtml(p.status.replaceAll('_',' '))}</option>`).join('');
    $('passage-select').value=passages.find(p=>p.status!=='pending').id;
    selectPassage();$('listen-dialog').showModal();
  } catch(error){$('action-message').textContent=error.message;}
}
function selectPassage(){const p=passages.find(p=>p.id===$('passage-select').value);$('passage-text').textContent=p.text;$('player').src=route('/audio?id=')+encodeURIComponent(p.id);$('player-status').textContent=p.status==='verified'?'Checked, with pacing applied.':'Original generated audio · '+p.status.replaceAll('_',' ');}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>setView(button.dataset.view)));
$('pause').addEventListener('click',()=>safeAction({action:'pause'}));
$('resume').addEventListener('click',()=>safeAction({action:'resume'}));
$('chapters').addEventListener('click',e=>{const b=e.target.closest('[data-chapter]');if(b)openChapter(Number(b.dataset.chapter));});
$('review-filter').addEventListener('change',()=>renderReview(true));
$('review-list').addEventListener('click',e=>{
  const approve=e.target.closest('[data-approve]'),regenerate=e.target.closest('[data-regenerate]');
  if(approve){selectedReview=current.review.find(r=>r.id===approve.dataset.approve);$('heard-text').value='';$('review-note').value='';$('review-error').textContent='';$('review-dialog').showModal();}
  if(regenerate){const r=current.review.find(r=>r.id===regenerate.dataset.regenerate);safeAction({action:'regenerate',id:r.id,fingerprint:r.fingerprint});}
});
$('submit-review').addEventListener('click',async()=>{try{await action({action:'approve',id:selectedReview.id,fingerprint:selectedReview.fingerprint,transcript:$('heard-text').value,note:$('review-note').value});$('review-dialog').close();}catch(error){$('review-error').textContent=error.message;}});
$('passage-select').addEventListener('change',selectPassage);
$('close-listen').addEventListener('click',()=>$('listen-dialog').close());
$('listen-dialog').addEventListener('close',()=>$('player').pause());
$('close-review').addEventListener('click',()=>$('review-dialog').close());
$('player').addEventListener('ended',()=>{const index=passages.findIndex(p=>p.id===$('passage-select').value);const next=passages[index+1];if(next&&next.status!=='pending'){$('passage-select').value=next.id;selectPassage();$('player').play().catch(()=>{});}});
$('refresh-log').addEventListener('click',async()=>{try{const response=await fetch(route('/api/log'));const data=await response.json();$('worker-log').textContent=data.lines?data.lines.join('\n'):data.error;}catch(error){$('worker-log').textContent=error.message;}});
setView(location.hash.slice(1));
const events=new EventSource(route('/events'));
events.onmessage=event=>{try{show(JSON.parse(event.data));lastEvent=Date.now();}catch(error){$('connection').textContent='Display update failed';console.error(error);}};
events.onerror=()=>{$('connection').textContent='Reconnecting…';};
async function fallback(){if(Date.now()-lastEvent>8000){try{const response=await fetch(route('/api/workspace'));if(!response.ok)throw Error('Unavailable');show(await response.json());lastEvent=Date.now();}catch{$('connection').textContent='Connection lost · saved progress retained';activity(null);}}}
fallback();setInterval(fallback,5000);

async function loadLibrary(){
  try {
    if(document.activeElement===$('book-select'))return;
    const response=await fetch('/api/library');if(!response.ok)throw Error('Book library unavailable');
    const books=await response.json();const selected=current?.job_id || decodeURIComponent(base.split('/')[2] || '');
    $('book-select').innerHTML=books.map(b=>`<option value="${escapeHtml(b.url || '')}" ${b.id===selected?'selected':''} ${b.url?'':'disabled'}>${escapeHtml(b.title)} — ${escapeHtml(b.id)} · ${escapeHtml(b.state)}${b.total!=null?' · '+b.generated+'/'+b.total:''}</option>`).join('');
    $('library-error').textContent='';
  } catch(error){$('library-error').textContent=error.message;}
}
$('book-select').addEventListener('change',()=>{if($('book-select').value)location.assign($('book-select').value);});
loadLibrary();setInterval(loadLibrary,15000);
