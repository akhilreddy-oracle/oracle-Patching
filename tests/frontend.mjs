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
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
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
const { renderPlanList, renderPlanDetail, renderPlanNew, renderPlanDemoNew, renderRollbackNew } = await import('../webapp/static/plans.js');
const { renderRecoveryList, renderRecoveryDetail, renderRecoveryNew } = await import('../webapp/static/recovery_pages.js');
const { renderWorkspace } = await import('../webapp/static/workspace.js');
const { renderExecuteStage } = await import('../webapp/static/stages/execute.js');
const { renderDiscoverStage } = await import('../webapp/static/stages/discover.js');
const { renderPlanStage } = await import('../webapp/static/stages/plan.js');
const { renderReadinessStage } = await import('../webapp/static/stages/readiness.js');
const { renderRecoveryStage } = await import('../webapp/static/stages/recovery.js');
const { hydrateBackupPolicy, policyRecoveryBlock, backupPolicyChooser } = await import('../webapp/static/backup_policy.js');
const { createPageRenderer } = await import('../webapp/static/navigation.js');
const { renderValidation } = await import('../webapp/static/validation.js');
const { renderEstate } = await import('../webapp/static/estate.js');
const { startRun, pollRun, runToCompletion } = await import('../webapp/static/runs.js');
const { reconciliationCard } = await import('../webapp/static/run_reconciliation.js');
const { refreshSession, refreshHostNav } = await import('../webapp/static/shell.js');
const { executionWindow } = await import('../webapp/static/plan_window.js');
const { evidenceReport } = await import('../webapp/static/report_view.js');
const { executionConsole } = await import('../webapp/static/execution_console.js');
const { canInspectExtjob, extjobInspection } = await import('../webapp/static/extjob_inspection.js');
const { buildProcedure, PROCEDURE_ADAPTERS, REQUIRED_PRECHECKS } = await import('../webapp/static/procedure_adapters.js');

