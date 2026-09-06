// Execute the real inline state engine with controllable network completion.
// DOM stand-ins cover rendering/focus state; these are not browser layout tests.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync('templates/index.html', 'utf8');
const script = template.split('<script>')[1].split('// ---------- boot')[0];
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  const nodes = new Map();
  const requests = [];
  const listeners = {};
  const node = selector => {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {
        innerHTML: '', textContent: '', listeners: {}, dataset: {}, children: [],
        replaceChildren(...children) { this.children = children; },
        classList: { add: (...xs) => xs.forEach(x => classes.add(x)),
          remove: (...xs) => xs.forEach(x => classes.delete(x)),
          toggle: (x, on) => on ? classes.add(x) : classes.delete(x) },
        setAttribute() {}, addEventListener(name, fn) { this.listeners[name] = fn; },
        scrollIntoView() {}, focus() {}, blur() {},
      });
    }
    return nodes.get(selector);
  };
  const context = vm.createContext({
    AbortController, URLSearchParams, URL, console,
    setTimeout: () => 0, clearTimeout() {},
    document: { querySelector: node, querySelectorAll: () => [],
      createElement: tag => ({ tagName: tag.toUpperCase(), classList: { add() {} } }),
      getElementById: id => node('#' + id),
      addEventListener: (name, fn) => { listeners[name] = fn; } },
    window: { open() {} },
    request: (url, opts = {}) => new Promise((resolve, reject) => {
      requests.push({ url, opts, resolve, reject });
    }),
  });
  const run = code => vm.runInContext(code, context);
  run(script);
  run('api = request; loadStats = () => {};');
  const jobs = ['A', 'B', 'C'].map(id => ({
    id, company: 'Company ' + id, title: 'Engineer ' + id,
    status: 'new', track: 'full_time', domain: 'SWE',
    url: 'https://example.com/' + id, match_score: 50,
  }));
  run(`state.jobs = ${JSON.stringify(jobs)}; state.loading = false;
    state.jobs.forEach(j => detailCache.set(j.id, {...j})); reconcileSelection('A');`);
  return { run, nodes, requests, jobs, listeners,
    list: () => requests.filter(r => r.url.startsWith('/api/jobs?')),
    writes: () => requests.filter(r => r.opts.method === 'POST'),
    ids: () => Array.from(run('state.jobs.map(j => j.id)')) };
}

test('failure of A after B succeeds restores only A and keeps B undoable', async () => {
  const f = fixture();
  f.run("setStatus('A', 'saved'); setStatus('B', 'archived');");
  f.writes()[1].resolve({ ok: true });
  f.writes()[0].reject(new Error('offline'));
  await tick();
  assert.deepEqual(f.ids(), ['A', 'C']);
  assert.deepEqual(Array.from(f.run('undoStack.map(op => op.id)')), ['B']);
  assert.equal(f.writes().length, 2); // no compensating POST for B
  assert.equal(f.run('state.selectedId'), 'C');
});

test('undo restores ID selection and reloads the same inspector', async () => {
  const f = fixture();
  f.run("setStatus('A', 'saved')");
  f.writes()[0].resolve({ ok: true });
  await tick();
  f.run('undo()');
  await tick();
  f.writes()[1].resolve({ ok: true });
  await tick();
  const detail = f.requests.filter(r => r.url === '/api/jobs/A').at(-1);
  detail.resolve(f.jobs[0]);
  await tick();
  assert.equal(f.run('state.selectedId'), 'A');
  assert.equal(f.run('selectedJob().id'), 'A');
  assert.match(f.nodes.get('#rows').innerHTML, /row active[^>]+aria-selected="true" data-id="A"/);
  assert.match(f.nodes.get('#detail').innerHTML, /Company A/);
  assert.doesNotMatch(f.nodes.get('#detail').innerHTML, /Company B/);
});

