import assert from 'node:assert/strict';
import { test, beforeEach } from 'node:test';
import fs from 'node:fs';

// Small DOM double for the APIs this framework-free UI uses. No browser,
// managed hosts, external packages, or server process are required.
class Element {
  constructor(tag, text = '') {
    this.tagName = tag.toLowerCase(); this.children = []; this.attributes = {};
    this.style = {}; this.listeners = new Map(); this._text = text; this._value = undefined;
    this.className = ''; this.hidden = false;
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
      toggle: (name, enabled) => { if (enabled ?? !this.className.split(/\s+/).includes(name)) this.classList.add(name); else this.classList.remove(name); },
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  get isConnected() { return this === document.body || Boolean(this.parentElement?.isConnected); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name === 'value' && this.tagName !== 'textarea') this._value = String(value);
    if (name === 'readonly') this.readOnly = true;
    if (name === 'checked') this.checked = true;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  get id() { return this.attributes.id; }
  get value() { return this._value ?? (this.tagName === 'select' ? this.children.find(child => child.tagName === 'option')?.value ?? '' : ''); }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value ?? ''); }
  set innerHTML(value) { assert.equal(value, '', 'The UI must render data as text'); this.replaceChildren(); }
  appendChild(child) { if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(item => item !== child); child.parentElement = this; this.children.push(child); return child; }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ''; children.forEach(child => this.appendChild(child)); }
  addEventListener(name, listener) { const handlers = this.listeners.get(name) || []; handlers.push(listener); this.listeners.set(name, handlers); }
  async fire(name) { for (const listener of this.listeners.get(name) || []) await listener({ target: this, preventDefault() {} }); }
  focus() { document.activeElement = this; }
  select() {}
  scrollIntoView() {}
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    return this.tagName === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(item => item.trim()); const found = [];
    const walk = node => { for (const child of node.children) { if (selectors.some(item => child.matches(item))) found.push(child); walk(child); } };
    walk(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

globalThis.document = {
  body: null, activeElement: null, createElement: tag => new Element(tag), createTextNode: text => new Element('#text', text),
  getElementById(id) { return this.body.querySelector(`#${id}`); },
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
};
document.body = new Element('body');
globalThis.window = new EventTarget();
window.setTimeout = setTimeout;
globalThis.location = { hash: '#/estate', replace(value) { this.hash = value; window.dispatchEvent(new Event('hashchange')); } };
const storage = new Map();
globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) };
const response = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const mount = () => document.body.appendChild(new Element('main'));
const field = (container, title) => {
  const label = container.querySelectorAll('label').find(label => label.children[0]?.textContent === title);
  assert.ok(label, `Missing field ${title}`); return label.children[1];
};
const button = (container, text) => { const found = container.querySelectorAll('button').find(item => item.textContent === text); assert.ok(found, `Missing button ${text}`); return found; };

const api = await import('../webapp/static/api.js');
const actor = await import('../webapp/static/actor.js');
const { classifyStatus } = await import('../webapp/static/dom.js');
const { belongsToHost, newestFirst } = await import('../webapp/static/host_scope.js');
const { renderPlanList } = await import('../webapp/static/plans.js');
const { renderRecoveryList } = await import('../webapp/static/recovery_pages.js');
const { renderWorkspace } = await import('../webapp/static/workspace.js');
const { renderExecuteStage } = await import('../webapp/static/stages/execute.js');
const { renderReadinessStage } = await import('../webapp/static/stages/readiness.js');
const { createPageRenderer } = await import('../webapp/static/navigation.js');
const { startRun, pollRun, runToCompletion } = await import('../webapp/static/runs.js');
const { buildProcedure, PROCEDURE_ADAPTERS, REQUIRED_PRECHECKS } = await import('../webapp/static/procedure_adapters.js');