beforeEach(() => {
  document.body.replaceChildren(); document.activeElement = null; storage.clear();
  storage.set('opu-webapp-token', 'fixture-token'); api.setReadSignal(undefined); api.setSessionCsrf(null); actor.setSessionIdentity(null); actor.setActor('fixture-operator');
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
  fetch = async url => response(url === '/api/plans' ? { plans } : url === '/api/recovery' || url.startsWith('/api/recovery?host_id=prod') ? { requests } : { steps: [] });
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

test('discovery exposes authentication recovery instead of swallowing an expired session', async () => {
  fetch = async () => response({ message: 'Session expired' }, 401);
  const page = mount(); const render = createPageRenderer(page, view => renderDiscoverStage(view, 'prod'));
  await render();
  assert.match(page.textContent, /Authentication required/);
  assert.ok(page.querySelector('.auth-recovery-form'));
  render.cancel();
});

test('discovery refresh cannot retain green success when its evidence reload is missing or fails', async () => {
  actor.setSessionIdentity({ mode: 'lab', permissions: { live_discovery: true } });
  for (const reload of [response({ steps: [] }), response({ message: 'Evidence read failed' }, 503)]) {
    let pipelineReads = 0;
    fetch = async (url, options = {}) => {
      if (options.method === 'POST') return response({ run_id: 'discover-refresh' }, 202);
      if (url.startsWith('/api/runs/')) return response({ status: 'succeeded' });
      assert.equal(url, '/api/hosts/prod/pipeline');
      return ++pipelineReads === 1 ? response({ steps: [{ step: 'discovery', done: true, status: 'ok', evidence: { host: { name: 'previous-host' } } }] }) : reload;
    };
    const page = mount(); await renderDiscoverStage(page, 'prod');
    assert.equal(page.querySelector('#discover-status').textContent, 'ok');
    await button(page, 'Run live discovery').fire('click');
    const status = page.querySelector('#discover-status');
    assert.equal(status.classList.contains('is-ok'), false);
    assert.equal(status.textContent, reload.status === 503 ? 'unavailable' : 'no evidence');
    assert.doesNotMatch(page.textContent, /previous-host/);
    assert.equal(button(page, 'Run live discovery').disabled, false);
  }
});

test('discovery controls prevent denied or unavailable sessions from issuing a POST', async () => {
  const identities = [
    null,
    { mode: 'principal', actor: 'requester', roles: ['requester'], rbac_enabled: true, permissions: { live_discovery: false } },
    { mode: 'principal', actor: 'operator', roles: ['operator'], rbac_enabled: true, permissions: { live_discovery: false } },
    { mode: 'company', actor: 'requester', roles: ['requester'], rbac_enabled: true, csrf_token: 'fixture', expires_at: Date.now() / 1000 + 600, permissions: { live_discovery: false } },
    { mode: 'company', actor: 'operator', roles: ['operator'], rbac_enabled: true, csrf_token: 'fixture', expires_at: Date.now() / 1000 - 1, permissions: { live_discovery: true } },
  ];
  for (const session of identities) {
    actor.setSessionIdentity(session);
    const posts = [];
    fetch = async (url, options = {}) => {
      if (options.method === 'POST') posts.push(url);
      return response(url === '/api/estate' ? { hosts: [{ id: 'prod', status: 'pending' }] } : url === '/api/fleet' ? { databases: [] } : { steps: [] });
    };
    for (const [render, label] of [[renderEstate, 'Refresh live SSH'], [page => renderDiscoverStage(page, 'prod'), 'Run live discovery']]) {
      const page = mount(); await render(page); const control = button(page, label);
      assert.equal(control.disabled, true);
      const hint = page.querySelector('#' + control.getAttribute('aria-describedby'));
      assert.ok(hint.textContent); assert.equal(hint.hidden, false);
      await control.fire('click'); // Even a synthetic event cannot bypass the UI admission guard.
    }
    assert.deepEqual(posts, []);
  }
});

test('server-admitted discovery works for principal, company and lab sessions', async () => {
  for (const mode of ['principal', 'company', 'lab']) {
    actor.setSessionIdentity({ mode, actor: 'fixture-operator', rbac_enabled: mode !== 'lab', permissions: { live_discovery: true },
      csrf_token: 'fixture-csrf', expires_at: Date.now() / 1000 + 600 });
    for (const [render, label] of [[renderEstate, 'Refresh live SSH'], [page => renderDiscoverStage(page, 'prod'), 'Run live discovery']]) {
      const posts = [];
      fetch = async (url, options = {}) => {
        if (options.method === 'POST') { posts.push(url); return response({ run_id: 'discovery' }, 202); }
        if (url.startsWith('/api/runs/')) return response({ status: 'succeeded' });
        return response(url === '/api/estate' ? { hosts: [{ id: 'prod', status: 'pending' }] } : url === '/api/fleet' ? { databases: [] } : { steps: [] });
      };
      const page = mount(); await render(page); const control = button(page, label);
      assert.equal(control.disabled, false, mode);
      assert.equal(page.querySelector('#' + control.getAttribute('aria-describedby')).hidden, true);
      await control.fire('click');
      assert.deepEqual(posts, ['/api/hosts/prod/pipeline/discovery']);
      assert.equal(control.disabled, false);
    }
  }
});

test('authentication recovery exposes a blank password form and only replaces the credential on explicit sign-in', async () => {
  storage.set('opu-webapp-token', 'old-shared-token');
  fetch = async url => response(url === '/api/auth/config' ? { configured: false } : { message: 'Missing or invalid principal credential' }, url === '/api/auth/config' ? 200 : 401);
  const page = mount(); const render = createPageRenderer(page, renderPlanList); await render();
  const form = page.querySelector('.auth-recovery-form'); const input = form.querySelector('input');
  assert.equal(input.getAttribute('type'), 'password'); assert.equal(input.value, '');
  assert.equal(button(form, 'Sign in').getAttribute('type'), 'submit');
  input.value = ' personal-principal-token '; await input.fire('input');
  assert.equal(api.getApiToken(), 'old-shared-token');
  let changed = 0; const handler = () => { changed++; }; window.addEventListener(api.TOKEN_EVENT, handler);
  await form.fire('submit'); window.removeEventListener(api.TOKEN_EVENT, handler);
  assert.equal(api.getApiToken(), 'personal-principal-token'); assert.equal(changed, 1);
  assert.equal(input.value, ''); assert.doesNotMatch(page.textContent, /personal-principal-token|old-shared-token/);
  assert.equal(location.hash, '#/estate'); render.cancel();
});

test('authentication recovery retains company login and can retry an unchanged token', async () => {
  let attempts = 0;
  fetch = async () => response({ configured: true, login_url: '/api/auth/login', label: 'Company sign in' });
  const page = mount(); const render = createPageRenderer(page, async () => { attempts++; throw new api.ApiError({ message: 'Company sign in is required.' }, 401); });
  await render(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.querySelector('a').getAttribute('href'), '/api/auth/login');
  assert.match(page.textContent, /Company sign in is required/);
  const form = page.querySelector('form'); form.querySelector('input').value = api.getApiToken();
  await form.fire('submit'); assert.equal(attempts, 2); render.cancel();
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

test('cancelled execution view cannot start a native log refresh from a late saved response', async () => {
  const controller = new AbortController(); api.setReadSignal(controller.signal);
  let reads = 0, release; const posts = [];
  const data = { guidance: 'Original execution history', runs: [{ run_id: 'native-run', can_observe: true, status: 'running' }] };
  fetch = async (url, options = {}) => {
    if (options.method === 'POST') { posts.push(url); return response({ run_id: 'unexpected-observe' }); }
    assert.equal(url, '/api/plans/P1/execution');
    return ++reads === 1 ? response(data) : { ok: true, status: 200, json: () => new Promise(resolve => { release = resolve; }) };
  };
  const page = mount(); const panel = executionConsole('P1'); page.appendChild(panel);
  await new Promise(resolve => setImmediate(resolve));
  panel.querySelector('input').checked = true;
  const refresh = button(panel, 'Refresh timeline').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  controller.abort(); api.setReadSignal(new AbortController().signal);
  release({ ...data, guidance: 'Stale response' }); await refresh;
  assert.deepEqual(posts, []);
  assert.match(panel.textContent, /Original execution history/);
  assert.doesNotMatch(panel.textContent, /Stale response/);
});

test('replaced execution panel cannot start native log following while its page remains active', async () => {
  const controller = new AbortController(); api.setReadSignal(controller.signal);
  let reads = 0, release; const posts = [];
  const data = { guidance: 'Execution history', runs: [{ run_id: 'native-run', can_observe: true, status: 'running' }] };
  fetch = async (url, options = {}) => {
    if (options.method === 'POST') { posts.push(url); return response({ run_id: 'unexpected-observe' }); }
    assert.equal(url, '/api/plans/P1/execution');
    return ++reads === 1 ? response(data) : { ok: true, status: 200, json: () => new Promise(resolve => { release = resolve; }) };
  };
  const page = mount(); const panel = executionConsole('P1'); page.appendChild(panel);
  await new Promise(resolve => setImmediate(resolve));
  panel.querySelector('input').checked = true;
  const refresh = button(panel, 'Refresh timeline').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  panel.remove(); release(data); await refresh;
  assert.equal(controller.signal.aborted, false, 'a same-page panel refresh does not cancel the route');
  assert.deepEqual(posts, [], 'a detached follow widget must not start SSH observation');
  controller.abort();
});

test('obsolete estate discovery completion cannot clear the new active host or launch new reads', async () => {
  actor.setSessionIdentity({ mode: 'lab', permissions: { live_discovery: true } });
  const controller = new AbortController(); api.setReadSignal(controller.signal);
  const rail = document.body.appendChild(new Element('nav')); rail.setAttribute('id', 'rail-hosts');
  let release; const calls = [];
  const hosts = [{ id: 'prod', label: 'Production', status: 'ok' }];
  fetch = async (url, options = {}) => {
    calls.push([url, options.method || 'GET']);
    if (options.method === 'POST') {
      assert.equal(url, '/api/hosts/prod/pipeline/discovery');
      return new Promise(resolve => { release = () => resolve(response({ run_id: 'accepted-discovery' }, 202)); });
    }
    if (url === '/api/fleet') return response({ databases: [] });
    assert.equal(url, '/api/estate'); return response({ hosts });
  };
  const page = mount(); await renderEstate(page);
  const refresh = button(page, 'Refresh live SSH').fire('click');
  controller.abort(); api.setReadSignal(new AbortController().signal); page.remove();
  await refreshHostNav('prod');
  const count = calls.length; release(); await refresh;
  assert.equal(calls.length, count, 'old refresh must not poll or reload data under the new route identity');
  assert.equal(rail.querySelector('a').getAttribute('aria-current'), 'page');
});

test('host navigation ignores a cancelled response even when its JSON body completes late', async () => {
  const controller = new AbortController(); api.setReadSignal(controller.signal);
  const rail = document.body.appendChild(new Element('nav')); rail.setAttribute('id', 'rail-hosts');
  rail.textContent = 'Current host navigation';
  let release;
  fetch = async () => ({ ok: true, status: 200, json: () => new Promise(resolve => { release = resolve; }) });
  const pending = refreshHostNav(null);
  await new Promise(resolve => setImmediate(resolve));
  controller.abort(); api.setReadSignal(new AbortController().signal);
  release({ hosts: [{ id: 'obsolete', status: 'ok' }] });
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(rail.textContent, 'Current host navigation');
});

test('execution timeline retains the newest refresh when older responses arrive later', async () => {
  const pending = []; let reads = 0;
  fetch = async () => ++reads === 1 ? response({ guidance: 'Initial history' })
    : { ok: true, status: 200, json: () => new Promise(resolve => pending.push(resolve)) };
  const page = mount(); const panel = executionConsole('P1'); page.appendChild(panel);
  await new Promise(resolve => setImmediate(resolve));
  const first = button(panel, 'Refresh timeline').fire('click');
  const second = button(panel, 'Refresh timeline').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  pending[1]({ guidance: 'Current history' }); await second;
  pending[0]({ guidance: 'Stale history' }); await first;
  assert.match(panel.textContent, /Current history/);
  assert.doesNotMatch(panel.textContent, /Stale history/);
});

test('release validation stops loading after its response and retains every evidence level', async () => {
  let release;
  fetch = async url => {
    assert.equal(url, '/api/validation');
    return new Promise(resolve => { release = () => resolve(response({
      fixture_tested: { status: 'unknown', reason: 'Source changed after validation.' },
      live_lab_verified: { status: 'unverified', reason: 'No live lab evidence recorded.' },
      production_approved: { status: 'unverified', reason: 'No production approval recorded.' },
    })); });
  };
  const page = mount(); const render = createPageRenderer(page, renderValidation);
  const pending = render();
  assert.match(page.textContent, /Loading…/);
  assert.equal(page.querySelector('.route-view').getAttribute('aria-busy'), 'true');
  release(); await pending;
  assert.doesNotMatch(page.textContent, /Loading…/);
  assert.equal(page.querySelector('.route-view').getAttribute('aria-busy'), 'false');
  assert.deepEqual(page.querySelectorAll('h2').map(node => node.textContent), ['Fixture tests', 'Live lab verification', 'Production approval']);
  assert.deepEqual(page.querySelectorAll('.badge').map(node => node.textContent), ['unknown', 'unverified', 'unverified']);
  for (const reason of ['Source changed after validation.', 'No live lab evidence recorded.', 'No production approval recorded.']) assert.ok(page.textContent.includes(reason));
  assert.equal(page.querySelectorAll('.badge-ok').length, 0);
  render.cancel();
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

test('a cancelled run response cannot publish completion after another page is opened', async () => {
  const controller = new AbortController(); let release; const ticks = [];
  fetch = async () => ({ ok: true, status: 200, json: () => new Promise(resolve => { release = resolve; }) });
  const pending = pollRun('previous-page-run', { signal: controller.signal, onTick: record => ticks.push(record) });
  await new Promise(resolve => setImmediate(resolve));
  controller.abort(); release({ status: 'succeeded', run_id: 'previous-page-run' });
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.deepEqual(ticks, []);
});

test('a late authentication failure from cancelled navigation cannot clear the newer identity or CSRF', async () => {
  const previous = new AbortController(); api.setReadSignal(previous.signal);
  let release;
  fetch = async () => new Promise(resolve => { release = resolve; });
  const oldSession = refreshSession();
  previous.abort(); api.setReadSignal(new AbortController().signal);
  fetch = async () => response({ rbac_enabled: true, actor: 'new-operator', roles: ['operator'], csrf_token: 'new-fixture-csrf' });
  await refreshSession();
  release(response({ message: 'Old session expired' }, 401));
  await assert.rejects(oldSession, error => error.status === 401);
  assert.equal(actor.authenticatedActor(), 'new-operator');
  let headers;
  fetch = async (_url, options) => { headers = options.headers; return response({}); };
  await api.apiFetch('/fixture-write', { method: 'POST', body: '{}' });
  assert.equal(headers['X-CSRF-Token'], 'new-fixture-csrf');
  assert.equal(headers['X-OPU-Actor'], 'new-operator');
});

test('creation pages open the submitted record even if its editable ID changes while the run finishes', async () => {
  const cases = [
    [renderPlanNew, 'source', 'Plan ID', 'Create plan', '/api/plans', 'plan_id', 'plans'],
    [renderPlanDemoNew, null, 'Plan ID', 'Build fixture and create plan', '/api/plans/testmode-demo', 'plan_id', 'plans'],
    [renderRollbackNew, 'source-plan', 'Rollback plan ID', 'Create rollback plan', '/api/plans/source-plan/create-rollback', 'plan_id', 'plans'],
    [renderPlanStage, 'source', 'Plan ID', 'Create plan', '/api/plans', 'plan_id', 'plans'],
    [renderRecoveryNew, null, 'Request ID', 'Build fixture and create request', '/api/recovery/testmode-demo', 'request_id', 'recovery'],
  ];
  const procedure = buildProcedure('database_single_instance_opatch', procedureFields, artifact);
  for (const [renderer, target, label, action, route, key, index] of cases) {
    let finish; const posted = []; location.hash = '#/estate';
    fetch = async (url, options) => {
      if (options.method === 'POST') { posted.push({ url, body: JSON.parse(options.body) }); return response({ run_id: 'create-run' }, 202); }
      if (url === '/api/runs/create-run') return new Promise(resolve => { finish = () => resolve(response({ status: 'succeeded' })); });
      if (url.endsWith('/pipeline') || url.endsWith('/plan-preview')) return response({ confirmation: {
        expected_creation_binding_sha256: 'c'.repeat(64), patch_id: procedure.patch_id,
        database: procedure.target.database_unique_name,
      }, steps: [
        { step: 'artifact-inspect', done: true, evidence: { artifact } },
        { step: 'procedure-validate', done: true, status: 'ready_for_planning', evidence: { procedure } },
        { step: 'readiness-evaluate', done: true, status: 'ready_for_approval', evidence: { status: 'ready_for_approval', valid_until: '2099-01-01T00:00:00Z' } },
      ] });
      return response({ plans: [], tasks: [] });
    };
    const page = mount(); await renderer(page, target);
    if (renderer === renderPlanNew) {
      const reviewedTarget = page.querySelector('.wizard-target');
      assert.match(reviewedTarget.textContent, /Host: source/);
      assert.match(reviewedTarget.textContent, /Patch: 12345678/);
      assert.match(reviewedTarget.textContent, /Database: ORCL/);
    }
    const id = field(page, label); id.value = 'submitted-record';
    const pending = button(page, action).fire('click');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(typeof finish, 'function', `${renderer.name} should have submitted its run`);
    id.value = 'edited-after-submit'; finish(); await pending;
    assert.equal(posted.length, 1);
    assert.equal(posted[0].url, route); assert.equal(posted[0].body[key], 'submitted-record');
    assert.equal(location.hash, `#/${index}/submitted-record`, renderer.name);
  }
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
    assert.match(card.textContent, /support non-CDB databases only; CDB\/PDB patching is unavailable/);
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

const procedureCard = page => page.querySelectorAll('.step-card').find(card => card.querySelector('h3')?.textContent === 'Procedure validation');
const readinessSteps = (media = artifact, saved = null, input = null, databases = [{ db_unique_name: 'ORCL' }]) => [
  { step: 'discovery', done: true, evidence: { databases } },
  { step: 'artifact-inspect', done: true, status: 'ready_for_catalog', evidence: { artifact: media } },
  { step: 'procedure-validate', done: Boolean(saved), status: saved ? 'ready_for_planning' : null, evidence: saved ? { procedure: saved } : null, input },
];

test('expired or undated readiness cannot appear current in the workspace or offer a plan handoff', async () => {
  for (const valid_until of [undefined, '2000-01-01T00:00:00Z', 'bad timestamp', '2099-01-01T00:00:00', '2099-02-30T00:00:00Z']) {
    const steps = readinessSteps();
    steps.push({ step: 'readiness-evaluate', done: true, status: 'ready_for_approval', evidence: { status: 'ready_for_approval', valid_until } });
    fetch = async url => response(url.endsWith('/pipeline') ? { steps } : { plans: [], requests: [] });
    const page = mount(); await renderWorkspace(page, 'prod', 'readiness');
    const state = page.querySelectorAll('.stage-rail-item').find(item => item.textContent.startsWith('Readiness')).querySelector('.badge');
    assert.equal(state.textContent, 'refresh required');
    assert.equal(state.classList.contains('is-ok'), false);
    const card = page.querySelector('#readiness-step-readiness-evaluate');
    assert.equal(card.querySelector('.badge').textContent, 'refresh required');
    assert.match(card.textContent, /Refresh evidence and evaluate readiness again/);
    assert.equal(card.querySelectorAll('a').some(link => link.textContent === 'Continue to Plan →'), false);
  }
});

test('plan creation rechecks readiness expiry after review before issuing any POST', async () => {
  const procedure = buildProcedure('database_single_instance_opatch', procedureFields, artifact);
  const steps = readinessSteps(artifact, procedure);
  const observedAt = Date.parse('2030-01-01T01:00:00Z');
  const expiresAt = observedAt + 60_000;
  steps.push({ step: 'readiness-evaluate', done: true, status: 'ready_for_approval', evidence: { status: 'ready_for_approval', valid_until: new Date(expiresAt).toISOString() } });
  const posts = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') posts.push(url);
    return response(url.endsWith('/plan-preview') ? { steps, confirmation: {
      expected_creation_binding_sha256: 'c'.repeat(64), patch_id: procedure.patch_id, database: procedure.target.database_unique_name,
    } } : { plans: [] });
  };
  const originalNow = Date.now;
  try {
    Date.now = () => observedAt;
    const page = mount(); await renderPlanStage(page, 'prod');
    const submit = button(page, 'Create plan');
    assert.equal(submit.disabled, false);
    Date.now = () => expiresAt;
    await submit.fire('click');
    assert.deepEqual(posts, []);
    assert.equal(submit.disabled, true);
    assert.match(page.textContent, /Readiness expired at/);
    const reloaded = mount(); await renderPlanStage(reloaded, 'prod');
    assert.equal(button(reloaded, 'Create plan').disabled, true);
  } finally { Date.now = originalNow; }
});
const readmeHint = (media = artifact, identifier = 'README.html') => ({
  artifact_sha256: media.sha256, readme_identifier: identifier,
  readme_sha256: media.readme_files.find(entry => entry.path === identifier).sha256,
  required_opatch_version: '12.2.0.1.49', evidence: 'Use OPatch utility version 12.2.0.1.49 or later.', warnings: [],
});

test('workspace follows saved procedure and readiness changes without recovery reads or replacing policy drafts', async () => {
  const media = { ...artifact };
  const saved = buildProcedure('database_single_instance_opatch', procedureFields, media);
  const steps = readinessSteps(media, saved, null, [
    { db_unique_name: 'ORCL', oracle_home: '/oracle/one' }, { db_unique_name: 'OTHER', oracle_home: '/oracle/two' },
  ]);
  steps[0].status = 'complete'; steps[0].evidence.collected_at = new Date().toISOString();
  steps.push(...['reconcile', 'compatibility-collect', 'compatibility-reconcile'].map(step => ({ step, done: true, status: 'passed' })));
  const ready = { step: 'readiness-evaluate', done: true, status: 'blocked', evidence: { status: 'blocked', gates: [] } };
  steps.push(ready);
  const reads = [], posts = [];
  fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push(url);
      if (url.endsWith('/procedure-validate')) {
        steps[2].evidence.procedure = JSON.parse(options.body).procedure;
        ready.done = false; ready.status = null; ready.evidence = null;
      } else if (url.endsWith('/readiness-evaluate')) {
        ready.done = true; ready.status = 'ready_for_approval'; ready.evidence = { status: ready.status, valid_until: '2099-01-01T00:00:00Z' };
      } else assert.fail(`Unexpected mutation ${url}`);
      return response({ run_id: 'workflow-refresh' }, 202);
    }
    reads.push(url);
    if (url === '/api/runs/workflow-refresh') return response({ status: 'succeeded' });
    if (url === '/api/hosts/prod/pipeline') return response({ steps });
    if (url === '/api/plans') return response({ plans: [] });
    if (url === '/api/recovery?host_id=prod&view=saved') return response({ requests: [{ host_id: 'prod', state: 'completed', evidence_mode: 'saved' }] });
    assert.fail(`Unexpected read ${url}`);
  };
  const page = mount(); await renderWorkspace(page, 'prod', 'readiness');
  const readinessBadge = page.querySelectorAll('.stage-rail-item').find(item => item.textContent.startsWith('Readiness')).querySelector('.badge');
  assert.equal(readinessBadge.textContent, 'blocked');
  assert.match(page.querySelector('.wizard-target').textContent, /Database: ORCL.*Patch: 12345678/);
  assert.match(page.querySelector('.stage-rail').textContent, /Saved: completed/);
  const policyDraft = field(page, 'Filesystem free space reserve (GiB)');
  policyDraft.value = '321'; await policyDraft.fire('input');
  const card = procedureCard(page);
  field(card, 'Database unique name').value = 'OTHER';
  await button(card, 'Validate procedure').fire('click');
  assert.match(page.querySelector('.wizard-target').textContent, /Database: OTHER.*Patch: 12345678/);
  assert.match(page.querySelector('.wizard-target').textContent, /Oracle home: \/oracle\/two/);
  assert.equal(readinessBadge.textContent, '5/6');
  assert.equal(field(page, 'Filesystem free space reserve (GiB)'), policyDraft);
  assert.equal(policyDraft.value, '321');
  await button(page, 'Evaluate readiness').fire('click');
  assert.equal(readinessBadge.textContent, 'ready_for_approval');
  assert.deepEqual(posts, ['/api/hosts/prod/pipeline/procedure-validate', '/api/hosts/prod/pipeline/readiness-evaluate']);
  assert.deepEqual(reads.filter(url => url.startsWith('/api/recovery')), ['/api/recovery?host_id=prod&view=saved']);
});

test('readiness blocker reviews focus exact controls without reload, mutation or draft loss', async () => {
  const steps = readinessSteps();
  steps.push({ step: 'readiness-evaluate', done: true, status: 'blocked', evidence: { status: 'blocked', gates: [
    { name: 'artifact', status: 'blocker' }, { name: 'compatibility_contract', status: 'blocker' },
    { name: 'patch_not_installed', status: 'blocker' }, { name: 'database_invalid_objects', status: 'blocker' },
  ] } });
  let reads = 0;
  fetch = async (url, options = {}) => { assert.equal(options.method || 'GET', 'GET'); assert.equal(url, '/api/hosts/prod/pipeline'); reads++; return response({ steps }); };
  const page = mount(); await renderReadinessStage(page, 'prod');
  const draft = field(procedureCard(page), 'Rollback precondition'); draft.value = 'Preserve this unsubmitted condition';
  for (const [label, step] of [['Review patch media', 'artifact-inspect'], ['Review compatibility checks', 'compatibility-collect'],
    ['Review installed patch and selection', 'procedure-validate'], ['Review readiness controls', 'readiness-evaluate']]) {
    await page.querySelectorAll('a').find(link => link.textContent === label).fire('click');
    assert.equal(document.activeElement, page.querySelector(`#readiness-step-${step}`).querySelector('h3'));
    assert.equal(field(procedureCard(page), 'Rollback precondition'), draft);
    assert.equal(draft.value, 'Preserve this unsubmitted condition');
  }
  assert.equal(reads, 1);
});

test('procedure form restores saved validation and marks edits as an unvalidated draft', async () => {
  const saved = buildProcedure('database_single_instance_opatch', { ...procedureFields, required_opatch_version: '12.2.0.1.51' }, artifact);
  const steps = readinessSteps(artifact, saved, saved, [{ db_unique_name: 'OTHER' }]);
  fetch = async url => response(url.includes('/procedure-hints?') ? readmeHint() : { steps });
  const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
  assert.equal(field(card, 'Required OPatch').value, '12.2.0.1.51');
  assert.equal(field(card, 'Rollback precondition').value, procedureFields.rollback_precondition);
  assert.equal(field(card, 'Adapter').value, 'database_single_instance_opatch');
  assert.equal(field(card, 'README identifier').value, 'README.html');
  assert.equal(card.querySelector('.badge').textContent, 'Saved: ready_for_planning');
  const version = field(card, 'Required OPatch'); version.value = '12.2.0.1.52'; await version.fire('input');
  assert.equal(card.querySelector('.badge').textContent, 'Draft · not validated');
  assert.equal(card.querySelector('.badge').classList.contains('badge-ok'), false);
  version.value = '12.2.0.1.51'; await version.fire('input');
  assert.equal(card.querySelector('.badge').textContent, 'Saved: ready_for_planning');
  await button(card, 'Autofill from artifact').fire('click');
  assert.equal(version.value, '12.2.0.1.51');
  assert.equal(field(card, 'Database unique name').value, 'ORCL');
  assert.equal(field(card, 'Rollback precondition').value, procedureFields.rollback_precondition);
  assert.match(card.textContent, /Verified README.html: minimum OPatch 12.2.0.1.49/);
  assert.equal(card.querySelector('.form-error').textContent, '');
});

test('resubmitting a saved workflow retains supporting references and recovery mode', async () => {
  const saved = buildProcedure('database_single_instance_opatch', procedureFields, artifact);
  const supplement = { kind: 'mos_note', identifier: 'Approved change recovery instructions', sha256: 'c'.repeat(64) };
  saved.oracle_references.push(supplement); saved.rollback.mode = 'manual_recovery';
  const posted = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { posted.push(JSON.parse(options.body)); return response({ run_id: 'validation' }); }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded' } : { steps: readinessSteps(artifact, saved) });
  };
  const page = mount(); await renderReadinessStage(page, 'prod');
  await button(procedureCard(page), 'Validate procedure').fire('click');
  assert.deepEqual(posted[0].procedure, saved);
  const card = procedureCard(page); const adapter = field(card, 'Adapter');
  adapter.value = 'database_out_of_place_switch'; await adapter.fire('change');
  await button(card, 'Validate procedure').fire('click');
  assert.equal(posted[1].procedure.rollback.mode, 'home_switch_back');
  assert.equal(posted[1].procedure.oracle_references.length, 1);
  assert.deepEqual(posted[1].procedure.execution.operations, PROCEDURE_ADAPTERS.database_out_of_place_switch.operations);
});

test('last submitted procedure inputs stay unvalidated and stale saved bindings are not restored', async () => {
  const saved = buildProcedure('database_single_instance_opatch', procedureFields, artifact);
  const variants = [
    { steps: readinessSteps(artifact, null, saved), expected: procedureFields.required_opatch_version, text: /last submitted draft/ },
    { steps: readinessSteps({ ...artifact, sha256: 'c'.repeat(64) }, saved, saved), expected: '', text: /different artifact evidence/ },
    { steps: readinessSteps({ ...artifact, readme_files: [{ path: 'README.html', sha256: 'd'.repeat(64) }] }, saved), expected: '', text: /different artifact evidence/ },
    { steps: readinessSteps({ ...artifact, readme_files: [{ path: 'other.html', sha256: 'b'.repeat(64) }] }, saved), expected: '', text: /different artifact evidence/ },
  ];
  for (const variant of variants) {
    fetch = async () => response({ steps: variant.steps });
    const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
    assert.equal(field(card, 'Required OPatch').value, variant.expected);
    assert.equal(card.querySelector('.badge').textContent, 'Draft · not validated');
    assert.match(card.textContent, variant.text);
  }
});

test('autofill uses verified README hints and reports every missing required field including rollback', async () => {
  const media = { ...artifact, classification_evidence: 'OPatch utility version 99.99.99.99' };
  let hintReads = 0;
  fetch = async url => {
    if (url.includes('/procedure-hints?')) { hintReads++; return response(readmeHint(media)); }
    return response({ steps: readinessSteps(media) });
  };
  const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
  await button(card, 'Autofill from artifact').fire('click');
  assert.equal(hintReads, 1); assert.equal(field(card, 'Required OPatch').value, '12.2.0.1.49');
  assert.equal(field(card, 'Rollback precondition').value, '');
  assert.match(card.querySelector('.form-error').textContent, /Rollback precondition from the README/);
  assert.equal(card.querySelector('.badge').textContent, 'Draft · not validated');

  const empty = { ...artifact, patch_ids: [], platforms: [], readme_files: [] };
  fetch = async () => response({ steps: readinessSteps(empty, null, null, []) });
  const emptyPage = mount(); await renderReadinessStage(emptyPage, 'prod'); const emptyCard = procedureCard(emptyPage);
  await button(emptyCard, 'Autofill from artifact').fire('click');
  for (const label of ['Patch ID', 'Platform ID', 'README identifier', 'Required OPatch', 'Rollback precondition', 'Database unique name']) {
    assert.ok(emptyCard.querySelector('.form-error').textContent.includes(label), label);
  }
  await button(emptyCard, 'Validate procedure').fire('click');
  assert.match(emptyCard.querySelector('.form-error').textContent, /Platform ID.*Rollback precondition.*Database unique name/);
});

test('autofill requires explicit choices for ambiguous databases, patch media, platforms and READMEs', async () => {
  const media = {
    ...artifact, patch_ids: ['12345678', '23456789'], platforms: [{ id: '226' }, { id: '23' }],
    readme_files: [...artifact.readme_files, { path: 'subpatch/README.html', sha256: 'd'.repeat(64) }],
  };
  let hintReads = 0;
  fetch = async url => {
    if (url.includes('/procedure-hints?')) { hintReads++; return response(readmeHint(media, decodeURIComponent(url.split('=')[1]))); }
    return response({ steps: readinessSteps(media, null, null, [{ db_unique_name: 'FIRST' }, { db_unique_name: 'SECOND' }]) });
  };
  const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
  await button(card, 'Autofill from artifact').fire('click');
  for (const title of ['Patch ID', 'Platform ID', 'Database unique name', 'README identifier']) assert.equal(field(card, title).value, '', title);
  assert.equal(hintReads, 0); assert.match(card.textContent, /Choose a database: FIRST, SECOND/);
  const readme = field(card, 'README identifier'); readme.value = 'subpatch/README.html'; await readme.fire('change');
  field(card, 'Database unique name').value = 'SECOND';
  await button(card, 'Autofill from artifact').fire('click');
  assert.equal(hintReads, 1); assert.equal(field(card, 'Database unique name').value, 'SECOND');
  assert.equal(field(card, 'Required OPatch').value, '12.2.0.1.49');
  readme.value = 'README.html'; await readme.fire('change');
  assert.equal(field(card, 'Required OPatch').value, '', 'A hint from another README must not follow the new selection');
});

test('README verification rejects stale digest bindings and preserves edits made during a slow request', async () => {
  let hints = { ...readmeHint(), readme_sha256: 'e'.repeat(64) }; let releaseHint;
  fetch = async url => url.includes('/procedure-hints?')
    ? response(hints)
    : response({ steps: readinessSteps() });
  const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
  await button(card, 'Autofill from artifact').fire('click');
  assert.equal(field(card, 'Required OPatch').value, '');
  assert.match(card.querySelector('.form-error').textContent, /do not match the inspected artifact/);
  fetch = async url => url.includes('/procedure-hints?')
    ? new Promise(resolve => { releaseHint = () => resolve(response(readmeHint())); })
    : response({ steps: readinessSteps() });
  const pending = button(card, 'Autofill from artifact').fire('click');
  while (!releaseHint) await new Promise(resolve => setImmediate(resolve));
  const version = field(card, 'Required OPatch'); version.value = '12.2.0.1.51'; await version.fire('input');
  releaseHint(); await pending; assert.equal(version.value, '12.2.0.1.51');
  version.value = ''; await version.fire('input'); releaseHint = null;
  const changed = button(card, 'Autofill from artifact').fire('click');
  while (!releaseHint) await new Promise(resolve => setImmediate(resolve));
  const readme = field(card, 'README identifier'); readme.value = ''; await readme.fire('change');
  releaseHint(); await changed;
  assert.equal(version.value, ''); assert.match(card.textContent, /README selection changed during verification/);
});

test('failed procedure validation retains entered values without restoring an earlier successful badge', async () => {
  const saved = buildProcedure('database_single_instance_opatch', procedureFields, artifact);
  fetch = async (url, options) => {
    if (options.method === 'POST') return response({ run_id: 'failed-validation' });
    if (url.startsWith('/api/runs/')) return response({ status: 'failed', error: { message: 'README validation failed' } });
    return response({ steps: readinessSteps(artifact, saved) });
  };
  const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
  await button(card, 'Validate procedure').fire('click');
  assert.equal(card.querySelector('.badge').textContent, 'Draft · not validated');
  assert.match(card.textContent, /README validation failed/);
  const version = field(card, 'Required OPatch'); await version.fire('input');
  assert.equal(version.value, procedureFields.required_opatch_version);
  assert.equal(card.querySelector('.badge').classList.contains('badge-ok'), false);
});

test('an artifact change while editing cannot silently rebind a saved procedure or keep its success badge', async () => {
  const saved = buildProcedure('database_single_instance_opatch', procedureFields, artifact);
  let currentArtifact = artifact; let writes = 0;
  fetch = async (_url, options) => { if (options.method === 'POST') writes++; return response({ steps: readinessSteps(currentArtifact, saved) }); };
  const page = mount(); await renderReadinessStage(page, 'prod'); const card = procedureCard(page);
  currentArtifact = { ...artifact, sha256: 'f'.repeat(64) };
  await button(card, 'Validate procedure').fire('click');
  assert.equal(writes, 0); assert.equal(card.querySelector('.badge').textContent, 'Draft · not validated');
  assert.match(card.querySelector('.form-error').textContent, /Artifact evidence changed.*Reload Readiness/);
});

const openWindow = () => ({ start: new Date(Date.now() - 60000).toISOString(), end: new Date(Date.now() + 3600000).toISOString() });

test('execution window rejects missing, impossible and closed bounds and preserves the native 30 second limit', () => {
  const now = Date.parse('2030-01-01T12:00:00Z');
  const plan = (start, end) => ({ maintenance_window: { start, end } });
  assert.equal(executionWindow({}, now).status, 'unknown');
  assert.equal(executionWindow(plan('2030-02-30T01:00:00Z', '2030-03-05T01:00:00Z'), now).status, 'unknown');
  assert.equal(executionWindow(plan('2030-01-01T11:00:00Z', '2030-01-01T12:00:00Z'), now).status, 'closed');
  assert.equal(executionWindow(plan('2030-01-01T11:00:00Z', '2030-01-01T12:00:29Z'), now).status, 'closing');
  assert.equal(executionWindow(plan('2030-01-01T11:00:00Z', '2030-01-01T12:00:30Z'), now).open, true);
  assert.equal(executionWindow(plan('2030-01-01T13:00:00Z', '2030-01-01T14:00:00Z'), now).status, 'upcoming');
});

test('paused plans with expired or absent windows preserve completed tasks and cannot submit retry', async () => {
  for (const bounds of [undefined, { start: '2020-01-01T00:00:00Z', end: '2020-01-01T01:00:00Z' }]) {
    const writes = [];
    const plan = { plan_id: 'paused-plan', host_id: 'prod', state: 'paused', maintenance_window: bounds };
    fetch = async (url, options) => {
      if (options.method === 'POST') { writes.push(url); throw new Error('Unexpected retry'); }
      if (url === '/api/plans') return response({ plans: [plan] });
      if (url.endsWith('/tasks')) return response({ tasks: [{ task_id: '002-apply', stage: 'apply', status: 'succeeded' }, { task_id: '005-final', stage: 'final_validate', status: 'failed' }] });
      return response(plan);
    };
    const page = mount(); await renderExecuteStage(page, 'prod');
    const retry = button(page, 'Retry 005-final');
    assert.equal(retry.disabled, true);
    await retry.fire('click');
    assert.deepEqual(writes, []);
    assert.match(page.textContent, /maintenance window/);
    assert.match(page.querySelector('tbody').textContent, /002-apply.*succeeded/);
  }
});

test('execution controls recheck window expiry after the page has rendered', async t => {
  let now = Date.parse('2030-01-01T12:00:00Z');
  t.mock.method(Date, 'now', () => now);
  const plan = { plan_id: 'timed-plan', state: 'running', maintenance_window: { start: '2030-01-01T11:00:00Z', end: '2030-01-01T13:00:00Z' } };
  const writes = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { writes.push(url); throw new Error('Unexpected execution'); }
    return response(url.endsWith('/tasks') ? { tasks: [] } : plan);
  };
  const page = mount(); await renderPlanDetail(page, plan.plan_id);
  assert.equal(button(page, 'Execute next task').disabled, false);
  now = Date.parse('2030-01-01T13:00:00Z');
  await button(page, 'Execute next task').fire('click');
  assert.deepEqual(writes, []);
  assert.match(page.textContent, /maintenance window closed/);
});

test('evidence report shows saved failure diagnostics safely and does not offer a repair action', async () => {
  fetch = async () => response({ comparison: [], after_scope: 'Final validation incomplete', interpretation: 'Saved evidence only',
    evidence: { tasks: [{ task_id: '005-final', status: 'failed', finished_at: '2026-09-14T18:23:42Z', postcondition: { detail: 'final_validate failed' },
      failure_diagnostics: [{ text: 'extjob uid=54321 mode=0755 <script>not executable</script>' }], next_action: 'Verify binary provenance before a supported repair.' }] } });
  const page = mount(); page.appendChild(evidenceReport('p'));
  await button(page, 'View before and after report').fire('click');
  assert.match(page.textContent, /Saved failure: 005-final/);
  assert.match(page.textContent, /uid=54321 mode=0755/);
  assert.match(page.textContent, /not a new database check/);
  assert.equal(page.querySelectorAll('script').length, 0);
  assert.equal(page.querySelectorAll('button').some(item => /repair/i.test(item.textContent)), false);
});

test('Execute preserves a failed operation while refreshing task and plan status', async () => {
  let state = 'execution_authorized';
  fetch = async (url, options) => {
    if (options.method === 'POST') { state = 'paused'; return response({ run_id: 'failure-run' }); }
    if (url.startsWith('/api/runs/')) return response({ status: 'failed', error: { message: 'Native prerequisite failed', stderr: 'OPatch conflict' } });
    if (url === '/api/plans') return response({ plans: [{ plan_id: 'CHG-42', host_id: 'prod', state }] });
    if (url.endsWith('/tasks')) return response({ tasks: [] });
    return response({ plan_id: 'CHG-42', state, maintenance_window: openWindow() });
  };
  const page = mount(); await renderExecuteStage(page, 'prod');
  await button(page, 'Dispatch').fire('click');
  assert.match(page.textContent, /Native prerequisite failed/); assert.match(page.textContent, /OPatch conflict/);
  assert.match(page.textContent, /paused/); assert.equal(page.querySelector('.card-failure').getAttribute('role'), 'alert');
});

test('unknown run preserves native diagnostics and offers only reconciliation until verified', async () => {
  const writes = [];
  let terminal = false;
  const unknown = { run_id: 'lost-run', status: 'unknown', context: { task_id: '003-validate' },
    error: { message: 'No verified task result', stderr: 'another Oracle operation owns the host lock' } };
  fetch = async (url, options) => {
    if (options.method === 'POST') {
      writes.push({ url, body: JSON.parse(options.body) });
      assert.equal(url, '/api/runs/lost-run/reconcile');
      return response(terminal ? { status: 'succeeded' } : unknown);
    }
    if (url.endsWith('/tasks')) return response({ tasks: [{ task_id: '003-validate', stage: 'validate', status: 'pending' }] });
    return response({ plan_id: 'P1', state: 'running', maintenance_window: openWindow(), unresolved_run: terminal ? null : unknown });
  };
  const page = mount(); await renderPlanDetail(page, 'P1');
  assert.match(page.textContent, /003-validate/);
  assert.match(page.textContent, /owns the host lock/);
  assert.equal(button(page, 'Execute next task').disabled, true);
  assert.equal(button(page, 'Execute remaining tasks').disabled, true);
  await button(page, 'Execute next task').fire('click'); assert.equal(writes.length, 0);
  await button(page, 'Inspect and reconcile').fire('click');
  assert.deepEqual(writes[0].body, { actor: 'fixture-operator' });
  assert.equal(button(page, 'Execute next task').disabled, true);
  terminal = true;
  await button(page, 'Inspect and reconcile').fire('click');
  assert.match(page.textContent, /reconciled as succeeded/);
  assert.equal(button(page, 'Execute next task').disabled, false);
  assert.equal(writes.length, 2); // Reconciliation never launches the next task.
});

test('an unknown poll retains run identity and the host Execute page offers reconciliation after reload', async () => {
  const unknown = { run_id: 'lost-run', status: 'unknown', context: { task_id: '003-validate' }, error: { message: 'Native validation did not claim its task' } };
  fetch = async () => response(unknown);
  await assert.rejects(pollRun('lost-run', { intervalMs: 0 }), error => error.runId === 'lost-run' && error.record === unknown);
  const writes = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { writes.push(url); return response(unknown); }
    if (url === '/api/plans') return response({ plans: [{ plan_id: 'P1', state: 'running', host_id: 'prod' }] });
    if (url.endsWith('/tasks')) return response({ tasks: [] });
    return response({ plan_id: 'P1', state: 'running', unresolved_run: unknown });
  };
  const page = mount(); await renderExecuteStage(page, 'prod');
  assert.equal(button(page, 'Execute remaining').disabled, true);
  await button(page, 'Execute remaining').fire('click'); assert.equal(writes.length, 0);
  await button(page, 'Inspect and reconcile').fire('click');
  assert.deepEqual(writes, ['/api/runs/lost-run/reconcile']);
});

const hostLockUnknown = () => ({
  run_id: 'aaaaaaaaaaaa', status: 'unknown',
  context: { plan_id: 'P1', task_id: '003-validate-source', detached_execution: true },
  error: { message: 'remote exit exists but sealed task has no verified terminal result',
    stderr: 'opu-agent: another Oracle executor owns this host or its lock file is unsafe',
    result: { exit_code: 75, task_status: 'pending' } },
});
const eligibleHostLock = () => ({
  status: 'eligible', recovery_eligible: true, blockers: [],
  plan_id: 'P1', task_id: '003-validate-source', execution_run_id: 'aaaaaaaaaaaa',
  target: { database_unique_name: 'SALES' },
});

test('inherited host lock requires inspection and explicit recovery, then refreshes without executing a task', async () => {
  const writes = []; const refreshed = [];
  let posts = 0;
  fetch = async (url, options) => {
    if (options.method === 'POST') {
      writes.push({ url, body: JSON.parse(options.body) }); posts++;
      return response({ run_id: posts === 1 ? 'bbbbbbbbbbbb' : 'cccccccccccc' }, 202);
    }
    if (url === '/api/runs/bbbbbbbbbbbb') return response({ status: 'succeeded', result: eligibleHostLock() });
    if (url === '/api/runs/cccccccccccc') return response({ status: 'succeeded', result: { status: 'completed', execution_run_id: 'aaaaaaaaaaaa', maintenance_run_id: 'cccccccccccc' } });
    throw new Error(`Unexpected fetch: ${url}`);
  };
  const page = mount(); page.appendChild(reconciliationCard({ run_id: 'aaaaaaaaaaaa', record: hostLockUnknown() }, next => refreshed.push(next)));
  assert.equal(page.querySelectorAll('button').some(item => item.textContent === 'Recover inherited lock'), false);
  await button(page, 'Inspect host lock').fire('click');
  assert.deepEqual(writes, [{ url: '/api/plans/P1/lock-inspect', body: { actor: 'fixture-operator', run_id: 'aaaaaaaaaaaa' } }]);
  assert.match(page.textContent, /controlled restart.*SALES database and listener/);
  assert.match(page.textContent, /Validation stays pending/);
  assert.equal(refreshed.length, 0);
  await button(page, 'Recover inherited lock').fire('click');
  assert.deepEqual(writes[1], { url: '/api/plans/P1/lock-recover', body: { actor: 'fixture-operator', run_id: 'aaaaaaaaaaaa' } });
  assert.equal(writes.length, 2);
  assert.equal(refreshed[0].title, 'Inherited host lock recovered');
  assert.match(refreshed[0].message, /Validation remains pending/);
  assert.equal(refreshed[0].run_id, undefined);
});

test('native lock blockers and mismatched eligibility never expose recovery', async () => {
  for (const report of [
    { ...eligibleHostLock(), status: 'blocked', recovery_eligible: false, blockers: ['Unexpected non-Oracle lock holder 123'] },
    { ...eligibleHostLock(), execution_run_id: 'dddddddddddd' },
    { ...eligibleHostLock(), recovery_eligible: false },
    { ...eligibleHostLock(), blockers: ['Plan seal changed'] },
  ]) {
    const writes = [];
    fetch = async (url, options) => {
      if (options.method === 'POST') { writes.push(url); return response({ run_id: 'bbbbbbbbbbbb' }, 202); }
      return response({ status: 'succeeded', result: report });
    };
    const page = mount(); page.appendChild(reconciliationCard({ run_id: 'aaaaaaaaaaaa', record: hostLockUnknown() }, () => assert.fail('blocked inspection should not execute or refresh')));
    await button(page, 'Inspect host lock').fire('click');
    assert.match(page.textContent, /Host lock recovery is blocked/);
    if (report.blockers.length) assert.ok(page.textContent.includes(report.blockers[0]));
    assert.equal(page.querySelectorAll('button').some(item => item.textContent === 'Recover inherited lock'), false);
    assert.deepEqual(writes, ['/api/plans/P1/lock-inspect']);
  }
});

test('lock recovery controls require the pending validation lock rejection and suppress repeat recovery', () => {
  for (const mutate of [
    record => { record.context.task_id = '002-apply-source'; },
    record => { record.error.result.task_status = 'running'; },
    record => { record.error.stderr = 'Unrelated native error'; },
    record => { delete record.context.plan_id; },
    record => { record.context.lock_recovery_run_id = 'cccccccccccc'; },
  ]) {
    const record = hostLockUnknown(); mutate(record);
    const page = mount(); page.appendChild(reconciliationCard({ run_id: record.run_id, record }, () => {}));
    assert.equal(page.querySelectorAll('button').some(item => item.textContent === 'Inspect host lock'), false);
    assert.equal(page.querySelectorAll('button').some(item => item.textContent === 'Recover inherited lock'), false);
    button(page, 'Inspect and reconcile');
  }
});

test('unknown maintenance preserves its run and offers reconciliation without another restart', async () => {
  let refreshed; const writes = [];
  const maintenance = { run_id: 'cccccccccccc', status: 'unknown', context: { lock_recovery: true, plan_id: 'P1', original_run_id: 'aaaaaaaaaaaa' }, error: { message: 'Maintenance wrapper has no verified result' } };
  fetch = async (url, options) => {
    if (options.method === 'POST') {
      writes.push(url);
      if (url.endsWith('/lock-inspect')) return response({ run_id: 'bbbbbbbbbbbb' }, 202);
      if (url.endsWith('/lock-recover')) return response({ run_id: 'cccccccccccc' }, 202);
      if (url === '/api/runs/cccccccccccc/reconcile') return response({ status: 'succeeded' });
      throw new Error(`Unexpected write: ${url}`);
    }
    return response(url.endsWith('bbbbbbbbbbbb') ? { status: 'succeeded', result: eligibleHostLock() } : maintenance);
  };
  const page = mount(); page.appendChild(reconciliationCard({ run_id: 'aaaaaaaaaaaa', record: hostLockUnknown() }, next => { refreshed = next; }));
  await button(page, 'Inspect host lock').fire('click');
  await button(page, 'Recover inherited lock').fire('click');
  assert.equal(refreshed.run_id, 'cccccccccccc');
  assert.equal(refreshed.record, maintenance);
  page.replaceChildren(reconciliationCard(refreshed, next => { refreshed = next; }));
  assert.match(page.textContent, /Lock recovery outcome needs reconciliation/);
  assert.equal(page.querySelectorAll('button').some(item => item.textContent === 'Recover inherited lock'), false);
  await button(page, 'Inspect and reconcile').fire('click');
  assert.match(refreshed.message, /original execution and pending validation/);
  assert.deepEqual(writes, ['/api/plans/P1/lock-inspect', '/api/plans/P1/lock-recover', '/api/runs/cccccccccccc/reconcile']);
});

test('host lock operations retain token and actor requirements', async () => {
  const page = mount(); page.appendChild(reconciliationCard({ run_id: 'aaaaaaaaaaaa', record: hostLockUnknown() }, () => {}));
  storage.delete('opu-webapp-token');
  await button(page, 'Inspect host lock').fire('click');
  assert.match(page.textContent, /token/i);
  storage.set('opu-webapp-token', 'fixture-token');
  field(page, 'Actor').value = '';
  await button(page, 'Inspect host lock').fire('click');
  assert.match(page.textContent, /actor/i);
});

test('token changes refresh the real app permissions and authenticated actor inputs cannot impersonate another principal', async () => {
  location.hash = '#/estate';
  for (const id of ['app', 'rail-session', 'rail-hosts']) { const node = new Element('div'); node.setAttribute('id', id); document.body.appendChild(node); }
  let sessions = 0;
  fetch = async (url, options) => {
    if (url === '/api/session') {
      sessions++; const who = options.headers.Authorization === 'Bearer bob-token' ? 'bob' : 'alice';
      return response({ actor: who, mode: 'principal', roles: [who === 'bob' ? 'operator' : 'requester'], rbac_enabled: true,
        permissions: { live_discovery: who === 'bob' } });
    }
    return response(url === '/api/estate' ? { hosts: [{ id: 'prod', status: 'pending' }] } : url === '/api/fleet' ? { databases: [] } : { steps: [] });
  };
  await import('../webapp/static/app.js');
  const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
  await settle(); assert.equal(actor.getActor(), 'alice');
  assert.equal(button(document.getElementById('app'), 'Refresh live SSH').disabled, true);
  const input = document.getElementById('session-acting-as'); assert.equal(input.readOnly, true);
  actor.setActor('mallory'); assert.equal(actor.getActor(), 'alice');
  api.setApiToken('bob-token'); await settle();
  assert.equal(actor.getActor(), 'bob'); assert.equal(input.value, 'bob'); assert.ok(sessions >= 2);
  assert.match(document.getElementById('rail-session').textContent, /Authenticated as bob/);
  assert.equal(button(document.getElementById('app'), 'Refresh live SSH').disabled, false);
  location.hash = '#/hosts/prod/discover'; window.dispatchEvent(new Event('hashchange')); await settle();
  assert.equal(button(document.getElementById('app'), 'Run live discovery').disabled, false);
  api.setApiToken('alice-token'); await settle();
  assert.equal(actor.getActor(), 'alice');
  assert.equal(button(document.getElementById('app'), 'Run live discovery').disabled, true);
  location.hash = '#/estate'; window.dispatchEvent(new Event('hashchange')); await settle();
});


const filesystemPolicy = {
  schema_version: '1.0', maximum_snapshot_age_seconds: 3600, require_xml_inventory: false,
  recovery: { require_backup: true, max_backup_age_minutes: 90, minimum_fra_free_bytes: 0,
    require_guaranteed_restore_point: false, storage_mode: 'filesystem', capacity_basis: 'rman_unused_blocks',
    minimum_filesystem_free_bytes: 10 * 1024 ** 3, require_distinct_backup_storage: false },
  database: { require_primary_read_write: true, maximum_invalid_objects: 2 },
};
const recoveryCapability = (hostId = 'prod') => ({
  requests: [], live_available: true, supported_adapter: 'standalone_primary_noarchivelog_spfile',
  validation_level: 'fixture_tested', restore_validation: 'RMAN RESTORE DATABASE VALIDATE; not a separate test restore',
  target_capability_context: { host_id: hostId },
  target_capabilities: [{ database: 'ORCL', status: 'needs_native_analysis', can_create: true, requirements: [], blockers: [] }],
});
const recoverySteps = (policy = filesystemPolicy) => [
  ...['discovery', 'reconcile', 'artifact-inspect', 'procedure-validate', 'compatibility-collect', 'compatibility-reconcile'].map(step => ({
    step, done: true, evidence: step === 'discovery' ? { collected_at: new Date().toISOString(), databases: [{ db_unique_name: 'ORCL' }] } : {},
  })),
  { step: 'readiness-evaluate', done: false, input: policy },
];

test('server recovery policy overrides a stale browser waiver and filesystem selection always retains backup', async () => {
  storage.set('opu-webapp-backup-policy:prod', 'waive');
  hydrateBackupPolicy('prod', filesystemPolicy);
  assert.equal(policyRecoveryBlock('prod').require_backup, true);
  assert.equal(policyRecoveryBlock('prod').capacity_basis, 'rman_unused_blocks');
  const chooser = backupPolicyChooser('prod');
  const mode = field(chooser, 'Backup storage');
  mode.value = 'fra'; await mode.fire('change');
  mode.value = 'filesystem'; await mode.fire('change');
  assert.equal(policyRecoveryBlock('prod').require_backup, true);
  const waive = chooser.querySelector('#backup-waive-prod');
  assert.equal(waive.disabled, true);
  waive.checked = true; await waive.fire('change');
  assert.equal(policyRecoveryBlock('prod').require_backup, true);
  assert.equal(waive.checked, false);
  assert.equal(policyRecoveryBlock('prod').minimum_filesystem_free_bytes, 10 * 1024 ** 3);
  hydrateBackupPolicy('prod', null);
  assert.equal(policyRecoveryBlock('prod').storage_mode, 'fra');
  assert.equal(policyRecoveryBlock('prod').require_backup, true);
});

test('readiness submits the saved full policy and blocks an invalid filesystem reserve', async () => {
  const posted = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { posted.push(JSON.parse(options.body)); return response({ run_id: 'readiness' }); }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded' } : { steps: recoverySteps() });
  };
  storage.set('opu-webapp-backup-policy:prod', 'waive');
  const page = mount(); await renderReadinessStage(page, 'prod');
  assert.equal(field(page, 'Backup storage').value, 'filesystem');
  assert.equal(field(page, 'Max snapshot age (s)').value, '3600');
  assert.equal(field(page, 'Require XML inventory').checked, false);
  const reserve = field(page, 'Filesystem free space reserve (GiB)');
  reserve.value = ''; await reserve.fire('input');
  await button(page, 'Evaluate readiness').fire('click');
  assert.equal(posted.length, 0);
  assert.match(page.textContent, /reserve must be a nonnegative/);
  reserve.value = '10'; await reserve.fire('input');
  await button(page, 'Evaluate readiness').fire('click');
  assert.deepEqual(posted[0].policy, filesystemPolicy);
});

test('live recovery form uses the selected host, discovered database and session actor with explicit UTC window', async () => {
  const calls = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { calls.push({ url, body: JSON.parse(options.body) }); return response({ run_id: 'create-recovery' }, 202); }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded', result: { mode: 'live' } } : url === '/api/recovery?host_id=prod' ? recoveryCapability() : { steps: recoverySteps() });
  };
  const page = mount(); await renderRecoveryStage(page, 'prod');
  assert.match(page.textContent, /Execution stops this database/);
  assert.equal(field(page, 'Requester').value, 'fixture-operator');
  assert.equal(field(page, 'Database').value, 'ORCL');
  field(page, 'Request ID').value = 'CHG-live-001';
  field(page, 'Backup parent directory').value = '/u02/backup';
  field(page, 'Window start (UTC)').value = '2030-01-01T01:00:00';
  field(page, 'Window end (UTC)').value = '2030-01-01T05:00:00Z';
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(calls.length, 0); assert.match(page.textContent, /Use UTC timestamps/);
  field(page, 'Window start (UTC)').value = '2030-01-01T01:00:00Z';
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(calls.length, 1); assert.equal(calls[0].url, '/api/recovery');
  assert.deepEqual(calls[0].body, {
    request_id: 'CHG-live-001', requester: 'fixture-operator', host_id: 'prod', database: 'ORCL', backup_parent: '/u02/backup',
    window_start: '2030-01-01T01:00:00Z', window_end: '2030-01-01T05:00:00Z', policy: filesystemPolicy,
  });
  assert.equal(location.hash, '#/recovery/CHG-live-001');
});