test('undo queues behind original write and duplicate undo does not race', async () => {
  const f = fixture();
  f.run("setStatus('A', 'saved'); undo(); undo();");
  await tick();
  assert.equal(f.writes().length, 1);
  f.writes()[0].resolve({ ok: true });
  await tick();
  assert.equal(f.writes().length, 2);
  assert.equal(JSON.parse(f.writes()[1].opts.body).status, 'new');
  f.writes()[1].resolve({ ok: true });
  await tick();
  assert.equal(f.run('undoStack.length'), 0);
});

test('failed undo retains its history entry for retry', async () => {
  const f = fixture();
  f.run("setStatus('A', 'saved')");
  f.writes()[0].resolve({ ok: true });
  await tick();
  f.run('undo()');
  await tick();
  f.writes()[1].reject(new Error('offline'));
  await tick();
  assert.equal(f.run('undoStack[0].id'), 'A');
  f.run('undo()');
  await tick();
  assert.equal(f.writes().length, 3);
});

test('undo in another filter delegates reconciliation to the server', async () => {
  const f = fixture();
  f.run("setStatus('A', 'saved')");
  f.writes()[0].resolve({ ok: true });
  await tick();
  f.run("state.search = 'Company C'; state.jobs = state.jobs.filter(j => j.id === 'C'); reconcileSelection('C'); undo();");
  await tick();
  f.writes()[1].resolve({ ok: true });
  await tick();
  assert.equal(f.run('state.loading'), true);
  assert.equal(f.ids().includes('A'), false);
  f.list()[0].resolve([f.jobs[2]]);
  await tick();
  assert.deepEqual(f.ids(), ['C']);
  assert.equal(f.run('state.selectedId'), 'C');
});

test('older list response is ignored even if transport disregards abort', async () => {
  const f = fixture();
  f.run("loadJobs();");
  await tick();
  const old = f.list()[0];
  f.run("state.status = 'saved'; loadJobs(); setStatus('A', 'archived');");
  await tick();
  assert.equal(old.opts.signal.aborted, true);
  assert.equal(f.writes().length, 0);
  f.list()[1].resolve([{ ...f.jobs[1], status: 'saved' }]);
  await tick();
  old.resolve(f.jobs);
  await tick();
  assert.deepEqual(f.ids(), ['B']);
  assert.equal(f.run('state.status'), 'saved');
  assert.equal(f.run('state.selectedId'), 'B');
});

test('search input invalidates requests before debounce fires', async () => {
  const f = fixture();
  f.run('loadJobs()');
  await tick();
  const old = f.list()[0];
  f.nodes.get('#search').listeners.input({ target: { value: 'different' } });
  assert.equal(old.opts.signal.aborted, true);
  old.resolve(f.jobs);
  await tick();
  assert.equal(f.run('state.loading'), true);
  assert.deepEqual(f.ids(), []);
  f.run("setStatus('A', 'saved')");
  assert.equal(f.writes().length, 0);
});

test('list fetch waits for writes before reading the next view', async () => {
  const f = fixture();
  f.run("setStatus('A', 'saved'); state.status = 'saved'; loadJobs();");
  await tick();
  assert.equal(f.list().length, 0);
  f.writes()[0].resolve({ ok: true });
  await tick();
  assert.equal(f.list().length, 1);
  f.list()[0].resolve([{ ...f.jobs[0], status: 'saved' }]);
  await tick();
  assert.deepEqual(f.ids(), ['A']);
});

test('late detail response cannot replace selected job or repopulate stale cache', async () => {
  const f = fixture();
  f.run("detailCache.clear(); selectRow(0); selectRow(1);");
  const a = f.requests.find(r => r.url === '/api/jobs/A');
  const b = f.requests.find(r => r.url === '/api/jobs/B');
  assert.doesNotMatch(f.nodes.get('#detail').innerHTML, /onclick=/);
  b.resolve(f.jobs[1]);
  await tick();
  a.resolve(f.jobs[0]);
  await tick();
  assert.match(f.nodes.get('#detail').innerHTML, /Company B/);
  assert.equal(f.run("detailCache.has('A')"), false);
});