beforeEach(() => {
  document.body.replaceChildren(); document.activeElement = null; storage.clear();
  storage.set('opu-webapp-token', 'fixture-token'); api.setReadSignal(undefined); actor.setSessionIdentity(null); actor.setActor('fixture-operator');
  globalThis.fetch = async url => { throw new Error(`Unexpected fetch: ${url}`); };
});

test('typed status handling cannot turn failed or partial observations green', () => {
  for (const status of ['not running', 'not_running', 'unhealthy', 'inactive', 'abnormal', 'failed', 'blocked']) assert.equal(classifyStatus(status), 'bad', status);
  for (const status of ['incomplete', 'partially_collected', 'partial', 'unknown', 'pending']) assert.equal(classifyStatus(status), 'warn', status);
  assert.equal(classifyStatus('OPEN'), 'ok'); assert.equal(classifyStatus('node1 (Inactive)'), 'neutral');
  assert.equal(classifyStatus('unknown_success_text'), 'neutral');
});

test('host identity is explicit and chronological ordering ignores plan names', () => {
  assert.equal(belongsToHost({ host_id: 'prod-east', plan_id: 'prod-east-patch' }, 'prod'), false);
  assert.equal(belongsToHost({ plan_id: 'prod-change' }, 'prod'), false);
  assert.equal(belongsToHost({ host_id: 'prod', plan_id: 'CHG-123' }, 'prod'), true);
  assert.deepEqual(newestFirst([
    { plan_id: 'a-new', created_at: '2026-09-14T12:00:00Z' }, { plan_id: 'z-old', created_at: '2026-09-13T12:00:00Z' }, { plan_id: 'unknown' },
  ]).map(record => record.plan_id), ['a-new', 'z-old', 'unknown']);
});

test('plan index and host lifecycle show the newest attributable records', async () => {
  const plans = [
    { plan_id: 'z-old', host_id: 'prod', state: 'failed', created_at: '2026-09-13T00:00:00Z' },
    { plan_id: 'a-new', host_id: 'prod', state: 'succeeded', created_at: '2026-09-14T00:00:00Z' },
  ];
  const requests = [
    { request_id: 'foreign', host_id: 'other', state: 'completed', created_at: '2026-09-15T00:00:00Z' },
    { request_id: 'unattributed', state: 'completed' },
    { request_id: 'local', host_id: 'prod', state: 'awaiting_approval', created_at: '2026-09-13T00:00:00Z' },
  ];
  fetch = async url => response(url === '/api/plans' ? { plans } : url === '/api/recovery' ? { requests } : { steps: [] });
  const list = mount(); await renderPlanList(list);
  assert.match(list.querySelector('tbody').children[0].textContent, /^a-new/);
  const page = mount(); await renderWorkspace(page, 'prod', 'recovery');
  const stages = page.querySelectorAll('.stage-rail-item');
  assert.match(stages.find(item => item.textContent.startsWith('Plan')).textContent, /succeeded/);
  assert.match(stages.find(item => item.textContent.startsWith('Recovery')).textContent, /awaiting_approval/);
  const table = page.querySelector('tbody'); assert.match(table.textContent, /local/);
  assert.doesNotMatch(table.textContent, /foreign|unattributed/);
});

test('real list renderers surface authentication failures through the page boundary', async () => {
  fetch = async () => response({ error: 'unauthorized', message: 'Missing token' }, 401);
  for (const renderer of [renderPlanList, renderRecoveryList]) {
    const page = mount(); const render = createPageRenderer(page, renderer); await render();
    assert.match(page.textContent, /Authentication required/); assert.doesNotMatch(page.textContent, /undefined|TypeError/);
    assert.equal(page.querySelector('.error-box').getAttribute('role'), 'alert');
    assert.equal(document.activeElement.tagName, 'h2'); render.cancel();
  }
});