async function recoveryAuthenticationForm(session, writeStatus = 202) {
  const writes = [];
  fetch = async (url, options) => {
    if (url === '/api/session') return response(session || { message: 'Company session expired; sign in again' }, session ? 200 : 401);
    if (options.method === 'POST') {
      writes.push({ url, headers: options.headers, body: JSON.parse(options.body) });
      return response(writeStatus === 202 ? { run_id: 'create-recovery' } : { message: 'Company session expired; sign in again' }, writeStatus);
    }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded', result: { mode: 'live' } }
      : url === '/api/recovery?host_id=prod' ? recoveryCapability() : { steps: recoverySteps() });
  };
  if (session) await refreshSession();
  const page = mount(); await renderRecoveryStage(page, 'prod');
  field(page, 'Request ID').value = 'company-backup';
  field(page, 'Backup parent directory').value = '/u02/backup';
  field(page, 'Window start (UTC)').value = '2030-01-01T01:00:00Z';
  field(page, 'Window end (UTC)').value = '2030-01-01T05:00:00Z';
  return { page, writes };
}

const companySession = () => ({ mode: 'company', rbac_enabled: true, actor: 'sso-fixture-user',
  roles: ['requester'], csrf_token: 'fixture-company-csrf', expires_at: Date.now() / 1000 + 300 });

