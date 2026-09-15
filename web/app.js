'use strict';
const $ = (id) => document.getElementById(id);
const tokenKey = 'voiceStudioToken';
let token = '';
let connectionError = false;
let activeId = null;
let pollTimer = null;
let historySignature = '';
let submitting = false;
let connected = false;
let closed = false;

function storedToken(storage) {
  try { return window[storage].getItem(tokenKey) || ''; }
  catch { return ''; }
}

function rememberToken() {
  try {
    localStorage.setItem(tokenKey, token);
    // Only remove the launch credential after it is safe to reopen this URL.
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
    try { sessionStorage.removeItem(tokenKey); } catch {}
  } catch {
    // Storage can be disabled in a browser: keep the launch fragment for reloads.
  }
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function error(message = '', isConnectionError = false) {
  connectionError = isConnectionError;
  $('error').textContent = message;
  $('error').hidden = !message;
}

function controls() {
  $('expressionGroup').hidden = $('engine').value !== 'expressive';
  $('engineNote').textContent = $('engine').value === 'expressive'
    ? 'A different model using your same voice reference. Its tone still needs your listening feedback.'
    : 'Your selected default voice, using the new recording.';
  $('speedValue').textContent = Number($('speed').value).toFixed(2) + '×';
  $('pauseValue').textContent = (Number($('pause').value) / 1000).toFixed(1) + ' s';
  $('expressionValue').textContent = Number($('expression').value).toFixed(2);
  const text = $('text').value.trim();
  const words = text ? text.split(/\s+/u).length : 0;
  $('wordCount').textContent = `${words.toLocaleString()} words · ${$('text').value.length.toLocaleString()} characters`;
  const disabled = !connected || !text || Boolean(activeId) || submitting;
  $('generate').disabled = disabled;
  $('preview').disabled = disabled;
  $('quit').disabled = !connected || Boolean(activeId) || submitting;
}

function duration(seconds) {
  const total = Math.round(seconds || 0);
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}m ${String(total % 60).padStart(2, '0')}s` : `${total}s`;
}

function audioUrl(job, format, download = false) {
  return `/audio/${job.id}/${format}?token=${encodeURIComponent(token)}${download ? '&download=1' : ''}`;
}

function recordings(jobs) {
  const finished = jobs.filter((j) => ['completed', 'failed', 'cancelled'].includes(j.status));
  const signature = JSON.stringify(finished.map((j) => [j.id, j.status]));
  if (signature === historySignature) return;
  historySignature = signature;
  $('empty').hidden = finished.length > 0;
  const count = finished.filter((j) => j.status === 'completed').length;
  $('recordingCount').textContent = count ? `${count} saved ${count === 1 ? 'recording' : 'recordings'}` : '';
  // Keep existing audio nodes so a newly completed render cannot interrupt playback.
  const existing = new Map([...$('recordings').children].map((node) => [node.dataset.id, node]));
  for (const job of finished) {
    if (existing.has(job.id)) continue;
    const card = document.createElement('article');
    card.className = 'recording';
    card.dataset.id = job.id;
    const intro = document.createElement('div');
    const title = document.createElement('h3');
    title.textContent = job.title;
    const details = document.createElement('p');
    details.className = 'details';
    const voice = job.engine === 'natural' ? 'Natural voice' : `Expression ${job.settings.expression.toFixed(2)} · audition`;
    const description = `${job.mode === 'preview' ? 'Preview' : 'Full recording'} · ${voice} · ${job.settings.speed.toFixed(2)}×`;
    details.textContent = job.status === 'completed'
      ? `${duration(job.audio_seconds)} · ${job.words} words · ${description} · Made in ${duration(job.elapsed_seconds)}`
      : `${description} · ${job.status === 'cancelled' ? 'Cancelled' : 'Failed'}`;
    intro.append(title, details);
    card.append(intro);
    if (job.status === 'completed') {
      const links = document.createElement('div');
      links.className = 'downloads';
      for (const format of ['mp3', 'wav']) {
        const link = document.createElement('a');
        link.href = audioUrl(job, format, true);
        link.textContent = `↓ ${format.toUpperCase()}`;
        link.setAttribute('aria-label', `Download ${job.title} as ${format.toUpperCase()}`);
        links.append(link);
      }
      const player = document.createElement('audio');
      player.controls = true;
      player.preload = 'metadata';
      player.src = audioUrl(job, 'mp3');
      player.setAttribute('aria-label', job.title);
      card.append(links, player);
    } else if (job.error) {
      const failure = document.createElement('p');
      failure.className = 'failure';
      failure.textContent = job.error;
      card.append(failure);
    }
    $('recordings').append(card);
  }
  const order = new Map(finished.map((job, index) => [job.id, index]));
  for (const node of $('recordings').children) node.style.order = order.get(node.dataset.id);
  $('recordings').style.display = 'flex';
  $('recordings').style.flexDirection = 'column';
}

function progress(job) {
  if (!job || ['completed', 'failed', 'cancelled'].includes(job.status)) {
    activeId = null;
    $('statusPanel').hidden = true;
    controls();
    return;
  }
  activeId = job.id;
  $('statusPanel').hidden = false;
  $('cancel').disabled = Boolean(job.cancelling);
  $('cancel').textContent = job.cancelling
    ? (job.status === 'encoding' ? 'Cancelling export…' : 'Stopping after this passage…')
    : 'Cancel render';
  const stages = {queued: 'Preparing your recording…', loading: 'Loading your voice…', rendering: 'Giving your words a voice…', encoding: 'Preparing your audio files…'};
  $('statusTitle').textContent = stages[job.status] || 'Working…';
  $('statusDetail').textContent = `${job.title} · ${job.completed_passages} of ${job.total_passages} passages · ${duration(job.audio_seconds)} of audio`;
  $('progress').max = job.total_passages;
  $('progress').value = job.completed_passages;
  controls();
}

async function refresh() {
  if (closed) return;
  try {
    const jobs = await api('/api/jobs');
    if (closed) return;
    connected = true;
    if (connectionError) error();
    recordings(jobs);
    progress(jobs.find((j) => !['completed', 'failed', 'cancelled'].includes(j.status)));
  } catch (e) {
    if (closed) return;
    connected = false;
    error(e.message + ' If the app has stopped, open Launch Voice Studio.command.', true);
    controls();
  }
  clearTimeout(pollTimer);
  if (!closed) pollTimer = setTimeout(connected ? refresh : init, activeId ? 1200 : 5000);
}

async function submit(mode) {
  error();
  submitting = true;
  controls();
  const data = {text: $('text').value, title: $('title').value || 'Untitled narration', mode,
    engine: $('engine').value, speed: Number($('speed').value), paragraph_pause_ms: Number($('pause').value)};
  if (data.engine === 'expressive') data.expression = Number($('expression').value);
  try {
    const job = await api('/api/jobs', data);
    progress(job);
    $('statusPanel').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch (e) { error(e.message); }
  finally { submitting = false; controls(); }
  clearTimeout(pollTimer);
  refresh();
}

for (const id of ['text', 'engine', 'speed', 'pause', 'expression']) $(id).addEventListener('input', controls);
$('preview').addEventListener('click', () => submit('preview'));
$('generate').addEventListener('click', () => submit('full'));
$('quit').addEventListener('click', async () => {
  try {
    await api('/api/quit', {});
    closed = true;
    connected = false;
    clearTimeout(pollTimer);
    controls();
    error('Studio closed. Your recordings are saved. Open Launch Voice Studio.command to return.');
  } catch (e) { error(e.message); }
});
$('cancel').addEventListener('click', async () => {
  if (!activeId) return;
  try { progress(await api(`/api/jobs/${activeId}/cancel`, {})); }
  catch (e) { error(e.message); }
});
$('fileInput').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  error();
  try {
    if (file.size > 2000000) throw new Error('Choose a plain-text file under 2 MB.');
    const text = await file.text();
    if (text.length > 500000) throw new Error('The text exceeds 500,000 characters. Split it into smaller documents.');
    $('text').value = text;
    $('title').value = file.name.replace(/\.txt$/iu, '').slice(0, 100);
    controls();
  } catch (e) { error(e.message); }
  event.target.value = '';
});

async function init() {
  if (closed) return;
  clearTimeout(pollTimer);
  controls();
  try {
    const nextToken = new URLSearchParams(location.hash.slice(1)).get('token')
      || storedToken('localStorage') || storedToken('sessionStorage') || token;
    if (!nextToken) throw new Error('Open Launch Voice Studio.command once in this browser to connect.');
    if (nextToken !== token) {
      token = nextToken;
      historySignature = '';
      $('recordings').replaceChildren();
    }
    const config = await api('/api/config');
    $('voice-name').textContent = config.voice || 'Your voice';
    $('voice-avatar').textContent = (config.voice || 'V').slice(0, 1).toUpperCase();
    rememberToken();
    for (const option of $('engine').options) {
      const engine = config.engines[option.value];
      option.disabled = !engine?.available;
      if (engine) option.textContent = engine.label;
    }
    connected = true;
    if (connectionError) error();
    controls();
    await refresh();
  } catch (e) {
    connected = false;
    error(e.message, true);
    controls();
    if (!closed) pollTimer = setTimeout(init, 5000);
  }
}
init();