test('navigation aborts old reads and prevents stale results or errors replacing a new page', async () => {
  let release; let firstSignal; let call = 0;
  const page = mount();
  const render = createPageRenderer(page, async (view, signal) => {
    if (++call === 1) { firstSignal = signal; await new Promise(resolve => { release = resolve; }); view.textContent = 'stale'; throw new Error('stale failure'); }
    view.textContent = 'current';
  });
  const first = render(); await render(); assert.equal(firstSignal.aborted, true); release(); await first;
  assert.equal(page.textContent, 'current'); render.cancel();
});

test('API sends the acting identity only on writes and still joins an active run conflict', async () => {
  const calls = [];
  fetch = async (url, options) => { calls.push({ url, ...options }); return response(url === '/read' ? {} : { error: 'run_in_progress', run_id: 'same-run' }, url === '/read' ? 200 : 409); };
  await api.apiFetch('/read'); assert.equal(calls[0].headers['X-OPU-Actor'], undefined);
  assert.equal(await startRun('/operation', {}), 'same-run'); assert.equal(calls[1].headers['X-OPU-Actor'], 'fixture-operator');
  fetch = async () => response({ message: 'Not permitted' }, 403);
  await assert.rejects(api.apiFetch('/read'), error => error.status === 403 && error.message === 'Not permitted');
});

test('unknown outcomes and a navigation during launch cannot poll forever or report success', async () => {
  fetch = async () => response({ status: 'unknown' });
  await assert.rejects(pollRun('interrupted', { intervalMs: 0 }), /unknown outcome.*reconcile/);
  let release; let reads = 0; const controller = new AbortController(); api.setReadSignal(controller.signal);
  fetch = async (_url, options) => {
    if (options.method === 'POST') return new Promise(resolve => { release = () => resolve(response({ run_id: 'launched' })); });
    reads++; return response({ status: 'succeeded' });
  };
  const run = runToCompletion('/operation', {}); controller.abort(); api.setReadSignal(new AbortController().signal); release();
  await assert.rejects(run, error => error.name === 'AbortError'); assert.equal(reads, 0);
});

const artifact = { sha256: 'a'.repeat(64), patch_ids: ['12345678'], platforms: [{ id: '226' }], readme_files: [{ path: 'README.html', sha256: 'b'.repeat(64) }] };
const procedureFields = { patch_id: '12345678', platform_id: '226', database_unique_name: 'ORCL', required_opatch_version: '12.2.0.1.49', readme_identifier: 'README.html', rollback_precondition: 'Exact README rollback condition' };

test('all supported apply adapters produce coherent procedures with required gates and exact README binding', () => {
  const schema = JSON.parse(fs.readFileSync(new URL('../contracts/procedure/oracle-patch-procedure-v1.schema.json', import.meta.url)));
  assert.deepEqual(Object.keys(PROCEDURE_ADAPTERS).sort(), [...schema.properties.execution.properties.adapter.enum].sort());
  for (const name of Object.keys(PROCEDURE_ADAPTERS)) {
    const procedure = buildProcedure(name, { ...procedureFields, prechecks: '', postchecks: '' }, artifact);
    for (const check of REQUIRED_PRECHECKS) assert.ok(procedure.mandatory_prechecks.includes(check));
    assert.deepEqual(procedure.mandatory_postchecks, ['binary_inventory', 'service_health']);
    if (name.startsWith('grid_')) assert.equal('database_unique_name' in procedure.target, false);
  }
  const oop = buildProcedure('database_out_of_place_switch', procedureFields, artifact);
  assert.equal(oop.target.method, 'switch_home'); assert.equal(oop.rollback.mode, 'home_switch_back');
  const auto = buildProcedure('grid_rolling_opatchauto', { ...procedureFields, database_unique_name: '' }, artifact);
  assert.equal(auto.target.method, 'opatchauto'); assert.deepEqual(auto.execution.operations, ['grid_opatchauto_apply']);
  assert.throws(() => buildProcedure('database_rolling_opatch', { ...procedureFields, database_unique_name: '' }, artifact), /database unique name/);
  assert.throws(() => buildProcedure('grid_rolling_opatch', { ...procedureFields, readme_identifier: 'wrong.html' }, artifact), /match a hashed file/);
});