test('company session submits recovery without a lab token and includes its bound actor and CSRF', async () => {
  storage.delete('opu-webapp-token');
  const { page, writes } = await recoveryAuthenticationForm(companySession());
  assert.equal(field(page, 'Requester').readOnly, true);
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url, '/api/recovery');
  assert.equal(writes[0].body.requester, 'sso-fixture-user');
  assert.equal(writes[0].headers['X-OPU-Actor'], 'sso-fixture-user');
  assert.equal(writes[0].headers['X-CSRF-Token'], 'fixture-company-csrf');
  assert.equal(writes[0].headers.Authorization, undefined);
});

test('expired or incomplete company session cannot submit recovery even with a leftover API token', async () => {
  for (const changes of [{ expires_at: Date.now() / 1000 - 1 }, { csrf_token: '' }, { rbac_enabled: false }]) {
    const { page, writes } = await recoveryAuthenticationForm({ ...companySession(), ...changes });
    await button(page, 'Create live recovery request').fire('click');
    assert.equal(writes.length, 0);
    assert.match(page.textContent, /Company session expired or unavailable/);
  }
});

test('lost company authentication clears cached admission and server rejection remains authoritative', async () => {
  storage.delete('opu-webapp-token');
  const { page, writes } = await recoveryAuthenticationForm(companySession(), 401);
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(writes.length, 1);
  assert.match(page.textContent, /Company session expired; sign in again/);
  fetch = async () => response({ message: 'Company session expired; sign in again' }, 401);
  await assert.rejects(refreshSession(), /Company session expired/);
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(writes.length, 1);
  assert.match(page.textContent, /Sign in with your company account/);
});

