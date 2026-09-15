'use strict';
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(process.env.VOICE_WEB_SCRIPT || path.join(__dirname, 'web/app.js'), 'utf8');
const key = 'voiceStudioToken';
const goodToken = 'test-local-session';

function storage(values = new Map(), blocked = false) {
  return {
    getItem(name) { if (blocked) throw new Error('Storage blocked'); return values.get(name) || null; },
    setItem(name, value) { if (blocked) throw new Error('Storage blocked'); values.set(name, value); },
    removeItem(name) { if (blocked) throw new Error('Storage blocked'); values.delete(name); },
  };
}

async function page({hash = '', shared = new Map(), legacy = new Map(), blocked = false, failConfig = 0} = {}) {
  const elements = new Map();
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: ({text: 'A short test passage.', engine: 'natural', speed: '1', pause: '600', expression: '0.5'})[id] || '',
      hidden: false, disabled: false, textContent: '', children: [], style: {},
      options: [{value: 'natural'}, {value: 'expressive'}],
      addEventListener() {}, replaceChildren() { this.children = []; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    URLSearchParams,
    location: {hash, pathname: '/', search: ''},
    localStorage: storage(shared, blocked), sessionStorage: storage(legacy, blocked),
    document: {getElementById: getElement},
    history: {replaceState() { context.location.hash = ''; }},
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      requests.push({url, authorization: options.headers.Authorization});
      if (url === '/api/config' && failConfig-- > 0) throw new Error('Temporary connection failure');
      const authorized = options.headers.Authorization === `Bearer ${goodToken}`;
      return {ok: authorized, status: authorized ? 200 : 401, async json() {
        if (!authorized) return {error: 'Open the studio using its launcher to reconnect.'};
        if (url === '/api/config') return {voice: 'Example Reader', engines: {natural: {available: true, label: 'Natural · New B'}, expressive: {available: true, label: 'Expressive'}}};
        return [];
      }};
    },
  });
  context.window = context;
  await vm.runInContext(source, context);
  return {context, shared, legacy, elements, requests, timers, async retry() {
    const [id, callback] = timers.entries().next().value || [];
    assert.ok(callback, 'a reconnect attempt should be scheduled');
    timers.delete(id);
    await callback();
  }};
}

test('successful launch allows a new tab to use the plain URL', async () => {
  const first = await page({hash: `#token=${goodToken}`});
  assert.equal(first.shared.get(key), goodToken);
  assert.equal(first.context.location.hash, '');
  const second = await page({shared: first.shared});
  assert.equal(second.elements.get('generate').disabled, false);
  assert.equal(second.requests[0].authorization, `Bearer ${goodToken}`);
});

test('a bare unauthenticated tab reconnects after another tab launches', async () => {
  const tab = await page();
  assert.equal(tab.requests.length, 0, 'do not flood the console with unauthenticated requests');
  assert.equal(tab.elements.get('generate').disabled, true);
  assert.match(tab.elements.get('error').textContent, /Launch Voice Studio/);
  tab.shared.set(key, goodToken);
  await tab.retry();
  assert.equal(tab.elements.get('generate').disabled, false);
  assert.equal(tab.elements.get('error').hidden, true);
});

test('failed initialization retries without discarding the launch fragment', async () => {
  const tab = await page({hash: `#token=${goodToken}`, failConfig: 1});
  assert.equal(tab.context.location.hash, `#token=${goodToken}`);
  assert.equal(tab.shared.has(key), false);
  await tab.retry();
  assert.equal(tab.elements.get('generate').disabled, false);
  assert.equal(tab.elements.get('error').hidden, true);
  assert.equal(tab.context.location.hash, '');
});

test('a fresh launcher token replaces a stale saved credential', async () => {
  const tab = await page({hash: `#token=${goodToken}`, shared: new Map([[key, 'stale']])});
  assert.equal(tab.shared.get(key), goodToken);
  assert.equal(tab.elements.get('generate').disabled, false);
});

test('legacy tab credentials migrate after successful authentication', async () => {
  const tab = await page({legacy: new Map([[key, goodToken]])});
  assert.equal(tab.shared.get(key), goodToken);
  assert.equal(tab.legacy.has(key), false);
  assert.equal(tab.elements.get('generate').disabled, false);
});

test('blocked browser storage keeps a working credential in the launch URL', async () => {
  const tab = await page({hash: `#token=${goodToken}`, blocked: true});
  assert.equal(tab.elements.get('generate').disabled, false);
  assert.equal(tab.context.location.hash, `#token=${goodToken}`);
});

test('rejected credentials are not saved or removed from the URL', async () => {
  const tab = await page({hash: '#token=invalid'});
  assert.equal(tab.elements.get('generate').disabled, true);
  assert.equal(tab.shared.has(key), false);
  assert.equal(tab.context.location.hash, '#token=invalid');
});

test('successful polling keeps non-connection errors visible', async () => {
  const tab = await page({hash: `#token=${goodToken}`});
  vm.runInContext("error('Invalid text supplied.');", tab.context);
  await tab.retry();
  assert.equal(tab.elements.get('error').textContent, 'Invalid text supplied.');
  assert.equal(tab.elements.get('error').hidden, false);
});


test('profile name and avatar come from the configured voice', async () => {
  const tab = await page({hash: `#token=${goodToken}`});
  assert.equal(tab.elements.get('voice-name').textContent, 'Example Reader');
  assert.equal(tab.elements.get('voice-avatar').textContent, 'E');
});