test('actual readiness form can submit every adapter and does not require a database for Grid', async () => {
  const steps = [{ step: 'discovery', done: true, evidence: { databases: [], collected_at: new Date().toISOString() } }, { step: 'artifact-inspect', done: true, status: 'ready_for_catalog', evidence: { artifact } }];
  for (const name of Object.keys(PROCEDURE_ADAPTERS)) {
    const posted = [];
    fetch = async (url, options) => {
      if (options.method === 'POST') { posted.push(JSON.parse(options.body)); return response({ run_id: 'validated' }); }
      return response(url.startsWith('/api/runs/') ? { status: 'succeeded' } : { steps });
    };
    const page = mount(); await renderReadinessStage(page, 'grid-host');
    const card = page.querySelectorAll('.step-card').find(card => card.querySelector('h3')?.textContent === 'Procedure validation');
    const adapter = field(card, 'Adapter'); adapter.value = name; await adapter.fire('change');
    field(card, 'Patch ID').value = '12345678'; field(card, 'Platform ID').value = '226';
    field(card, 'Required OPatch').value = '12.2.0.1.49'; field(card, 'README identifier').value = 'README.html';
    field(card, 'Rollback precondition').value = 'Exact README rollback condition';
    const db = field(card, 'Database unique name');
    if (name.startsWith('grid_')) { assert.equal(db.disabled, true); assert.equal(db.value, ''); }
    else db.value = 'ORCL';
    await button(card, 'Validate procedure').fire('click');
    assert.equal(posted.length, 1, `${name}: ${card.textContent}`);
    assert.equal(posted[0].procedure.execution.adapter, name);
  }
});

test('Execute preserves a failed operation while refreshing task and plan status', async () => {
  let state = 'execution_authorized';
  fetch = async (url, options) => {
    if (options.method === 'POST') { state = 'paused'; return response({ run_id: 'failure-run' }); }
    if (url.startsWith('/api/runs/')) return response({ status: 'failed', error: { message: 'Native prerequisite failed', stderr: 'OPatch conflict' } });
    if (url === '/api/plans') return response({ plans: [{ plan_id: 'CHG-42', host_id: 'prod', state }] });
    if (url.endsWith('/tasks')) return response({ tasks: [] });
    return response({ plan_id: 'CHG-42', state });
  };
  const page = mount(); await renderExecuteStage(page, 'prod');
  await button(page, 'Dispatch').fire('click');
  assert.match(page.textContent, /Native prerequisite failed/); assert.match(page.textContent, /OPatch conflict/);
  assert.match(page.textContent, /paused/); assert.equal(page.querySelector('.card-failure').getAttribute('role'), 'alert');
});

test('token changes refresh the real app and authenticated actor inputs cannot impersonate another principal', async () => {
  for (const id of ['app', 'rail-session', 'rail-hosts']) { const node = new Element('div'); node.setAttribute('id', id); document.body.appendChild(node); }
  let sessions = 0;
  fetch = async (url, options) => {
    if (url === '/api/session') { sessions++; const who = options.headers.Authorization === 'Bearer bob-token' ? 'bob' : 'alice'; return response({ actor: who, roles: ['operator'], rbac_enabled: true }); }
    return response({ hosts: [] });
  };
  await import('../webapp/static/app.js');
  const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
  await settle(); assert.equal(actor.getActor(), 'alice');
  const input = document.getElementById('session-acting-as'); assert.equal(input.readOnly, true);
  actor.setActor('mallory'); assert.equal(actor.getActor(), 'alice');
  api.setApiToken('bob-token'); await settle();
  assert.equal(actor.getActor(), 'bob'); assert.equal(input.value, 'bob'); assert.ok(sessions >= 2);
  assert.match(document.getElementById('rail-session').textContent, /Authenticated as bob/);
});