test('anonymous and lab recovery forms continue to require an API token', async () => {
  storage.delete('opu-webapp-token');
  const { page, writes } = await recoveryAuthenticationForm(null);
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(writes.length, 0);
  assert.match(page.textContent, /Sign in with your company account, or enter an API token/);
  storage.set('opu-webapp-token', 'fixture-lab-token');
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].headers.Authorization, 'Bearer fixture-lab-token');
  assert.equal(writes[0].headers['X-CSRF-Token'], undefined);
});

test('recovery approval requires a displayed passed analysis and a separate approver', async () => {
  let request = { request_id: 'live-1', host_id: 'prod', mode: 'live', state: 'awaiting_approval', requester: 'fixture-operator' };
  const calls = [];
  const analysis = { status: 'blocked', reason: 'insufficient backup capacity', capacity: { required_bytes: 20 * 1024 ** 3, available_bytes: 10 * 1024 ** 3, capacity_basis: 'allocated' } };
  fetch = async (url, options) => {
    if (options.method === 'POST') { calls.push(url); return response({ run_id: 'analysis' }); }
    if (url.startsWith('/api/runs/')) {
      request.analysis = structuredClone(analysis);
      return response({ status: 'succeeded', result: analysis });
    }
    return response(request);
  };
  const page = mount(); await renderRecoveryDetail(page, 'live-1');
  assert.equal(button(page, 'Approve').disabled, true);
  await button(page, 'Analyze recovery').fire('click');
  assert.match(page.textContent, /insufficient backup capacity/);
  assert.match(page.textContent, /Required capacity: 20.00 GiB/);
  assert.match(page.textContent, /Available capacity: 10.00 GiB/);
  assert.equal(button(page, 'Approve').disabled, true);
  analysis.status = 'passed'; analysis.reason = null;
  await button(page, 'Refresh analysis').fire('click');
  assert.equal(button(page, 'Approve').disabled, false);
  field(page, 'Ticket').value = 'CHG-1';
  await button(page, 'Approve').fire('click');
  assert.match(page.textContent, /requester cannot approve their own/);
  assert.equal(calls.filter(url => url.endsWith('/approve')).length, 0);
});