test('prep completion after navigation does not steal selection', async () => {
  const f = fixture();
  f.run("genPrep('A'); selectRow(1);");
  f.writes()[0].resolve({ summary: 'Prepared A', bullets: [] });
  await tick();
  assert.equal(f.run('state.selectedId'), 'B');
  assert.match(f.nodes.get('#detail').innerHTML, /Company B/);
});

test('Escape clears both selection and keyboard triage target', () => {
  const f = fixture();
  f.listeners.keydown({ key: 'Escape', target: { tagName: 'BODY' } });
  f.listeners.keydown({ key: 'a', target: { tagName: 'BODY' } });
  assert.equal(f.run('state.selectedId'), null);
  assert.equal(f.writes().length, 0);
});

test('latest list failure exposes retry instead of stale actions', async () => {
  const f = fixture();
  f.run('loadJobs()');
  await tick();
  f.list()[0].reject(new Error('offline'));
  await tick();
  assert.deepEqual(f.ids(), []);
  assert.equal(f.run('state.selectedId'), null);
  assert.match(f.nodes.get('#rows').innerHTML, /Retry/);
});

test('URL validation rejects malformed, active protocols and raw controls', () => {
  const f = fixture();
  for (const value of ['javascript:alert(1)', 'data:text/html,x', '/relative',
    'https://', 'https://example.com/\npath', '\thttps://example.com',
    'https://example.com/\u0000', 'https://example.com/\u007f']) {
    assert.equal(f.run(`safeUrl(${JSON.stringify(value)})`), null);
  }
  assert.equal(f.run("safeUrl('HTTPS://EXAMPLE.COM:443/a b')"), 'https://example.com/a%20b');
  assert.equal(f.run("safeUrl('http://example.com')"), 'http://example.com/');
});

test('posting URL is assigned as a property without injected HTML attributes', () => {
  const f = fixture();
  const malicious = 'https://example.com/" onclick="alert(1)';
  f.run(`renderDetail({...state.jobs[0], url: ${JSON.stringify(malicious)}})`);
  const link = f.nodes.get('#posting-link').children[0];
  assert.equal(link.tagName, 'A');
  assert.equal(link.href, new URL(malicious).href);
  assert.equal(link.target, '_blank');
  assert.equal(link.rel, 'noopener noreferrer');
  assert.equal(link.onclick, undefined);
  assert.doesNotMatch(f.nodes.get('#detail').innerHTML, /onclick=|href=/);
  f.run("renderDetail({...state.jobs[0], url:'javascript:alert(1)'})");
  const fallback = f.nodes.get('#posting-link').children[0];
  assert.equal(fallback.tagName, 'SPAN');
  assert.equal(fallback.href, undefined);
});

test('malicious IDs remain data and delegated actions use their exact value', () => {
  const f = fixture();
  const id = `bad'\" onclick=\"alert(1)`;
  f.run(`state.jobs[0].id = ${JSON.stringify(id)};
    detailCache.set(state.jobs[0].id, {...state.jobs[0]});
    reconcileSelection(state.jobs[0].id);`);
  assert.doesNotMatch(f.nodes.get('#rows').innerHTML, /data-id="bad'"/);
  assert.equal(f.nodes.get('#detail').dataset.id, id);
  assert.doesNotMatch(f.nodes.get('#detail').innerHTML, /onclick=/);
  f.nodes.get('#detail').listeners.click({ target: {
    closest: () => ({ dataset: { action: 'saved' } }),
  } });
  assert.equal(f.writes()[0].url, '/api/jobs/' + encodeURIComponent(id) + '/status');
});

test('information-bearing muted tokens are legible without opacity reduction', () => {
  assert.doesNotMatch(template, /(?:text|placeholder)-zinc-500|opacity-60|onclick="/);
  function luminance(hex) {
    return hex.match(/../g).map(x => parseInt(x, 16) / 255)
      .map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4)
      .reduce((sum, x, i) => sum + x * [.2126, .7152, .0722][i], 0);
  }
  for (const background of ['18181b', '09090b']) {
    assert.ok((luminance('a1a1aa') + .05) / (luminance(background) + .05) >= 4.5);
  }
});