test('failed recovery analysis refresh clears previously passed approval evidence', async () => {
  const request = { request_id: 'live-1', host_id: 'prod', mode: 'live', state: 'awaiting_approval', requester: 'another-requester', analysis: { status: 'passed', reason: null } };
  const calls = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') {
      calls.push(url);
      request.analysis = null;
      return response({ run_id: 'failed-analysis' });
    }
    return response(url.startsWith('/api/runs/') ? { status: 'failed', error: 'Capacity probe timed out' } : request);
  };
  const page = mount(); await renderRecoveryDetail(page, 'live-1');
  assert.equal(button(page, 'Approve').disabled, false);
  await button(page, 'Refresh analysis').fire('click');
  assert.equal(button(page, 'Approve').disabled, true);
  assert.match(page.textContent, /Run analysis and resolve its findings before approval/);
  await button(page, 'Approve').fire('click');
  assert.equal(calls.filter(url => url.endsWith('/approve')).length, 0);
});

test('recovery analysis cannot relaunch while a running or unknown operation awaits resolution', async () => {
  const posts = [];
  for (const status of ['pending', 'queued', 'running', 'unknown']) {
    for (const analysis of [null, { status: 'passed' }]) {
      fetch = async (url, options = {}) => {
        if (options.method === 'POST') posts.push(url);
        return response({ request_id: 'live-1', state: 'awaiting_approval', mode: 'live', analysis,
          latest_run: { run_id: 'existing-run', status } });
      };
      const page = mount(); await renderRecoveryDetail(page, 'live-1');
      const analyze = button(page, analysis ? 'Refresh analysis' : 'Analyze recovery');
      assert.equal(analyze.disabled, true, status);
      await analyze.fire('click');
      assert.match(page.textContent, /Existing operation must finish or be reconciled/);
      assert.equal(page.querySelectorAll('button').some(node => node.textContent === 'Approve'), false);
    }
  }
  assert.deepEqual(posts, []);
});

test('live recovery execution describes real downtime and fixtures are never offered as host planning evidence', async () => {
  fetch = async () => response({ request_id: 'live-1', host_id: 'prod', mode: 'live', state: 'authorized', authorization: { actor: 'fixture-operator' } });
  const page = mount(); await renderRecoveryDetail(page, 'live-1');
  assert.match(page.textContent, /Database downtime is required/);
  assert.doesNotMatch(page.textContent, /\(fake\)|TEST_MODE executes/);
  const requests = [
    { request_id: 'test-1', host_id: 'prod', mode: 'test_mode', state: 'completed' },
    { request_id: 'live-1', host_id: 'prod', mode: 'live', state: 'completed' },
  ];
  const posts = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { posts.push({ url, body: JSON.parse(options.body) }); return response({ run_id: 'collect' }); }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded', result: { status: 'passed' } } : url === '/api/recovery?host_id=prod' ? { ...recoveryCapability(), requests } : { steps: recoverySteps() });
  };
  const stage = mount(); await renderRecoveryStage(stage, 'prod');
  assert.equal(stage.querySelectorAll('button').filter(item => item.textContent === 'Validate for patch planning').length, 1);
  await button(stage, 'Validate for patch planning').fire('click');
  assert.deepEqual(posts, [{ url: '/api/hosts/prod/pipeline/recovery-collect', body: { request_id: 'live-1' } }]);
});


test('live recovery remains unavailable unless the server explicitly exposes the capability', async () => {
  let writes = 0;
  fetch = async (url, options) => {
    if (options.method === 'POST') writes++;
    return response(url === '/api/recovery?host_id=prod' ? { requests: [] } : { steps: recoverySteps() });
  };
  const page = mount(); await renderRecoveryStage(page, 'prod');
  assert.match(page.textContent, /Live recovery has not been enabled on this server/);
  assert.equal(button(page, 'Create live recovery request').disabled, true);
  assert.equal(field(page, 'Backup parent directory').disabled, true);
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(writes, 0);
});

test('failed or interrupted backup selection removes the old planning handoff', async () => {
  for (const outcome of ['failed', 'unknown', 'poll-error', 'unconfirmed-success']) {
    let started = false;
    fetch = async (url, options = {}) => {
      if (options.method === 'POST') { started = true; return response({ run_id: 'replacement' }); }
      if (url.startsWith('/api/runs/')) {
        if (outcome === 'poll-error') throw new Error('Connection lost');
        return response({ run_id: 'replacement', status: outcome === 'unconfirmed-success' ? 'succeeded' : outcome,
          result: { status: 'passed' }, error: { message: 'Backup validation failed' } });
      }
      if (url === '/api/recovery?host_id=prod') return response({ ...recoveryCapability(), requests: [
        { request_id: 'backup-B', host_id: 'prod', mode: 'live', state: 'completed' },
      ] });
      const steps = recoverySteps();
      steps.find(step => step.step === 'readiness-evaluate').recovery_selection = started ? null : { request_id: 'backup-A', host_id: 'prod' };
      return response({ steps });
    };
    const page = mount(); await renderRecoveryStage(page, 'prod');
    const summary = page.querySelector('.recovery-selection');
    assert.match(summary.textContent, /Selected request: backup-A/, outcome);
    assert.equal(summary.querySelectorAll('a').length, 1, outcome);
    await button(page, 'Validate for patch planning').fire('click');
    assert.doesNotMatch(summary.textContent, /Selected request: backup-A/, outcome);
    assert.match(summary.textContent, /Backup selection is not confirmed/, outcome);
    assert.equal(summary.querySelectorAll('a').length, 0, outcome);
  }
});

function inspectionFixture() {
  const plan = { plan_id: 'P1', plan_sha256: 'a'.repeat(64), intent: 'patch_apply', state: 'paused', nodes: ['source'],
    procedure: { adapter: 'database_single_instance_opatch' }, sod: { operator: 'fixture-operator' },
    maintenance_window: { start: '2020-01-01T00:00:00Z', end: '2020-01-01T01:00:00Z' } };
  const tasks = [['001-precheck-source', 'precheck'], ['002-apply-source', 'apply'], ['003-validate-source', 'validate'],
    ['004-datapatch-local', 'datapatch'], ['005-final-validate-local', 'final_validate']].map(([task_id, stage], index) =>
    ({ task_id, stage, status: index === 4 ? 'failed' : 'succeeded', evidence_sha256: 'b'.repeat(64), task_result_sha256: 'c'.repeat(64) }));
  const report = { plan_id: 'P1', plan_sha256: plan.plan_sha256, actor: 'fixture-operator', read_only: true, mutation_authorized: false,
    status: 'blocked', error: 'Archive not protected <script>untrusted</script>', current: { uid: 54321, mode: '0755', sha256: 'd'.repeat(64) },
    authority: { plan_state: 'paused', final_task_id: tasks[4].task_id, final_task_status: 'failed', final_task_result_sha256: tasks[4].task_result_sha256,
      native_datapatch_evidence_sha256: tasks[3].evidence_sha256, native_apply_evidence_sha256: tasks[1].evidence_sha256 } };
  const record = { run_id: 'a'.repeat(12), kind: 'extjob_inspect', key: 'plan:P1:execute', status: 'succeeded', result: report };
  return { plan, tasks, record };
}

function inspectionRead(url, { plan, tasks, record }) {
  if (url.endsWith('/execution')) return response({ runs: [record] });
  if (url.endsWith('/tasks')) return response({ tasks });
  if (url === '/api/plans/P1') return response(plan);
  if (url === `/api/runs/${record.run_id}`) return response(record);
  assert.fail(`Unexpected inspection read: ${url}`);
}

test('extjob inspection admits only the complete standalone chain and exact final validation states', () => {
  const { plan, tasks } = inspectionFixture();
  assert.equal(canInspectExtjob(plan, tasks), true);
  assert.equal(canInspectExtjob({ ...plan, state: 'running' }, tasks), false);
  assert.equal(canInspectExtjob({ ...plan, intent: 'patch_rollback' }, tasks), false);
  assert.equal(canInspectExtjob(plan, tasks.slice(1)), false);
  assert.equal(canInspectExtjob(plan, tasks.map((task, index) => index === 2 ? { ...task, status: 'running' } : task)), false);
  assert.equal(canInspectExtjob({ ...plan, state: 'running' }, tasks.map((task, index) => index === 4 ? { ...task, status: 'pending' } : task)), true);
});

test('paused extjob inspection uses read-only route after window expiry and reports blocked comparison honestly', async () => {
  const { plan, tasks, record } = inspectionFixture(); const writes = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { writes.push({ url, body: JSON.parse(options.body) }); return response({ run_id: record.run_id }); }
    return inspectionRead(url, { plan, tasks, record });
  };
  const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
  await button(page, 'Inspect extjob (read-only)').fire('click');
  assert.deepEqual(writes, [{ url: '/api/plans/P1/extjob-inspect', body: { actor: 'fixture-operator' } }]);
  assert.match(page.textContent, /Comparison blocked/);
  assert.match(page.textContent, /UID 54321 · mode 0755/);
  assert.equal(page.querySelectorAll('script').length, 0);
  assert.equal(page.querySelectorAll('button').some(item => /repair|retry|apply/i.test(item.textContent)), false);
});

test('extjob card blocks unresolved execution and a different authorizer before any write', async () => {
  const { plan, tasks } = inspectionFixture(); let writes = 0;
  fetch = async () => { writes++; throw new Error('Unexpected request'); };
  const locked = mount(); locked.appendChild(extjobInspection('P1', plan, tasks, true));
  assert.equal(button(locked, 'Inspect extjob (read-only)').disabled, true);
  await button(locked, 'Inspect extjob (read-only)').fire('click');
  const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
  field(page, 'Inspection actor').value = 'someone-else';
  await button(page, 'Inspect extjob (read-only)').fire('click');
  assert.match(page.textContent, /must match the sealed plan authorizer/);
  assert.equal(writes, 0);
});

test('saved extjob inspections reload without launching work and stale final task evidence is labelled historical', async () => {
  const { plan, tasks, record } = inspectionFixture(); const writes = [];
  let serverTasks = structuredClone(tasks);
  fetch = async (url, options) => {
    if (options.method === 'POST') writes.push(url);
    return inspectionRead(url, { plan, tasks: serverTasks, record });
  };
  const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
  await button(page, 'Load saved inspection').fire('click');
  assert.match(page.textContent, /UID 54321/);
  serverTasks[4].task_result_sha256 = 'e'.repeat(64);
  await button(page, 'Load saved inspection').fire('click');
  assert.equal(tasks[4].task_result_sha256, 'c'.repeat(64), 'Captured page tasks remain unchanged while server state advances');
  assert.match(page.textContent, /Historical or incomplete authority/);
  assert.doesNotMatch(page.textContent, /Observed extjob: UID/);
  assert.match(page.textContent, /Saved inspection JSON/);
  assert.doesNotMatch(page.textContent, /Plan execution and completed tasks are unchanged/);
  assert.deepEqual(writes, []);
});

test('incomplete or malformed matching inspection hashes cannot establish current authority', async () => {
  for (const field of ['plan', 'final', 'apply', 'datapatch']) {
    for (const value of [undefined, null, '', 'short', 'g'.repeat(64)]) {
      document.body.replaceChildren(); actor.setActor('fixture-operator');
      const { plan, tasks, record } = inspectionFixture();
      if (field === 'plan') record.result.plan_sha256 = plan.plan_sha256 = value;
      if (field === 'final') record.result.authority.final_task_result_sha256 = tasks[4].task_result_sha256 = value;
      if (field === 'apply') record.result.authority.native_apply_evidence_sha256 = tasks[1].evidence_sha256 = value;
      if (field === 'datapatch') record.result.authority.native_datapatch_evidence_sha256 = tasks[3].evidence_sha256 = value;
      fetch = async url => inspectionRead(url, { plan, tasks, record });
      const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
      await button(page, 'Load saved inspection').fire('click');
      assert.match(page.textContent, /Historical or incomplete authority/);
      assert.doesNotMatch(page.textContent, /Observed extjob: UID/);
      assert.match(page.textContent, /Saved inspection JSON/);
    }
  }
});

test('a newly completed inspection refreshes plan and tasks and detects execution advancing during collection', async () => {
  const { plan, tasks, record } = inspectionFixture(); const requests = [];
  const completedPlan = { ...plan, state: 'succeeded' };
  const completedTasks = tasks.map(task => ({ ...task, status: 'succeeded' }));
  fetch = async (url, options) => {
    requests.push(url);
    if (options.method === 'POST') return response({ run_id: record.run_id }, 202);
    return inspectionRead(url, { plan: completedPlan, tasks: completedTasks, record });
  };
  const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
  await button(page, 'Inspect extjob (read-only)').fire('click');
  assert.ok(requests.includes('/api/plans/P1'));
  assert.ok(requests.includes('/api/plans/P1/tasks'));
  assert.match(page.textContent, /Historical or incomplete authority/);
  assert.doesNotMatch(page.textContent, /Observed extjob: UID/);
  assert.equal(button(page, 'Inspect extjob (read-only)').disabled, true);
  assert.match(page.textContent, /Saved inspection JSON/);
});

test('failed current-authority reads preserve clearly historical saved inspection JSON', async () => {
  const { plan, tasks, record } = inspectionFixture();
  fetch = async url => url === '/api/plans/P1' ? response({ message: 'Plan temporarily unavailable' }, 503) : inspectionRead(url, { plan, tasks, record });
  const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
  await button(page, 'Load saved inspection').fire('click');
  assert.match(page.textContent, /Current plan authority could not be refreshed: Plan temporarily unavailable/);
  assert.match(page.textContent, /Historical or incomplete authority/);
  assert.match(page.textContent, /Saved inspection JSON/);
  assert.doesNotMatch(page.textContent, /Observed extjob: UID/);
  assert.equal(button(page, 'Inspect extjob (read-only)').disabled, true);
});

test('an unrelated execution reservation is rejected before inspection progress or polling', async () => {
  const { plan, tasks, record } = inspectionFixture(); const requests = [];
  fetch = async (url, options) => {
    requests.push({ url, method: options.method || 'GET' });
    if (options.method === 'POST') return response({ error: 'run_in_progress', run_id: record.run_id }, 409);
    assert.equal(requests.length, 2, 'Only one identity read is permitted before rejecting the conflicting run');
    return response({ ...record, kind: 'plan', status: 'running' });
  };
  const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
  await button(page, 'Inspect extjob (read-only)').fire('click');
  assert.match(page.textContent, /existing run belongs to another operation/);
  assert.doesNotMatch(page.textContent, /Inspection run [a-f0-9]|Comparison inspected|Saved inspection JSON/);
  assert.deepEqual(requests, [{ url: '/api/plans/P1/extjob-inspect', method: 'POST' }, { url: `/api/runs/${record.run_id}`, method: 'GET' }]);
});

test('a matching in-flight inspection can be joined and mismatched poll identity is rejected', async () => {
  for (const mismatch of [false, true]) {
    const { plan, tasks, record } = inspectionFixture(); let runReads = 0;
    fetch = async (url, options) => {
      if (options.method === 'POST') return response({ error: 'run_in_progress', run_id: record.run_id }, 409);
      if (url === `/api/runs/${record.run_id}`) {
        runReads++;
        return response(mismatch && runReads > 1 ? { ...record, run_id: 'b'.repeat(12) } : record);
      }
      return inspectionRead(url, { plan, tasks, record });
    };
    const page = mount(); page.appendChild(extjobInspection('P1', plan, tasks));
    await button(page, 'Inspect extjob (read-only)').fire('click');
    assert.equal(runReads, 2);
    if (mismatch) {
      assert.match(page.textContent, /existing run belongs to another operation/);
      assert.doesNotMatch(page.textContent, /Observed extjob: UID|Saved inspection JSON/);
    } else {
      assert.match(page.textContent, /Observed extjob: UID 54321/);
      assert.match(page.textContent, /they do not prove the host is unchanged now/);
    }
  }
});

test('artifact remediation is passive until an admitted operator explicitly probes hosts', async () => {
  const steps = [{ step: 'discovery', done: true, evidence: { databases: [] } },
    { step: 'artifact-inspect', done: true, status: 'blocked', evidence: { artifact: {
      path: '/fixture/patch', status: 'blocked', reason: 'artifact is incomplete' } } }];
  for (const allowed of [false, true]) {
    actor.setSessionIdentity({ mode: 'company', actor: 'fixture-operator', rbac_enabled: true,
      permissions: { live_discovery: allowed }, csrf_token: 'probe-csrf', expires_at: Date.now() / 1000 + 600 });
    api.setSessionCsrf('probe-csrf');
    const calls = [];
    fetch = async (url, options = {}) => {
      calls.push({ url, options });
      if (url === '/api/hosts/prod/pipeline') return response({ steps });
      assert.equal(url, '/api/hosts/prod/artifact-sources');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['X-CSRF-Token'], 'probe-csrf');
      assert.deepEqual(JSON.parse(options.body), { artifact_dir: '/fixture/patch' });
      return response({ targets: [{ node: 'prod', state: 'incomplete' }], sources: [{ host_id: 'source', label: 'Source', state: 'complete' }] });
    };
    const page = mount(); await renderReadinessStage(page, 'prod');
    assert.deepEqual(calls.map(call => call.url), ['/api/hosts/prod/pipeline']);
    assert.match(page.textContent, /all other configured source hosts/);
    const probe = button(page, 'Probe managed hosts');
    assert.equal(probe.disabled, !allowed);
    assert.equal(button(page, 'Stage media').disabled, !allowed);
    await probe.fire('click');
    assert.equal(calls.length, allowed ? 2 : 1);
    if (allowed) assert.equal(field(page, 'Source host').value, 'source');
    actor.setSessionIdentity({ mode: 'principal', actor: 'fixture-operator', rbac_enabled: true, permissions: { live_discovery: false } });
    await probe.fire('click');
    await button(page, 'Stage media').fire('click');
    assert.equal(calls.length, allowed ? 2 : 1, 'Revoked permission must not issue more host operations');
  }
});

test('artifact path edits invalidate a pending source probe and prevent stale staging selection', async () => {
  actor.setSessionIdentity({ mode: 'principal', actor: 'fixture-operator', rbac_enabled: true, permissions: { live_discovery: true } });
  let releaseProbe;
  const calls = [];
  fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === '/api/hosts/prod/pipeline') return response({ steps: [
      { step: 'discovery', done: true, evidence: { databases: [] } },
      { step: 'artifact-inspect', done: true, status: 'blocked', evidence: { artifact: {
        path: '/fixture/old', status: 'blocked', reason: 'artifact is incomplete' } } },
    ] });
    assert.equal(url, '/api/hosts/prod/artifact-sources');
    return new Promise(resolve => { releaseProbe = () => resolve(response({
      targets: [{ node: 'prod', state: 'incomplete' }], sources: [{ host_id: 'old-source', label: 'Old source', state: 'complete' }],
    })); });
  };
  const page = mount(); await renderReadinessStage(page, 'prod');
  const pending = button(page, 'Probe managed hosts').fire('click');
  while (!releaseProbe) await new Promise(resolve => setImmediate(resolve));
  assert.equal(button(page, 'Stage media').disabled, true);
  field(page, 'Artifact path on this host').value = '/fixture/new';
  await field(page, 'Artifact path on this host').fire('input');
  releaseProbe(); await pending;
  assert.equal(field(page, 'Source host').value, '');
  assert.doesNotMatch(page.textContent, /Old source/);
  await button(page, 'Stage media').fire('click');
  assert.match(page.textContent, /Pick a source host with complete media/);
  assert.equal(calls.length, 2, 'Staging cannot reuse a source from the previous path');
});
